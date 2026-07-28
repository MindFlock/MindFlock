"""Cross-OS terminal-tab launching for detached tmux sessions.

When the ingestion pipeline starts an agent inside a detached tmux session it
tries to pop open a terminal *attached* to that session so a human can watch /
take over. How you open a terminal is OS-specific:

* **WSL**  → ``wt.exe`` (Windows Terminal) running ``wsl.exe -d <distro> -- tmux attach``
* **Linux** → the first available of ``gnome-terminal`` / ``konsole`` /
  ``xterm`` (respecting ``$MINDFLOCK_TERMINAL`` first)
* **macOS** → ``Terminal.app`` via ``osascript``

This module only *builds the argv* — the caller runs it (so the caller keeps its
own subprocess machinery / test seams). :func:`build_terminal_tab_argv` returns
``None`` when no terminal emulator is available; the session is still fully
usable (attach manually with ``tmux attach -t <session>``), so callers degrade
to a logged no-op rather than failing.
"""

from __future__ import annotations

import os
import shutil
from typing import List, Optional

from backend import osenv

__all__ = [
    "build_terminal_tab_argv",
    "wsl_distro",
    "wsl_interop_available",
    "wt_command",
]

_WSL_DISTRO_DEFAULT = "Ubuntu"
_WT_COMMAND_DEFAULT = "wt.exe"
_BINFMT_MISC_DIR = "/proc/sys/fs/binfmt_misc"


def wsl_distro() -> str:
    """WSL distro for ``wsl.exe -d <distro>`` (env → settings → "Ubuntu")."""
    from backend.config import settings as _s

    return _s.resolve_str(
        env="MINDFLOCK_WSL_DISTRO",
        settings_getter=lambda s: s.platform.wsl_distro,
        default=_WSL_DISTRO_DEFAULT,
    )


def wt_command() -> str:
    """Windows Terminal executable (env → settings → "wt.exe", then PATH)."""
    from backend.config import settings as _s

    cmd = _s.resolve_str(
        env="MINDFLOCK_WT_COMMAND",
        settings_getter=lambda s: s.platform.wt_command,
        default=_WT_COMMAND_DEFAULT,
    )
    return shutil.which(cmd) or cmd


def wsl_interop_available() -> bool:
    """Whether this WSL instance can exec Windows binaries at all.

    WSL runs ``.exe`` files through a binfmt_misc handler that matches the PE
    ``MZ`` magic (registered as ``WSLInterop``). Tools that reset binfmt —
    Docker/qemu multi-arch setup is the usual culprit — flush that entry, after
    which *every* ``.exe`` spawn raises ``OSError(ENOEXEC)`` "Exec format
    error". Detect that up front so terminal launches degrade to a logged
    no-op instead of crashing the invoke. Restore with::

        sudo sh -c "echo ':WSLInterop:M::MZ::/init:PF' > /proc/sys/fs/binfmt_misc/register"

    or a full WSL restart (``wsl.exe --shutdown`` from Windows).
    """
    try:
        entries = os.listdir(_BINFMT_MISC_DIR)
    except OSError:
        return True  # can't tell — let the exec surface the real error
    for name in entries:
        if name in ("register", "status"):
            continue
        try:
            with open(os.path.join(_BINFMT_MISC_DIR, name)) as f:
                data = f.read()
        except OSError:
            continue
        # An enabled handler matching the PE magic (4d5a = "MZ") means .exe
        # files will exec, whatever the entry happens to be named.
        if data.startswith("enabled") and "4d5a" in data:
            return True
    return False


def _linux_terminal() -> Optional[str]:
    """First available Linux terminal emulator (``$MINDFLOCK_TERMINAL`` wins)."""
    pref = os.environ.get("MINDFLOCK_TERMINAL", "").strip()
    candidates = [pref] if pref else []
    candidates += [
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "xterm",
        "kitty",
        "alacritty",
    ]
    for c in candidates:
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def build_terminal_tab_argv(title: str, tmux_session: str) -> Optional[List[str]]:
    """Build the argv that opens a terminal attached to ``tmux_session``.

    Returns ``None`` when no terminal emulator is available on this host (the
    caller should log and continue — the tmux session is still attachable).
    """
    attach = ["tmux", "attach", "-t", tmux_session]
    kind = osenv.os_kind()

    if kind == "wsl":
        wt = wt_command()
        # wt_command() falls back to the literal "wt.exe" when Windows Terminal
        # isn't on PATH. That happens when the server runs with a stripped PATH
        # (systemd/background service) that lacks the Windows interop dirs.
        # Spawning an unresolvable literal raises FileNotFoundError and would
        # crash the whole invoke, so degrade to a no-op like the Linux branch.
        if shutil.which(wt) is None:
            return None
        # Interop can be dead even with wt.exe on PATH (binfmt entry flushed
        # by a Docker/qemu binfmt reset) — spawning any .exe then raises
        # OSError(ENOEXEC), so degrade to a no-op like the Linux branch.
        if not wsl_interop_available():
            return None
        # Byte-compatible with the historical WSL path (a new Windows Terminal
        # tab that runs `wsl.exe -d <distro> -- tmux attach -t <session>`).
        return [
            wt,
            "-w",
            "0",
            "nt",
            "--title",
            title,
            "wsl.exe",
            "-d",
            wsl_distro(),
            "--",
            *attach,
        ]

    if kind == "macos":
        # `open -na Terminal --args` can't run a command, so drive Terminal.app
        # via AppleScript: open a new window running the attach command.
        script = 'tell application "Terminal" to do script "%s"' % " ".join(attach)
        return ["osascript", "-e", script]

    if kind == "linux":
        term = _linux_terminal()
        if term is None:
            return None
        base = os.path.basename(term)
        if base == "gnome-terminal":
            return [term, "--title", title, "--", *attach]
        if base in ("konsole", "xfce4-terminal"):
            return [term, "-e", " ".join(attach)]
        if base == "kitty":
            return [term, "--title", title, *attach]
        if base == "alacritty":
            return [term, "--title", title, "-e", *attach]
        # xterm and generic fallbacks.
        return [term, "-T", title, "-e", *attach]

    # Native Windows (no tmux) or anything unknown: no terminal-tab launch.
    return None
