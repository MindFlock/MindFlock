"""Operating-system detection — the one place MindFlock branches on platform.

MindFlock's session engine runs on tmux + Unix PTYs + ``fcntl`` locks, so it
supports Linux, macOS, and Windows-via-WSL. Native Windows (PowerShell/cmd,
no tmux) is not a supported host for the engine. This module centralises the
detection so OS-specific integrations (terminal-tab launchers, Cursor config
paths, window management) can pick the right path and degrade gracefully rather
than assuming one environment.

Usage::

    from backend import osenv
    if osenv.is_wsl():
        ...
    match osenv.os_kind():
        case "macos": ...
        case "linux": ...
"""

from __future__ import annotations

import functools
import os
import sys

__all__ = [
    "os_kind",
    "is_linux",
    "is_macos",
    "is_wsl",
    "is_windows",
    "is_unix_like",
]


@functools.lru_cache(maxsize=1)
def _detect() -> str:
    """Return one of ``"windows"`` | ``"wsl"`` | ``"macos"`` | ``"linux"``.

    Cached for the process. WSL is detected *before* Linux because a WSL guest
    reports ``sys.platform == "linux"`` but needs the Windows-interop path
    (``wt.exe`` / ``powershell.exe`` / ``/mnt/<drive>`` Cursor state).
    """
    plat = sys.platform
    if plat == "darwin":
        return "macos"
    if plat.startswith(("win", "cygwin")) or os.name == "nt":
        return "windows"
    # Linux family — distinguish a real WSL guest from native Linux.
    if _looks_like_wsl():
        return "wsl"
    return "linux"


def _looks_like_wsl() -> bool:
    """Heuristic WSL guest detection (never raises).

    Checks the WSL interop env var first (fast, set inside every WSL shell),
    then the kernel release string, which contains ``microsoft`` / ``WSL`` under
    both WSL1 and WSL2.
    """
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    for probe in ("/proc/sys/kernel/osrelease", "/proc/version"):
        try:
            with open(probe, "r", encoding="utf-8", errors="replace") as f:
                text = f.read().lower()
            if "microsoft" in text or "wsl" in text:
                return True
        except OSError:
            continue
    return False


def os_kind() -> str:
    """The host OS class: ``"linux"`` | ``"macos"`` | ``"wsl"`` | ``"windows"``."""
    return _detect()


def is_linux() -> bool:
    """True on native Linux only (WSL is reported separately)."""
    return _detect() == "linux"


def is_macos() -> bool:
    return _detect() == "macos"


def is_wsl() -> bool:
    """True inside a Windows Subsystem for Linux guest."""
    return _detect() == "wsl"


def is_windows() -> bool:
    """True on native Windows (cmd/PowerShell) — an *unsupported* engine host."""
    return _detect() == "windows"


def is_unix_like() -> bool:
    """True where tmux / PTYs / fcntl are available (Linux, macOS, WSL)."""
    return _detect() in ("linux", "macos", "wsl")
