"""The default Claude (Claude Code) provider.

The launch command builder lives here; the worktree launcher script generator
lives in :mod:`backend.session.provisioned`. This provider delegates to the
latter so the produced command strings and launcher scripts stay a single
source of truth. New, genuinely different CLIs (aider/codex/…) get their own
providers.
"""

from __future__ import annotations

import os
import threading

from typing import Optional

from .base import (
    BaseProvider,
    LauncherSpec,
    LaunchContext,
    TrustSpec,
    seed_prompt_expr,
)
from ._timeparse import ts_epoch

# Activity markers live in the shared, provider-agnostic module (Codex reuses
# the same {state, ts} marker + hook-command machinery).
from .activity_markers import ensure_git_excluded as _ensure_git_excluded
from .activity_markers import (
    merge_activity_hooks,
    read_activity_marker,
    read_activity_marker_age,
)


def claude_launch_command(
    program: str,
    *,
    resume: bool,
    skip_permissions: bool = False,
    seed: str = "",
    thread_id: str = "",
    launch_args=(),
) -> str:
    """Build the shell command for a Claude Code launch.

    With ``resume`` the prior conversation is continued; a failed resume is
    retried once after a short pause (transient failures — network not up yet
    right after boot — exit non-zero exactly like "nothing to continue"), then
    falls back to a PLAIN unseeded launch. The seed is never re-sent on a
    resume: a non-zero exit can't distinguish "no conversation" from a hiccup,
    and re-seeding while the conversation still exists silently restarts the
    whole task in a fresh thread. When ``thread_id`` names THIS window's
    recorded conversation, resume targets it (``--resume <id>``) instead of
    ``--continue`` — with several windows on one directory, ``--continue``
    grabs whichever sibling spoke last. Returned as a single shell string
    (tmux runs it via the shell).
    """
    import shlex

    base = program or "claude"
    if launch_args:
        base = "%s %s" % (base, " ".join(shlex.quote(a) for a in launch_args))
    if skip_permissions:
        base += " --dangerously-skip-permissions"
    fresh = f"{base}{seed}"
    if resume:
        if thread_id:
            # Resume THIS window's conversation (NOT --continue, which would
            # steal a sibling's newest thread).
            rcmd = f"{base} --resume {shlex.quote(thread_id)}"
        else:
            rcmd = f"{base} --continue"
        plain = (
            "{ echo '[mindflock] resume failed twice; starting a fresh session"
            " WITHOUT re-sending the task prompt'; " + base + "; }"
        )
        return f"{rcmd} || {{ sleep 3; {rcmd}; }} || {plain}"
    return fresh


