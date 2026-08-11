"""Config-driven provider definitions.

A :class:`ProviderConfig` is pure data describing how to launch + classify a
coding CLI. The bundled aider/codex providers are just configs; a user
can register a brand-new CLI with zero Python by dropping a TOML file in
``~/.mindflock/providers/`` (or ``$MINDFLOCK_PROVIDERS_DIR``).

TOML shape::

    [provider]
    name = "mycli"
    program = "mycli"            # or a list of executable basenames it claims
    command = "mycli"           # base invocation (default: name)

    [launch]
    args = ["--dangerously-skip-permissions"] # always appended when this provider starts
    resume_flag = "--continue"  # appended on resume ("" -> always relaunch fresh)
    skip_perms_flag = "--yolo"  # trust/skip-permissions bypass ("" -> omitted)
    resume_fallback = true      # emit `<resume> || <fresh>` like claude
    prompt_arg = "{prompt}"     # how to pass a start prompt ("" -> can't seed one)
    oneshot_args = ["-p", "{prompt}"]  # run one prompt headlessly and print it

    [exit]
    natural_codes = [0, 130]    # exit codes that mean a clean quit

    [classify]
    trust_patterns = ["Open documentation url for more info"]
    trust_keystroke = "d_enter" # enter | d_enter | y_enter
    idle_pattern = "(Y)es/(N)o"
    working_patterns = ["(?i)esc to interrupt"]   # status line of a LIVE turn
    waiting_patterns = ["Apply this change\\?"]   # paused-for-the-human prompts
    progress_pattern = "([\\d.,]+\\s*[kmKM]?)\\s*tokens"  # climbing turn counter
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Keystroke names -> raw bytes sent to the PTY to dismiss a prompt.
KEYSTROKES: Dict[str, bytes] = {
    "enter": b"\r",
    "d_enter": b"\x44\x0d",  # 'D' then Enter (claude's "Open documentation url" path)
    "y_enter": b"\x79\x0d",  # 'y' then Enter
}

# Exit codes that mean a CLI quit cleanly (0 = normal exit, 130 = Ctrl-C / SIGINT).
DEFAULT_NATURAL_CODES: Tuple[int, ...] = (0, 130)


@dataclass
class ProviderConfig:
    name: str
    program_aliases: Tuple[str, ...]
    command: str = ""
    resume_flag: str = ""
    skip_perms_flag: str = ""
    resume_fallback: bool = True
    natural_codes: Tuple[int, ...] = DEFAULT_NATURAL_CODES
    trust_patterns: Tuple[str, ...] = ("Open documentation url for more info",)
    trust_keystroke: str = "d_enter"
    idle_pattern: str = ""
    #: Regexes visible in the pane while the CLI is RUNNING a turn (its in-flight
    #: status line, e.g. an "esc to interrupt" hint). Feeds
    #: :meth:`BaseProvider.working_pane_patterns` so the activity poller can see
    #: work even at ~0 local CPU (generation runs server-side). Empty = no signal.
    working_patterns: Tuple[str, ...] = ()
    #: Regexes shown when the CLI has PAUSED to ask the human something
    #: (permission prompt / plan approval / clarifying question). Feeds
    #: :meth:`BaseProvider.waiting_prompt_patterns` -> the UI's 'clarify' state.
    waiting_patterns: Tuple[str, ...] = ()
    #: Regex with ONE capture group holding a climbing turn counter from the
    #: CLI's status line. Feeds :meth:`BaseProvider.progress_token_pattern`.
    progress_pattern: str = ""
    #: Template appended to the base command to resume ONE specific
    #: conversation by id (``{id}`` placeholder), e.g. codex ``"resume {id}"``,
    #: antigravity ``"--conversation {id}"``. Used with a per-window recorded
    #: thread id so siblings sharing a workdir don't all resume the newest
    #: conversation. Empty = provider can't resume by id (bulk resume_flag only).
    resume_id_flag: str = ""
    #: Template that passes a session's initial prompt to the CLI at launch
    #: (``{prompt}`` placeholder, substituted with a shell-safe expression that
    #: expands to the prompt text). E.g. codex accepts a bare positional prompt
    #: (``"{prompt}"`` -> ``codex "<prompt>"``); a CLI wanting a flag would use
    #: ``"--message {prompt}"``. Empty = this provider can't be seeded a start
    #: prompt, so a session created with one launches idle (the prior behaviour).
    prompt_arg: str = ""
    #: argv tokens that run ONE prompt non-interactively and print the answer to
    #: stdout, with ``{prompt}`` standing in for the prompt text. This is how
    #: MindFlock asks a CLI a *question* — no tmux, no session, no PTY — which is
    #: what writing a commit message needs (see
    #: :mod:`backend.web.core.commit_message`).
    #:
    #: Deliberately NOT derived from ``prompt_arg``: that one seeds an
    #: *interactive* session and its flag is often the opposite of this one
    #: (antigravity's ``--prompt-interactive`` vs ``--print``), and ``-p`` means
    #: print for claude but ``--plan`` for cline. Every value below was read off
    #: the CLI's own ``--help``.
    #:
    #: These are appended to the resolved *executable*, not to ``command`` —
    #: goose's base command is ``goose session`` while its one-shot is
    #: ``goose run``. Empty = this CLI has no text-only mode we trust (aider's
    #: ``--message`` edits files and can commit on its own), so callers fall back
    #: to whatever they do without a model.
    oneshot_args: Tuple[str, ...] = ()
    #: Extra argv tokens appended to the provider's base executable every time
    #: it starts. This is for provider-local saved launch flags such as
    #: ``--dangerously-skip-permissions``. Values are shell-quoted by
    #: :meth:`launch_args_shell`; invalid TOML/user config is rejected while
    #: loading so a bad persisted provider never reaches command construction.
    launch_args: Tuple[str, ...] = ()
    #: Optional absolute path / command override for the executable. When set it
    #: is the launched binary (settings/env overrides still win — see
    #: :func:`binary_override`). Empty = use ``command``/``name`` off PATH.
    binary_path: str = ""
    # --- usage-window knowledge (roadmap E) -------------------------------- #
    #: How this CLI's usage limits reset in time — the knowledge the scheduled
    #: window-refresh and the UI use. ``kind``: ``"rolling"`` (a sliding window
    #: that anchors on first use), ``"daily"`` (per-day quota), or ``""``
    #: (unknown / pay-per-token, no MindFlock-managed window). ``hours`` = the
    #: primary window length; ``weekly_hours`` = a secondary weekly cap when the
    #: plan has one (0 = none). ``note`` = a human sentence for the UI.
    usage_window_kind: str = ""
    usage_window_hours: float = 0.0
    usage_window_weekly_hours: float = 0.0
    usage_window_note: str = ""
    #: Regexes that identify this CLI's "usage limit reached" screen (roadmap D).
    #: Empty = use :data:`usage_limits.DEFAULT_LIMIT_PATTERNS`.
    usage_limit_patterns: Tuple[str, ...] = ()
    #: Whether this provider appears in the sidebar's overall cost/usage panel
    #: by default. Only the bundled default CLIs (codex/antigravity/aider —
    #: plus Claude, which opts in on its own provider class) set this; user
    #: TOML providers can opt in with ``[usage] visible = true``.
    usage_visible: bool = False
    # --- activity-hook integration ----------------------------------------- #
    #: Repo-local settings file for a CLI whose own lifecycle hooks can report
    #: activity (Codex's ``.codex/hooks.json``; Claude wires its own directly in
    #: ``claude.py``). When set, :class:`GenericProvider` installs per-session
    #: ``{state, ts}`` marker hooks into ``<workdir>/<activity_hooks_file>``
    #: right before launch and reads them for ``activity_state`` — the same
    #: authoritative signal Claude uses, superseding the version-fragile
    #: ``working_patterns``/``waiting_patterns`` pane regexes (which stay as a
    #: fallback for older CLI builds). Empty = no hooks engine, pane inspection
    #: only (unchanged). User TOML providers opt in with ``[activity] hooks_file``
    #: plus ``[[activity.events]]`` tables.
    activity_hooks_file: str = ""
    #: ``(event, state)`` pairs installed into :attr:`activity_hooks_file`: each
    #: hook event records ``state`` (``working``/``idle``/``clarify``) when it
    #: fires. Empty = install nothing even if a file is named.
    activity_hook_events: Tuple[Tuple[str, str], ...] = ()
    # --- connection: install + login detection (Settings → Providers) ------ #
    #: Candidate credential file paths whose existence is evidence the CLI is
    #: logged in. ``~`` and ``$VAR`` are expanded; the first that exists wins.
    #: Best-effort and version-fragile by nature, so a miss is reported as
    #: "login status unknown", never as "not logged in".
    auth_files: Tuple[str, ...] = ()
    #: Environment variables whose presence indicates the CLI is authenticated
    #: (a bring-your-own API key, e.g. ``OPENAI_API_KEY``).
    auth_env: Tuple[str, ...] = ()
    #: The command to run in a terminal to log this CLI in (e.g.
    #: ``"codex login"``). Empty = the CLI has no dedicated login flow, so the
    #: one-click "Log in" terminal just runs the CLI itself (many prompt to
    #: authenticate on first launch).
    login_command: str = ""
    #: A copy-paste command that installs this CLI (e.g.
    #: ``"npm install -g @openai/codex"``). Empty = fall back to the platform
    #: package-manager hint (``brew``/``apt``) keyed on the program name.
    install_hint: str = ""

    def usage_window(self) -> dict:
        return {
            "kind": self.usage_window_kind,
            "hours": self.usage_window_hours,
            "weekly_hours": self.usage_window_weekly_hours,
            "note": self.usage_window_note,
        }

    def base_command(self) -> str:
        return self.command or self.name

    def resolved_binary(self) -> str:
        """The executable to launch, honoring overrides.

        Precedence: settings/env override (:func:`binary_override`) → this
        config's ``binary_path`` → ``base_command()`` (``command``/``name`` off
        PATH)."""
        override = binary_override(self.name)
        if override:
            return override
        if self.binary_path:
            return self.binary_path
        return self.base_command()

    def launch_args_shell(self) -> str:
        return " ".join(shlex.quote(a) for a in self.launch_args)

    def keystroke_bytes(self) -> bytes:
        return KEYSTROKES.get(self.trust_keystroke, b"\r")


def _env_bin_var(name: str) -> str:
    """The env var that overrides a provider's binary: ``MINDFLOCK_PROVIDER_BIN_<NAME>``
    (uppercased, non-alphanumerics -> ``_``)."""
    import re

    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"MINDFLOCK_PROVIDER_BIN_{slug}"


def binary_override(name: str) -> str:
    """A user-supplied binary path override for provider ``name``, or ``""``.

    Order: ``env MINDFLOCK_PROVIDER_BIN_<NAME>`` → settings
    ``coding_cli.binary_paths[name]``. Returns ``""`` when the user has set no
    override, so callers can leave the built-in launch behaviour untouched.
    """
    ev = os.environ.get(_env_bin_var(name))
    if ev:
        return ev
    try:
        from backend.config.settings import load_settings

        return load_settings().coding_cli.binary_paths.get(name, "") or ""
    except Exception:  # noqa: BLE001 — settings are optional; never break launch
        return ""


def resolve_provider_binary(name: str, cfg: Optional["ProviderConfig"] = None) -> str:
    """Resolve the executable for a provider by name (+ optional config).

    Precedence: settings/env override → the config's ``binary_path`` /
    ``base_command`` (when a config is given) → the bare ``name``. Used by the
    launch paths that don't already hold a :class:`ProviderConfig`."""
    if cfg is not None:
        return cfg.resolved_binary()
    override = binary_override(name)
    return override or name


