"""Launch one Windows Terminal window per PR with a tab per comment.

Each tab is a detached tmux session running an autonomous agent session with a
prompt scoped to a single PR comment. The PR's workspace is shared across tabs;
the agent is instructed to `flock` a workspace edit lock around any write/commit
phase to serialize edits.

Which CLI runs comes from ``[mindflock].agent`` (the ingestion-wide default) —
PR review has no ticketing source of its own to override it — and the launch
command is built from that provider's spec, so a flock configured for codex or a
local model reviews PRs with it too.
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from backend import osenv
from backend.providers import launch_script
from backend.ticket_ingestion.claude_runner import (
    _prompt_dir,
    _prune_old_prompts,
    default_agent,
)
from backend.ticket_ingestion.models import (
    PRComment,
    ProvisionedPRWorkspace,
    PullRequest,
)
from backend.ticket_ingestion.terminal_launch import (
    build_terminal_tab_argv,
    wsl_distro,
    wsl_interop_available,
    wt_command,
)

_logger = logging.getLogger(__name__)

_LOCK_FILENAME = ".pr-edit-lock"

# Overall wall-clock deadline on waiting for a PR's comment sessions. Without
# it, a session that never touches its .done marker (and no xdotool to detect
# the closed window) blocks the PR loop forever.
_DONE_MARKER_DEADLINE_SECONDS = 2 * 60 * 60


def _pr_tmp_file(pr_number: int, comment_id: int, kind: str, suffix: str) -> Path:
    """A unique file in the prunable prompt dir (mkstemp, not deprecated
    mktemp-in-/tmp); pruned by ``_prune_old_prompts`` like the story prompts."""
    fd, path = tempfile.mkstemp(
        suffix=suffix,
        prefix=f"pr_{kind}_{pr_number}_c_{comment_id}_",
        dir=str(_prompt_dir()),
    )
    os.close(fd)
    return Path(path)


def _session_name(pr_number: int, comment_id: int) -> str:
    return f"pr-{pr_number}-c-{comment_id}"


class PRClaudeRunner:
    def __init__(self, agent: str = "") -> None:
        # Empty = resolve the engine's configured default at launch time. Kept
        # optional so `PRClaudeRunner()` (tests, and any caller predating agent
        # selection) still constructs.
        self.agent = agent

    async def launch(
        self,
        pr: PullRequest,
        workspace: ProvisionedPRWorkspace,
        comments: list[PRComment],
    ) -> None:
        """Run one detached Claude session per actionable PR comment.

        Writes a per-comment prompt, starts a tmux session for each in the PR's
        shared workspace (edits serialized via a ``flock`` lock outside the
        working tree), surfaces them as terminal tabs, then blocks until every
        session touches its ``.done`` marker (or the wall-clock deadline). A no-op
        when ``comments`` is empty.
        """
        if not comments:
            _logger.info(
                "No actionable comments on PR #%d; nothing to launch.", pr.number
            )
            return

        # Lock file lives outside the working tree so it can never be staged.
        lock_path = (
            workspace.directory.parent / f"{workspace.directory.name}{_LOCK_FILENAME}"
        )
        lock_path.touch(exist_ok=True)

        _prune_old_prompts(
            _prompt_dir(),
            max_age_s=_DONE_MARKER_DEADLINE_SECONDS + 3600.0,
            patterns=("pr_prompt_*.md", "pr_done_*.done"),
        )
        sessions: list[tuple[PRComment, str]] = []
        done_markers: list[Path] = []
        for comment in comments:
            session = _session_name(pr.number, comment.id)
            prompt_file = _pr_tmp_file(pr.number, comment.id, "prompt", ".md")
            prompt_file.write_text(
                _build_prompt(pr, workspace, comment, lock_path),
                encoding="utf-8",
            )
            # mkstemp creates the file, but the waiter treats an EXISTING
            # marker as "session done" — unlink it and keep only the unique
            # path for the tmux EXIT trap to touch.
            done_marker = _pr_tmp_file(pr.number, comment.id, "done", ".done")
            done_marker.unlink()
            await _kill_session(session)
            await _start_tmux_session(
                session,
                workspace.directory,
                prompt_file,
                done_marker,
                agent=self.agent,
            )
            sessions.append((comment, session))
            done_markers.append(done_marker)

        await _open_terminal_windows(pr, sessions)
        _logger.info(
            "PR #%d: launched %d tabs (sessions: %s); waiting for completion",
            pr.number,
            len(sessions),
            ", ".join(s for _, s in sessions),
        )
        await _wait_for_done_markers(
            done_markers,
            workspace.directory.name,
            sessions=[s for _, s in sessions],
        )
        _logger.info("PR #%d: all tabs finished.", pr.number)


def build_consolidated_pr_prompt(
    pr: PullRequest,
    comments: list[PRComment],
    workspace_dir,
) -> str:
    """Build ONE prompt that addresses every actionable PR comment in a single
    pass — so a PR is handled by a single session/window (like every other
    MindFlock session) rather than one tab per comment.
    """
    lines: list[str] = []
    lines.append(f"# PR #{pr.number}: {pr.title}")
    lines.append("")
    lines.append(f"- PR URL: {pr.url}")
    lines.append(f"- Branch (already checked out): `{pr.head_ref}` @ `{pr.head_sha}`")
    lines.append(f"- Workspace: `{workspace_dir}`")
    lines.append(f"- Author: {pr.author}")
    lines.append("")
    lines.append(f"## Review comments to address ({len(comments)})")
    lines.append("")
    lines.append(
        "Work through ALL of the comments below in a single pass on this "
        "branch. For each: decide whether it is actionable (requests a concrete "
        "code change you can make here). Make the change for the actionable "
        "ones; for non-actionable ones (discussion, acknowledgements, or "
        "questions that need a human answer), briefly note why you're skipping "
        "it. Do not stop after the first comment — handle every one."
    )
    lines.append("")
    for i, comment in enumerate(comments, 1):
        lines.append(f"### {i}. {comment.kind} comment by {comment.author}")
        lines.append(f"- URL: {comment.url}")
        if comment.path:
            loc = comment.path + (f":{comment.line}" if comment.line else "")
            lines.append(f"- File: `{loc}`")
        if comment.diff_hunk:
            lines.append("")
            lines.append("```diff")
            lines.append(_strip_media(comment.diff_hunk))
            lines.append("```")
        lines.append("")
        lines.append(_strip_media(comment.body) or "_(empty)_")
        lines.append("")
    lines.append("## When done")
    lines.append("1. Make all the needed code changes in this workspace.")
    lines.append(
        "2. Leave the changes **unstaged** in the working tree — do NOT run "
        "`git add`, `git commit`, or `git push`. The human reviews them in their "
        "editor and decides what to commit."
    )
    lines.append(
        "3. Finish with a concise per-comment summary: what you changed, or why "
        "you skipped it."
    )
    return "\n".join(lines)


def _build_prompt(
    pr: PullRequest,
    workspace: ProvisionedPRWorkspace,
    comment: PRComment,
    lock_path: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"# PR #{pr.number}: {pr.title}")
    lines.append("")
    lines.append(f"- PR URL: {pr.url}")
    lines.append(f"- Branch (already checked out): `{pr.head_ref}` @ `{pr.head_sha}`")
    lines.append(f"- Workspace: `{workspace.directory}`")
    lines.append(f"- Author: {pr.author}")
    lines.append("")
    lines.append(f"## Comment to address ({comment.kind})")
    lines.append(f"- By: {comment.author}")
    lines.append(f"- URL: {comment.url}")
    if comment.path:
        loc = comment.path + (f":{comment.line}" if comment.line else "")
        lines.append(f"- File: `{loc}`")
    lines.append("")
    if comment.diff_hunk:
        lines.append("### Diff context")
        lines.append("```diff")
        lines.append(_strip_media(comment.diff_hunk))
        lines.append("```")
        lines.append("")
    lines.append("### Body")
    lines.append(_strip_media(comment.body) or "_(empty)_")
    lines.append("")
    lines.append("## Your task")
    lines.append(
        "1. Decide whether this comment is actionable — i.e. it requests a "
        "concrete code change that you can make on this branch. Discussion-only "
        "comments, acknowledgements, or questions that need a human answer are "
        "NOT actionable; print your reasoning and exit without changing files."
    )
    lines.append(
        "2. If actionable, make the change in this workspace. Other Claude "
        "sessions may be editing the same checkout in sibling tabs, so wrap "
        f"any file write phase in `flock {shlex.quote(str(lock_path))} -- <cmd>` "
        "so only one tab edits at a time."
    )
    lines.append(
        "3. **Do not run `git add`, `git commit`, or `git push`.** Leave the "
        "changes unstaged in the working tree — the human will review them in "
        "their editor and decide what to commit."
    )
    lines.append(
        "4. If you decide NOT to act, explain why and exit. Do not touch files."
    )
    lines.append("")
    lines.append("Be terse. Show your decision up front, then do the work.")
    return "\n".join(lines)


_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_VIDEO_RE = re.compile(r"<video\b[^>]*>.*?</video>", re.IGNORECASE | re.DOTALL)
_GH_ATTACH_RE = re.compile(
    r"https?://(?:user-images\.githubusercontent\.com|github\.com/user-attachments/assets)/\S+"
)
_DATA_URI_RE = re.compile(r"data:image/[^\s)]+", re.IGNORECASE)


def _strip_media(text: str | None) -> str:
    if not text:
        return ""
    out = _MD_IMAGE_RE.sub("[image omitted]", text)
    out = _HTML_VIDEO_RE.sub("[video omitted]", out)
    out = _HTML_IMG_RE.sub("[image omitted]", out)
    out = _GH_ATTACH_RE.sub("[attachment omitted]", out)
    out = _DATA_URI_RE.sub("[data-uri omitted]", out)
    return out


async def _wait_for_done_markers(
    markers: list[Path],
    cursor_search: str,
    sessions: list[str] | None = None,
    deadline_seconds: float = _DONE_MARKER_DEADLINE_SECONDS,
) -> None:
    cursor_seen = False
    remaining = list(markers)
    started = time.monotonic()
    while remaining:
        if time.monotonic() - started >= deadline_seconds:
            _logger.warning(
                "Gave up waiting for %d done marker(s) after %.0fs; killing "
                "%d session(s) and moving on.",
                len(remaining),
                deadline_seconds,
                len(sessions or []),
            )
            for session in sessions or []:
                await _kill_session(session)
            break
        await asyncio.sleep(5)
        alive = await _cursor_window_alive(cursor_search)
        if alive:
            cursor_seen = True
        elif cursor_seen:
            _logger.info(
                "Cursor window for %r closed; treating PR as complete.",
                cursor_search,
            )
            break
        remaining = [m for m in remaining if not m.exists()]
    for m in markers:
        try:
            m.unlink()
        except OSError:
            pass


async def _cursor_window_alive(search: str) -> bool:
    if not shutil.which("xdotool"):
        return True
    proc = await asyncio.create_subprocess_exec(
        "xdotool",
        "search",
        "--name",
        search,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return False
    return bool(stdout.strip())


async def _kill_session(session: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        session,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _start_tmux_session(
    session: str, workdir: Path, prompt_file: Path, done_marker: Path, agent: str = ""
) -> None:
    shell = shutil.which("zsh") or shutil.which("bash") or "bash"
    shell_flag = "-ilc"
    marker = shlex.quote(str(done_marker))
    # The agent's own skip-prompts flag and prompt-passing form (see
    # backend.providers.launch_script) — these tabs are unattended, so
    # skip_permissions is on wherever the CLI supports it.
    preamble, command = launch_script.launch_command(
        agent or default_agent(),
        str(prompt_file),
        skip_permissions=True,
    )
    # The EXIT trap is what the waiter below blocks on, so it is armed before the
    # agent starts and fires however the session ends.
    inner = f"{preamble}trap 'touch {marker}' EXIT; {command}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(workdir),
        shell,
        shell_flag,
        inner,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"tmux new-session '{session}' failed: "
            f"{stderr.decode(errors='replace').strip() or 'unknown error'}"
        )
    for option, value in (
        ("mouse", "on"),
        ("history-limit", "100000"),
        ("remain-on-exit", "on"),
    ):
        opt = await asyncio.create_subprocess_exec(
            "tmux",
            "set-option",
            "-t",
            session,
            option,
            value,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await opt.wait()
    hook = await asyncio.create_subprocess_exec(
        "tmux",
        "set-hook",
        "-t",
        session,
        "client-detached",
        f"kill-session -t {session}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await hook.wait()


async def _open_terminal_windows(
    pr: PullRequest, sessions: list[tuple["PRComment", str]]
) -> None:
    """Open terminal(s) attached to the PR's tmux sessions (best-effort, per-OS).

    On WSL this opens a single Windows Terminal window with one tab per session
    (``wt.exe -w new`` + chained ``; new-tab``). On Linux/macOS — where there is
    no cross-emulator "one window, many tabs" primitive — it opens one terminal
    per session via the shared launcher. If no emulator is available the sessions
    still run; attach with ``tmux attach -t <session>``.
    """
    if not sessions:
        return

    if osenv.is_wsl():
        await _open_wsl_terminal_window(pr, sessions)
        return

    # Linux / macOS: one terminal per session (graceful no-op if none found).
    opened = 0
    for comment, session in sessions:
        title = f"PR #{pr.number}: {comment.kind} c-{comment.id}"
        argv = build_terminal_tab_argv(title, session)
        if argv is None:
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            opened += 1
        except OSError:
            continue
    if opened == 0:
        _logger.info(
            "no terminal emulator found for PR #%d; attach with: tmux attach -t <session>",
            pr.number,
        )


async def _open_wsl_terminal_window(
    pr: PullRequest, sessions: list[tuple["PRComment", str]]
) -> None:
    """WSL: a single Windows Terminal window with one tab per session."""
    wt = wt_command()
    if shutil.which(wt) is None or not wsl_interop_available():
        _logger.info(
            "Windows Terminal not launchable for PR #%d; attach with: "
            "tmux attach -t <session>",
            pr.number,
        )
        return
    distro = wsl_distro()
    args: list[str] = [wt, "-w", "new"]

    first_comment, first_session = sessions[0]
    args += [
        "--title",
        f"PR #{pr.number}: {first_comment.kind} c-{first_comment.id}",
        "wsl.exe",
        "-d",
        distro,
        "--",
        "tmux",
        "attach",
        "-t",
        first_session,
    ]

    for comment, session in sessions[1:]:
        args += [
            ";",
            "new-tab",
            "--title",
            f"{comment.kind} c-{comment.id}",
            "wsl.exe",
            "-d",
            distro,
            "--",
            "tmux",
            "attach",
            "-t",
            session,
        ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # Best-effort convenience only — e.g. wt.exe resolving to an unexecable
        # WindowsApps alias with no cmd.exe to shim through.
        _logger.warning(
            "could not launch Windows Terminal for PR #%d: %s. Sessions are "
            "still running; attach with: tmux attach -t <session>",
            pr.number,
            exc,
        )
        return
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        _logger.warning(
            "wt.exe failed to open window for PR #%d: %s. Sessions are still "
            "running; attach with: tmux attach -t <session>",
            pr.number,
            stderr.decode(errors="replace").strip() or "unknown error",
        )
