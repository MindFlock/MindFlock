"""Coding-provider abstraction.

A :class:`CodingProvider` encapsulates everything that is specific to *which*
coding-agent CLI a session runs: how to build its launch command, how to write
a worktree launcher script, how it selects a backend/profile, what exit codes
mean a clean quit, how its terminal prompts are classified, and how to read its
token telemetry.

Both launch call-sites — the engine's :meth:`Instance.Start` and the web UI's
``_ensure_agent_session`` — resolve a provider from ``Instance.Program`` and
drive it through this one interface, so the two can never drift apart again.

The default :class:`BaseProvider` implements provider-agnostic behaviour (run a
custom program, resume it with ``--continue``, treat 0/130 as a clean quit, no
profiles, no telemetry). The bundled Claude provider (:mod:`claude`)
overrides the pieces that are specific to Claude Code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class LaunchContext:
    """Everything a provider needs to build a launch command for one session.

    This is the union of the inputs the engine and web launch paths pass today.
    """

    program: str = ""  # the raw Instance.Program string (provider may re-parse)
    workdir: str = ""  # absolute worktree / clone / in-place path
    prompt: str = ""  # ticket seed; "" = warm / no-seed
    resume: bool = (
        False  # True = continue the prior conversation (unnatural death/reboot)
    )
    skip_permissions: bool = False
    in_place: bool = False  # borrowing a worktree; never owns a launcher
    session_name: str = ""  # sanitized tmux name — the marker key
    # Per-session argv tokens appended after the provider's saved defaults.
    launch_args: Sequence[str] = ()
    # Cache env vars the workspace's warm caches need exported in the launcher
    # (e.g. {"TESTMON_ENV": "shared"}). None = "not provided" -> the launcher
    # writer falls back to its built-in default; {} = provisioned with no caches.
    cache_env: Optional[Mapping[str, str]] = None
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TrustSpec:
    """How a provider's CLI asks for trust/confirmation and how to dismiss it.

    ``patterns`` are substrings to look for in the pane; ``keystroke`` is the raw
    bytes to send to accept (Claude: Enter)."""

    patterns: Sequence[str] = ()
    keystroke: bytes = b"\r"


_NATURAL_EXIT_CODES = (0, 130)


@dataclass(frozen=True)
class LauncherSpec:
    """The CLI-specific vocabulary the provisioned-workspace launcher needs.

    :func:`backend.session.provisioned.write_launcher` generates ONE shell
    script — the thing tmux runs for a provisioned session (a ticket ingested by
    the pipeline, a PR workspace, a warm workspace). It used to hardcode Claude
    Code's spelling of all four knobs below, which meant a provisioned session
    running any other CLI was launched with Claude's flags: an ingested ticket
    on ``aider`` got ``aider --dangerously-skip-permissions "<prompt>"`` and
    resumed with ``aider --continue``, neither of which aider accepts. This spec
    is that vocabulary, per provider, so the launcher is provider-neutral.

    * ``skip_perms_flag`` — the CLI's "don't ask me to approve each action" flag,
      appended when the workspace was provisioned with skip-permissions on.
      ``""`` = the CLI has no such flag (goose configures it via ``GOOSE_MODE``,
      cline auto-approves by default), so nothing is appended.
    * ``prompt_arg`` — template that passes the seed prompt at launch, with a
      ``{prompt}`` placeholder (Claude/codex take it positionally, so
      ``"{prompt}"``; antigravity wants ``"--prompt-interactive {prompt}"``).
      ``""`` = this CLI takes no prompt argument, and the launcher falls back to
      typing the prompt into the pane once the TUI has drawn.
    * ``resume_flag`` — how to continue the workspace's prior conversation on a
      relaunch (``"--continue"``, ``"resume --last"``, ``"-r"``). ``""`` = the
      CLI cannot resume, so a relaunch starts a fresh conversation.
    * ``resume_fallback`` — whether a failed resume should be retried once and
      then fall back to a plain launch. False for a CLI whose resume flag is
      harmless with nothing to resume (aider's ``--restore-chat-history``).
    * ``natural_codes`` — exit codes that mean the human quit deliberately, so
      the launcher's in-session loop drops to a shell instead of relaunching.
    * ``command`` — the provider's own resolved base command, for CLIs whose
      interactive entry point is a SUBCOMMAND rather than the bare binary
      (``goose session``, ``cline -i``) or whose binary differs from its name
      (``antigravity`` -> ``agy``). It replaces the executable token of the
      session's program string, keeping any trailing args the caller added.
      ``""`` = launch the program string verbatim (Claude, and any custom
      program), which is what keeps the generated script byte-stable.
    """

    skip_perms_flag: str = ""
    prompt_arg: str = ""
    resume_flag: str = "--continue"
    resume_fallback: bool = True
    natural_codes: Sequence[int] = _NATURAL_EXIT_CODES
    command: str = ""


@dataclass(frozen=True)
class EffortSpec:
    """How ONE CLI spells "think harder about this".

    Every coding CLI that exposes reasoning effort spells it differently — and
    names the rungs differently too — so MindFlock keeps a single neutral ladder
    (:mod:`backend.providers.effort`) and asks each provider to translate:

    * ``args`` — argv template tokens with a ``{level}`` placeholder, appended to
      the launch command. Claude: ``("--effort", "{level}")``; codex:
      ``("-c", "model_reasoning_effort={level}")``; antigravity's ``agy``:
      ``("--effort", "{level}")``. Empty = this CLI has no effort flag, so a
      requested level adds nothing (the launch is exactly what it is today).
    * ``levels`` — the level names THIS CLI accepts, ordered cheapest-first. Only
      these are ever passed to it: a request above the CLI's ceiling clamps down
      to its top rung rather than handing it a value it would reject (claude
      warns and silently uses its default; codex forwards the string to the API,
      which 400s).
    * ``ultra_level`` — a level name of its own for the top ``ultra`` rung, passed
      through ``args`` like any other level even though it is NOT in ``levels``
      (Claude Code: ``--effort ultracode``, which is xhigh effort plus standing
      multi-agent orchestration, i.e. a different mode rather than a higher rung).
      Empty = ``ultra`` clamps to the CLI's highest ordinary level.
    * ``prompt_keyword`` — a keyword the CLI recognises IN THE PROMPT ITSELF for
      the top ``ultra`` rung, appended to the seed prompt. The fallback for a CLI
      that has such a keyword but no flag to carry it; when ``ultra_level`` is set
      the flag says it once, for the whole session, and the keyword is not added.
      Empty = the top rung is just the CLI's highest level.
    """

    args: Sequence[str] = ()
    levels: Sequence[str] = ()
    ultra_level: str = ""
    prompt_keyword: str = ""


def seed_prompt_expr(session_name: str, prompt: str) -> str:
    """A shell expression that expands to ``prompt`` as ONE argument, or ``""``.

    Writes ``prompt`` to a MindFlock-owned file (outside any repo, so an
    in-place session never shows a dirty prompt file in the user's checkout) and
    returns ``"$(cat <path>)"``. The non-provisioned launch paths (plain /
    in-place sessions, generic providers) append this so a session created with
    an initial prompt seeds it into the agent at launch — the same delivery the
    provisioned worktree launcher already does (it writes ``.mindflock_prompt.md``
    into the worktree). Passing the prompt as a launch argument means there is no
    keystroke race and no readiness wait: the CLI receives it as its own argv the
    moment it starts.

    Best-effort: returns ``""`` when the prompt is empty or the file can't be
    written (the launch then just proceeds with no seed, the prior behaviour).
    ``MINDFLOCK_SEED_PROMPT_DIR`` overrides the directory (tests point it at a
    tmp dir).
    """
    if not prompt:
        return ""
    import os
    import re
    import shlex
    from pathlib import Path

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name or "session")
    base_dir = os.environ.get("MINDFLOCK_SEED_PROMPT_DIR") or os.path.join(
        os.path.expanduser("~"), ".mindflock-assistant", ".seed-prompts"
    )
    try:
        d = Path(base_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / (safe + ".md")
        path.write_text(prompt, encoding="utf-8")
    except OSError:
        return ""
    return '"$(cat %s)"' % shlex.quote(str(path))


#: The token an ``oneshot_args`` template uses for the prompt text.
ONESHOT_PROMPT_TOKEN = "{prompt}"


def oneshot_command(
    binary: str, args: Sequence[str], prompt: str
) -> Optional[list[str]]:
    """Splice ``prompt`` into a one-shot argv template, or ``None``.

    ``None`` for a CLI with no template (``args`` empty) or an empty prompt —
    "this provider can't answer a question headlessly", which callers treat as
    "do it without a model" rather than as an error.

    Only the FIRST token of ``binary`` is used: a provider's base command can
    carry an interactive subcommand (``goose session``, ``cline -i``) that the
    one-shot must not inherit — its own subcommand is in ``args`` (``run -t``).
    No shell is involved anywhere; this is an argv list for
    :func:`subprocess.run`, which is why a prompt holding a whole diff is safe.
    """
    import shlex

    if not args or not prompt:
        return None
    exe = (shlex.split(binary or "") or [""])[0]
    if not exe:
        return None
    return [exe] + [
        prompt if a == ONESHOT_PROMPT_TOKEN else a.replace(ONESHOT_PROMPT_TOKEN, prompt)
        for a in args
    ]


class BaseProvider:
    """Default, provider-agnostic implementation. Subclass and override only
    what differs for a specific CLI."""

    #: Registry key (e.g. "claude", "aider").
    name: str = "base"
    #: Executable basenames this provider claims.
    program_aliases: Sequence[str] = ()

    # --- identity --------------------------------------------------------- #
    def matches(self, program: str) -> bool:
        if not program:
            return False
        import os

        return os.path.basename(program.split()[0]) in self.program_aliases

    # --- launch ----------------------------------------------------------- #
    def build_launch_command(self, ctx: LaunchContext) -> Optional[str]:
        """The shell string tmux runs (before the exit-marker wrapper), or None
        to launch the bare program. Default: run the program, resuming it with
        ``--continue`` (falling back to a fresh run) when ``ctx.resume``."""
        prog = ctx.program
        if not prog:
            return None
        if ctx.resume:
            return "%s --continue || %s" % (prog, prog)
        return prog

    # --- worktree launcher script ----------------------------------------- #
    def owns_launcher(self, ctx: LaunchContext) -> bool:
        """Whether this provider generates a launcher script. Only providers that
        actually implement ``write_launcher`` (Claude) return True."""
        return False

    def write_launcher(self, ctx: LaunchContext) -> str:
        """Write a launcher script into ``ctx.workdir`` and return its path.

        Uses ``ctx.program``/``ctx.prompt``/``ctx.skip_permissions``. Base has no
        launcher concept; providers that own one override this.
        """
        raise NotImplementedError("%s has no launcher script" % self.name)

    def launcher_spec(self) -> LauncherSpec:
        """This CLI's flag vocabulary for the provisioned-workspace launcher.

        :func:`backend.session.provisioned.write_launcher` asks the provider that
        claims a session's program for this, so one generated script serves every
        CLI instead of hardcoding Claude Code's spelling. Base default mirrors
        :meth:`build_launch_command`: run the program bare, resume it with
        ``--continue`` (retried, then a plain launch), no skip-permissions flag
        and no prompt argument — the safe reading of an unknown program, since
        appending a flag it doesn't accept makes it refuse to start at all.
        """
        return LauncherSpec(natural_codes=_NATURAL_EXIT_CODES)

    # --- exit / resume policy --------------------------------------------- #
    def effort_spec(self) -> EffortSpec:
        """How this CLI spells reasoning effort. Default: it doesn't.

        An empty spec means a requested effort adds nothing to the launch — the
        honest answer for a CLI with no such setting (aider, goose, cline,
        opencode and any custom program), and the reason the neutral ladder in
        :mod:`backend.providers.effort` reports what it actually applied."""
        return EffortSpec()

    def is_natural_exit(self, code) -> bool:
        """True when the agent ended the way the user asked (clean quit / Ctrl-C)
        — i.e. restart fresh rather than ``--continue`` resume."""
        return code in _NATURAL_EXIT_CODES

    # --- terminal classification ------------------------------------------ #
    def trust_prompt(self) -> Optional[TrustSpec]:
        """The CLI's trust/confirmation prompt + the keystroke to dismiss it.

        Default mirrors the generic non-Claude handling: dismiss an "Open
        documentation url for more info" gate with 'D' then Enter. Claude
        overrides with its own per-folder trust + MCP patterns (Enter)."""
        return TrustSpec(
            patterns=("Open documentation url for more info",),
            keystroke=b"\x44\x0d",  # 'D' then Enter
        )

    def idle_prompt_pattern(self) -> Optional[str]:
        """A substring that signals the CLI is idle/waiting at a prompt (used to
        distinguish 'working' from 'waiting'), or None."""
        return None

    def waiting_prompt_patterns(self) -> tuple:
        """Substrings shown when the agent has PAUSED to ask the user something
        (tool-permission prompt, plan approval, clarifying question) and is
        blocked on an answer — distinct from merely idle after finishing. Lets
        the UI surface a 'clarify' state. Default: none (no such detection)."""
        return ()

    def working_pane_patterns(self) -> tuple:
        """Regexes shown while the CLI is running a turn (thinking / generating /
        awaiting a tool) — its in-flight status line, e.g. the spinner and an
        ``esc to interrupt`` hint. A match means the agent is WORKING even at ~0
        local CPU: during extended thinking the work runs server-side and the
        local process blocks on a network read, so it's indistinguishable from
        an idle prompt by CPU alone (the root cause of thinking reading as idle).
        These markers live on the very bottom pane lines that the activity change
        hash strips as per-frame noise, so the web layer scans the RAW pane for
        them, separately from that hash. Default: none (no such detection)."""
        return ()

    def progress_token_pattern(self) -> Optional[str]:
        """A regex with ONE capture group holding a monotonically climbing turn
        counter from the CLI's status line (e.g. the ``12.3k tokens`` figure Claude
        Code shows while working). The captured value may carry a ``k``/``m``
        suffix; the web layer normalizes it and treats any INCREASE since the last
        poll as positive proof of work — robust where CPU is ~0 (thinking) and the
        spinner glyph is too volatile to match. Default: no such counter."""
        return None

    # --- activity signal ---------------------------------------------------- #
    def activity_state(self, session_name: str) -> Optional[str]:
        """The CLI's own report of what it is doing right now, or None.

        Providers whose CLI can announce its state (Claude Code via hooks —
        see :meth:`install_activity_hooks`) return ``"working"`` / ``"idle"`` /
        ``"clarify"`` read from a per-session marker file; the web layer treats
        that as authoritative over pane-hash guessing. Default: no such signal.
        """
        return None

    def activity_state_age(self, session_name: str) -> Optional[float]:
        """Seconds since :meth:`activity_state` was last refreshed, or None.

        A marker-based signal only updates when the CLI fires a hook (tool call,
        prompt submit, stop, notification); between those it goes stale. The web
        layer uses this age to stop trusting a stale ``working`` / ``clarify``
        marker and fall back to live pane inspection. Default: no signal (None),
        which the web layer reads as "trust the state" (unchanged behaviour).
        """
        return None

    def install_activity_hooks(self, workdir: str, session_name: str) -> None:
        """Arrange for the CLI launched in ``workdir`` to report its activity
        for ``session_name`` (feeding :meth:`activity_state`). Called
        best-effort right before a session launches. Default: no-op."""
        return None

    def last_turn_snippet(self, session_name: str, workdir: str) -> Optional[str]:
        """A one-line snippet of the session's latest conversational turn
        (newest assistant/user message, first meaningful line, ≤120 chars) for
        N-session triage, or None. Default: provider has no transcript to read.
        Implementations must be cheap — this is called from the UI poll."""
        return None

    def last_prompt_snippet(self, session_name: str, workdir: str) -> Optional[str]:
        """A one-line snippet of the newest USER prompt (first meaningful
        line, ≤120 chars) — what the human last asked this session to do; the
        panes pin it above the terminal. Same cheapness contract as
        :meth:`last_turn_snippet`. Default: no transcript to read."""
        return None

    def last_prompt_full(self, session_name: str, workdir: str) -> Optional[str]:
        """The newest USER prompt's whole body (capped ~4000 chars) — the
        pinned line's hover/click expansion. Must come from the same entry
        :meth:`last_prompt_snippet` reported. Default: no transcript."""
        return None

    def find_prompt_full(
        self, session_name: str, workdir: str, prefix: str
    ) -> Optional[str]:
        """Full body of the newest USER prompt starting with ``prefix`` (a
        width-truncated line from the terminal), for expanding OLDER prompts
        than the latest. Default: no transcript to search."""
        return None

    # --- per-session resume thread ----------------------------------------- #
    def resume_thread_id(self, session_name: str) -> str:
        """The conversation/session id recorded for THIS window, or ``""``.

        Several windows can share one working directory; the CLIs' bulk resume
        flags (``--continue`` / ``resume --last``) pick the newest conversation
        for the directory, so siblings all resumed the same thread. Providers
        that can resume by id use this in their resume launch command instead.
        """
        from . import thread_markers

        return thread_markers.read(session_name)

    def record_thread(
        self,
        session_name: str,
        workdir: str,
        since_ts: Optional[float] = None,
        profile_id: str = "",
    ) -> None:
        """Discover and persist the conversation id this window is running
        (feeding :meth:`resume_thread_id`). ``since_ts`` is the current tmux
        pane's creation time — only conversations started after it belong to
        this run. ``profile_id`` is the auth profile the session runs as, so
        the id is also remembered per account and survives a swap away and
        back (:mod:`backend.providers.thread_markers`). Called best-effort from
        the UI poll; must be cheap. Default: provider has no discoverable
        thread (Claude records via its hooks instead)."""
        return None

    # --- usage-limit detection (roadmap D) -------------------------------- #
    def usage_limit_patterns(self) -> Sequence[str]:
        """Regexes that identify this CLI's 'usage limit reached' screen.
        Default: the shared :data:`usage_limits.DEFAULT_LIMIT_PATTERNS`."""
        from .usage_limits import DEFAULT_LIMIT_PATTERNS

        return DEFAULT_LIMIT_PATTERNS

    def usage_limit_state(self, pane_text: str, now: Optional[float] = None) -> dict:
        """``{"limited": bool, "reset_at": float|None}`` from the agent pane —
        whether the CLI is currently usage-limited and when it resets, so the
        queue can wait out the limit and resume when it reopens."""
        from .usage_limits import detect_limit

        return detect_limit(pane_text, self.usage_limit_patterns(), now)

    # --- minimal / connection-free launch (roadmap E) -------------------- #
    def minimal_launch_command(
        self, workdir: str = "", session_name: str = ""
    ) -> Optional[str]:
        """A launch command with NO MCP servers / connections attached, for the
        scheduled window-refresh: a throwaway session that only needs to accept a
        1-token ping to anchor the usage window. Default: run the program bare."""
        prog = self.program_aliases[0] if self.program_aliases else self.name
        return prog or None

    # --- headless one-shot ------------------------------------------------ #
    def oneshot_argv(self, prompt: str) -> Optional[list[str]]:
        """argv that asks this CLI ONE question and prints the answer, or None.

        The way MindFlock uses a model for its own purposes — no tmux, no PTY, no
        session — currently just writing commit messages
        (:mod:`backend.web.core.commit_message`). None means "this CLI has no
        text-only mode we trust", and every caller must have a no-model fallback
        for that: a provider that can't answer must never block the work.

        The default is None rather than a guess. Print-mode spellings genuinely
        conflict between CLIs (``-p`` prints for claude and plans for cline), and
        a wrong flag would launch an editing agent on the user's worktree.
        """
        return None

    # --- usage-window knowledge (roadmap E) ------------------------------- #
    def usage_window(self) -> dict:
        """How this CLI's usage limits reset in time — the knowledge the
        scheduled window-refresh and the UI use.

        Returns ``{kind, hours, weekly_hours, note}``. ``kind``: ``"rolling"``
        (a sliding window that anchors on first use), ``"daily"`` (per-day
        quota), or ``""`` (unknown / pay-per-token — no MindFlock-managed
        window). Default: unknown."""
        return {"kind": "", "hours": 0.0, "weekly_hours": 0.0, "note": ""}

    def usage_mode(self) -> str:
        """How this CLI's usage is PAID for — drives whether the UI leads with
        dollars or with window/reset info.

        ``"metered"``: pay-per-token (own API key) — dollar estimates are real
        marginal spend, so show them. ``"windowed"``: a subscription plan — the
        marginal cost of a turn is $0 and dollar figures are only API-equivalent
        estimates, so lead with percent-used / reset time instead. Default
        derives from :meth:`usage_window`: a declared window kind means a plan;
        no window means metered."""
        return "windowed" if (self.usage_window() or {}).get("kind") else "metered"

    def usage_live(self) -> Optional[dict]:
        """Authoritative live usage from the provider's own service, if it has
        one — ``{"percent_used", "end", "weekly", "extra"}`` (all optional) or
        None. When present it overrides MindFlock's transcript-derived window
        estimate (which can drift). Default: no live source."""
        return None

    def usage_periods(self) -> Optional[dict]:
        """Rolling-window token+cost totals for THIS provider, keyed
        ``{day, week, month, year}`` with each value
        ``{in, out, cache_read, cache_write, cost}`` — the per-provider
        equivalent of the combined breakdown, read from the provider's own
        on-disk history. Default: no history available (None)."""
        return None

    def usage_panel_visible(self) -> bool:
        """Whether this provider appears in the sidebar's overall cost/usage
        panel by DEFAULT (tabs + combined totals). Only the bundled default
        CLIs (claude/codex/antigravity/aider) opt in, so the panel isn't
        cluttered by every provider that happens to run a session. Default:
        hidden."""
        return False

    # --- connection: install + login (Settings → Providers) --------------- #
    def install_hint(self) -> str:
        """A copy-paste command that installs this CLI, or ``""`` when there is
        no provider-specific one (the UI then falls back to a platform
        package-manager hint keyed on the program name). Default: none."""
        return ""

    def login_command(self) -> Optional[str]:
        """The command the one-click "Log in" terminal runs so the user can
        authenticate the CLI *through the CLI itself*. Default: run the program
        bare (many CLIs prompt to log in on first launch)."""
        prog = self.program_aliases[0] if self.program_aliases else self.name
        return prog or None

    def auth_evidence(self) -> str:
        """Best-effort human string when the CLI looks logged in, else ``""``
        (reported as "login status unknown", never "logged out"). Default: no
        probe — providers that know where their credentials live override this."""
        return ""

    # --- telemetry -------------------------------------------------------- #
    def session_tokens(
        self,
        workdir: str,
        since_ts: Optional[float],
        until_ts: Optional[float] = None,
        shared_cwd: bool = False,
    ) -> dict:
        """Tokens used since ``since_ts``. Default: provider has no telemetry.

        ``since_ts``/``until_ts`` bound the session's lifetime in the shared
        workspace: when several sessions share one ``workdir`` (a window and its
        copies — ``shared_cwd=True``), ``until_ts`` is the next sibling session's
        start, so each session's telemetry is attributed to only its own
        conversation(s) instead of summing every run in the folder. When
        ``shared_cwd`` is False (the only session on the cwd) the provider keeps
        simple ``since_ts`` filtering so a resumed/continued conversation still
        counts. ``None`` = open-ended.

        ``ctx`` is the newest turn's context-window fill; ``model`` is that turn's
        model id (used to price the raw sums). Providers without telemetry return
        zeros — the web layer then reports no cost/context for them.
        """
        return {
            "in": 0,
            "out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "ctx": 0,
            "model": "",
        }


# A structural alias — callers type against "CodingProvider"; BaseProvider (and
# its subclasses) satisfy it. Kept simple (no typing.Protocol) to avoid runtime
# import cost; BaseProvider IS the canonical shape.
CodingProvider = BaseProvider