class ClaudeProvider(BaseProvider):
    name = "claude"
    program_aliases = ("claude",)

    # ``claude`` — and an empty program, which defaults to claude.
    def matches(self, program: str) -> bool:
        if not program:
            return True
        return os.path.basename(program.split()[0]) in self.program_aliases

    # --- minimal / connection-free launch (roadmap E) -------------------- #
    def minimal_launch_command(self, workdir: str = "", session_name: str = "") -> str:
        """A connection-free Claude launch for the window-refresh ping: no MCP
        servers (``--strict-mcp-config`` with an empty ``--mcp-config``) and the
        permission gate skipped, so a 1-token ping just anchors the usage window
        with nothing attached. Best-effort flags — tune if the CLI changes."""
        return (
            "claude --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' "
            "--dangerously-skip-permissions"
        )

    # --- usage-window knowledge (roadmap E) ------------------------------- #
    def usage_window(self) -> dict:
        """Anthropic plans reset on a rolling 5-hour window that anchors on your
        first message of the window, plus a weekly cap on paid plans. This is
        what the scheduled window-refresh anchors and the UI explains."""
        return {
            "kind": "rolling",
            "hours": 5.0,
            "weekly_hours": 168.0,
            "note": "Anthropic plans: a rolling 5-hour window that anchors on your "
            "first message, plus a weekly cap on paid plans.",
        }

    def usage_mode(self) -> str:
        """Claude runs on a subscription plan (windowed) UNLESS an API key is
        driving billing: with ``ANTHROPIC_API_KEY`` in the serving environment
        Claude Code bills that key per-token even for plan holders — real
        marginal spend, so the UI should lead with dollars ("metered")."""
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            return "metered"
        return "windowed"

    def usage_live(self) -> Optional[dict]:
        """Real window utilization + reset from Anthropic's OAuth usage
        endpoint (the same source as Claude Code's ``/usage`` screen), or None
        — callers then fall back to the transcript estimate."""
        from . import claude_usage_api

        return claude_usage_api.live_usage()

    def usage_periods(self) -> Optional[dict]:
        """Rolling day/week/month/year token+cost totals from the Claude Code
        transcript scan."""
        from . import usage_history

        return usage_history.windows()

    def usage_panel_visible(self) -> bool:
        # Claude is one of the default CLIs the cost panel always shows.
        return True

    # --- connection: install + login -------------------------------------- #
    def install_hint(self) -> str:
        """Prefer npm when it's already on PATH (no separate installer step);
        otherwise the native install script, which needs no Node."""
        import shutil

        if shutil.which("npm"):
            return "npm install -g @anthropic-ai/claude-code"
        return "curl -fsSL https://claude.ai/install.sh | sh"

    def login_command(self) -> Optional[str]:
        # `claude` prompts to sign in on first run — no separate login command.
        return "claude"

    def auth_evidence(self) -> str:
        """Probe the known Claude Code credential locations (honoring
        ``CLAUDE_CONFIG_DIR``) for a stored login / API key. Never raises.

        The macOS login Keychain is one of those locations: there Claude Code
        keeps its OAuth credentials in the Keychain instead of
        ``~/.claude/.credentials.json`` (see
        :func:`backend.providers.claude_usage_api._keychain_doc`), so the file
        scan below finds nothing and a fully logged-in Mac was told "no sign of a
        login was found" by the doctor on every single run. It is consulted only
        after the files come up empty, because that lookup shells out to
        ``security`` and may raise a one-time keychain prompt — nobody should pay
        for either when a file already answered the question.
        """
        import os
        from pathlib import Path

        candidates = []
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if cfg:
            candidates += [Path(cfg) / ".claude.json", Path(cfg) / ".credentials.json"]
        home = Path.home()
        candidates += [home / ".claude.json", home / ".claude" / ".credentials.json"]
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for marker in ("oauthAccount", "primaryApiKey", "claudeAiOauth"):
                if marker in text:
                    return "login state found in %s" % path
        if _keychain_login_evidence():
            return "login state found in the macOS Keychain"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY is set"
        return ""

    # --- launch ----------------------------------------------------------- #
    def build_launch_command(self, ctx: LaunchContext) -> Optional[str]:
        # Launcher-less launches (plain / in-place sessions) still get the
        # activity-reporting hooks when the workdir is known — the engine's
        # Instance.Start passes it; the web relaunch path installs explicitly.
        if ctx.workdir and ctx.session_name:
            install_activity_hooks(ctx.workdir, ctx.session_name)
            pre_trust_workdir(ctx.workdir)
        if self.matches(ctx.program):
            # Seed the initial prompt as a launch argument (plain / in-place
            # sessions — the provisioned worktree launcher seeds it separately).
            # claude_launch_command only uses the seed on a fresh launch, so an
            # existing conversation is never re-seeded.
            seed = seed_prompt_expr(ctx.session_name, ctx.prompt)
            return claude_launch_command(
                ctx.program or "claude",
                resume=ctx.resume,
                skip_permissions=ctx.skip_permissions,
                seed=(" " + seed) if seed else "",
                launch_args=ctx.launch_args,
                # Resume THIS window's own conversation (recorded by the
                # activity hooks) — --continue alone grabs the directory's
                # newest thread, wrong when siblings share the workdir.
                thread_id=self.resume_thread_id(ctx.session_name) if ctx.resume else "",
            )
        # A genuinely custom program slipped through to this provider — run it
        # as the generic base does (bare, resume with --continue).
        return super().build_launch_command(ctx)

    # --- worktree launcher ------------------------------------------------ #
    def owns_launcher(self, ctx: LaunchContext) -> bool:
        # Claude generates the workspace launcher; in-place sessions borrow a
        # worktree and never do.
        return not ctx.in_place

    def launcher_spec(self) -> LauncherSpec:
        # The provisioned launcher's original, hardcoded vocabulary — now stated
        # as data so the generated script is byte-identical for Claude while
        # every other CLI gets its own flags instead of these.
        return LauncherSpec(
            skip_perms_flag="--dangerously-skip-permissions",
            prompt_arg="{prompt}",  # claude accepts the prompt positionally
            resume_flag="--continue",
            resume_fallback=True,
        )

    def write_launcher(self, ctx: LaunchContext) -> str:
        # Install the activity-reporting hooks alongside the launcher so the
        # very first Claude run in this worktree already announces its state
        # (Claude snapshots hook config at process start). Pre-trusting the
        # worktree in the same breath means that first run never stalls on the
        # invisible "do you trust this folder?" gate (F2) — MindFlock created
        # the folder, so it is trusted by construction.
        if ctx.workdir and ctx.session_name:
            install_activity_hooks(ctx.workdir, ctx.session_name)
            pre_trust_workdir(ctx.workdir)
        from backend.session import provisioned

        # ``program`` is threaded through unchanged so a custom-program
        # provisioned session still launches that program. ``cache_env``
        # carries the workspace's cache env exports (None -> the built-in
        # TESTMON_ENV default).
        return provisioned.write_launcher(
            ctx.workdir,
            ctx.prompt,
            program=ctx.program or "claude",
            skip_permissions=ctx.skip_permissions,
            cache_env=ctx.cache_env,
            launch_args=ctx.launch_args,
        )

    # --- terminal classification ------------------------------------------ #
    def trust_prompt(self) -> Optional[TrustSpec]:
        # Claude's per-folder trust gate + MCP-server confirmation. Newer Claude
        # Code phrases the folder gate as "Is this a project you created or one
        # you trust?" — match both wordings so sessions never hang at trust.
        return TrustSpec(
            patterns=(
                "Do you trust the files in this folder?",
                "Is this a project you created or one you trust?",
                "new MCP server",
            ),
            keystroke=b"\r",
        )

    def idle_prompt_pattern(self) -> Optional[str]:
        return "No, and tell Claude what to do differently"

    def waiting_prompt_patterns(self) -> tuple:
        # Regexes (matched in the visible pane) that mean Claude Code has stopped
        # to ask you to choose — a permission prompt, plan approval, or an
        # AskUserQuestion box — as opposed to merely idle. The numbered selection
        # cursor "❯ 1." is the version-stable signal common to all of them
        # (present whichever option is highlighted), but it is only trusted when
        # ANCHORED the way Claude Code renders it: at the start of a line,
        # optionally inside the dialog's box-drawing border. A numbered list the
        # agent happens to be *printing* mid-generation ("see ❯ 1. below") never
        # sits at line start behind the cursor glyph, so it no longer matches.
        # The phrases are exact strings from the permission / AskUserQuestion UI.
        return (
            r"(?m)^\s*(?:│\s*)?❯\s+\d+\.",  # select-menu cursor at line start
            "Yes, and don't ask again",  # permission box option
            "No, and tell Claude what to do differently",  # tool-permission box
            "Type something\\.",  # AskUserQuestion free-text option
            "Chat about this",  # AskUserQuestion footer
        )

    def working_pane_patterns(self) -> tuple:
        # Claude Code renders an interrupt hint on its status line for the whole
        # duration of a live turn — thinking, generating, or running a tool —
        # and it is GONE at an idle prompt. That makes "esc to interrupt" the
        # version-stable proof that the agent is working even while it burns ~0
        # local CPU (extended thinking runs server-side). Matched
        # case-insensitively against the raw bottom pane lines. The bracketed
        # "(esc to interrupt)" form and the older bare phrasing both match.
        return (r"esc to interrupt",)

    def progress_token_pattern(self) -> Optional[str]:
        # The status line shows a climbing token tally while a turn runs, e.g.
        # "· 1.2k tokens" / "↑ 15.3k tokens". Capture the number (with optional
        # k/m suffix); the web layer treats any increase as proof of work. The
        # trailing "tokens" word anchors it so an unrelated figure can't match.
        return r"([\d.,]+\s*[kmKM]?)\s*tokens"

    # --- activity signal ---------------------------------------------------- #
    def activity_state(self, session_name: str) -> Optional[str]:
        # Prefer Claude Code's own real-time report from `claude agents --json`
        # (never stale — it reflects the live session, including a long think
        # between tool calls that leaves the hook marker aging). Fall back to the
        # per-session hook marker when the live signal is unavailable (binary too
        # old, no conversation id recorded yet, or this session not listed).
        live = _live_agent_state(session_name)
        if live is not None:
            return live
        return read_activity_marker(session_name)

    def activity_state_age(self, session_name: str) -> Optional[float]:
        # The live signal is real-time, so report a fresh age (0) — the web layer
        # then trusts a working/clarify report without re-verifying it against the
        # pane. Only when we're on the marker fallback does its real age matter,
        # so a stale working/clarify marker still triggers pane re-verification.
        if _live_agent_state(session_name) is not None:
            return 0.0
        return read_activity_marker_age(session_name)

    def install_activity_hooks(self, workdir: str, session_name: str) -> None:
        install_activity_hooks(workdir, session_name)
        # Same lifecycle spot (every launch path re-pins hooks right before
        # starting the CLI), so every launch is also pre-trusted (F2).
        pre_trust_workdir(workdir)

    # --- telemetry -------------------------------------------------------- #
    def session_tokens(
        self,
        workdir: str,
        since_ts: Optional[float],
        until_ts: Optional[float] = None,
        shared_cwd: bool = False,
    ) -> dict:
        return _claude_transcript_tokens(workdir, since_ts, until_ts, shared_cwd)

    def last_turn_snippet(self, session_name: str, workdir: str) -> Optional[str]:
        return _claude_last_turn_snippet(workdir)


