"""Dependency doctor — preflight checks for a working MindFlock host.

One pure-Python module (no web deps) shared by the ``/api/doctor`` addon
(:mod:`backend.web.addons.doctor`) and the ``mindflock doctor`` CLI
(:mod:`backend.cli`), so a missing tmux/claude surfaces as an actionable
checklist instead of a cryptic ``FileNotFoundError`` at session-create time.
Optional tools (``gh``, ``uv``, ``tailscale``, ``git`` itself) are reported the
same way but can never fail the run — notably ``gh``, whose absence costs only
the PR create/merge shortcut, never a push.

Each check is independent, fast (subprocesses capped at ~5s), and never raises:
a broken probe degrades to a ``warn`` result. Remediation hints are picked per
platform via :func:`backend.osenv.os_kind` (apt for linux/WSL, brew for
macOS).

Statuses: ``ok`` (good) · ``info`` (optional dep absent) · ``warn`` (works but
needs attention) · ``fail`` (a required dependency is missing). The overall
``ok`` flag is "no fails".
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from backend import osenv

__all__ = [
    "Check",
    "CHECKS_BY_ID",
    "run_checks",
    "to_payload",
    "check_agent_cli",
    "check_agent_auth",
]

#: Cap on every subprocess probe so /api/doctor stays snappy.
_TIMEOUT_S = 5

_DOCS = {
    "gh": "https://cli.github.com",
    "tmux": "https://github.com/tmux/tmux/wiki/Installing",
    "uv": "https://docs.astral.sh/uv/getting-started/installation/",
    "tailscale": "https://tailscale.com/download",
    "claude": "https://docs.anthropic.com/en/docs/claude-code/setup",
}


@dataclass
class Check:
    """One doctor result, serialized verbatim into the ``/api/doctor`` payload."""

    id: str
    label: str
    status: str  # ok | info | warn | fail
    detail: str = ""
    fix: str = ""  # one-line, platform-appropriate remediation ("" when none)
    docs: str = ""  # optional docs URL hint ("" when none)
    cmd: str = ""  # shell command `doctor --fix` may offer to run ("" = not runnable)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Probe helpers
# --------------------------------------------------------------------------- #
def _run(argv: List[str]) -> Tuple[Optional[int], str]:
    """Run ``argv`` with a hard timeout. Returns ``(returncode, output)``;
    ``(None, "")``-ish on any failure (missing binary, timeout) — never raises."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text else ""


def _linux_pkg_manager() -> str:
    """The host's package manager: ``apt`` | ``dnf`` | ``pacman`` | ``zypper``.

    Probed once per process from what's actually on PATH, so the fix line we
    print is the one command that works on THIS machine (Debian/Ubuntu, Fedora,
    Arch, openSUSE). Falls back to ``apt`` (the most common) when none match.
    """
    for mgr in ("apt", "dnf", "pacman", "zypper"):
        if shutil.which(mgr):
            return mgr
    return "apt"


def _pkg_fix(pkg: str) -> str:
    """A one-line install hint for ``pkg`` on the current platform."""
    kind = osenv.os_kind()
    if kind == "macos":
        return f"brew install {pkg}"
    if kind in ("linux", "wsl"):
        mgr = _linux_pkg_manager()
        if mgr == "pacman":
            return f"sudo pacman -S {pkg}"
        if mgr == "zypper":
            return f"sudo zypper install {pkg}"
        return f"sudo {mgr} install {pkg}"
    return "use WSL — native Windows is not a supported MindFlock host"


def _parse_version(text: str) -> Tuple[int, ...]:
    """First ``X.Y[.Z]`` looking token in ``text`` as an int tuple; ``()`` when
    none found (never raises — a weird version string degrades to 'unknown')."""
    import re

    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
#: Minimum versions, derived from the newest flag/subcommand MindFlock uses:
#: git ``worktree remove`` needs 2.17; tmux ``send-keys -X -N <count>`` (the
#: copy-mode scroll tuning) needs 2.4.
GIT_MIN = (2, 17)
TMUX_MIN = (2, 4)


