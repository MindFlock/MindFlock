"""Standalone agent-CLI runner for the ticket-ingestion pipeline.

Spawns the ticket's agent CLI inside a detached tmux session per story, then
opens a terminal tab that attaches to it. Lets the developer continue the
conversation interactively while the pipeline keeps processing other stories.

Which CLI runs is per-ticket: ``Ticket.agent`` (stamped from the ingesting
source's ``agent``), else ``[mindflock].agent``, else the engine's configured
default program. The launch command itself is built from that provider's
:class:`~backend.providers.base.LauncherSpec` via
:mod:`backend.providers.launch_script` — the same vocabulary the engine's
provisioned launcher uses, so the two ingestion paths start a CLI identically.

The module keeps its historical name (and the ``ClaudeCodeRunner`` alias) so
existing imports and the on-disk prompt-file naming are undisturbed; the runner
itself has not been Claude-specific since agent selection landed.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import aiohttp

from backend.providers import launch_script
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import (
    Attachment,
    ProvisionedEnvironment,
    Ticket,
)
from backend.ticket_ingestion.terminal_launch import build_terminal_tab_argv

logger = logging.getLogger(__name__)


def default_agent() -> str:
    """The user's configured default agent program, or ``"claude"``.

    The last link in the ``ticket.agent -> [mindflock].agent -> user default``
    chain, where "user default" is Settings → Coding provider first and the
    engine config second. Read lazily and defensively: the standalone launcher
    is exactly the path that runs when the engine half of the package is
    unavailable, so a missing :mod:`backend.config` must degrade to the
    historical default rather than fail the launch.
    """
    try:
        from backend.config.program import resolve_default_program

        return resolve_default_program() or "claude"
    except Exception:  # noqa: BLE001
        return "claude"


_ATTACHMENT_DIR_NAME = ".ticket_attachments"
_ATTACHMENT_DOWNLOAD_TIMEOUT = 60.0
_MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MiB per file


def _tmux_session_name(story) -> str:
    """The ticket's provider-scoped slug names its tmux session."""
    return str(story.slug)


def _prompt_dir() -> Path:
    """Directory holding the per-launch prompt files (was /tmp via mktemp, which
    accumulated forever). Under the assistant dir so it's a known, prunable spot."""
    home = os.environ.get(
        "MINDFLOCK_ASSISTANT_DIR", str(Path.home() / ".mindflock-assistant")
    )
    d = Path(home) / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_old_prompts(
    directory: Path,
    max_age_s: float = 3600.0,
    patterns: tuple[str, ...] = ("claude_prompt_*.md",),
) -> None:
    """Delete prompt files older than ``max_age_s`` (keeps recent ones for
    debugging; never touches a file we're about to use). Best-effort.
    ``patterns`` lets other launchers (e.g. the PR runner) prune their own
    prompt/marker files the same way."""
    now = time.time()
    try:
        for pattern in patterns:
            for p in directory.glob(pattern):
                try:
                    if now - p.stat().st_mtime > max_age_s:
                        p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


