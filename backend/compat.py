"""What MindFlock needs from the OS that POSIX and native Windows spell
differently — one place, so no module imports ``fcntl``, ``pwd`` or
``termios`` at its top or calls ``os.killpg`` itself (docs/windows.md).

On Linux and macOS every function here is the POSIX call it names, unchanged:
the Windows branches only ever run on ``os.name == "nt"``.

* **File locks** — :func:`flock` with ``fcntl.flock``'s operations
  (:data:`LOCK_SH`, :data:`LOCK_EX`, :data:`LOCK_NB`, :data:`LOCK_UN`) and
  its errors (``BlockingIOError`` for a held non-blocking lock). Windows:
  ``LockFileEx`` / ``UnlockFileEx`` on the file's first byte (a range past
  the end is allowed), shared or exclusive like ``flock``; the lock is the
  handle's, so it goes with the file's close as ``flock``'s does.
* **Process groups** — :func:`detached_kwargs` (``subprocess.Popen``
  keywords for a child in its own group: ``start_new_session`` on POSIX,
  ``CREATE_NEW_PROCESS_GROUP`` with no console window on Windows) and
  :func:`kill_group` (``os.killpg`` on POSIX; the whole process tree on
  Windows, ``taskkill /T``, as there is no group signal there).
* **The POSIX shell** — :func:`posix_shell`: ``/bin/sh`` (or ``bash``) on
  POSIX; Git for Windows' ``bash.exe`` on Windows (never
  ``C:\\Windows\\System32\\bash.exe``, the WSL launcher), which runs the
  launch scripts, exit markers and ``sh -c`` strings unchanged.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any, Dict

__all__ = [
    "IS_WINDOWS",
    "LOCK_EX",
    "LOCK_NB",
    "LOCK_SH",
    "LOCK_UN",
    "detached_kwargs",
    "flock",
    "kill_group",
    "posix_shell",
]

IS_WINDOWS = os.name == "nt"

if not IS_WINDOWS:
    import fcntl as _fcntl

    LOCK_SH = _fcntl.LOCK_SH
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
else:  # pragma: no cover - exercised on Windows
    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8


def flock(fd: Any, op: int) -> None:
    """``fcntl.flock(fd, op)`` on any OS (``fd``: an int or anything with
    ``fileno()``)."""
    if not IS_WINDOWS:
        _fcntl.flock(fd, op)
        return
    _win_flock(fd if isinstance(fd, int) else fd.fileno(), op)  # pragma: no cover


def _win_flock(fd: int, op: int) -> None:  # pragma: no cover - Windows only
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    ov = _Overlapped()
    if op & LOCK_UN:
        kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))
        return
    flags = 0x2 if op & LOCK_EX else 0  # LOCKFILE_EXCLUSIVE_LOCK
    if op & LOCK_NB:
        flags |= 0x1  # LOCKFILE_FAIL_IMMEDIATELY
    if not kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        if err in (33, 158):  # ERROR_LOCK_VIOLATION, ERROR_NOT_LOCKED
            raise BlockingIOError(11, "the file is locked")
        raise OSError(err, ctypes.FormatError(err))


def detached_kwargs() -> Dict[str, Any]:
    """``subprocess.Popen`` keywords for a child in a process group of its
    own (so :func:`kill_group` can end it and everything it started)."""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)  # pragma: no cover
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)  # pragma: no cover
    return {"creationflags": flags}  # pragma: no cover


def kill_group(pid: int, sig: int = signal.SIGTERM) -> None:
    """End the process group led by ``pid`` (one started with
    :func:`detached_kwargs`): ``os.killpg(pid, sig)`` on POSIX, the whole
    process tree on Windows (forcibly for SIGKILL). ``ProcessLookupError``
    when it is gone, as ``os.killpg`` raises."""
    if not IS_WINDOWS:
        os.killpg(pid, sig)
        return
    args = ["taskkill", "/T", "/PID", str(pid)]  # pragma: no cover
    if sig == getattr(signal, "SIGKILL", 9):  # pragma: no cover
        args.insert(1, "/F")
    res = subprocess.run(  # pragma: no cover
        args, capture_output=True, creationflags=0x08000000, check=False
    )
    if res.returncode == 128:  # pragma: no cover - "no such process"
        raise ProcessLookupError(pid)
    if sys.platform == "win32" and res.returncode not in (0, 128):  # pragma: no cover
        if sig != getattr(signal, "SIGKILL", 9):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=0x08000000,
                check=False,
            )


def posix_shell(bash: bool = False) -> str:
    """The shell that runs MindFlock's shell strings: ``/bin/sh`` (``bash``
    for ``bash=True``, from PATH) on POSIX; on Windows Git for Windows'
    ``bash.exe`` — ``CLAUDE_CODE_GIT_BASH_PATH`` (Claude Code's own
    setting) or ``MINDFLOCK_GIT_BASH``, else next to ``git.exe`` on PATH
    (``…\\Git\\cmd\\git.exe`` → ``…\\Git\\bin\\bash.exe``), else the default
    install folders. ``FileNotFoundError`` with a sentence when there is
    none (``mindflock doctor --fix`` installs Git for Windows)."""
    if not IS_WINDOWS:
        if bash:
            import shutil

            return shutil.which("bash") or "/bin/bash"
        return "/bin/sh"
    import shutil  # pragma: no cover - Windows

    for env in ("MINDFLOCK_GIT_BASH", "CLAUDE_CODE_GIT_BASH_PATH"):  # pragma: no cover
        p = os.environ.get(env, "")
        if p and os.path.isfile(p):
            return p
    tried = []  # pragma: no cover
    git = shutil.which("git")  # pragma: no cover
    if git:  # pragma: no cover
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
        tried += [
            os.path.join(root, "bin", "bash.exe"),
            os.path.join(root, "usr", "bin", "bash.exe"),
        ]
    for base in (  # pragma: no cover
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ):
        tried.append(os.path.join(base, "Git", "bin", "bash.exe"))
    for p in tried:  # pragma: no cover
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError(  # pragma: no cover
        "Git for Windows' bash.exe was not found: install Git for Windows"
        " (mindflock doctor --fix does) or set MINDFLOCK_GIT_BASH"
    )