def check_git() -> Check:
    path = shutil.which("git")
    if not path:
        fix = _pkg_fix("git")
        cmd = fix
        if osenv.os_kind() == "macos":
            fix = "xcode-select --install (or: brew install git)"
            cmd = "xcode-select --install"
        # Optional: sessions run in-place in plain folders without git — only
        # the worktree/diff/commit/PR features need it.
        return Check(
            "git",
            "git",
            "info",
            "not found (optional — sessions run in plain folders; "
            "diff/commit/PR and isolated worktrees need git)",
            fix,
            cmd=cmd,
        )
    _, out = _run(["git", "--version"])
    line = _first_line(out) or path
    ver = _parse_version(out)
    if ver and ver < GIT_MIN:
        want = ".".join(map(str, GIT_MIN))
        return Check(
            "git",
            "git",
            "fail",
            f"{line} is too old — `git worktree remove` needs git ≥ {want}",
            _pkg_fix("git"),
            cmd=_pkg_fix("git"),
        )
    return Check("git", "git", "ok", line)


def check_tmux() -> Check:
    path = shutil.which("tmux")
    if not path:
        return Check(
            "tmux",
            "tmux",
            "fail",
            "not found on PATH — sessions cannot start without it",
            _pkg_fix("tmux"),
            docs=_DOCS["tmux"],
            cmd=_pkg_fix("tmux"),
        )
    _, out = _run(["tmux", "-V"])
    line = _first_line(out) or path
    ver = _parse_version(out)
    if ver and ver < TMUX_MIN:
        want = ".".join(map(str, TMUX_MIN))
        return Check(
            "tmux",
            "tmux",
            "fail",
            f"{line} is too old — MindFlock's copy-mode scroll control needs tmux ≥ {want}",
            _pkg_fix("tmux"),
            docs=_DOCS["tmux"],
            cmd=_pkg_fix("tmux"),
        )
    return Check("tmux", "tmux", "ok", line)


def check_gh() -> Check:
    path = shutil.which("gh")
    if not path:
        # Optional: MindFlock runs fine without it — only the GitHub PR features
        # (opening/merging PRs and the automated PR-review loop) need gh, and
        # even those fall back to the REST API or a prefilled browser URL.
        # Pushing never touches gh: it is plain `git push` over whatever remote
        # (SSH or HTTPS) the user already configured. Absent gh is ``info``
        # (optional dep absent), not ``fail``, so it never trips the "required
        # dependency missing" exit.
        return Check(
            "gh",
            "GitHub CLI (gh)",
            "info",
            "not found (optional — only PR create/merge and PR review need it; "
            "pushing uses plain git)",
            _pkg_fix("gh"),
            docs=_DOCS["gh"],
            cmd=_pkg_fix("gh"),
        )
    code, out = _run(["gh", "auth", "status"])
    if code == 0:
        return Check("gh", "GitHub CLI (gh)", "ok", "installed and authenticated")
    return Check(
        "gh",
        "GitHub CLI (gh)",
        "warn",
        _first_line(out) or "installed but not authenticated",
        "run `gh auth login`",
        docs=_DOCS["gh"],
        cmd="gh auth login",
    )


def _default_provider_name() -> str:
    """The configured default coding-CLI provider ("claude" unless overridden)."""
    name = ""
    try:
        from backend.config.settings import load_settings

        name = load_settings().coding_cli.default_provider
    except Exception:  # noqa: BLE001 — settings are optional
        name = ""
    if name:
        return name
    try:
        from backend import providers

        return providers.DEFAULT_PROVIDER
    except Exception:  # noqa: BLE001
        return "claude"


def _resolve_agent_binary(name: str) -> str:
    """Resolve the provider's executable honoring settings/env overrides."""
    try:
        from backend import providers
        from backend.providers import config as provider_config

        p = providers.get(name)
        cfg = getattr(p, "cfg", None) if p is not None else None
        return provider_config.resolve_provider_binary(name, cfg)
    except Exception:  # noqa: BLE001
        return name


def check_agent_cli() -> Check:
    """Check the default coding-agent CLI is available.

    An explicit binary-path override (a name containing ``os.sep``) is validated
    directly — it must be an executable file; otherwise the provider's binary
    name is resolved on ``PATH``. A missing binary is a ``fail`` with a
    platform-appropriate install fix (auto-runnable only for ``claude``, which
    ships an installer)."""
    name = _default_provider_name()
    binary = _resolve_agent_binary(name)
    label = f"agent CLI ({name})"
    if os.sep in binary:  # explicit path override — check it directly
        p = Path(binary).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return Check("agent-cli", label, "ok", str(p))
        return Check(
            "agent-cli",
            label,
            "fail",
            f"configured binary {binary} is missing or not executable",
            "fix the binary path in Settings → Coding CLI",
        )
    path = shutil.which(binary)
    if path:
        return Check("agent-cli", label, "ok", path)
    if binary == "claude":
        # Native installer needs no Node; prefer npm only when it's already there.
        fix = (
            "npm install -g @anthropic-ai/claude-code"
            if shutil.which("npm")
            else "curl -fsSL https://claude.ai/install.sh | sh"
        )
        cmd = fix
    else:
        fix = f"install `{binary}` or set a binary path in Settings → Coding CLI"
        cmd = ""
    return Check(
        "agent-cli",
        label,
        "fail",
        f"`{binary}` not found on PATH",
        fix,
        docs=_DOCS["claude"] if binary == "claude" else "",
        cmd=cmd,
    )