class AgentCliRunner:
    """Starts the ticket's agent CLI in a tmux pane and surfaces it in a terminal tab."""

    def __init__(self, config: PipelineConfig):
        self.config = config

    def agent_for(self, story: Ticket) -> str:
        """Which agent CLI ``story`` runs: its own, else the pipeline default."""
        return (
            getattr(story, "agent", "")
            or self.config.agent_for(getattr(story, "provider", ""))
            or default_agent()
        )

    async def invoke(
        self,
        env: ProvisionedEnvironment,
        story: Ticket,
        supplemental_context: str | None = None,
    ) -> None:
        downloaded = await self._download_attachments(env.directory, story)
        prompt = self._build_prompt(story, supplemental_context, downloaded)

        prompt_dir = _prompt_dir()
        _prune_old_prompts(prompt_dir)
        fd, prompt_path = tempfile.mkstemp(
            suffix=".md", prefix="claude_prompt_", dir=str(prompt_dir)
        )
        os.close(fd)
        prompt_file = Path(prompt_path)
        prompt_file.write_text(prompt, encoding="utf-8")

        session = _tmux_session_name(story)
        agent = self.agent_for(story)
        logger.info(
            "Starting %s for ticket %s in tmux session '%s' (prompt file: %s)",
            agent,
            story.slug,
            session,
            prompt_file,
        )

        try:
            await self._kill_existing_session(session)
            await self._start_tmux_session(session, env.directory, prompt_file, agent)
            await self._open_terminal_tab(session)
            logger.info(
                "%s session '%s' is live. Attach manually with: tmux attach -t %s",
                agent,
                session,
                session,
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(
                "Unexpected error invoking %s for ticket %s: %s",
                agent,
                story.slug,
                e,
            )
            raise RuntimeError(
                f"Failed to invoke {agent} for ticket {story.slug}: {e}"
            ) from e

    async def _kill_existing_session(self, session: str) -> None:
        """Best-effort cleanup of a stale tmux session by the same name."""
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "kill-session",
            "-t",
            session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()

    async def _start_tmux_session(
        self, session: str, workdir: Path, prompt_file: Path, agent: str = ""
    ) -> None:
        # bash -ilc: login+interactive shell so PATH from .bashrc/.profile is set.
        # Trailing `exec bash -i` keeps the tmux pane alive after the agent exits,
        # so the user can re-run or inspect output.
        #
        # The command comes from the agent's own provider (its entry subcommand,
        # its binary-path override, and how it accepts a seed prompt — as argv, or
        # typed into the pane for a CLI that takes none), so this path launches
        # codex/aider/goose correctly instead of assuming Claude's argv shape.
        preamble, command = launch_script.launch_command(
            agent or default_agent(), str(prompt_file)
        )
        inner = f"{preamble}{command}; exec bash -i"
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(workdir),
            "bash",
            "-ilc",
            inner,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"tmux new-session failed: {stderr.decode().strip() or 'unknown error'}"
            )

        # Enable mouse-wheel scrolling (drops into copy mode automatically) and
        # bump the scrollback buffer so long Claude transcripts stay reachable.
        for option, value in (("mouse", "on"), ("history-limit", "100000")):
            opt_proc = await asyncio.create_subprocess_exec(
                "tmux",
                "set-option",
                "-t",
                session,
                option,
                value,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, opt_err = await opt_proc.communicate()
            if opt_proc.returncode != 0:
                logger.warning(
                    "tmux set-option %s=%s failed for session '%s': %s",
                    option,
                    value,
                    session,
                    opt_err.decode().strip() or "unknown error",
                )

    async def _open_terminal_tab(self, session: str) -> None:
        """Open a terminal attached to the tmux session (best-effort, per-OS).

        Uses the right emulator for the host — Windows Terminal on WSL,
        gnome-terminal/konsole/xterm on Linux, Terminal.app on macOS. If none is
        available the session still runs; attach manually with
        ``tmux attach -t <session>``.
        """
        argv = build_terminal_tab_argv(session, session)
        if argv is None:
            logger.info(
                "no terminal emulator found to open a tab for session '%s'; "
                "attach manually with: tmux attach -t %s",
                session,
                session,
            )
            return
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            # Opening the tab is a best-effort convenience and must never fail
            # the invoke on any OS. Spawning can fail in platform-specific ways:
            # OSError/FileNotFoundError (emulator not on this process's PATH),
            # or NotImplementedError (event loop without subprocess support).
            # Catch broadly, log, and continue — the tmux session is still live.
            logger.warning(
                "could not launch terminal emulator (%s) for session '%s': %s. "
                "Session is still running; attach manually with: tmux attach -t %s",
                argv[0],
                session,
                exc,
                session,
            )
            return
        _, stderr = await process.communicate()
        if process.returncode != 0:
            logger.warning(
                "failed to open a terminal tab for session '%s': %s. "
                "Session is still running; attach manually with: tmux attach -t %s",
                session,
                stderr.decode().strip() or "unknown error",
                session,
            )

    async def _download_attachments(
        self, workdir: Path, story: Ticket
    ) -> list[tuple[Attachment, Path]]:
        if not story.attachments:
            return []
        target_dir = workdir / _ATTACHMENT_DIR_NAME
        target_dir.mkdir(parents=True, exist_ok=True)

        results: list[tuple[Attachment, Path]] = []
        timeout = aiohttp.ClientTimeout(total=_ATTACHMENT_DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for idx, att in enumerate(story.attachments):
                # Provider-supplied download auth (Bearer/Basic/Shortcut-Token).
                headers: dict[str, str] = dict(att.auth_headers or {})
                filename = _safe_filename(att.name, idx)
                dest = target_dir / filename
                try:
                    async with session.get(
                        att.url, headers=headers, allow_redirects=True
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                # %s for the id (string for Jira/Linear/Asana),
                                # %d only for the numeric HTTP status.
                                "Attachment download failed for story %s (%s): HTTP %d",
                                story.id,
                                att.url,
                                resp.status,
                            )
                            continue
                        total = 0
                        with open(dest, "wb") as fh:
                            async for chunk in resp.content.iter_chunked(64 * 1024):
                                total += len(chunk)
                                if total > _MAX_ATTACHMENT_BYTES:
                                    fh.close()
                                    dest.unlink(missing_ok=True)
                                    logger.warning(
                                        "Attachment %s for story %s exceeds %d bytes; skipping",
                                        att.url,
                                        story.id,
                                        _MAX_ATTACHMENT_BYTES,
                                    )
                                    break
                                fh.write(chunk)
                            else:
                                results.append((att, dest))
                                continue
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    logger.warning(
                        "Attachment download error for story %s (%s): %s",
                        story.id,
                        att.url,
                        e,
                    )
        if results:
            logger.info(
                "Downloaded %d attachment(s) for story %s into %s",
                len(results),
                story.id,
                target_dir,
            )
        return results

    def _build_prompt(
        self,
        story: Ticket,
        supplemental_context: str | None = None,
        downloaded_attachments: list[tuple[Attachment, Path]] | None = None,
    ) -> str:
        lines: list[str] = []
        lines.append(
            "Follow the ticket requirements closely and do what needs to be "
            "done in this repository to advance the ticket. If external things "
            "need to be done (infra provisioning, work in other repos, IAM "
            "changes, schema migrations elsewhere, etc.), state that "
            "explicitly rather than attempting them here."
        )
        lines.append("")
        lines.append(f"# Story: {story.name}")
        lines.append("")
        lines.append(f"{story.source_label} URL: {story.app_url}")
        lines.append("")
        lines.append("## Description")
        lines.append("")
        lines.append(story.description)
        lines.append("")
        if story.acceptance_criteria:
            lines.append("## Acceptance Criteria")
            lines.append("")
            for criterion in story.acceptance_criteria:
                lines.append(f"- {criterion}")
            lines.append("")
        if story.comments:
            lines.append("## Comments")
            lines.append("")
            for comment in story.comments:
                lines.append(f"- {comment}")
            lines.append("")
        if downloaded_attachments:
            lines.append("## Attached Files")
            lines.append("")
            lines.append(
                f"The following files were attached to the {story.source_label} "
                "ticket (in the description or comments) and have been downloaded "
                "into the working tree. Read them as part of the ticket context."
            )
            lines.append("")
            for att, path in downloaded_attachments:
                ctype = f" ({att.content_type})" if att.content_type else ""
                lines.append(f"- `{path}`{ctype} — source: {att.url}")
            lines.append("")
        if supplemental_context:
            lines.append("## Supplemental Context")
            lines.append("")
            lines.append(supplemental_context)
            lines.append("")
        return "\n".join(lines)


#: Historical name for :class:`AgentCliRunner`, kept because the class is
#: constructed from the orchestrator, the clarification handler, the engine
#: bridge and two web force-start paths (plus their tests).
ClaudeCodeRunner = AgentCliRunner


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, index: int) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._-")
    if not cleaned:
        cleaned = f"attachment-{index}"
    # Prefix with index to guarantee uniqueness even on duplicate names.
    return f"{index:02d}-{cleaned}"