def _keychain_login_evidence() -> bool:
    """Whether the macOS login Keychain holds Claude Code's credentials.

    Delegates to the lookup :mod:`claude_usage_api` already needs for the live
    usage token — it is darwin-only and timeout-bounded, so this costs nothing on
    Linux/WSL — and is imported lazily so an auth probe never drags the usage/HTTP
    module in just to answer "is this CLI logged in?". Any failure (no
    ``security`` binary, a denied keychain prompt, a headless session where no
    prompt can be answered) counts as no evidence: this is a hint for the doctor,
    never a gate on anything.
    """
    try:
        from . import claude_usage_api

        return bool(claude_usage_api._keychain_doc())
    except Exception:  # noqa: BLE001 — no evidence is the whole failure mode here
        return False


# --------------------------------------------------------------------------- #
# Activity markers: Claude Code hooks -> a per-session {state, ts} JSON file.
#
# install_activity_hooks() merges the hook commands below into the worktree's
# .claude/settings.local.json; each writes the shared per-session marker at
# ~/.mindflock-assistant/.activity-markers/<session>.json (the read/write and
# hook-command machinery live in the provider-agnostic activity_markers module,
# shared with Codex). Claude Code snapshots hook config at process start, so the
# session name resolved by the command is pinned to the run launched right after
# installing — copies sharing a worktree each re-install with their own name.
# --------------------------------------------------------------------------- #