def _agent_provider(name: str):
    """The registered provider object for ``name``, or ``None`` when the registry
    cannot produce one (a stale provider name in settings, a user TOML that was
    deleted, an import that failed). Never raises — the auth check has to say
    something useful even when the registry is unhappy."""
    try:
        from backend import providers

        return providers.get(name)
    except Exception:  # noqa: BLE001 — a broken registry must not break the doctor
        return None


def _declares_auth_sources(provider) -> bool:
    """Whether ``provider`` has told us where its credentials could live.

    This is what separates the two meanings of "no evidence". A CLI that named
    its credential files or env vars and has none of them is probably logged
    out, which is worth a nudge; a CLI that named nothing keeps its token
    somewhere we never look, so its absence proves nothing and warning about it
    would nag on every doctor run forever (antigravity, cline, goose). Config-
    driven providers declare it as data; the hand-written ones (claude) carry no
    ``cfg``, so overriding the base no-op probe IS their declaration.
    """
    cfg = getattr(provider, "cfg", None)
    if cfg is not None:
        return bool(getattr(cfg, "auth_files", ()) or getattr(cfg, "auth_env", ()))
    try:
        from backend.providers.base import BaseProvider

        return type(provider).auth_evidence is not BaseProvider.auth_evidence
    except Exception:  # noqa: BLE001 — unknowable, so claim nothing and stay quiet
        return False


def _auth_evidence(provider) -> str:
    """The provider's own login evidence, or ``""``.

    Same doctrine as Settings → Providers (:func:`backend.web.addons.settings.
    _provider_status`): a probe that finds nothing — or blows up — means "login
    status unknown", never "logged out"."""
    try:
        return provider.auth_evidence() or ""
    except Exception:  # noqa: BLE001 — a broken probe is no evidence, not a 500
        return ""


def _declared_login_command(provider) -> str:
    """The login command ``provider`` EXPLICITLY declares, or ``""``.

    :meth:`BaseProvider.login_command` never answers nothing — with no login
    flow to name it hands back the bare program name — and taking that at face
    value made the doctor offer to run the agent itself: ``doctor --fix`` and
    the ``mindflock init`` wizard printed "agent auth (aider): run `aider`?" and
    Enter replaced the wizard with aider's own REPL in whatever directory it was
    started in, having authenticated nothing. So a bare program name only counts
    when a provider means it: config-driven providers declare it as data
    (``cfg.login_command``), and the hand-written ones declare it by overriding
    the base method — which claude does deliberately, because the ``claude`` CLI
    really does prompt to sign in on first run.
    """
    cfg = getattr(provider, "cfg", None)
    if cfg is not None:
        return str(getattr(cfg, "login_command", "") or "")
    try:
        from backend.providers.base import BaseProvider

        if type(provider).login_command is BaseProvider.login_command:
            return ""
        return provider.login_command() or ""
    except Exception:  # noqa: BLE001 — a provider must never break the doctor
        return ""


def _login_fix(provider, base: str) -> Tuple[str, str]:
    """``(fix line, runnable command)`` for a CLI that looks logged out.

    The command comes from the provider itself, so each CLI is pointed at its own
    flow instead of everyone being told to run ``claude``. One that already says
    "login" (``codex login``) reads as an instruction on its own, while a bare
    program name (``claude``) needs the "once to log in" tail to explain why you
    would run it at all. A provider that declares no flow gets a human fix line
    and no runnable command at all — naming the API keys it reads is remediation,
    dropping the user into an agent REPL is not.
    """
    cmd = _declared_login_command(provider)
    if "login" in cmd:
        return f"run `{cmd}`", cmd
    if cmd:
        return f"run `{cmd}` once to log in", cmd
    cfg = getattr(provider, "cfg", None)
    env = [str(v) for v in (getattr(cfg, "auth_env", ()) or ())]
    if env:
        keys = ", ".join(env[:3])
        return f"set one of the API keys `{base}` reads ({keys})", ""
    return f"log `{base}` in from inside the CLI itself", ""


