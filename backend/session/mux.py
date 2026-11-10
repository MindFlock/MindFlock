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
  returns a :class:`Terminal` (started by :func:`spawn_pty`).
* :func:`available` instead of ``shutil.which("tmux")``.

One rule is added on the way through: in a session that MindFlock itself put
in copy-mode (a voice "scroll up": :func:`guard_typing`), a ``send-keys`` that
TYPES into it (keys, ``-l`` text, ``-H`` bytes — not ``-X`` / ``-M`` / ``-K``)
first leaves copy-mode if the pane is still in it (:func:`leave_copy_mode`).
Keys sent to a pane in copy-mode are copy-mode's commands, not the app's
input: a prompt dictated after the scroll would be eaten letter by letter
("q" leaves, Space starts a selection…) and its Enter would copy, not submit.
Every typer — ``/send``, live dictation, ``/answer``, the queue drain, a
guest's keys — goes through :func:`run`, so this is the one place that keeps
them all safe. The guard is tmux's own ``if-shell -F`` in the same
invocation: no extra process, no race between the check and the keys. Any
other session's argv goes to tmux exactly as given.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from typing import Any, List, Optional, Protocol, Sequence, Tuple

__all__ = [
    "IS_WINDOWS",
    "Terminal",
    "attach",
    "available",
    "backend",
    "exec_attach",
    "guard_typing",
    "leave_copy_mode",
    "popen",
    "run",
    "spawn_pty",
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


#: tmux's own options that take a value (``tmux -L name send-keys …``).
_GLOBAL_VALUE = frozenset(("-c", "-f", "-L", "-S", "-T"))
#: ``send-keys`` flags that take a value.
_SEND_VALUE = "cNt"
#: ``send-keys`` flags under which nothing is typed into the pane: a
#: copy-mode command (``-X``), a mouse event (``-M``), a client key (``-K``).
_SEND_NOT_TYPING = frozenset("XMK")
#: Only copy-mode: ``-X cancel`` errors in a mode without commands (a tree).
_IN_COPY_MODE = "#{==:#{pane_mode},copy-mode}"
#: tmux sessions MindFlock put in copy-mode (:func:`guard_typing`), oldest
#: first; their typing is guarded from then on (a no-op once out of it).
_GUARDED: "OrderedDict[str, None]" = OrderedDict()
_GUARDED_MAX = 1024
_GUARD_LOCK = threading.Lock()


def guard_typing(session: str) -> None:
    """MindFlock is putting tmux session ``session`` in copy-mode (a voice
    scroll): from now on every typing ``send-keys`` into it leaves
    copy-mode first (:func:`leave_copy_mode`)."""
    name = _session_of(session)
    if not name:
        return
    with _GUARD_LOCK:
        _GUARDED.pop(name, None)
        while len(_GUARDED) >= _GUARDED_MAX:
            _GUARDED.popitem(last=False)
        _GUARDED[name] = None


def _session_of(target: str) -> str:
    """The session a ``-t`` target names: ``=name:`` / ``name:0.1`` → name."""
    t = str(target or "").lstrip("=")
    for sep in (":", "."):
        t = t.split(sep, 1)[0]
    return t


def _typing_target(cmd: List[str]) -> Optional[str]:
    """For one tmux command: its ``-t`` target (``""`` for none) when it is a
    ``send-keys`` that types into the pane, else None."""
    if not cmd or cmd[0] not in ("send-keys", "send"):
        return None
    flags, target, j = set(), "", 1
    while j < len(cmd):
        a = cmd[j]
        if a == "--":
            j += 1
            break
        if not a.startswith("-") or len(a) < 2:
            break
        for k, ch in enumerate(a[1:], start=1):
            if ch in _SEND_VALUE:
                val = a[k + 1 :]
                if not val and j + 1 < len(cmd):
                    j += 1
                    val = cmd[j]
                if ch == "t":
                    target = val
                break
            flags.add(ch)
        j += 1
    if j >= len(cmd) or flags & _SEND_NOT_TYPING:
        return None
    return target


def leave_copy_mode(argv: Sequence[str], sessions=None) -> List[str]:
    """``argv`` with every ``send-keys`` that types into a pane preceded by
    ``if-shell -F -t T '#{==:#{pane_mode},copy-mode}' "send-keys -X -t T
    cancel" ;`` — the pane leaves copy-mode (if it is in it) right before
    the keys land, in the same tmux invocation (module docstring).
    ``sessions``: only those whose target names one of these sessions
    (:func:`run` passes the guarded ones); None: every one. Any other argv
    comes back unchanged. A target holding a quote (never a MindFlock name)
    is left unguarded rather than mis-quoted."""
    argv = list(argv)
    i = 1
    while i < len(argv) and argv[i].startswith("-") and len(argv[i]) > 1:
        i += 2 if argv[i] in _GLOBAL_VALUE else 1
    out, seg = argv[:i], []

    def flush() -> None:
        target = _typing_target(seg)
        if sessions is not None and (not target or _session_of(target) not in sessions):
            target = None
        if target is not None and "'" not in target:
            out.extend(["if-shell", "-F"])
            if target:
                out.extend(["-t", target])
            out.append(_IN_COPY_MODE)
            out.append(
                "send-keys -X -t '%s' cancel" % target
                if target
                else "send-keys -X cancel"
            )
            out.append(";")
        out.extend(seg)

    for a in argv[i:]:
        if a == ";":
            flush()
            out.append(a)
            seg = []
        else:
            seg.append(a)
    flush()
    return out


def _guarded(argv: Sequence[str]) -> List[str]:
    with _GUARD_LOCK:
        guarded = frozenset(_GUARDED)
    if not guarded:
        return list(argv)
    return leave_copy_mode(argv, guarded)


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, **kwargs)`` for a tmux argv, on any OS — a
    typing ``send-keys`` guarded out of copy-mode first
    (:func:`leave_copy_mode`) in a session :func:`guard_typing` named."""
    _check(argv)
    argv = _guarded(argv)
    if not IS_WINDOWS:
        return subprocess.run(argv, **kwargs)
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.run(argv, **kwargs)  # pragma: no cover


def popen(argv: Sequence[str], **kwargs: Any) -> Any:
    """``subprocess.Popen(argv, **kwargs)`` for a tmux argv, on any OS (the
    result has ``communicate``, ``wait``, ``returncode``, ``stdout``)."""
    _check(argv)
    argv = _guarded(argv)
    if not IS_WINDOWS:
        return subprocess.Popen(argv, **kwargs)
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.popen(argv, **kwargs)  # pragma: no cover


def attach(
    session: str,
    *,
    readonly: bool = False,
    dimensions: Tuple[int, int] = (24, 80),
    env: Optional[dict] = None,
) -> Terminal:
    """An interactive client on ``session`` (``readonly``: ``attach -r``),
    sized ``dimensions`` (rows, cols) — started by :func:`spawn_pty`
    (``posix_spawn`` on Linux, never a fork of the server)."""
    if not IS_WINDOWS:
        argv = (
            ["tmux", "attach-session"] + (["-r"] if readonly else []) + ["-t", session]
        )
        return spawn_pty(
            argv,
            dimensions=dimensions,
            env=env if env is not None else {**os.environ, "TERM": "xterm-256color"},
        )
    from backend.session.winmux import client  # pragma: no cover - Windows

    return client.attach(
        session, readonly=readonly, dimensions=dimensions
    )  # pragma: no cover


#: Linux: a PTY client starts by ``posix_spawn`` (:func:`spawn_pty`). Elsewhere
#: (macOS: opening a tty does not make it the controlling one) ptyprocess.
_POSIX_SPAWN_PTY = sys.platform.startswith("linux") and hasattr(os, "posix_spawn")
#: :func:`spawn_pty`'s ``cwd=``: the shell that changes directory and then
#: ``exec``\ s the command (``$1`` = the directory, the rest = the argv).
_SH = "/bin/sh"
_CD_EXEC = 'cd "$1" && shift && exec "$@"'
#: ``POSIX_SPAWN_SETSID`` in glibc's (≥ 2.26) and musl's ``<spawn.h>``.
_LIBC_POSIX_SPAWN_SETSID = 0x80


def _spawn_session_leader(path: str, argv: List[str], env: dict, tty: str) -> int:
    """``posix_spawn`` ``path`` as a session leader whose fd 0 is ``tty``
    opened by path (so it is the controlling tty), 1 and 2 dups of it.

    CPython compiles ``os.posix_spawn``'s ``setsid=`` in only when ITS build
    headers define ``POSIX_SPAWN_SETSID``. python-build-standalone — the
    CPython uv installs when the system has no 3.12 (Debian bookworm ships
    3.11; ``install.sh`` pins 3.12) — is built against an older glibc, so
    ``setsid=True`` raises ``NotImplementedError: posix_spawn: setsid
    unavailable on this platform`` (before anything is spawned) although the
    libc it runs on has had the flag since 2.26. That is why moving the
    engine's PTY here failed the bare-container quickstart. There the same
    call goes to libc's ``posix_spawn`` through ctypes with the flag set:
    the same ``clone(CLONE_VM | CLONE_VFORK)``, the GIL released, no Python
    in the child."""
    try:
        return os.posix_spawn(
            path,
            argv,
            env,
            setsid=True,
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, tty, os.O_RDWR, 0),
                (os.POSIX_SPAWN_DUP2, 0, 1),
                (os.POSIX_SPAWN_DUP2, 0, 2),
            ],
        )
    except NotImplementedError:
        return _libc_spawn_session_leader(path, argv, env, tty)


def _libc_spawn_session_leader(path: str, argv: List[str], env: dict, tty: str) -> int:
    """:func:`_spawn_session_leader` through libc's own ``posix_spawn``
    (ctypes), for a CPython built without ``setsid=``. A libc that predates
    ``POSIX_SPAWN_SETSID`` (glibc < 2.26) refuses the flag: ``OSError``."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.posix_spawnattr_setflags.argtypes = [ctypes.c_void_p, ctypes.c_short]
    # The opaque structs (glibc: file actions 80 bytes, attr 336), oversized.
    actions = ctypes.create_string_buffer(512)
    attr = ctypes.create_string_buffer(1024)

    def _check(rc: int, what: str) -> None:
        if rc:
            raise OSError(rc, "%s: %s" % (what, os.strerror(rc)))

    _check(libc.posix_spawn_file_actions_init(actions), "posix_spawn_file_actions_init")
    try:
        _check(libc.posix_spawnattr_init(attr), "posix_spawnattr_init")
        try:
            _check(
                libc.posix_spawnattr_setflags(attr, _LIBC_POSIX_SPAWN_SETSID),
                "posix_spawnattr_setflags(POSIX_SPAWN_SETSID)",
            )
            _check(
                libc.posix_spawn_file_actions_addopen(
                    actions, 0, os.fsencode(tty), os.O_RDWR, 0
                ),
                "posix_spawn_file_actions_addopen",
            )
            for fd in (1, 2):
                _check(
                    libc.posix_spawn_file_actions_adddup2(actions, 0, fd),
                    "posix_spawn_file_actions_adddup2",
                )
            c_argv = (ctypes.c_char_p * (len(argv) + 1))(
                *[os.fsencode(a) for a in argv], None
            )
            c_env = (ctypes.c_char_p * (len(env) + 1))(
                *[os.fsencode(k) + b"=" + os.fsencode(v) for k, v in env.items()],
                None,
            )
            pid = ctypes.c_int()  # pid_t
            rc = libc.posix_spawn(
                ctypes.byref(pid), os.fsencode(path), actions, attr, c_argv, c_env
            )
            if rc:  # glibc reports a failed exec here, as os.posix_spawn does
                raise OSError(rc, os.strerror(rc), path)
            return pid.value
        finally:
            libc.posix_spawnattr_destroy(attr)
    finally:
        libc.posix_spawn_file_actions_destroy(actions)