def detect_auth(files: Tuple[str, ...], envs: Tuple[str, ...]) -> str:
    """Best-effort login evidence: a human string when some credential source
    is found, else ``""``.

    Checks ``files`` (``~`` and ``$VAR`` expanded) first — the first that exists
    wins — then ``envs`` for a set API key. Never raises: a permission error or
    a weird path just counts as "no evidence". Callers treat ``""`` as "login
    status unknown", not "definitely logged out".
    """
    for raw in files or ():
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
            if p.exists():
                return "credentials found at %s" % p
        except OSError:
            continue
    for var in envs or ():
        if os.environ.get(var):
            return "%s is set" % var
    return ""


def validate_launch_args(value) -> Tuple[str, ...]:
    """Return a normalized launch-args tuple or raise ``ValueError``.

    Provider configs are user-editable and are later interpolated into a shell
    command for tmux, so the persisted shape must be unambiguous: a list of
    non-empty argv tokens. Shell quoting happens at launch time; newlines/NULs
    are rejected because they make review/logging ambiguous and are never valid
    CLI flags.
    """
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        raise ValueError("launch args must be a list of strings")
    if not isinstance(value, (list, tuple)):
        raise ValueError("launch args must be a list of strings")
    out: List[str] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, str):
            raise ValueError(f"launch arg {i + 1} must be a string")
        arg = raw.strip()
        if not arg:
            raise ValueError(f"launch arg {i + 1} must not be empty")
        if "\x00" in arg or "\n" in arg or "\r" in arg:
            raise ValueError(f"launch arg {i + 1} contains an invalid character")
        if len(arg) > 512:
            raise ValueError(f"launch arg {i + 1} is too long")
        out.append(arg)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Bundled providers. Flags are conservative defaults; some are marked TODO
