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

Installing is one step, not one per tool: :func:`install_plan` folds every
missing dependency this host actually needs (``Check.install``) into ONE script
— a single package-manager run for everything that has a system package
(``Check.pkg``), then each tool's own installer. ``doctor --fix`` runs it after
one confirmation, and the web UI runs it in a terminal (one sudo prompt).
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
    "check_node",
    "install_plan",
]

#: Cap on every subprocess probe so /api/doctor stays snappy.
_TIMEOUT_S = 5

_DOCS = {
    "gh": "https://cli.github.com",
    "tmux": "https://github.com/tmux/tmux/wiki/Installing",
    "uv": "https://docs.astral.sh/uv/getting-started/installation/",
    "tailscale": "https://tailscale.com/download",
    "cloudflared": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
    "claude": "https://docs.anthropic.com/en/docs/claude-code/setup",
    "node": "https://nodejs.org/en/download",
    "brew": "https://brew.sh",
    "bubblewrap": "https://github.com/containers/bubblewrap",
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
    #: The system package that provides this tool on THIS host's package
    #: manager ("" = not a package install). Every missing package in the
    #: install plan goes into one ``apt``/``dnf``/``brew`` run.
    pkg: str = ""
    #: Part of the one-shot install plan: a dependency this host needs (given
    #: its settings) that is missing and that ``pkg``/``cmd`` installs. Login
    #: commands and optional extras stay out — they are offered on their own.
    install: bool = False
    #: The agent provider a row is about (agent CLI / agent auth rows), so the
    #: UI can offer that provider's own sign-in terminal ("" for everything
    #: else).
    provider: str = ""

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


def _pkg_supported() -> bool:
    """Whether :func:`_pkg_fix` names a real package manager here."""
    return osenv.os_kind() in ("macos", "linux", "wsl")


#: Where Homebrew's installer puts ``brew`` (Apple silicon, then Intel). A
#: desktop-launched engine may not have either on PATH even when it exists.
_BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")

#: Homebrew's official installer. NONINTERACTIVE skips its "Press RETURN", but
#: it then refuses to ask for a password itself (``sudo -n``), so ``sudo -v``
#: asks first — in the install terminal, where the one sudo prompt goes.
_BREW_INSTALL = (
    "sudo -v && NONINTERACTIVE=1 /bin/bash -c "
    '"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)

#: Puts a just-installed (or merely off-PATH) Homebrew on this shell's PATH.
_BREW_SHELLENV = (
    'eval "$( (/opt/homebrew/bin/brew shellenv || /usr/local/bin/brew shellenv)'
    ' 2>/dev/null)"'
)


def _brew_installed() -> bool:
    """Whether Homebrew exists on this Mac, on PATH or not."""
    return bool(shutil.which("brew")) or any(os.path.isfile(p) for p in _BREW_PATHS)


def _npm_path() -> str:
    """``npm`` on PATH, or ``""``.

    On WSL the Windows PATH is appended, so a Windows Node answers ``which npm``
    from ``/mnt/c/…`` — and installs Windows binaries a Linux engine can't run.
    That one doesn't count."""
    path = shutil.which("npm") or ""
    if path and osenv.os_kind() == "wsl" and path.startswith("/mnt/"):
        return ""
    return path


def _node_pkg() -> str:
    """The system package(s) that bring ``npm`` on this host ("" = no route)."""
    kind = osenv.os_kind()
    if kind == "macos":
        return "node"
    if kind in ("linux", "wsl"):
        # Debian/Ubuntu split npm out of nodejs; naming both is harmless on
        # the managers that don't.
        return "nodejs npm"
    return ""


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
        pkg = "git" if _pkg_supported() else ""
        if osenv.os_kind() == "macos":
            fix = "xcode-select --install (or: brew install git)"
            cmd = "xcode-select --install"
            pkg = ""
        # Optional: sessions run in-place in plain folders without git — only
        # the worktree/diff/commit/PR features need it. Still in the install
        # plan: nearly everything people use MindFlock for wants it.
        return Check(
            "git",
            "git",
            "info",
            "not found (optional — sessions run in plain folders; "
            "diff/commit/PR and isolated worktrees need git)",
            fix,
            cmd=cmd,
            pkg=pkg,
            install=bool(cmd),
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
            pkg="git" if _pkg_supported() else "",
            install=_pkg_supported(),
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
            pkg="tmux" if _pkg_supported() else "",
            install=_pkg_supported(),
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
            pkg="tmux" if _pkg_supported() else "",
            install=_pkg_supported(),
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


def _agent_install_cmd(name: str, binary: str) -> str:
    """The command that installs provider ``name``'s CLI, or ``""``.

    Asked of the provider itself (``install_hint``: each vendor's own
    installer — claude's, codex's, aider's…), so whichever agent you picked
    gets a runnable install — not just claude. A provider that names no installer gets
    none: guessing a package name for an arbitrary custom CLI would install the
    wrong thing.
    """
    provider = _agent_provider(name)
    if provider is None:
        return ""
    try:
        return provider.install_hint() or ""
    except Exception:  # noqa: BLE001 — a provider quirk must not break the doctor
        return ""


def _agent_version(name: str, binary: str) -> str:
    """The CLI's own version line (``"2.1.295 (Claude Code)"``), or ``""``.

    Asked with the provider's declared ``version_args`` only — a CLI that
    declares none is never run — and only an answer that exits 0 and carries
    a version number counts, so a CLI that ignores the flag and starts its UI
    (killed by the probe timeout) reports nothing rather than garbage."""
    provider = _agent_provider(name)
    try:
        args = tuple(provider.version_args()) if provider is not None else ()
    except Exception:  # noqa: BLE001 — a provider quirk must not break the doctor
        args = ()
    if not args:
        return ""
    code, out = _run([binary, *args])
    line = _first_line(out)
    if code != 0 or not _parse_version(line):
        return ""
    return line[:80]


def _with_version(name: str, binary: str) -> str:
    ver = _agent_version(name, binary)
    return f"{binary} — {ver}" if ver else binary


def _agent_cli_check(cid: str, name: str, role: str, missing: str) -> Check:
    """One agent CLI's install check (``role`` names it in the label).

    An explicit binary-path override (a name containing ``os.sep``) is validated
    directly — it must be an executable file; otherwise the provider's binary
    name is resolved on ``PATH``. ``missing`` is the status when it isn't
    there."""
    binary = _resolve_agent_binary(name)
    label = f"{role} ({name})"
    if os.sep in binary:  # explicit path override — check it directly
        p = Path(binary).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return Check(cid, label, "ok", _with_version(name, str(p)), provider=name)
        return Check(
            cid,
            label,
            missing,
            f"configured binary {binary} is missing or not executable",
            "fix the binary path in Settings → Coding CLI",
            provider=name,
        )
    path = shutil.which(binary)
    if path:
        return Check(cid, label, "ok", _with_version(name, path), provider=name)
    cmd = _agent_install_cmd(name, binary)
    fix = cmd or f"install `{binary}` or set a binary path in Settings → Coding CLI"
    return Check(
        cid,
        label,
        missing,
        f"`{binary}` not found on PATH",
        fix,
        docs=_DOCS["claude"] if binary == "claude" else "",
        cmd=cmd,
        install=bool(cmd),
        provider=name,
    )


def check_agent_cli() -> Check:
    """Check the default coding-agent CLI is available — whichever provider is
    the default (Settings → Coding CLI), with that provider's own installer as
    the fix. A missing binary is a ``fail``: sessions launch it."""
    return _agent_cli_check("agent-cli", _default_provider_name(), "agent CLI", "fail")


def _assistant_provider_name() -> str:
    """Settings → Coding CLI's Assistant provider, or ``""`` when unset."""
    try:
        from backend.config.settings import load_settings

        return load_settings().coding_cli.assistant_provider or ""
    except Exception:  # noqa: BLE001 — settings are optional
        return ""


def check_assistant_cli() -> Optional[Check]:
    """The Assistant's CLI, when it is set to a different provider than the
    default (``None`` otherwise — the default's own check already covers it).
    ``warn``, not ``fail``: only the Assistant needs it."""
    name = _assistant_provider_name()
    if not name or name == _default_provider_name():
        return None
    return _agent_cli_check("assistant-cli", name, "assistant CLI", "warn")


def check_synced_agents() -> List[Check]:
    """Agent CLIs your other devices' synced settings name (the default
    agent, PR review's…) that aren't installed here. Settings sync holds
    such a value back until the CLI exists (``settings_sync.DEFER_PATHS``);
    listing them as ``install`` rows puts them in the one-shot install plan,
    so Settings → Devices can install them with one click and the next sync
    applies the setting. Empty outside a server (only it syncs)."""
    try:
        from backend.web.core import settings_sync

        names = sorted(settings_sync.deferred_providers())
    except Exception:  # noqa: BLE001 — no web extras / no sync state
        return []
    covered = {_default_provider_name(), _assistant_provider_name()}
    return [
        _agent_cli_check(f"synced-{n}-cli", n, "synced agent CLI", "warn")
        for n in names
        if n and n not in covered
    ]


def check_git_identity() -> Optional[Check]:
    """git's ``user.name``/``user.email`` (``warn`` when either is unset).
    Without them the first commit on a fresh machine or WSL distro fails with
    "Please tell me who you are". ``None`` without git (its own row says so)."""
    if not shutil.which("git"):
        return None
    _, name = _run(["git", "config", "--get", "user.name"])
    _, email = _run(["git", "config", "--get", "user.email"])
    name, email = _first_line(name), _first_line(email)
    if name and email:
        return Check("git-identity", "git identity", "ok", f"{name} <{email}>")
    unset = " and ".join(
        k for k, v in (("user.name", name), ("user.email", email)) if not v
    )
    return Check(
        "git-identity",
        "git identity",
        "warn",
        f"{unset} not set — commits will fail on this computer",
        "Setup → Connect GitHub fills it in from your account, or: "
        'git config --global user.name "Your Name" && '
        "git config --global user.email you@example.com",
    )


def _npm_install_wanted(name: str) -> bool:
    """Whether agent ``name`` is missing AND its installer is an npm one."""
    import re

    binary = _resolve_agent_binary(name)
    if os.sep in binary or shutil.which(binary):
        return False
    cmd = _agent_install_cmd(name, binary)
    return bool(re.search(r"(?:^|[;&|(]\s*)npm\s", cmd))


def check_node() -> Optional[Check]:
    """Node.js, but only as a means: when an agent CLI this host needs is
    missing and npm is the only way its vendor ships it (cline). ``None`` —
    no row at all — otherwise; nobody else needs Node.

    Its package joins the install plan's one package-manager run, which goes
    before every installer, so the npm step that follows finds npm."""
    names = [_default_provider_name(), _assistant_provider_name()]
    wanting = [n for n in dict.fromkeys(names) if n and _npm_install_wanted(n)]
    if not wanting:
        return None
    who = ", ".join(wanting)
    path = _npm_path()
    if path:
        return Check("node", "Node.js (npm)", "ok", f"{path} (installs {who})")
    pkg = _node_pkg()
    detail = f"npm not found — {who} is installed with npm"
    if shutil.which("npm"):  # only the Windows one, through WSL interop
        detail += " (the Windows npm under /mnt/ can't install Linux tools)"
    return Check(
        "node",
        "Node.js (npm)",
        "warn",
        detail,
        _pkg_fix(pkg) if pkg else "install Node.js from nodejs.org",
        docs=_DOCS["node"],
        cmd=_pkg_fix(pkg) if pkg else "",
        pkg=pkg,
        install=bool(pkg),
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
        return Check("agent-auth", label, "ok", evidence, provider=name)
    if not _declares_auth_sources(provider):
        return Check(
            "agent-auth",
            label,
            "info",
            f"there is no login probe for `{base}` — check its status inside the CLI itself",
            provider=name,
        )
    if not shutil.which(base) and os.sep not in binary:
        return Check(
            "agent-auth",
            label,
            "warn",
            "agent CLI not installed — cannot probe auth",
            provider=name,
        )
    fix, cmd = _login_fix(provider, base)
    # `provider` is what the UI's "Sign in to <agent>" button opens the login
    # terminal for; `cmd` (a DECLARED login flow) is what makes it offer one.
    return Check(
        "agent-auth",
        label,
        "warn",
        "CLI is installed but no sign of a login was found",
        fix,
        docs=_DOCS["claude"] if base == "claude" else "",
        cmd=cmd,
        provider=name,
    )


def _uv_install_cmd() -> str:
    """Download → verify → run the SAME pinned uv installer ``install.sh``
    uses (:mod:`backend._pins`), never an unpinned ``curl | sh``. A checksum
    mismatch refuses to run it."""
    from backend import _pins

    url = f"https://astral.sh/uv/{_pins.UV_PINNED_VERSION}/install.sh"
    return (
        f'T="$(mktemp)" && curl -LsSf -o "$T" {url}'
        ' && S="$( (sha256sum "$T" 2>/dev/null || shasum -a 256 "$T") | cut -d" " -f1)"'
        f' && if [ "$S" = "{_pins.UV_INSTALLER_SHA256}" ]; then sh "$T";'
        ' else echo "uv installer checksum mismatch — not running it" >&2; false; fi;'
        ' r=$?; rm -f "$T"; [ "$r" = 0 ]'
    )


def check_uv() -> Check:
    path = shutil.which("uv")
    if not path:
        from backend import _pins

        return Check(
            "uv",
            "uv",
            "warn",
            "not found on PATH (used for installs/updates)",
            f"install uv {_pins.UV_PINNED_VERSION} (Astral's installer, "
            "pinned and sha256-verified)",
            docs=_DOCS["uv"],
            cmd=_uv_install_cmd(),
            install=True,
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


def _tailscale_wanted() -> bool:
    """Whether this host uses something that needs Tailscale: tailscale serve
    mode, the shared phone link, or "Your devices". Then a missing Tailscale
    is a ``warn`` in the install plan instead of an optional ``info``."""
    try:
        from backend.config.settings import load_settings

        g = load_settings().general
        if (g.serve_mode or "") == "tailscale" or (g.shared_link or ""):
            return True
    except Exception:  # noqa: BLE001 — settings are optional
        pass
    try:
        from backend.web.core import fleet as _fleet

        return bool(_fleet.in_fleet())
    except Exception:  # noqa: BLE001
        return False


def check_tailscale() -> Check:
    """Tailscale, from :func:`backend.tailscale_cli.health`: found where (PATH,
    the macOS app bundle, or only as Windows' ``tailscale.exe`` from WSL),
    signed in or not, key expiry. Present-but-broken is a ``warn`` with the
    one next step, not a ✓ on presence alone."""
    from backend import tailscale_cli

    h = tailscale_cli.health()
    issues = h["issues"]
    if not h["installed"]:
        issue = issues[0] if issues else {}
        if issue.get("id") == "wsl_windows_only":
            # A second Tailscale node inside WSL is the user's call (it is
            # its own device in the admin console): offered, never in the
            # one-shot install plan.
            return Check(
                "tailscale",
                "tailscale",
                "warn",
                issue["message"],
                issue.get("fix", ""),
                docs=issue.get("docs", ""),
                cmd=issue.get("fix", ""),
            )
        wanted = _tailscale_wanted()
        fix = issue.get("fix") or tailscale_cli.LINUX_INSTALL
        if osenv.os_kind() == "macos":
            # The GUI app (cask `tailscale-app`, or the Standalone download) is
            # what Tailscale recommends on a Mac; formula `tailscale` is the
            # headless daemon.
            hint = "%s (or the Standalone app from %s)" % (
                fix,
                tailscale_cli.DOWNLOAD_MAC,
            )
        else:
            hint = fix
        return Check(
            "tailscale",
            "tailscale",
            "warn" if wanted else "info",
            (
                "not found (needed for phone access and Your devices)"
                if wanted
                else "not found (optional — only needed for phone/tailnet access)"
            ),
            hint,
            docs=issue.get("docs") or _DOCS["tailscale"],
            cmd=fix,
            install=wanted,
        )
    where = h["path"] + (" (Tailscale app)" if h["kind"] == "app-bundle" else "")
    problem = next((i for i in issues if i["level"] in ("fail", "warn")), None)
    if problem:
        return Check(
            "tailscale",
            "tailscale",
            problem["level"],
            problem["message"],
            problem.get("fix", ""),
            docs=problem.get("docs", ""),
        )
    who = " · ".join(
        x for x in (h["tailnet"], h["device"]["dns"] or h["device"]["name"]) if x
    )
    return Check("tailscale", "tailscale", "ok", where + (" — " + who if who else ""))


def _peer_settings() -> dict:
    """``peer.*`` as the engine reads it (``{}`` when settings can't load)."""
    try:
        from backend.config.settings import load_settings

        return load_settings().peer.effective() or {}
    except Exception:  # noqa: BLE001 — settings are optional
        return {}


def check_bwrap() -> Check:
    """bubblewrap, which every peer shared session runs inside.

    Wanted (``warn`` + in the install plan) only when peer links are on; on a
    host that doesn't use them it is ``info`` and never installed. A ``bwrap``
    that is present but fails the sandbox's own self-test (user namespaces
    switched off, an AppArmor restriction) is reported with that reason —
    reinstalling would not fix it, so there is no install command for it."""
    label = "peer sandbox (bubblewrap)"
    wanted = bool(_peer_settings().get("enabled"))
    if osenv.os_kind() not in ("linux", "wsl"):
        return Check(
            "bwrap",
            label,
            "info",
            "peer shared sessions need Linux (bubblewrap is Linux-only)",
        )
    try:
        from backend.peer import sandbox
    except Exception as err:  # noqa: BLE001
        return Check("bwrap", label, "warn", f"sandbox module failed to load: {err}")
    ok, why = sandbox.available()
    if ok:
        return Check("bwrap", label, "ok", why)
    status = "warn" if wanted else "info"
    if sandbox.find_bwrap():
        return Check(
            "bwrap",
            label,
            status,
            why,
            "bubblewrap is installed but can't create a sandbox here — "
            "unprivileged user namespaces must be allowed",
            docs=_DOCS["bubblewrap"],
        )
    fix = _pkg_fix("bubblewrap")
    return Check(
        "bwrap",
        label,
        status,
        (
            "not found, but peer links are on — shared sessions can't start"
            if wanted
            else "not found (optional — only for peer shared sessions)"
        ),
        fix,
        docs=_DOCS["bubblewrap"],
        cmd=fix,
        pkg="bubblewrap",
        install=wanted,
    )


#: ``platform.machine()`` → the arch suffix on cloudflared's release assets.
_CLOUDFLARED_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "i686": "386",
    "i386": "386",
}


def _cloudflared_install() -> Tuple[str, str]:
    """``(command, package)`` that installs cloudflared on this host.

    Through the package manager where it carries cloudflared (Homebrew, Arch),
    otherwise Cloudflare's own signed ``.deb``/``.rpm`` from its GitHub
    releases — the route Cloudflare's docs give for Debian/Ubuntu/Fedora, whose
    distro repos don't have it. ``("", "")`` when there is no route here."""
    import platform

    kind = osenv.os_kind()
    if kind == "macos":
        return "brew install cloudflared", "cloudflared"
    if kind not in ("linux", "wsl"):
        return "", ""
    mgr = _linux_pkg_manager()
    if mgr == "pacman":
        return "sudo pacman -S cloudflared", "cloudflared"
    arch = _CLOUDFLARED_ARCH.get(platform.machine().lower())
    if not arch:
        return "", ""
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    if mgr == "apt":
        deb = "/tmp/cloudflared.deb"
        return (
            f"curl -fsSL -o {deb} {base}cloudflared-linux-{arch}.deb"
            f" && sudo dpkg -i {deb} && rm -f {deb}"
        ), ""
    rpm_arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    return f"sudo rpm -Uvh {base}cloudflared-linux-{rpm_arch}.rpm", ""


def check_cloudflared() -> Check:
    """Peer links across networks run a locally installed ``cloudflared``.
    Wanted — and in the install plan — when ``peer.relay`` is ``cloudflare``,
    or ``auto`` (the default) with peer links on: that is what lets an invite
    reach someone on another network."""
    import os

    from backend.peer.tunnel import find_cloudflared

    path = find_cloudflared(os.environ.get("MINDFLOCK_CLOUDFLARED", ""))
    if path:
        return Check("cloudflared", "cloudflared", "ok", path)
    peer = _peer_settings()
    relay = peer.get("relay")
    wanted = relay == "cloudflare" or (relay == "auto" and bool(peer.get("enabled")))
    cmd, pkg = _cloudflared_install()
    if relay == "cloudflare":
        detail = "not found, but peer.relay is 'cloudflare' — invites will fail"
    elif wanted:
        detail = (
            "not found — your peer-link invites only reach people on your own "
            "network or tailnet until it's installed"
        )
    else:
        detail = "not found (optional — only for peer links across networks)"
    return Check(
        "cloudflared",
        "cloudflared",
        "warn" if wanted else "info",
        detail,
        cmd or "install cloudflared from Cloudflare's downloads page",
        docs=_DOCS["cloudflared"],
        cmd=cmd,
        pkg=pkg,
        install=wanted and bool(cmd),
    )


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


#: A seed this many refresh intervals old means the refresher is not publishing.
_SEED_STALE_INTERVALS = 3
#: Floor for the staleness window, so a short interval doesn't warn on a single
#: slow refresh (a full-suite testmon rebuild can take most of an hour).
_SEED_STALE_FLOOR_S = 6 * 3600


def _human_age(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def _refresher_lag(directory: Path, branch: str) -> Optional[int]:
    """Commits the refresher checkout is behind its (already fetched) remote
    branch, or None when that can't be read. Local refs only — no fetch — so
    the doctor stays fast; the refresher's own fetch runs even on a cycle that
    then fails, so ``origin/<branch>`` is current exactly when it matters."""
    if not (directory / ".git").exists():
        return None
    rc, out = _run(
        ["git", "-C", str(directory), "rev-list", "--count", f"HEAD..origin/{branch}"]
    )
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def check_cache_seeds() -> Check:
    """Warn when a warm cache seed (e.g. testmon's) has stopped refreshing.

    Every provisioned workspace is seeded from ``seed_path``; the background
    refresher rebuilds it from ``refresh_branch`` every interval. When it stops
    publishing — a failing cycle only logs, hourly, to the ingestion log — each
    new workspace starts from an ever-older seed, and for testmon that means
    the FULL suite on its first commit (a changed package list, or a month of
    base-branch drift, invalidates everything). This went unnoticed for a month
    once, hence the check. ``warn``, never ``fail``: tests still run, just slow.
    """
    import time

    from backend.session import provisioned
    from backend.workspace_setup import refresher_dirname

    settings = provisioned.load_provision_settings()
    caches = [
        c
        for c in (settings.caches if settings else [])
        if c.refresh_enabled and c.refresh_command
    ]
    if not caches:
        return Check("cache-seeds", "warm cache seeds", "info", "none configured")

    problems: List[str] = []
    healthy: List[str] = []
    for cache in caches:
        directory = settings.workspace_dir / refresher_dirname(cache.name)
        lag = _refresher_lag(directory, cache.refresh_branch)
        lag_note = (
            f"; refresher is {lag} commits behind origin/{cache.refresh_branch}"
            if lag
            else ""
        )
        if not cache.seed_path.is_file():
            problems.append(
                f"'{cache.name}' has no seed at {cache.seed_path}{lag_note}"
            )
            continue
        age = time.time() - cache.seed_path.stat().st_mtime
        limit = max(
            _SEED_STALE_INTERVALS * cache.refresh_interval_seconds, _SEED_STALE_FLOOR_S
        )
        if age > limit:
            problems.append(
                f"'{cache.name}' seed is {_human_age(age)} old "
                f"(refreshes every {_human_age(cache.refresh_interval_seconds)}){lag_note}"
            )
        else:
            healthy.append(f"'{cache.name}' {_human_age(age)} old")

    if problems:
        return Check(
            "cache-seeds",
            "warm cache seeds",
            "warn",
            "; ".join(problems)
            + " — new workspaces start cold (testmon re-runs the full suite)",
            fix=(
                "the refresher runs inside ticket ingestion — make sure it is on, "
                "then `grep cache_refresher logs/ticket-ingestion.log | tail` for "
                "the failing step"
            ),
        )
    return Check("cache-seeds", "warm cache seeds", "ok", ", ".join(healthy))


#: A probe may answer ``None`` — "doesn't apply on this host" (the Assistant
#: CLI check when the Assistant uses the default provider) — and is then left
#: out of the report entirely; or a list (one row per synced agent CLI).
CHECKS_BY_ID: dict[str, Callable[[], "Optional[Check] | List[Check]"]] = {
    "git": check_git,
    "git-identity": check_git_identity,
    "tmux": check_tmux,
    "gh": check_gh,
    "agent-cli": check_agent_cli,
    "assistant-cli": check_assistant_cli,
    "node": check_node,
    "agent-auth": check_agent_auth,
    "synced-agents": check_synced_agents,
    "local-model": check_local_model,
    "uv": check_uv,
    "clipboard": check_clipboard,
    "tailscale": check_tailscale,
    "bwrap": check_bwrap,
    "cloudflared": check_cloudflared,
    "state-schema": check_state_schema,
    "cache-seeds": check_cache_seeds,
}

_ALL_CHECKS: List[Callable[[], "Optional[Check] | List[Check]"]] = list(
    CHECKS_BY_ID.values()
)


def run_checks() -> List[Check]:
    """Run every check; an individual probe blowing up becomes a ``warn`` result
    (the doctor itself must never 500)."""
    out: List[Check] = []
    for fn in _ALL_CHECKS:
        try:
            check = fn()
        except Exception as err:  # noqa: BLE001 — degrade, never raise
            cid = fn.__name__.removeprefix("check_").replace("_", "-")
            check = Check(cid, cid, "warn", f"check errored: {err}")
        if isinstance(check, list):
            out.extend(check)
        elif check is not None:
            out.append(check)
    return out


def _pkg_install_line(pkgs: List[str]) -> str:
    """ONE command installing every package in ``pkgs`` with this host's package
    manager (non-interactive: the user already said yes to the whole plan)."""
    names = " ".join(pkgs)
    if osenv.os_kind() == "macos":
        if shutil.which("brew"):
            return f"brew install {names}"
        # Homebrew installed earlier in this same script, or present but off
        # this process's PATH (a desktop-launched engine): put it on PATH first.
        return f"{_BREW_SHELLENV}; brew install {names}"
    mgr = _linux_pkg_manager()
    if mgr == "pacman":
        return f"sudo pacman -S --needed --noconfirm {names}"
    if mgr == "zypper":
        return f"sudo zypper --non-interactive install {names}"
    if mgr == "dnf":
        return f"sudo dnf install -y {names}"
    # `;` not `&&`: one broken third-party source makes `update` exit non-zero
    # while the distro's own lists refreshed fine, and the install still works.
    return f"sudo apt-get update; sudo apt-get install -y {names}"


#: Prepended to every install script. Running as root (a container, a fresh
#: VPS) there may be no sudo at all, so it becomes a pass-through.
_SCRIPT_HEAD = """#!/bin/sh
# MindFlock: install everything this machine is missing, in one go.
if [ "$(id -u)" = 0 ] && ! command -v sudo >/dev/null 2>&1; then
  sudo() { "$@"; }
fi
failed=""
"""


def install_plan(checks: List[Check]) -> dict:
    """Everything missing that this host needs, as ONE runnable script.

    Every ``install`` check that has a system package joins a single
    package-manager run (one sudo prompt, one ``apt-get update``); the rest run
    their own installers after it, each in turn, so one failure doesn't stop
    the others. The script ends by saying what failed, and exits non-zero if
    anything did. On a Mac without Homebrew, installing Homebrew is the first
    step whenever there is a package to install.

    Returns ``{"steps": [{"id", "label", "cmd"}], "packages": [...],
    "script": str}`` — ``steps`` empty (and ``script`` ``""``) when there is
    nothing to install.
    """
    wanted = [c for c in checks if c.install and c.status != "ok" and (c.pkg or c.cmd)]
    pkgs: List[str] = []
    steps: List[dict] = []
    pkg_labels: List[str] = []
    for c in wanted:
        if c.pkg:
            if c.pkg not in pkgs:
                pkgs.append(c.pkg)
            pkg_labels.append(c.label)
    if pkgs and osenv.os_kind() == "macos" and not _brew_installed():
        # A fresh Mac has no Homebrew, and every package below is a `brew
        # install` — so Homebrew itself goes first (it asks for the password
        # once, in the install terminal).
        steps.append(
            {
                "id": "homebrew",
                "label": "Homebrew (the macOS package manager)",
                "cmd": _BREW_INSTALL,
            }
        )
    if pkgs:
        steps.append(
            {
                "id": "packages",
                "label": "system packages: " + ", ".join(pkgs),
                "cmd": _pkg_install_line(pkgs),
            }
        )
    for c in wanted:
        if not c.pkg:
            steps.append({"id": c.id, "label": c.label, "cmd": c.cmd})
    if not steps:
        return {"steps": [], "packages": [], "script": ""}
    import shlex

    body = [_SCRIPT_HEAD]
    for st in steps:
        label = shlex.quote(st["label"])
        body.append(
            f"echo; echo '==> '{label}\n"
            f"( {st['cmd']} ) || failed=\"$failed\n  - \"{label}\n"
        )
    body.append(
        "echo\n"
        'if [ -n "$failed" ]; then\n'
        '  printf "[mindflock] these did not install:%b\\n" "$failed"\n'
        "  exit 1\n"
        "fi\n"
        "echo '[mindflock] everything installed. Open a new terminal if a tool "
        "is still not found (PATH).'\n"
    )
    return {"steps": steps, "packages": pkgs, "script": "".join(body)}


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

    plan = install_plan(checks)
    return {
        "checks": [c.to_dict() for c in checks],
        "ok": all(c.status != "fail" for c in checks),
        # What "Install everything missing" would run — the steps only; the
        # script itself is rebuilt server-side when the terminal opens, so a
        # client can never hand the server a command to run.
        "install": {"steps": plan["steps"], "packages": plan["packages"]},
        "version": __version__,
        "state_notice": state_mod.downgrade_notice(),
    }
