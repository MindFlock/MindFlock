"""The pseudo-terminal a session's program runs in — pluggable, so the
whole host runs on Linux under pytest exactly as it runs on Windows.

* :class:`WinPty` — ConPTY through pywinpty (``winpty.PtyProcess``), the
  backend on Windows.
* :class:`PosixPty` — ``ptyprocess``, the backend everywhere else. Only the
  tests use it: on Linux and macOS MindFlock runs real tmux.

Both read and write bytes (pywinpty speaks ``str``; the adapter encodes),
block in :meth:`read` until output comes, and return ``b""`` once the child
has gone.
"""

from __future__ import annotations

import codecs
import os
import signal
import threading
from typing import Dict, List, Optional

from backend import compat

__all__ = ["Pty", "spawn", "process_name", "process_cwd"]


class Pty:
    """The interface the host uses."""

    pid: int

    def read(self) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def alive(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def exit_status(self) -> Optional[int]:  # pragma: no cover - interface
        raise NotImplementedError

    def foreground_pid(self) -> Optional[int]:
        return self.pid

    def close(self) -> None:
        """Release the terminal once :meth:`read` has returned ``b""``."""


class PosixPty(Pty):
    def __init__(
        self, argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int
    ) -> None:
        import ptyprocess

        self._p = ptyprocess.PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(rows, cols)
        )
        self.pid = self._p.pid
        self._fd = self._p.fd
        self._status: Optional[int] = None

    def read(self) -> bytes:
        try:
            return os.read(self._fd, 65536)
        except OSError:  # EIO once the child side has closed
            return b""

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._p.setwinsize(rows, cols)
        except OSError:
            pass

    def alive(self) -> bool:
        try:
            return self._p.isalive()
        except Exception:  # noqa: BLE001
            return False

    def kill(self) -> None:
        # tmux closes the pane's pty and the kernel hangs up its session; the
        # SIGHUP to the group is that, then SIGKILL for what ignores it. The
        # fd itself is closed by the reader thread (close()), never here: a
        # close under a blocked read could hand the number to another file.
        for sig in (signal.SIGHUP, signal.SIGCONT):
            try:
                os.killpg(self.pid, sig)
            except OSError:
                pass

        def _finish():
            if self.alive():
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except OSError:
                    pass

        t = threading.Timer(1.0, _finish)
        t.daemon = True
        t.start()

    def close(self) -> None:
        try:
            self._p.close(force=True)
        except Exception:  # noqa: BLE001
            pass

    def exit_status(self) -> Optional[int]:
        if self._status is None:
            try:
                self._p.wait()
            except Exception:  # noqa: BLE001
                pass
            st = self._p.exitstatus
            if st is None and self._p.signalstatus is not None:
                st = 128 + self._p.signalstatus
            self._status = st
        return self._status

    def foreground_pid(self) -> Optional[int]:
        try:
            return os.tcgetpgrp(self._fd)
        except OSError:
            return self.pid


class WinPty(Pty):  # pragma: no cover - exercised on Windows
    def __init__(
        self, argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int
    ) -> None:
        import shutil

        import winpty

        argv = list(argv)
        if not os.path.isabs(argv[0]):
            # pywinpty searches env["PATH"] only, and a Windows block often
            # spells it "Path": resolve here, with PATHEXT, like CreateProcess.
            path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
            argv[0] = shutil.which(argv[0], path=path) or argv[0]
        self._p = winpty.PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(rows, cols)
        )
        self.pid = self._p.pid
        self._enc = codecs.getincrementaldecoder("utf-8")("replace")
        self._wlock = threading.Lock()

    def read(self) -> bytes:
        while True:
            try:
                s = self._p.read(65536)
            except EOFError:
                return b""
            except Exception:  # noqa: BLE001 - a closed pty raises oddly
                return b""
            if s:
                return s.encode("utf-8", "surrogatepass")
            if not self._p.isalive():
                return b""

    def write(self, data: bytes) -> None:
        with self._wlock:
            text = self._enc.decode(data)
            if text:
                self._p.write(text)

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._p.setwinsize(rows, cols)
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        try:
            return self._p.isalive()
        except Exception:  # noqa: BLE001
            return False

    def kill(self) -> None:
        # No process group or hang-up on Windows: end the whole tree (bash.exe
        # and everything it started), then the pseudo-console.
        try:
            compat.kill_group(self.pid, getattr(signal, "SIGKILL", 9))
        except Exception:  # noqa: BLE001
            pass
        try:
            self._p.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass

    def exit_status(self) -> Optional[int]:
        try:
            st = self._p.exitstatus
        except Exception:  # noqa: BLE001
            st = None
        return st

    def foreground_pid(self) -> Optional[int]:
        return _youngest_descendant(self.pid)


_SKIP_NAMES = {"conhost.exe", "openconsole.exe"}


def _youngest_descendant(pid: int) -> Optional[int]:
    """The process a Windows pane is "running": follow the youngest child
    from the pane's shell down to a leaf. Windows has no foreground process
    group; for ``bash -c claude`` this lands on claude.exe, which is what
    tmux's ``pane_current_command`` names on Linux."""
    try:
        import psutil
    except ImportError:
        return pid
    try:
        proc = psutil.Process(pid)
        while True:
            kids = [k for k in proc.children() if k.name().lower() not in _SKIP_NAMES]
            if not kids:
                return proc.pid
            proc = max(kids, key=lambda k: k.create_time())
    except Exception:  # noqa: BLE001 - raced with an exit
        return pid


def process_name(pid: Optional[int]) -> str:
    """``pane_current_command`` for ``pid``: its argv[0]'s basename, without
    ``.exe`` (tmux reads the foreground group's cmdline on Linux)."""
    if not pid:
        return ""
    try:
        import psutil

        p = psutil.Process(pid)
        try:
            cmd = p.cmdline()
        except Exception:  # noqa: BLE001
            cmd = []
        name = os.path.basename(cmd[0]) if cmd and cmd[0] else p.name()
    except Exception:  # noqa: BLE001
        try:
            with open("/proc/%d/comm" % pid) as f:
                name = f.read().strip()
        except OSError:
            return ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def process_cwd(pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        import psutil

        return psutil.Process(pid).cwd()
    except Exception:  # noqa: BLE001
        return ""


def spawn(argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int) -> Pty:
    """A child in a new pseudo-terminal, with the backend for this OS (or
    the one ``WINMUX_PTY`` names: ``posix`` / ``winpty``)."""
    kind = os.environ.get("WINMUX_PTY") or ("winpty" if compat.IS_WINDOWS else "posix")
    if kind == "winpty":
        return WinPty(argv, cwd, env, rows, cols)
    return PosixPty(argv, cwd, env, rows, cols)