# Which hook event maps to which reported state. PreToolUse keeps "working"
# fresh on every tool call; PostToolUse fires when a tool CALL RETURNS,
# refreshing "working" at the start of the think-before-the-next-tool stretch —
# the gap that previously left the marker to age out mid-turn (the classic
# "thinking reads as idle"). It buys a fresh marker-trust window into that
# stretch; the pane/status-line layer covers the rest of a long think.
#
# PermissionRequest (added; Claude Code >= 2.x) fires the instant Claude blocks
# on a tool-permission dialog -> clarify, an explicit signal that no longer
# depends on the Notification timing. Notification still maps to clarify for
# plan/question prompts, but NOT every Notification means "needs input": Claude
# Code also fires one ~60s after the session goes idle ("Claude is waiting for
# your input"), so the Notification hook inspects its stdin payload and skips
# idle-timeout notifications (G1); see activity_markers.notification_hook_command().
#
# SessionEnd (added) fires when the CLI process exits (logout / prompt_input_exit
# / clear / …) -> idle, an explicit "turn/session is over" signal to complement
# the exit-marker + stale-marker death inference.
_HOOK_EVENT_STATES = (
    ("Stop", "idle"),
    ("SessionEnd", "idle"),
    ("UserPromptSubmit", "working"),
    ("PreToolUse", "working"),
    ("PostToolUse", "working"),
    ("PermissionRequest", "clarify"),
    ("Notification", "clarify"),
)


# --------------------------------------------------------------------------- #
# Live activity signal: `claude agents --json` (Claude Code >= 2.x).
#
# Claude Code reports the real-time state of every live session (interactive and
# background) via `claude agents --json`. Unlike the hook marker — which only
# refreshes when an event fires and goes stale during a long think between tool
# calls — this reflects the session as it is *now*, so it is the preferred
# signal, with the hook marker (then pane/CPU inspection) as the fallback the
# web layer already applies. We correlate a JSON entry to a MindFlock tmux
# session by the conversation id its hooks recorded (thread_markers) — the one
# field that uniquely identifies a session (cwd is shared by sibling windows).
# Best-effort and TTL-cached so a whole poll cycle spawns the binary at most
# once; any failure (binary missing/old, session not listed, no id yet) returns
# nothing and the caller falls back. MINDFLOCK_DISABLE_AGENTS_JSON=1 turns it off.
# --------------------------------------------------------------------------- #
_AGENTS_JSON_TTL = 2.0
_AGENTS_JSON_TIMEOUT = 8.0
# Keep serving the last SUCCESSFUL live map through a transient probe failure
# (slow/timing-out binary, a single non-zero exit) rather than blanking every
# session's live signal to marker-fallback on one hiccup — mirrors the
# last-good grace in claude_usage_api.live_usage.
_AGENTS_JSON_GRACE = 30.0
_agents_lock = threading.Lock()
_agents_cache = {
    "at": float("-inf"),
    "map": {},
    "good": {},
    "good_at": float("-inf"),
    # True while a probe subprocess is outstanding. The TTL (2s) is shorter than
    # the probe timeout (8s), so without this flag every poll during one slow
    # probe would re-claim the expired cache and spawn ANOTHER concurrent
    # `claude agents --json` — a thundering herd of subprocesses.
    "inflight": False,
}


def _map_agents_entry(e: dict) -> Optional[str]:
    """Normalize one ``claude agents --json`` entry to working/idle/clarify/None.

    Background sessions carry ``state`` (observed: working/blocked/done/failed/
    stopped); interactive sessions carry ``status`` (observed: busy/idle). A
    ``waitingFor`` reason (permission prompt / input needed / dialog open) means
    the session is blocked on the human -> clarify. Anything unrecognized -> None
    (fall back to the marker rather than guess a state)."""
    state = str(e.get("state") or "").strip().lower()
    if state:
        if state in ("working", "running", "busy"):
            return "working"
        if state == "blocked":
            return "clarify"
        if state in ("done", "completed", "idle", "stopped", "failed", "cancelled"):
            return "idle"
    if e.get("waitingFor"):
        return "clarify"
    status = str(e.get("status") or "").strip().lower()
    if status:
        if status in (
            "busy",
            "working",
            "running",
            "generating",
            "thinking",
            "tool_execution",
            "compacting",
        ):
            return "working"
        if status in ("waiting", "blocked", "needs-input", "needs_input"):
            return "clarify"
        if status in ("idle", "ready", "done", "completed"):
            return "idle"
    return None