# because the upstream CLIs change them — they are pure data and a user can
# override any of them with a TOML file (no code change).
# --------------------------------------------------------------------------- #
BUILTIN_CONFIGS: List[ProviderConfig] = [
    ProviderConfig(
        name="codex",
        program_aliases=("codex",),
        # Verified against codex-cli 0.142.5: `--full-auto` is gone; the
        # skip-all-prompts flag (parity with Claude's --dangerously-skip-
        # permissions) is --dangerously-bypass-approvals-and-sandbox, and it is
        # accepted as a global option BEFORE the `resume` subcommand.
        skip_perms_flag="--dangerously-bypass-approvals-and-sandbox",
        # Resume is a subcommand, not a flag: `codex [global] resume --last`.
        # GenericProvider appends this after the (skip-perms-decorated) binary,
        # yielding e.g. `codex --dangerously-... resume --last`, with a
        # relaunch-fresh fallback when there's no prior session to resume.
        resume_flag="resume --last",
        resume_fallback=True,
        # Verified: `codex resume <SESSION_ID>` resumes one specific session.
        resume_id_flag="resume {id}",
        # Codex takes the initial prompt as a bare positional argument
        # (`codex "explain this repo"`), so a session created with a prompt
        # seeds it at launch instead of starting idle.
        prompt_arg="{prompt}",
        # `codex exec` — "Run Codex non-interactively" (from `codex --help`).
        # `--sandbox read-only` because this asks a QUESTION about the tree and
        # must not rewrite it, and `--skip-git-repo-check` because exec otherwise
        # refuses to run outside a git repo (a plain in-place session's dir).
        oneshot_args=(
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "{prompt}",
        ),
        idle_pattern="",  # unknown -> never auto-confirm spuriously
        trust_patterns=(),  # unknown -> don't auto-dismiss
        trust_keystroke="enter",
        # Activity classification, verified against the codex-cli 0.142.5 binary
        # (strings in the TUI): a live turn renders "Working (12s • Esc to
        # interrupt)" on the status header for its whole duration — the same
        # version-stable proof-of-work Claude's "esc to interrupt" hint gives.
        working_patterns=(r"(?i)esc to interrupt",),
        # Approval / question dialogs (exact strings from the 0.142.5 approval
        # widget and the request-user-input feature) — the agent has paused for
        # a human decision -> the UI's 'clarify' state.
        waiting_patterns=(
            r"Would you like to run the following command\?",
            r"Would you like to make the following edits\?",
            r"Would you like to grant these permissions\?",
            "Yes, just this once",
            "No, and tell Codex what to do differently",
            "Yes, provide the requested info",
        ),
        # Codex ships a first-class hooks engine whose config shape and payload
        # field names (`session_id`, `hook_event_name`, `cwd`) match Claude
        # Code's settings hooks exactly, discoverable per-repo at
        # `<repo>/.codex/hooks.json`. MindFlock installs the same per-session
        # {state, ts} marker hooks it uses for Claude, giving Codex an
        # authoritative activity signal that supersedes the pane regexes above
        # (kept only as a fallback for older codex builds without hooks). The
        # dedicated PermissionRequest event -> clarify is cleaner than Claude's
        # Notification path (no idle-timeout notification to filter out); Codex
        # has no Notification/SessionEnd event, so Stop covers turn/session end.
        activity_hooks_file=".codex/hooks.json",
        activity_hook_events=(
            ("Stop", "idle"),
            ("UserPromptSubmit", "working"),
            ("PreToolUse", "working"),
            ("PostToolUse", "working"),
            ("PermissionRequest", "clarify"),
        ),
        # ChatGPT-plan Codex limits: a rolling window plus a weekly/monthly cap.
        # Live window %/reset is implemented in providers/codex_usage_api.py and
        # wired via CodexProvider.usage_live(): the CLI records `rate_limits`
        # snapshots (used_percent, resets_at per window) in its session rollout
        # files under ~/.codex/sessions/**/*.jsonl with every turn — we parse the
        # newest file's last snapshot (no network). Token telemetry comes from the
        # same files' `token_count` events. The hours below are the fallback
        # window shape used only when no live snapshot exists yet.
        usage_window_kind="rolling",
        usage_window_hours=5.0,
        usage_window_weekly_hours=168.0,
        usage_window_note="ChatGPT-plan limits: a rolling usage window plus a weekly cap.",
        usage_visible=True,
        # Codex stores its ChatGPT/API auth in ~/.codex/auth.json; `codex login`
        # opens the browser OAuth flow (an OPENAI_API_KEY also authenticates it).
        auth_files=("~/.codex/auth.json",),
        auth_env=("OPENAI_API_KEY",),
        login_command="codex login",
        install_hint="npm install -g @openai/codex",
    ),
    ProviderConfig(
        name="antigravity",
        program_aliases=("agy", "antigravity"),
        command="agy",
        # Verified live against Antigravity CLI (agy) 1.0.16 — Google's
        # successor to Gemini CLI. Flags from `agy --help`: the skip-all-
        # permission-prompts flag matches Claude's spelling, and resume is
        # `--continue` (continue the most recent conversation).
        skip_perms_flag="--dangerously-skip-permissions",
        resume_flag="--continue",
        resume_fallback=True,
        # From `agy --help`: --conversation resumes a previous conversation by ID.
        resume_id_flag="--conversation {id}",
        # Seed a session's initial prompt: agy's --prompt-interactive runs the
        # prompt AND continues the interactive session (verified string-flag arity
        # against agy 1.1.3 — `flag needs an argument`). This is the interactive
        # counterpart to --print/--prompt, which are one-shot and would exit the
        # session. Parity with codex's positional {prompt} seeding.
        prompt_arg="--prompt-interactive {prompt}",
        # …and --print is that one-shot mode, which is what we want here (`agy
        # --help`: "Run a single prompt non-interactively and print the
        # response"). The two are easy to mix up — hence the separate field.
        oneshot_args=("--print", "{prompt}"),
        # The tool-permission dialog's question line ("Do you want to
        # proceed?" with a numbered Yes/always-allow/No list).
        idle_pattern="Do you want to proceed?",
        # Per-project trust gate; "Yes, I trust this folder" is preselected,
        # so a bare Enter accepts it.
        trust_patterns=("Do you trust the contents of this project?",),
        trust_keystroke="enter",
        # A live turn pins "esc to cancel" at the start of the footer line
        # (idle shows "? for shortcuts" there instead) — captured from a real
        # 1.0.16 session.
        working_patterns=(r"(?i)esc to cancel",),
        # Paused-for-the-human dialogs, captured live. Permission dialog:
        # header, question, and the always-allow option text. Clarifying
        # question: its "Question 1/1:" header and the option-menu hint line
        # ("↑/↓ Navigate · enter Select · esc Skip") — needed because the
        # footer keeps showing "esc to cancel" (a working pattern) while the
        # menu waits, which otherwise pins the pill on "running".
        waiting_patterns=(
            "Requesting permission for:",
            r"Do you want to proceed\?",
            "Yes, and always allow",
            r"Question \d+/\d+:",
            r"Navigate.+enter Select.+esc Skip",
        ),
        # Plan-based Google quota: each model group (Gemini / Claude+GPT)
        # shares a WEEKLY limit. Real numbers come from a running agy's local
        # language server via AntigravityProvider.usage_live(); the rolling
        # shape below only marks the provider as windowed (plan) so that path
        # engages — the hours value is never shown.
        usage_window_kind="rolling",
        usage_window_hours=5.0,
        usage_window_note="Plan-based Google quota: Gemini and Claude+GPT model groups each share a weekly limit (read live from a running Antigravity session).",
        usage_visible=True,
        # agy authenticates through a Google sign-in on first run; there is no
        # separate login subcommand, so the one-click terminal just runs `agy`.
        login_command="agy",
    ),
    ProviderConfig(
        name="aider",
        program_aliases=("aider",),
        skip_perms_flag="--yes-always",  # auto-confirm edits/commands
        # aider's chat-history restore is OPT-IN (--restore-chat-history default
        # off — verified against aider 0.86.2), so a bare resume would start with
        # NO prior context. Resume with the flag so an unnatural-death restart
        # replays the per-repo .aider.chat.history.md; harmless when there's no
        # history (just starts fresh), so no `|| fresh` fallback is needed.
        resume_flag="--restore-chat-history",
        resume_fallback=False,
        idle_pattern="(Y)es/(N)o/(D)on't ask again",
        trust_patterns=("Open documentation url for more info",),
        trust_keystroke="d_enter",
        # Verified against the aider 0.86.2 source: waiting.py shows a
        # "Waiting for LLM" spinner for the whole model call (streaming after
        # that keeps the pane changing, which the hash layer already sees).
        working_patterns=("Waiting for LLM",),
        # io.confirm_ask always renders " (Y)es/(N)o" (plus optional /(A)ll,
        # /(S)kip all, /(D)on't ask again suffixes) when paused on a question.
        waiting_patterns=(r"\(Y\)es/\(N\)o",),
        # Uses YOUR provider API key — the upstream model API's rate limits
        # apply; MindFlock has no plan-level window to anchor.
        usage_window_kind="",
        usage_window_note="Runs on your own API key — the model API's rate limits apply; no MindFlock-managed window.",
        usage_visible=True,
        # aider has no login flow — it reads a provider API key from the
        # environment; any of these being set means it can talk to a model.
        auth_env=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
        ),
        # No oneshot_args on purpose: aider's non-interactive `--message` is an
        # EDITING run (it applies changes and auto-commits by default), so asking
        # it to write a commit message risks it writing the commit instead.
        install_hint="python -m pip install aider-chat",
    ),
    ProviderConfig(
        name="opencode",
        program_aliases=("opencode",),
        # Verified live against opencode 1.17.15: `--auto` auto-approves
        # permissions, `--continue` resumes the last session.
        skip_perms_flag="--auto",
        resume_flag="--continue",
        resume_fallback=True,
        idle_pattern="",
        trust_patterns=(),  # opens straight into the TUI
        trust_keystroke="enter",
        # A live turn pins "esc interrupt" on the footer status line (absent
        # at an idle prompt) — captured from a real 1.17.15 session.
        working_patterns=(r"(?i)esc\s+interrupt",),
        # Permission dialog title + options, from the TUI source bundled in
        # the 1.17.15 binary.
        waiting_patterns=(
            "Permission required",
            "Allow once",
            "Allow always",
        ),
        # The footer shows a climbing context tally during a turn, e.g.
        # "10.2K (5%)" — any increase is proof of work.
        progress_pattern=r"([\d.,]+\s*[kKmM]?)\s*\(\d+%\)",
        # `opencode run [message..]` — "run opencode with a message" (non-TUI).
        oneshot_args=("run", "{prompt}"),
        usage_window_kind="",
        usage_window_note="Bring-your-own provider keys/plans; no MindFlock-managed window.",
        auth_files=("~/.local/share/opencode/auth.json",),
        login_command="opencode auth login",
        install_hint="npm install -g opencode-ai",
    ),
    ProviderConfig(
        name="cline",
        program_aliases=("cline",),
        # `cline -i` opens the interactive TUI (cline 3.0.38); the bare CLI
        # defaults to one-shot act mode. Tool auto-approve defaults to ON, so
        # there is no extra skip-permissions flag to add.
        command="cline -i",
        skip_perms_flag="",
        resume_flag="",  # resume needs --id <session-id>; relaunch fresh
        resume_fallback=False,
        idle_pattern="",
        trust_patterns=(),
        trust_keystroke="enter",
        # From the TUI source bundled in the 3.0.38 binary: the input hint
        # flips to "Esc cancels turn" while running, and the status line shows
        # "Thinking... (esc to cancel)".
        working_patterns=(
            "Esc cancels turn",
            r"Thinking\.\.\. \(esc to cancel\)",
        ),
        # Dialog titles from the same TUI source.
        waiting_patterns=(
            "Cline needs permission",
            "Cline is asking a question",
        ),
        # The bare CLI (no -i) IS the one-shot mode — a positional prompt. Note
        # `-p` here is --plan, NOT print; auto-approve is on by default, so it is
        # turned OFF for a question that must not touch the tree.
        oneshot_args=("--auto-approve", "false", "{prompt}"),
        usage_window_kind="",
        usage_window_note="Cline account or bring-your-own key; no MindFlock-managed window.",
    ),
    ProviderConfig(
        name="goose",
        program_aliases=("goose",),
        # Interactive chat is the `session` subcommand (goose 1.41.0);
        # `goose session -r` resumes the most recent session, with a fresh
        # relaunch fallback. Permission mode is configured via GOOSE_MODE, not
        # a CLI flag, so there is no skip-permissions flag.
        command="goose session",
        skip_perms_flag="",
        resume_flag="-r",
        resume_fallback=True,
        idle_pattern="",
        trust_patterns=(),
        trust_keystroke="enter",
        # The 1.41.0 binary renders "(Ctrl+C to interrupt)" next to the
        # thinking spinner for the whole live turn.
        working_patterns=(r"\(Ctrl\+C to interrupt\)",),
        # Tool-confirmation prompt strings from the same binary: "... call the
        # above tool, do you allow?" with an "Allow the tool call once" option.
        waiting_patterns=(
            r"do you allow\?",
            "Allow the tool call once",
        ),
        # `goose run -t <text>` executes one instruction and exits. NOT `goose
        # session run` — one-shot args hang off the executable, which is why they
        # are kept separate from `command`.
        oneshot_args=("run", "-t", "{prompt}"),
        usage_window_kind="",
        usage_window_note="Bring-your-own provider keys; no MindFlock-managed window.",
        # `goose configure` sets up the provider + key interactively.
        login_command="goose configure",
    ),
]