def check_agent_auth() -> Check:
    """Auth probe for the configured coding-agent CLI, asked of the provider.

    Every provider already knows where its own credentials live
    (``auth_evidence``), so routing through the registry makes this work for
    codex/opencode/aider too. Before that, anything but claude got "no auth probe
    — skipped" and a logged-out CLI was discovered the hard way: the first
    session started and died silently.

    The three-way verdict is the whole point. Evidence is ``ok``. No evidence
    from a provider that DID tell us where to look is a ``warn`` carrying that
    provider's own login command as ``cmd`` — but only a login command it
    actually declared (see :func:`_declared_login_command`), so ``doctor --fix``
    never offers to run an agent that has no login flow. A provider that
    declares no credential sources at all is ``info``:
    there is nothing to probe, so silence is the honest report rather than a
    warning the user can never clear.
    """
    name = _default_provider_name()
    binary = _resolve_agent_binary(name)
    base = Path(binary).name
    label = f"agent auth ({name})"
    provider = _agent_provider(name)
    if provider is None:
        return Check(
            "agent-auth",
            label,
            "info",
            f"`{name}` is not a registered provider — no login probe",
        )
    evidence = _auth_evidence(provider)
    if evidence:
        return Check("agent-auth", label, "ok", evidence)
    if not _declares_auth_sources(provider):
        return Check(
            "agent-auth",
            label,
            "info",
            f"there is no login probe for `{base}` — check its status inside the CLI itself",
        )
    if not shutil.which(base) and os.sep not in binary:
        return Check(
            "agent-auth", label, "warn", "agent CLI not installed — cannot probe auth"
        )
    fix, cmd = _login_fix(provider, base)
    return Check(
        "agent-auth",
        label,
        "warn",
        "CLI is installed but no sign of a login was found",
        fix,
        docs=_DOCS["claude"] if base == "claude" else "",
        cmd=cmd,
    )