def _agents_state_map() -> dict:
    """``{claude_session_uuid: state}`` from ``claude agents --json``, TTL-cached.

    Never raises; returns ``{}`` when disabled, when the binary is missing/old,
    or on any error — the caller then uses the hook marker. The subprocess runs
    OUTSIDE the lock so a slow/timing-out binary never serializes the web poll;
    the lock only guards the tiny check-and-claim + store."""
    if os.environ.get("MINDFLOCK_DISABLE_AGENTS_JSON"):
        return {}
    import time

    now = time.time()
    with _agents_lock:
        if now - _agents_cache["at"] <= _AGENTS_JSON_TTL:
            return _agents_cache["map"]
        # A probe is already outstanding (it may run up to the 8s timeout, well
        # past the 2s TTL): serve the stale cached map rather than spawning
        # another concurrent subprocess. ``.get`` tolerates a partial cache dict.
        if _agents_cache.get("inflight"):
            return _agents_cache["map"]
        # Claim this refresh; concurrent pollers use the (slightly stale) cached
        # map until it lands, rather than each spawning the binary.
        _agents_cache["at"] = now
        _agents_cache["inflight"] = True
    m: dict = {}
    ok = False
    try:
        import json
        import subprocess

        cp = subprocess.run(
            ["claude", "agents", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_AGENTS_JSON_TIMEOUT,
        )
        if cp.returncode == 0:
            arr = json.loads(cp.stdout.decode("utf-8", "replace") or "[]")
            if isinstance(arr, list):
                ok = True  # a clean probe (even an empty list = genuinely idle)
                for e in arr:
                    if not isinstance(e, dict):
                        continue
                    sid = str(e.get("sessionId") or "")
                    st = _map_agents_entry(e)
                    if sid and st:
                        m[sid] = st
    except Exception:  # noqa: BLE001 — any failure -> grace/marker fallback
        ok = False
        m = {}
    now2 = time.time()
    with _agents_lock:
        # The probe is done (success, failure, or timeout — the except above is
        # total) — release the in-flight claim so the NEXT expiry can re-probe.
        _agents_cache["inflight"] = False
        # Re-stamp the attempt time to when the (possibly slow) probe FINISHED so
        # a binary that runs near its timeout isn't re-spawned on the very next
        # poll — one probe per TTL, measured from completion, not claim.
        _agents_cache["at"] = now2
        if ok:
            _agents_cache["map"] = m
            _agents_cache["good"] = m
            _agents_cache["good_at"] = now2
            return m
        # Probe failed: reuse the last good live map for a grace window instead
        # of wiping it (which would flip every live session to marker-fallback).
        # ``.get`` defaults keep a hand-constructed/partial cache dict from
        # raising here (the module global always carries these keys).
        good = _agents_cache.get("good") or {}
        good_at = _agents_cache.get("good_at", float("-inf"))
        if good and (now2 - good_at) < _AGENTS_JSON_GRACE:
            _agents_cache["map"] = good
            return good
        _agents_cache["map"] = {}
        return {}


def _live_agent_state(session_name: str) -> Optional[str]:
    """Claude Code's real-time state for this tmux session via
    ``claude agents --json``, correlated by the conversation id recorded for the
    session, or None when unavailable (no id yet, binary too old, not listed)."""
    try:
        from . import thread_markers

        sid = thread_markers.read(session_name)
        if not sid:
            return None
        return _agents_state_map().get(sid)
    except Exception:  # noqa: BLE001
        return None


def install_activity_hooks(workdir: str, session_name: str) -> None:
    """Merge MindFlock's activity-reporting hooks into the worktree's
    ``.claude/settings.local.json`` (creating it if needed).

    Delegates the JSON merge to the shared
    :func:`activity_markers.merge_activity_hooks` (Codex installs the same way
    into ``.codex/hooks.json``); the Claude-specific parts are the file path,
    routing ``Notification`` through the payload-inspecting hook, and adding the
    settings file to ``.git/info/exclude`` so a plain/in-place session never
    shows it as a dirty file. Merge, never clobber: only prior MindFlock entries
    are replaced, so re-installing with a new session name is idempotent.
    Best-effort: any failure is swallowed — activity detection just falls back
    to pane hashing.
    """
    import os
    from pathlib import Path

    try:
        if not workdir or not session_name or not os.path.isdir(workdir):
            return
        settings_path = Path(workdir) / ".claude" / "settings.local.json"
        # record_thread defaults True: the Claude hook also persists the
        # payload's session_id as this window's resume-thread marker (the id
        # `claude --resume <id>` targets after a crash).
        merge_activity_hooks(
            settings_path,
            _HOOK_EVENT_STATES,
            session_name,
            notification_event="Notification",
        )
        _ensure_git_excluded(workdir, ".claude/settings.local.json")
    except Exception:  # noqa: BLE001 — never break a launch over hook install
        pass


# --------------------------------------------------------------------------- #
# Pre-trust (roadmap F2): Claude Code gates every new folder behind a
# "Do you trust the files in this folder?" dialog, recorded per project in the
# user config as ``projects.<abs-path>.hasTrustDialogAccepted``. MindFlock
# *creates* the worktree, so it is trusted by construction — seed that flag
# before launch and the first run never stalls invisibly at the gate.
# --------------------------------------------------------------------------- #
def _claude_user_config_paths():
    """The ``.claude.json`` user-config file(s) to seed, most specific first.

    ``MINDFLOCK_CLAUDE_JSON`` overrides everything (tests point it at a tmp
    file so the real user config is never touched); otherwise an explicit
    ``CLAUDE_CONFIG_DIR`` is seeded alongside the default ``~/.claude.json``.
    """
    import os

    override = os.environ.get("MINDFLOCK_CLAUDE_JSON")
    if override:
        return [override]
    paths = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        paths.append(os.path.join(cfg, ".claude.json"))
    home = os.path.join(os.path.expanduser("~"), ".claude.json")
    if home not in paths:
        paths.append(home)
    return paths


def pre_trust_workdir(workdir: str) -> None:
    """Mark ``workdir`` as trusted in Claude Code's user config (F2).

    Merge, never clobber: only ``projects.<workdir>.hasTrustDialogAccepted``
    is added/updated; every other key (and every other project entry) is
    preserved byte-for-byte in JSON terms. An unparseable config is left
    untouched (better a trust prompt than a destroyed user config), and an
    already-trusted entry means no write at all. Best-effort: never raises.
    """
    import os

    try:
        if not workdir or not os.path.isdir(workdir):
            return
        real = os.path.realpath(workdir)
        for path in _claude_user_config_paths():
            _merge_claude_trust(path, real)
    except Exception:  # noqa: BLE001 — never break a launch over pre-trust
        pass


def _merge_claude_trust(path: str, workdir: str) -> None:
    """Set ``projects[workdir].hasTrustDialogAccepted = true`` in one config
    file, writing atomically (tmp + rename) so a concurrent Claude never sees
    a half-written file. Skips silently when the existing JSON won't parse."""
    import json
    import os

    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        raw = ""
    data = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return  # user config we can't parse — never clobber it
        if not isinstance(data, dict):
            return
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    entry = projects.get(workdir)
    if not isinstance(entry, dict):
        entry = {}
        projects[workdir] = entry
    if entry.get("hasTrustDialogAccepted") is True:
        return  # already trusted — leave the file alone
    entry["hasTrustDialogAccepted"] = True
    tmp = path + ".mindflock-tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def remove_trust_entry(workdir: str) -> None:
    """Garbage-collect the trust entry ``pre_trust_workdir`` added (G3).

    Called from the session delete/cleanup paths, so ``~/.claude.json`` doesn't
    accumulate one dead ``projects`` entry per worktree forever. Deliberately
    conservative: only the exact realpath key is ever removed, and only when
    MindFlock plausibly owns it — the path sits under the managed
    ``~/.mindflock/worktrees`` tree, or the directory no longer exists (it was
    just deleted). Every other key and project entry is preserved verbatim in
    JSON terms; an unparseable config is a no-op. Best-effort: never raises.
    """
    import os

    try:
        if not workdir:
            return
        real = os.path.realpath(workdir)
        under_managed = False
        try:
            from backend.session.git.worktree import get_worktree_directory

            root = os.path.realpath(get_worktree_directory())
            under_managed = bool(root) and real.startswith(root.rstrip(os.sep) + os.sep)
        except Exception:  # noqa: BLE001 — no config dir -> rely on existence
            under_managed = False
        if not under_managed and os.path.isdir(real):
            return  # a live dir outside our worktrees tree is not ours to touch
        for path in _claude_user_config_paths():
            _remove_claude_trust(path, real)
    except Exception:  # noqa: BLE001 — GC is best-effort, never break cleanup
        pass


def _remove_claude_trust(path: str, workdir: str) -> None:
    """Delete ``projects[workdir]`` from one config file, writing atomically
    (tmp + rename) like :func:`_merge_claude_trust`. Missing file, missing
    entry, or unparseable JSON = no-op; nothing else is modified."""
    import json
    import os

    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        return
    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return  # user config we can't parse — never clobber it
    if not isinstance(data, dict):
        return
    projects = data.get("projects")
    if not isinstance(projects, dict) or workdir not in projects:
        return
    del projects[workdir]
    tmp = path + ".mindflock-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _ts_epoch(s) -> Optional[float]:
    """Parse a transcript entry's ISO ``timestamp`` to epoch seconds (or None)."""
    return ts_epoch(s)


def _claude_project_dirs(workdir: str):
    """Claude Code transcript project dirs for ``workdir``, across every
    ``~/.claude*`` config root (plus ``$CLAUDE_CONFIG_DIR``) — wrappers and
    alternate installs may keep separate config dirs. Existing dirs only."""
    import os
    import re

    if not workdir:
        return []
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", workdir)
    home = os.environ.get("HOME", "")
    roots = set()
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.add(cfg)
    if home and os.path.isdir(home):
        for name in os.listdir(home):
            if name.startswith(".claude"):
                d = os.path.join(home, name)
                if os.path.isdir(d):
                    roots.add(d)
    out = []
    for root in roots:
        proj = os.path.join(root, "projects", encoded)
        if os.path.isdir(proj):
            out.append(proj)
    return out


# --------------------------------------------------------------------------- #
# Latest-turn snippet (L3 / J3 second half): the newest assistant/user message
# from the same transcript JSONL the token telemetry reads, reduced to one
# ≤120-char line for the N-parallel-sessions triage view. Mtime-guarded cache
# (~10s) so the 4s UI poll costs one stat pass, not a parse.
# --------------------------------------------------------------------------- #
_LAST_TURN_TTL = 10.0
_LAST_TURN_TAIL_BYTES = 128 * 1024  # newest entries live at the file's end
_LAST_TURN_CACHE = (
    {}
)  # workdir -> {"checked": epoch, "sig": (path, mtime, size), "snippet": str|None}
_LAST_TURN_CACHE_MAX = 512  # bound like _TT_FILE_CACHE — one entry per workdir


def _last_turn_cache_put(workdir: str, entry: dict) -> None:
    """Store a snippet-cache entry, evicting the oldest half when full so a
    long-running server with high session/workdir churn doesn't leak one entry
    per distinct workdir forever (mirrors _TT_FILE_CACHE's bound)."""
    if (
        workdir not in _LAST_TURN_CACHE
        and len(_LAST_TURN_CACHE) >= _LAST_TURN_CACHE_MAX
    ):
        for k in list(_LAST_TURN_CACHE)[: _LAST_TURN_CACHE_MAX // 2]:
            _LAST_TURN_CACHE.pop(k, None)
    _LAST_TURN_CACHE[workdir] = entry


def _snippet_from_text(text: str, limit: int = 120) -> Optional[str]:
    """First meaningful line of a message body, markdown/tool noise stripped,
    truncated to ``limit`` chars. None when nothing readable remains."""
    import re

    if not text:
        return None
    for line in str(text).splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        if s.startswith("<"):  # <system-reminder>/<command-…> tag noise
            continue
        if s.startswith("[Request interrupted"):
            continue
        # Strip markdown heading/list/quote prefixes + emphasis/backticks.
        s = re.sub(r"^[#>*\-\s]+", "", s)
        s = s.replace("**", "").replace("`", "").strip()
        if not s:
            continue
        if len(s) > limit:
            s = s[: limit - 1].rstrip() + "…"
        return s
    return None


def _entry_text(obj) -> Optional[str]:
    """The human-readable body of one transcript entry, or None to skip it
    (meta entries, tool_use/tool_result-only turns, non-conversation lines)."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") not in ("user", "assistant"):
        return None
    if obj.get("isMeta"):
        return None
    msg = obj.get("message") or {}
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t.strip():
                    return t
    return None


def _newest_transcript(workdir: str) -> Optional[tuple]:
    """The most-recently-modified transcript ``.jsonl`` across ``workdir``'s
    project dirs, as ``(mtime, size, path)``, or None when there are none."""
    import os

    newest = None  # (mtime, size, path)
    for proj in _claude_project_dirs(workdir):
        for fn in os.listdir(proj):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(proj, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if newest is None or st.st_mtime > newest[0]:
                newest = (st.st_mtime, st.st_size, p)
    return newest


def _snippet_from_transcript(path: str, size: int) -> Optional[str]:
    """One-line snippet of the newest conversational turn in one transcript
    ``.jsonl``, scanned tail-first (reading only the last
    ``_LAST_TURN_TAIL_BYTES`` of a large file). None when the file is
    unreadable or holds no conversational turn."""
    import json
    import os

    snippet = None
    try:
        with open(path, "rb") as f:
            if size > _LAST_TURN_TAIL_BYTES:
                f.seek(-_LAST_TURN_TAIL_BYTES, os.SEEK_END)
                tail = f.read().decode("utf-8", "replace")
                lines = tail.splitlines()[1:]  # drop the partial first line
            else:
                lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            text = _entry_text(obj)
            if text is None:
                continue
            snippet = _snippet_from_text(text)
            if snippet:
                break
    except OSError:
        snippet = None
    return snippet


def _claude_last_turn_snippet(workdir: str) -> Optional[str]:
    """One-line snippet of the newest conversational turn in ``workdir``'s
    transcripts (newest .jsonl by mtime, scanned tail-first). Never raises."""
    import time

    try:
        now = time.time()
        cached = _LAST_TURN_CACHE.get(workdir)
        if cached and now - cached["checked"] < _LAST_TURN_TTL:
            return cached["snippet"]

        newest = _newest_transcript(workdir)
        if newest is None:
            _last_turn_cache_put(
                workdir, {"checked": now, "sig": None, "snippet": None}
            )
            return None

        sig = (newest[2], newest[0], newest[1])
        if cached and cached.get("sig") == sig:
            cached["checked"] = now  # unchanged file — just re-arm the TTL
            return cached["snippet"]

        snippet = _snippet_from_transcript(newest[2], newest[1])
        _last_turn_cache_put(workdir, {"checked": now, "sig": sig, "snippet": snippet})
        return snippet
    except Exception:  # noqa: BLE001 — triage hint only; never break the poll
        return None


def _file_in_window(
    first_ts: Optional[float], since_ts: Optional[float], until_ts: Optional[float]
) -> bool:
    """Whether a transcript file (its conversation) belongs to this session.

    Attribution is by the file's FIRST turn: a Claude conversation is born once
    and belongs wholly to the session that was active when it started. This is
    what separates concurrent copies sharing one cwd — each copy starts its own
    ``.jsonl``, so summing whole files by birth time keeps a copy's later turns
    with the copy and the original's later turns with the original (pure time
    windowing per-turn cannot, since after a copy both sessions write turns in
    the same time range but to different files).

    A file with no parseable timestamps is counted only in the open-ended window
    (``until_ts is None`` — the latest/only session), never assigned to a bounded
    one where it can't be placed.
    """
    if first_ts is None:
        return until_ts is None
    if since_ts is not None and first_ts < since_ts:
        return False
    if until_ts is not None and first_ts >= until_ts:
        return False
    return True


def _claude_transcript_tokens(
    workdir: str,
    since_ts: Optional[float],
    until_ts: Optional[float] = None,
    shared_cwd: bool = False,
) -> dict:
    """Sum the four /usage figures from Claude Code's transcripts for ``workdir``.

    Claude Code stores per-session transcripts under
    ``<config-dir>/projects/<cwd-with-non-alnum->-dashes>/*.jsonl``; each file is
    ONE conversation and each assistant message carries a ``usage`` block. We
    scan every ``~/.claude*`` config root (plus ``$CLAUDE_CONFIG_DIR``) because
    wrappers and alternate installs may keep separate config dirs.

    Attribution depends on whether the cwd is shared:

    * ``shared_cwd=False`` (the only session on this cwd): sum turns at/after
      ``since_ts`` across all files — a resumed/``--continue``d conversation
      whose file predates this session still counts its post-start turns.
    * ``shared_cwd=True`` (a window and its copies share this cwd): attribute
      each *whole conversation file* to exactly one session by its first turn's
      time (see :func:`_file_in_window`) — each copy starts its own file, so this
      keeps each copy's cost separate instead of every copy summing them all.

    Never raises.
    """
    import os

    zero = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "ctx": 0, "model": ""}
    if not workdir:
        return dict(zero)
    try:
        in_total = out_total = cache_read_total = cache_write_total = 0
        # The most recent turn's full prompt size (real input + both cache legs) is
        # how full the context window is right now — tracked separately from the
        # cumulative sums, which re-count that context on every turn.
        latest_ts = None
        latest_ctx = 0
        latest_model = ""

        for proj in _claude_project_dirs(workdir):
            for fn in os.listdir(proj):
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(proj, fn)
                agg = _transcript_file_tokens(path, since_ts, until_ts, shared_cwd)
                if agg is None:
                    continue
                in_total += agg["in"]
                out_total += agg["out"]
                cache_write_total += agg["cache_write"]
                cache_read_total += agg["cache_read"]
                # Merge each file's newest turn the same way _fold_turn merged
                # turns within a file: a timestamped turn wins on >=; ts-less
                # turns only fill in while nothing timestamped has been seen.
                if agg["latest_ts"] is not None:
                    if latest_ts is None or agg["latest_ts"] >= latest_ts:
                        latest_ts, latest_ctx = agg["latest_ts"], agg["latest_ctx"]
                        latest_model = agg["latest_model"] or latest_model
                elif latest_ts is None and agg["counted"]:
                    latest_ctx = agg["latest_ctx"]
                    latest_model = agg["latest_model"] or latest_model
        return {
            "in": in_total,
            "out": out_total,
            "cache_read": cache_read_total,
            "cache_write": cache_write_total,
            "ctx": latest_ctx,
            "model": latest_model,
        }
    except Exception:  # noqa: BLE001
        return dict(zero)


# Per-transcript-file aggregate memo: (mtime, size, window) -> folded usage.
# Transcripts are append-only and multi-MB for long conversations; without
# this every telemetry refresh re-read and re-JSON-parsed every file in full.
# Values are a handful of numbers per file, so memory stays flat.
_TT_FILE_CACHE: dict = {}  # path -> (sig, agg)
_TT_FILE_CACHE_MAX = 512


def _read_transcript_turns(path: str) -> Optional[tuple]:
    """Parse one transcript ``.jsonl`` into its per-turn usage tuples.

    Returns ``(turns, first_ts)`` where ``turns`` is a list of
    ``(t_in, t_out, t_cw, t_cr, ts, model)`` for every line carrying a
    ``usage`` block and ``first_ts`` is the earliest parseable ``timestamp``
    (the conversation's birth). Returns ``None`` when the file can't be
    opened; a malformed line is skipped, never raised.
    """
    import json

    # One pass over the file, collecting its turns + first-turn time.
    turns = []  # (t_in, t_out, t_cw, t_cr, ts, model)
    f_first_ts = None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                ts = _ts_epoch(obj.get("timestamp"))
                if ts is not None and (f_first_ts is None or ts < f_first_ts):
                    f_first_ts = ts
                msg = obj.get("message") or {}
                u = msg.get("usage") or obj.get("usage") or {}
                turns.append(
                    (
                        int(u.get("input_tokens", 0) or 0),
                        int(u.get("output_tokens", 0) or 0),
                        int(u.get("cache_creation_input_tokens", 0) or 0),
                        int(u.get("cache_read_input_tokens", 0) or 0),
                        ts,
                        msg.get("model") or obj.get("model") or "",
                    )
                )
    except OSError:
        return None
    return turns, f_first_ts


def _transcript_file_tokens(
    path: str,
    since_ts: Optional[float],
    until_ts: Optional[float],
    shared_cwd: bool,
) -> Optional[dict]:
    """Fold one transcript file's usage into an aggregate dict (or ``None``
    for an unreadable / out-of-window file), memoized on (mtime, size) so an
    unchanged file is never re-read. Never raises."""
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size, since_ts, until_ts, shared_cwd)
    except OSError:
        return None
    hit = _TT_FILE_CACHE.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]

    parsed = _read_transcript_turns(path)
    if parsed is None:
        return None
    turns, f_first_ts = parsed

    agg = {
        "in": 0,
        "out": 0,
        "cache_write": 0,
        "cache_read": 0,
        "latest_ts": None,
        "latest_ctx": 0,
        "latest_model": "",
        "counted": False,
    }

    def _fold(t_in, t_out, t_cw, t_cr, ts, model):
        agg["in"] += t_in
        agg["out"] += t_out
        agg["cache_write"] += t_cw
        agg["cache_read"] += t_cr
        agg["counted"] = True
        turn_ctx = t_in + t_cr + t_cw
        if ts is not None:
            if agg["latest_ts"] is None or ts >= agg["latest_ts"]:
                agg["latest_ts"], agg["latest_ctx"] = ts, turn_ctx
                agg["latest_model"] = model or agg["latest_model"]
        elif agg["latest_ts"] is None:
            agg["latest_ctx"] = turn_ctx
            agg["latest_model"] = model or agg["latest_model"]

    if shared_cwd:
        # Whole conversation → the one session whose window it was born in.
        # All of its turns count (or none).
        if _file_in_window(f_first_ts, since_ts, until_ts):
            for t in turns:
                _fold(*t)
    else:
        # Lone session: keep simple since_ts turn filtering so a continued
        # conversation still counts its post-start turns.
        for t_in, t_out, t_cw, t_cr, ts, model in turns:
            if since_ts is not None and ts is not None and ts < since_ts:
                continue
            _fold(t_in, t_out, t_cw, t_cr, ts, model)

    if len(_TT_FILE_CACHE) >= _TT_FILE_CACHE_MAX:
        # Drop the oldest-inserted half — simple, rare, keeps the dict bounded.
        for k in list(_TT_FILE_CACHE)[: _TT_FILE_CACHE_MAX // 2]:
            _TT_FILE_CACHE.pop(k, None)
    _TT_FILE_CACHE[path] = (sig, agg)
    return agg
