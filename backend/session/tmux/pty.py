"""Port of the Go ``session/tmux/pty.go``.

Provides a factory-pattern abstraction for creating pseudo-terminals (PTYs),
mirroring the Go ``PtyFactory`` interface, the production ``Pty`` struct and
``MakePtyFactory()``.

Go source::

    type PtyFactory interface {
        Start(cmd *exec.Cmd) (*os.File, error)
        Close()
    }

    type Pty struct{}

    func (pt Pty) Start(cmd *exec.Cmd) (*os.File, error) {
        return pty.Start(cmd)
    }

    func (pt Pty) Close() {}

    func MakePtyFactory() PtyFactory { return Pty{} }

Python mapping:

- Go's ``github.com/creack/pty`` -> :mod:`ptyprocess` (``ptyprocess.PtyProcess``).
- Go's ``*os.File`` PTY master -> :class:`PtyFile`, a small file-like wrapper
  around the PTY master fd that exposes the subset of ``*os.File`` the tmux
  layer needs: ``write``, ``read``, ``close``, ``fileno`` and ``closed``. It is
  what ``Pty.start`` returns and what gets stored on ``TmuxSession.ptmx``.
- Go's ``error`` second return value -> the Python convention used throughout
  this port: methods *return* ``(handle, error_or_None)`` rather than raising
  (see :mod:`backend.cmd`). The caller checks ``err is not None``.

The ``cmd`` argument is a :class:`backend.cmd.Cmd` (the Python mirror of
``*exec.Cmd``); its ``args`` is the full argv (program name in slot 0) and
``dir`` is the optional working directory.
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from backend import cmd as cmd_pkg


class PtyFile:
    """File-like wrapper around a PTY master file descriptor.

    Mirrors the subset of Go's ``*os.File`` that the tmux package relies on:
    ``Write``, ``Read``, ``Close`` and ``Fd``. Backed by a :mod:`ptyprocess`
    ``PtyProcess`` (which owns the spawned child) so that closing this handle
    both closes the master fd and reaps/terminates the child, matching the
    creack/pty semantics where closing the master tears down the PTY.
    """

    __slots__ = ("_proc", "_fd", "_closed")

    def __init__(self, proc) -> None:
        self._proc = proc
        # ptyprocess exposes the master fd as ``.fd``.
        self._fd: int = proc.fd
        self._closed = False

    # --- *os.File surface --------------------------------------------------
    def write(self, data: bytes) -> int:
        """Write bytes to the PTY master (mirrors ``(*os.File).Write``)."""
        import os

        return os.write(self._fd, data)

    def read(self, n: int = 4096) -> bytes:
        """Read up to ``n`` bytes from the PTY master (mirrors ``Read``)."""
        import os

        return os.read(self._fd, n)

    def fileno(self) -> int:
        """Return the underlying fd (mirrors ``(*os.File).Fd``)."""
        return self._fd

    # Go uses .Fd(); keep a Pythonic alias too.
    def fd(self) -> int:
        return self._fd

    def close(self) -> None:
        """Close the PTY master and tear down the child (mirrors ``Close``)."""
        if self._closed:
            return
        self._closed = True
        try:
            # Closing the ptyprocess closes the master fd and terminates the
            # child if still alive (best-effort, like creack/pty master close).
            if self._proc.isalive():
                self._proc.close(force=True)
            else:
                self._proc.close()
        except Exception:
            # Match Go's master-close which, on a detach, may already be gone.
            import os

            try:
                os.close(self._fd)
            except OSError:
                pass
            # The ptyprocess close failed before it could reap the child —
            # reap defensively (non-blocking) so we don't leave a zombie.
            try:
                pid = getattr(self._proc, "pid", None)
                if pid:
                    os.waitpid(pid, os.WNOHANG)
            except (OSError, ChildProcessError):
                pass

    @property
    def closed(self) -> bool:
        return self._closed


class PtyFactory(ABC):
    """Interface for PTY factory implementations (Go ``PtyFactory``).

    Methods:
        start(cmd) -> (handle, error_or_None):
            Start ``cmd`` in a new PTY; return the master handle and an error
            (``None`` on success). Mirrors ``Start(cmd) (*os.File, error)``.
        close() -> None:
            Release any factory-level resources. Mirrors ``Close()``.
    """

    @abstractmethod
    def start(self, cmd: cmd_pkg.Cmd) -> Tuple[Optional[PtyFile], Optional[Exception]]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class Pty(PtyFactory):
    """Production :class:`PtyFactory` backed by :mod:`ptyprocess`.

    Stateless, mirroring Go's zero-value ``Pty{}`` struct. ``start`` delegates
    to ``ptyprocess.PtyProcess.spawn`` (the analogue of ``pty.Start(cmd)``):
    it allocates a PTY pair, forks/execs the command with the slave as stdio,
    and returns the master side wrapped in :class:`PtyFile`.
    """

    def start(self, cmd: cmd_pkg.Cmd) -> Tuple[Optional[PtyFile], Optional[Exception]]:
        try:
            from ptyprocess import PtyProcess

            kwargs = {}
            if cmd.dir is not None:
                kwargs["cwd"] = cmd.dir
            proc = PtyProcess.spawn(list(cmd.args), **kwargs)
        except Exception as e:  # noqa: BLE001 - mirror Go error return
            return None, e
        return PtyFile(proc), None

    def close(self) -> None:
        """No-op (Go ``(pt Pty) Close() {}``); safe to call repeatedly."""
        return None


def MakePtyFactory() -> PtyFactory:
    """Return a production :class:`Pty` (mirrors ``MakePtyFactory``)."""
    return Pty()


# snake_case alias for Pythonic call sites.
make_pty_factory = MakePtyFactory


class Winsize:
    """Python mirror of creack/pty's ``Winsize`` struct.

    Field order matches Go exactly so the packed ``TIOCSWINSZ`` payload is
    byte-identical::

        type Winsize struct {
            Rows uint16
            Cols uint16
            X    uint16
            Y    uint16
        }
    """

    __slots__ = ("rows", "cols", "x", "y")

    def __init__(self, rows: int = 0, cols: int = 0, x: int = 0, y: int = 0) -> None:
        self.rows = rows
        self.cols = cols
        self.x = x
        self.y = y


def set_size(f, ws: Winsize) -> Optional[Exception]:
    """Resize a PTY (mirrors ``pty.Setsize(f, &pty.Winsize{...})``).

    Issues a ``TIOCSWINSZ`` ioctl on the file descriptor of ``f`` with the
    window size packed in creack/pty's field order ``(Rows, Cols, X, Y)``,
    each a ``uint16`` (little/native endian via ``struct`` 'H'). Returns
    ``None`` on success or the raised exception (Go's ``error``).
    """
    import fcntl
    import termios

    try:
        fd = f.fileno()
        # struct winsize { ws_row; ws_col; ws_xpixel; ws_ypixel } — four
        # unsigned shorts, matching the Rows,Cols,X,Y field order.
        payload = struct.pack("HHHH", ws.rows, ws.cols, ws.x, ws.y)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, payload)
    except Exception as e:  # noqa: BLE001 - mirror Go error return
        return e
    return None


# Go-cased alias to mirror the creack/pty call site exactly.
Setsize = set_size


__all__ = [
    "PtyFile",
    "PtyFactory",
    "Pty",
    "MakePtyFactory",
    "make_pty_factory",
    "Winsize",
    "set_size",
    "Setsize",
]
