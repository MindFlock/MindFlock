"""The terminal multiplexer every MindFlock session lives in — ONE door for
all of it (docs/windows.md).

A session is an agent CLI in a named, persistent terminal that outlives the
server and that any number of clients can attach to, read and type into.
On Linux and macOS that is tmux, exactly as before: :func:`run` is
``subprocess.run`` of the same argv and :func:`attach` spawns ``tmux
attach-session`` in a PTY. On native Windows there is no tmux, so the same
argv goes to the MindFlock session host (``backend.session.winmux``): a
small per-user daemon that holds each session's ConPTY and a screen model
and answers the tmux subcommands MindFlock uses (new-session, has-session,
send-keys, capture-pane, display-message and its ``#{…}`` formats,
list-sessions, kill-session, rename-session, set-option, set-environment,
set-hook, load/paste/delete-buffer, copy-mode, resize-window, bind-key) the
way tmux answers them — same output, same exit codes — so no caller has a
second code path.

Rules for callers:

* Never ``subprocess.run(["tmux", …])`` or ``Popen(["tmux", …])``: call
  :func:`run` / :func:`popen` with that argv (``argv[0]`` is ``"tmux"``;
  ``-S <socket>`` / ``-L <name>`` may follow it). The keywords are
  ``subprocess.run``'s.
* Never spawn ``tmux attach-session`` in a PTY yourself: :func:`attach`
  returns a :class:`Terminal`.
* :func:`available` instead of ``shutil.which("tmux")``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Optional, Protocol, Sequence, Tuple

__all__ = [
    "IS_WINDOWS",
    "Terminal",
    "attach",
    "available",
    "backend",
    "exec_attach",
    "popen",
    "run",
]

IS_WINDOWS = os.name == "nt"


class Terminal(Protocol):
    """A client attached to a session — ``ptyprocess.PtyProcess``'s shape.

    ``fd`` is a selectable file descriptor on POSIX (an event loop's
    ``add_reader`` may watch it) and ``None`` on Windows, where a reader
    calls :meth:`read` from a thread; :meth:`read` blocks until bytes come
    and returns ``b""`` (or raises ``EOFError``) once the client is gone."""

    fd: Optional[int]
    pid: Optional[int]

    def read(self, size: int = 1024) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def setwinsize(self, rows: int, cols: int) -> None: ...

    def getwinsize(self) -> Tuple[int, int]: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = False) -> bool: ...

    def close(self, force: bool = True) -> None: ...


def backend() -> str:
    """``"tmux"`` or ``"winmux"`` (the MindFlock session host)."""
    return "winmux" if IS_WINDOWS else "tmux"


def _check(argv: Sequence[str]) -> None:
    if not argv or argv[0] != "tmux":
        raise ValueError("mux.run takes a tmux argv (argv[0] == 'tmux'): %r" % (argv,))


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, **kwargs)`` for a tmux argv, on any OS."""
    _check(argv)
    if not IS_WINDOWS:
        return subprocess.run(list(argv), **kwargs)
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.run(list(argv), **kwargs)  # pragma: no cover


def popen(argv: Sequence[str], **kwargs: Any) -> Any:
    """``subprocess.Popen(argv, **kwargs)`` for a tmux argv, on any OS (the
    result has ``communicate``, ``wait``, ``returncode``, ``stdout``)."""
    _check(argv)
    if not IS_WINDOWS:
        return subprocess.Popen(list(argv), **kwargs)
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.popen(list(argv), **kwargs)  # pragma: no cover


def attach(
    session: str,
    *,
    readonly: bool = False,
    dimensions: Tuple[int, int] = (24, 80),
    env: Optional[dict] = None,
) -> Terminal:
    """An interactive client on ``session`` (``readonly``: ``attach -r``),
    sized ``dimensions`` (rows, cols)."""
    if not IS_WINDOWS:
        import ptyprocess

        argv = (
            ["tmux", "attach-session"] + (["-r"] if readonly else []) + ["-t", session]
        )
        return ptyprocess.PtyProcess.spawn(
            argv,
            dimensions=dimensions,
            env=env if env is not None else {**os.environ, "TERM": "xterm-256color"},
        )
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.attach(
        session, readonly=readonly, dimensions=dimensions
    )  # pragma: no cover


def exec_attach(session: str) -> None:
    """Replace this process with a foreground client on ``session`` (the
    CLI's ``mindflock attach``); on Windows, run one in this console and
    exit with its code."""
    if not IS_WINDOWS:
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    from backend.session.winmux import client  # pragma: no cover - Windows

    raise SystemExit(client.console_attach(session))  # pragma: no cover


def available() -> bool:
    """Whether sessions can run here: tmux on PATH, or the session host's
    packages on Windows."""
    if not IS_WINDOWS:
        return shutil.which("tmux") is not None
    try:  # pragma: no cover - Windows
        import winpty  # noqa: F401
    except ImportError:  # pragma: no cover
        return False
    return True  # pragma: no cover