def check_uv() -> Check:
    path = shutil.which("uv")
    if not path:
        return Check(
            "uv",
            "uv",
            "warn",
            "not found on PATH (used for installs/updates)",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            docs=_DOCS["uv"],
            cmd="curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
    _, out = _run(["uv", "--version"])
    return Check("uv", "uv", "ok", _first_line(out) or path)


def check_clipboard() -> Check:
    """Copy-to-clipboard backend (optional). pyperclip silently no-ops on
    native Linux without xclip/xsel — surface that instead of letting the
    copy button be mysteriously dead. macOS (pbcopy) and WSL (clip.exe via
    interop) always have a backend."""
    if osenv.os_kind() != "linux":
        return Check("clipboard", "clipboard", "ok", "built-in backend")
    for tool in ("xclip", "xsel"):
        path = shutil.which(tool)
        if path:
            return Check("clipboard", "clipboard", "ok", path)
    return Check(
        "clipboard",
        "clipboard",
        "info",
        "no xclip/xsel found (optional — copy-to-clipboard will be a no-op)",
        _pkg_fix("xclip"),
        cmd=_pkg_fix("xclip"),
    )


def check_tailscale() -> Check:
    path = shutil.which("tailscale")
    if not path:
        fix = (
            "brew install tailscale"
            if osenv.os_kind() == "macos"
            else "curl -fsSL https://tailscale.com/install.sh | sh"
        )
        return Check(
            "tailscale",
            "tailscale",
            "info",
            "not found (optional — only needed for phone/tailnet access)",
            fix,
            docs=_DOCS["tailscale"],
            cmd=fix,
        )
    return Check("tailscale", "tailscale", "ok", path)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
#: Check id → probe, so `doctor --fix` can re-run a single check after its fix.
def check_state_schema() -> Check:
    """Report a state file this build refused to read (a downgrade).

    ``LoadState`` moves a newer-schema ``state.json`` aside and starts empty —
    nothing is lost, but every session disappears from the UI, which looks
    exactly like data loss unless we say otherwise. ``warn``, not ``fail``: it
    is not a missing dependency and must not make ``doctor`` exit 1 (the
    installer runs it) — but it is loud everywhere the doctor is shown.
    """
    from backend.config import state as state_mod

    notice = state_mod.downgrade_notice()
    if not notice:
        return Check("state-schema", "session state", "ok", "readable")
    where = notice.get("backup_path") or "(it could not be backed up)"
    return Check(
        "state-schema",
        "session state",
        "warn",
        "your state file was written by a newer MindFlock (v%s > v%s), so this "
        "build started with an empty session list. Nothing was deleted — the "
        "file is preserved at %s."
        % (notice.get("file_version"), notice.get("supported_version"), where),
        fix="upgrade MindFlock, then rename that file back to state.json to recover your sessions",
    )


def check_local_model() -> Check:
    """Check the configured local model server, when local models are enabled.

    Skipped entirely (``info``) when the feature is off — the common case — so
    this never nags a user on a hosted CLI. When it IS on, three things can be
    wrong and each has a different fix, so they are reported apart:

    * the server isn't reachable -> start it,
    * it's reachable but doesn't serve the configured model -> pull it,
    * it's fine, but the default agent has no local route -> the session would
      silently go on using its hosted API, which is the failure the privacy
      story most needs surfaced.
    """
    from backend.providers import local_models

    cfg = local_models.load_config()
    if not cfg.enabled:
        return Check("local-model", "local model", "info", "not enabled")
    if not cfg.model.strip():
        return Check(
            "local-model",
            "local model",
            "fail",
            "enabled but no model is set",
            "pick a model in Settings → Local model",
        )
    label = f"local model ({cfg.runtime})"
    result = local_models.probe(cfg)
    if not result.get("running"):
        fix = {
            "ollama": "start it with `ollama serve`",
            "lmstudio": "start LM Studio's local server (Developer → Start Server)",
        }.get(cfg.runtime, "start your OpenAI-compatible server")
        return Check("local-model", label, "fail", result.get("error", ""), fix)
    models = result.get("models") or []
    # Compare on the bare name too: servers report tags ("qwen2.5-coder:7b") and
    # a user may have configured either spelling.
    wanted = cfg.model.strip()
    served = any(m == wanted or m.split(":")[0] == wanted.split(":")[0] for m in models)
    if not served:
        listed = ", ".join(models[:5]) or "none"
        fix = (
            f"pull it with `ollama pull {wanted}`"
            if cfg.runtime == "ollama"
            else f"load {wanted} in your local server"
        )
        return Check(
            "local-model",
            label,
            "warn",
            f"{result['base_url']} is up but does not serve {wanted} (has: {listed})",
            fix,
        )
    note = local_models.unsupported_note(
        _resolve_agent_binary(_default_provider_name())
    )
    if note:
        return Check(
            "local-model",
            label,
            "warn",
            note,
            "set the session or source agent to codex, aider or goose",
        )
    return Check("local-model", label, "ok", f"{wanted} at {result['base_url']}")


CHECKS_BY_ID: dict[str, Callable[[], Check]] = {
    "git": check_git,
    "tmux": check_tmux,
    "gh": check_gh,
    "agent-cli": check_agent_cli,
    "agent-auth": check_agent_auth,
    "local-model": check_local_model,
    "uv": check_uv,
    "clipboard": check_clipboard,
    "tailscale": check_tailscale,
    "state-schema": check_state_schema,
}

_ALL_CHECKS: List[Callable[[], Check]] = list(CHECKS_BY_ID.values())


def run_checks() -> List[Check]:
    """Run every check; an individual probe blowing up becomes a ``warn`` result
    (the doctor itself must never 500)."""
    out: List[Check] = []
    for fn in _ALL_CHECKS:
        try:
            out.append(fn())
        except Exception as err:  # noqa: BLE001 — degrade, never raise
            cid = fn.__name__.removeprefix("check_").replace("_", "-")
            out.append(Check(cid, cid, "warn", f"check errored: {err}"))
    return out


def to_payload(checks: List[Check]) -> dict:
    """The wire shape served by ``GET /api/doctor``.

    Two fields ride along with the checks because this endpoint is the one
    thing every client already talks to:

    ``version``
        The running engine's version. The desktop shell compares it against its
        own to catch app/engine drift — the app only installs the engine when
        it is *absent*, so updating the app alone silently leaves an old engine
        in place. Serving it over HTTP works identically on macOS, Linux and
        Windows/WSL, unlike shelling out to ``mindflock --version``.
    ``state_notice``
        Present only after a downgrade left the session list empty
        (see :func:`backend.config.state.downgrade_notice`); drives the UI
        banner that explains where the preserved file went.
    """
    from backend import __version__
    from backend.config import state as state_mod

    return {
        "checks": [c.to_dict() for c in checks],
        "ok": all(c.status != "fail" for c in checks),
        "version": __version__,
        "state_notice": state_mod.downgrade_notice(),
    }