def _providers_dir() -> Path:
    """Directory scanned for user provider TOML files.

    ``MINDFLOCK_PROVIDERS_DIR`` overrides it outright; otherwise it is
    ``<assistant-dir>/providers`` (``MINDFLOCK_ASSISTANT_DIR`` or the default
    ``~/.mindflock-assistant``)."""
    env = os.environ.get("MINDFLOCK_PROVIDERS_DIR")
    if env:
        return Path(env)
    home = os.environ.get(
        "MINDFLOCK_ASSISTANT_DIR", str(Path.home() / ".mindflock-assistant")
    )
    return Path(home) / "providers"


def _config_from_toml(raw: dict) -> ProviderConfig:
    """Assemble a :class:`ProviderConfig` from a parsed provider TOML dict.

    Each TOML sub-table maps onto one facet of the config: ``[provider]``
    (identity + binary), ``[launch]`` (invocation / resume / seed flags),
    ``[exit]`` (clean-quit codes), ``[classify]`` (pane-matching regexes),
    ``[usage]`` (window knowledge + limit screens), ``[activity]`` (marker-hook
    integration), and ``[connect]`` (install + login-detection hints). Missing
    tables and keys fall back to the dataclass defaults. Raises ``KeyError``
    when ``[provider].name`` is absent and ``ValueError`` on invalid launch
    args — both caught by :func:`load_user_configs` so one bad file never
    breaks startup.
    """
    prov = raw.get("provider", {}) or {}
    launch = raw.get("launch", {}) or {}
    exit_ = raw.get("exit", {}) or {}
    classify = raw.get("classify", {}) or {}
    usage = raw.get("usage", {}) or {}
    activity = raw.get("activity", {}) or {}
    connect = raw.get("connect", {}) or {}
    program = prov.get("program") or prov.get("name")
    aliases = tuple(program) if isinstance(program, (list, tuple)) else (program,)
    return ProviderConfig(
        name=prov["name"],
        program_aliases=aliases,
        command=prov.get("command", ""),
        binary_path=prov.get("binary_path", ""),
        resume_flag=launch.get("resume_flag", ""),
        skip_perms_flag=launch.get("skip_perms_flag", ""),
        launch_args=validate_launch_args(launch.get("args", ())),
        resume_fallback=bool(launch.get("resume_fallback", True)),
        natural_codes=tuple(exit_.get("natural_codes", DEFAULT_NATURAL_CODES)),
        trust_patterns=tuple(classify.get("trust_patterns", ())),
        trust_keystroke=classify.get("trust_keystroke", "enter"),
        idle_pattern=classify.get("idle_pattern", ""),
        working_patterns=tuple(classify.get("working_patterns", ()) or ()),
        waiting_patterns=tuple(classify.get("waiting_patterns", ()) or ()),
        progress_pattern=classify.get("progress_pattern", ""),
        resume_id_flag=launch.get("resume_id_flag", ""),
        prompt_arg=launch.get("prompt_arg", ""),
        # Validated like launch args (they end up in an argv list, not a shell),
        # so a user CLI can opt into model-written commit messages from TOML.
        oneshot_args=validate_launch_args(launch.get("oneshot_args", ())),
        # [usage] window knowledge (optional): kind/hours/weekly_hours/note.
        usage_window_kind=usage.get("window_kind", ""),
        usage_window_hours=float(usage.get("window_hours", 0.0) or 0.0),
        usage_window_weekly_hours=float(usage.get("weekly_hours", 0.0) or 0.0),
        usage_window_note=usage.get("note", ""),
        usage_limit_patterns=tuple(usage.get("limit_patterns", ()) or ()),
        usage_visible=bool(usage.get("visible", False)),
        # [activity] hooks_file + [[activity.events]] (event, state) — opt-in
        # marker-hook integration for a user CLI that has its own hooks engine.
        activity_hooks_file=activity.get("hooks_file", "") or "",
        activity_hook_events=tuple(
            (str(e["event"]), str(e.get("state", "working")))
            for e in (activity.get("events", []) or [])
            if isinstance(e, dict) and e.get("event")
        ),
        # [connect] install + login-detection hints (all optional).
        auth_files=tuple(str(x) for x in (connect.get("auth_files", ()) or ())),
        auth_env=tuple(str(x) for x in (connect.get("auth_env", ()) or ())),
        login_command=str(connect.get("login_command", "") or ""),
        install_hint=str(connect.get("install_hint", "") or ""),
    )


def load_user_configs() -> List[ProviderConfig]:
    """Load user-supplied provider configs from the providers dir (best-effort)."""
    import tomllib

    out: List[ProviderConfig] = []
    d = _providers_dir()
    try:
        files = sorted(d.glob("*.toml"))
    except OSError:
        return out
    for f in files:
        try:
            raw = tomllib.loads(f.read_text(encoding="utf-8"))
            out.append(_config_from_toml(raw))
        except Exception:  # noqa: BLE001 — a bad TOML must not break startup
            continue
    return out
