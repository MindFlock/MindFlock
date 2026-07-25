"""Dependency doctor — preflight checks for a working MindFlock host.

One pure-Python module (no web deps) shared by the ``/api/doctor`` addon
(:mod:`backend.web.addons.doctor`) and the ``mindflock doctor`` CLI
(:mod:`backend.cli`), so a missing tmux/claude/gh surfaces as an actionable
checklist instead of a cryptic ``FileNotFoundError`` at session-create or push
time.

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
        # Optional: MindFlock runs fine without it — only the GitHub features
        # (push/open PRs and the automated PR-review loop) need gh. Absent gh is
        # ``info`` (optional dep absent), not ``fail``, so it never trips the
        # "required dependency missing" exit; those features simply stay off.
        return Check(
            "gh",
            "GitHub CLI (gh)",
            "info",
            "not found (optional — only GitHub push/PR and PR review need it)",
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


def _claude_login_evidence() -> str:
    """Best-effort probe for a Claude Code login. Returns a human detail string
    when some credential source is found, else ``""``. Never raises."""
    candidates: List[Path] = []
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
                return f"login state found in {path}"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY is set"
    return ""


def check_agent_auth() -> Check:
    """Auth probe for the agent CLI (implemented for the claude family)."""
    name = _default_provider_name()
    binary = _resolve_agent_binary(name)
    base = Path(binary).name
    label = f"agent auth ({name})"
    if base != "claude" and name != "claude":
        return Check(
            "agent-auth", label, "info", f"no auth probe for `{name}` — skipped"
        )
    evidence = _claude_login_evidence()
    if evidence:
        return Check("agent-auth", label, "ok", evidence)
    if not shutil.which(base) and os.sep not in binary:
        return Check(
            "agent-auth", label, "warn", "agent CLI not installed — cannot probe auth"
        )
    return Check(
        "agent-auth",
        label,
        "warn",
        "CLI is installed but no sign of a login was found",
        "run `claude` once to log in",
        docs=_DOCS["claude"],
        cmd="claude",
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
CHECKS_BY_ID: dict[str, Callable[[], Check]] = {
    "git": check_git,
    "tmux": check_tmux,
    "gh": check_gh,
    "agent-cli": check_agent_cli,
    "agent-auth": check_agent_auth,
    "uv": check_uv,
    "clipboard": check_clipboard,
    "tailscale": check_tailscale,
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
    """The wire shape served by ``GET /api/doctor``."""
    return {
        "checks": [c.to_dict() for c in checks],
        "ok": all(c.status != "fail" for c in checks),
    }
