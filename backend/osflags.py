"""The small OS differences ``backend.compat`` does not cover, for modules
that must stay stdlib-light (docs/windows.md).

Stdlib only and imports nothing from ``backend`` (``slot``, ``mcp`` and
``peer`` use it on hot paths). On Linux and macOS every name here is the
POSIX value or call it stands for, unchanged.

* ``os.open`` flags that do not exist on Windows (:data:`O_NOFOLLOW`,
  :data:`O_CLOEXEC`, :data:`O_NONBLOCK`, :data:`O_DIRECTORY`,
  :data:`O_NOCTTY`): ``0`` there. Windows fds are not inherited by default
  (``O_CLOEXEC``'s job) and there is no ``O_NOFOLLOW``: the symlink checks
  that matter are made with ``lstat`` by the callers that need them.
* :func:`pid_alive` — ``os.kill(pid, 0)`` is a liveness probe on POSIX but
  **terminates** the process on Windows (``os.kill`` there is
  ``TerminateProcess`` for any signal but the two console events), so no
  module may call it off POSIX.
* :func:`user_name` / :func:`home_dir` without ``pwd``.
* :func:`tmux_run` — ``subprocess.run`` of a tmux argv on POSIX (as it
  always was, without importing the session engine); the MindFlock session
  host through :func:`backend.session.mux.run` on Windows.
  :func:`tmux_exec` is its ``asyncio.create_subprocess_exec`` twin.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence

__all__ = [
    "IS_WINDOWS",
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOCTTY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
    "home_dir",
    "pid_alive",
    "tmux_exec",
    "tmux_run",
    "user_name",
]

IS_WINDOWS = os.name == "nt"

O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
O_NOCTTY: int = getattr(os, "O_NOCTTY", 0)


def pid_alive(pid: Any) -> bool:
    """Whether process ``pid`` exists (an alive-but-not-ours process counts).
    Never signals anything on Windows."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a dependency
        psutil = None
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001
            return False
    import ctypes  # pragma: no cover - Windows without psutil

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def user_name() -> str:
    """The login name: ``pwd`` on POSIX (as before), ``getpass`` elsewhere."""
    if not IS_WINDOWS:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    import getpass

    return getpass.getuser()


def home_dir() -> str:
    """The account's real home (not ``$HOME``): the passwd entry on POSIX,
    ``%USERPROFILE%`` on Windows."""
    if not IS_WINDOWS:
        import pwd

        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    return os.path.realpath(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def tmux_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of a tmux argv (``argv[0] == "tmux"``) on POSIX;
    :func:`backend.session.mux.run` — the session host — on Windows."""
    if not IS_WINDOWS:
        return subprocess.run(list(argv), **kwargs)
    from backend.session import mux

    return mux.run(argv, **kwargs)


class _Finished:
    """An ``asyncio.subprocess.Process``-shaped result of a tmux command the
    session host already ran (``communicate``, ``wait``, ``returncode``)."""

    def __init__(self, cp: subprocess.CompletedProcess) -> None:
        self.returncode = cp.returncode
        self._out = (cp.stdout, cp.stderr)
        self.pid = None

    async def communicate(self, input=None):  # noqa: A002 - asyncio's name
        return self._out

    async def wait(self) -> int:
        return self.returncode


async def tmux_exec(*argv: str, **kwargs: Any) -> Any:
    """``await asyncio.create_subprocess_exec(*argv, **kwargs)`` for a tmux
    argv on POSIX; on Windows the session host runs it (in a thread) and the
    result answers ``communicate()`` / ``wait()`` the same way. ``stdout`` /
    ``stderr`` keep their meaning (``PIPE`` captures, else discarded)."""
    import asyncio

    if not IS_WINDOWS:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    pipe = asyncio.subprocess.PIPE
    run_kw = {
        "stdout": (
            subprocess.PIPE if kwargs.get("stdout") == pipe else subprocess.DEVNULL
        ),
        "stderr": (
            subprocess.PIPE if kwargs.get("stderr") == pipe else subprocess.DEVNULL
        ),
        "stdin": subprocess.DEVNULL,
    }
    if kwargs.get("cwd") is not None:
        run_kw["cwd"] = kwargs["cwd"]
    if kwargs.get("env") is not None:
        run_kw["env"] = kwargs["env"]
    cp = await asyncio.to_thread(tmux_run, list(argv), **run_kw)
    return _Finished(cp)