def spawn_pty(
    argv: Sequence[str],
    *,
    dimensions: Tuple[int, int] = (24, 80),
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> Any:
    """``ptyprocess.PtyProcess.spawn(argv, dimensions=…, env=…, cwd=…)`` without
    ``fork()``: the same :class:`ptyprocess.PtyProcess` (same ``fd``,
    ``setwinsize``, ``terminate``, ``close``), its child started by
    ``os.posix_spawn``.

    Why: CPython forks while HOLDING the GIL, and a fork copies the page
    tables of the whole server — in a worker thread it still stalls every
    thread for as long as it takes (≈13 ms at 600 MB resident; on the
    2026-11-10 incident, 1.7 GB under WSL memory pressure, it never
    returned and the server froze). ``posix_spawn`` is ``clone(CLONE_VM |
    CLONE_VFORK)`` in glibc: nothing is copied and no Python runs in the
    child. The child becomes a session leader (``setsid``) and opens the PTY
    slave by path as fd 0 — on Linux that makes it the controlling tty with
    the child's group in the foreground, exactly what ``forkpty`` sets up,
    so a resize (``TIOCSWINSZ`` on the master) still SIGWINCHes the client
    and closing the master still SIGHUPs it. Window size is set before the
    spawn, so the client starts at ``dimensions``. Fds: the server's are
    close-on-exec (Python's default), so the client inherits only 0/1/2.

    ``cwd``: ``os.posix_spawn`` has no chdir action (glibc's
    ``posix_spawn_file_actions_addchdir_np`` is not exposed), and changing
    the server's own cwd around the spawn would move every thread. So the
    child is ``/bin/sh -c 'cd "$1" && shift && exec "$@"'`` with the
    absolute ``cwd`` and the resolved command as arguments — never pasted
    into the script, so nothing is quoted or interpreted — and ``exec``
    replaces the shell: same pid, same PTY, the command's own argv. As with
    ptyprocess, ``argv[0]`` is then the resolved path. The shell's ``cd``
    sets ``PWD`` to ``cwd`` (ptyprocess left the server's stale one); the
    rest of ``env`` passes through untouched. A missing ``cwd`` raises
    ``FileNotFoundError`` here, before anything is spawned.

    A CPython built without ``os.posix_spawn``'s ``setsid=`` (uv's
    python-build-standalone) gets the same spawn from libc's own
    ``posix_spawn`` (:func:`_spawn_session_leader`).

    Off Linux this is ``ptyprocess.PtyProcess.spawn`` itself."""
    import ptyprocess

    argv = list(argv)
    if not _POSIX_SPAWN_PTY:
        kwargs = {} if cwd is None else {"cwd": cwd}
        return ptyprocess.PtyProcess.spawn(
            argv, dimensions=dimensions, env=env, **kwargs
        )
    import fcntl
    import struct
    import termios

    path = shutil.which(argv[0])
    if path is None:  # ptyprocess's error, word for word
        raise FileNotFoundError(
            "The command was not found or was not executable: %s." % argv[0]
        )
    exe, child_argv = path, argv
    if cwd is not None:
        cwd = os.path.abspath(os.fspath(cwd))
        if not os.path.isdir(cwd):
            raise FileNotFoundError(
                errno.ENOENT, "No such working directory for %s" % argv[0], cwd
            )
        exe = _SH
        child_argv = [_SH, "-c", _CD_EXEC, "sh", cwd, path] + argv[1:]
    master, slave = os.openpty()  # both close-on-exec; slave opened O_NOCTTY
    try:
        rows, cols = dimensions
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        pid = _spawn_session_leader(
            exe,
            child_argv,
            dict(os.environ if env is None else env),
            os.ttyname(slave),
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    proc = ptyprocess.PtyProcess(pid, master)
    proc.argv = argv if cwd is None else [path] + argv[1:]
    if env is not None:
        proc.env = env
    if cwd is not None:
        proc.launch_dir = cwd
    return proc


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
