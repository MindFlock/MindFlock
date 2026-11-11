"""The engine's PTY (``session/tmux/pty.py`` ``Pty.start``) goes through
``mux.spawn_pty`` and works on a CPython whose ``os.posix_spawn`` has no
``setsid=``.

The cause found on PR #350: ``install.sh`` pins Python 3.12, Debian bookworm
ships 3.11, so uv installs a python-build-standalone CPython. That build was
compiled against a glibc without ``POSIX_SPAWN_SETSID``, so
``os.posix_spawn(..., setsid=True)`` raises ``NotImplementedError:
posix_spawn: setsid unavailable on this platform`` — and every session in the
bare-container quickstart "failed to start". ``spawn_pty`` now falls back to
libc's own ``posix_spawn`` with the flag (ctypes). These tests stand that
interpreter in by making ``os.posix_spawn`` refuse ``setsid=`` exactly as it
does there (before anything is spawned), then check that the child is still
a session leader on its PTY, that nothing forks, and that the engine's real
tmux create (``new-session -d``) and restore (``attach-session``) work.

Real tmux runs on a private ``-L`` socket with ``TMUX`` unset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from backend import cmd as cmd_pkg
from backend.session import mux
from backend.session.tmux import pty as tmux_pty

needs_posix_spawn = pytest.mark.skipif(
    not mux._POSIX_SPAWN_PTY, reason="Linux posix_spawn PTY"
)
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

_REAL_POSIX_SPAWN = os.posix_spawn


def _native_setsid() -> bool:
    """Whether this CPython's ``os.posix_spawn`` takes ``setsid=``."""
    try:
        _REAL_POSIX_SPAWN("/nonexistent/mf-probe", ["x"], {}, setsid=True)
    except NotImplementedError:
        return False
    except OSError:
        return True
    return True  # pragma: no cover - /nonexistent exists


@pytest.fixture
def no_native_setsid(monkeypatch):
    """``os.posix_spawn`` as python-build-standalone has it: ``setsid=``
    raises before anything is spawned. Records each fallback to libc."""
    calls: list = []

    def _posix_spawn(path, argv, env, **kw):
        if kw.get("setsid"):
            raise NotImplementedError(
                "posix_spawn: setsid unavailable on this platform"
            )
        return _REAL_POSIX_SPAWN(path, argv, env, **kw)

    real_libc = mux._libc_spawn_session_leader

    def _libc(path, argv, env, tty):
        calls.append(list(argv))
        return real_libc(path, argv, env, tty)

    monkeypatch.setattr(os, "posix_spawn", _posix_spawn)
    monkeypatch.setattr(mux, "_libc_spawn_session_leader", _libc)
    return calls


@pytest.fixture
def no_fork(monkeypatch):
    """A spawn that forks the server fails the test instead."""
    import pty

    import ptyprocess

    def _forks(*a, **k):
        raise AssertionError("fork() of the server")

    monkeypatch.setattr(os, "fork", _forks)
    monkeypatch.setattr(os, "forkpty", _forks)
    monkeypatch.setattr(pty, "fork", _forks)
    monkeypatch.setattr(ptyprocess.PtyProcess, "spawn", _forks)


@pytest.fixture
def tmux_sock(monkeypatch):
    """A private tmux server name, TMUX unset; its server is killed after."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    sock = "mf-engine-pty-%s" % uuid.uuid4().hex[:12]
    yield sock
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    subprocess.run(
        ["tmux", "-L", sock, "kill-server"], env=env, capture_output=True, timeout=10
    )


def _read_all(fd, timeout=10.0) -> bytes:
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def _read_until(fd, needle: bytes, timeout=10.0) -> bytes:
    import select

    out = b""
    end = time.time() + timeout
    while needle not in out and time.time() < end:
        if select.select([fd], [], [], 0.1)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def _has_session(sock: str, name: str) -> bool:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    return (
        subprocess.run(
            ["tmux", "-L", sock, "has-session", "-t", "=" + name],
            env=env,
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )


# A child that reports what forkpty would have given it.
_PROBE = (
    "import os, fcntl, struct, termios;"
    "print('LEADER', os.getsid(0) == os.getpid());"
    "print('FOREGROUND', os.tcgetpgrp(0) == os.getpgrp());"
    "os.close(os.open('/dev/tty', os.O_RDWR)); print('CTTY yes');"
    "print('STDIO', os.ttyname(0) == os.ttyname(1) == os.ttyname(2));"
    "print('WINSZ %d %d' % struct.unpack('HHHH',"
    " fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))[:2]);"
    "print('ENV', os.environ.get('MF_PROBE'));"
    "print('CWD', os.getcwd())"
)


# --------------------------------------------------------------------------- #
# The cause: a CPython without posix_spawn(setsid=)                            #
# --------------------------------------------------------------------------- #
@needs_posix_spawn
@pytest.mark.parametrize("cwd", [None, "tmp"])
def test_without_native_setsid_the_child_is_still_a_session_leader_on_its_pty(
    no_native_setsid, no_fork, tmp_path, cwd
):
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["MF_PROBE"] = "standalone"
    proc = mux.spawn_pty(
        [sys.executable, "-c", _PROBE],
        dimensions=(33, 101),
        env=env,
        cwd=None if cwd is None else str(tmp_path),
    )
    try:
        out = _read_all(proc.fd).decode()
    finally:
        proc.close(force=True)
    assert no_native_setsid, "the libc fallback was not taken"
    for line in (
        "LEADER True",
        "FOREGROUND True",
        "CTTY yes",
        "STDIO True",
        "WINSZ 33 101",
        "ENV standalone",
    ):
        assert line in out, out
    if cwd is not None:
        assert "CWD %s" % tmp_path in out, out


@needs_posix_spawn
def test_the_libc_path_reports_a_failed_exec_as_oserror(tmp_path):
    not_exec = tmp_path / "not-executable"
    not_exec.write_text("#!/bin/sh\n")
    master, slave = os.openpty()
    try:
        with pytest.raises(OSError):
            mux._libc_spawn_session_leader(
                str(not_exec), [str(not_exec)], {}, os.ttyname(slave)
            )
    finally:
        os.close(master)
        os.close(slave)


@needs_posix_spawn
@pytest.mark.skipif(not _native_setsid(), reason="this CPython has no setsid=")
def test_a_cpython_with_setsid_never_takes_the_libc_path(monkeypatch):
    def _libc(*a, **k):
        raise AssertionError("libc fallback with a native setsid=")

    monkeypatch.setattr(mux, "_libc_spawn_session_leader", _libc)
    proc = mux.spawn_pty([sys.executable, "-c", "print('ok')"])
    try:
        assert b"ok" in _read_all(proc.fd)
    finally:
        proc.close(force=True)


# --------------------------------------------------------------------------- #
# The engine's PTY: create and restore, on such a CPython                      #
# --------------------------------------------------------------------------- #
@needs_posix_spawn
@needs_tmux
def test_engine_create_and_restore_without_native_setsid(
    no_native_setsid, no_fork, tmux_sock, tmp_path
):
    # Create: what TmuxSession.start runs — `tmux new-session -d … -c <dir>`.
    handle, err = tmux_pty.Pty().start(
        cmd_pkg.Cmd(
            [
                "tmux",
                "-L",
                tmux_sock,
                "new-session",
                "-d",
                "-s",
                "mf-engine",
                "-c",
                str(tmp_path),
                "echo ENGINE-READY; sleep 60",
            ]
        )
    )
    assert err is None, err
    deadline = time.monotonic() + 5
    while not _has_session(tmux_sock, "mf-engine"):
        assert time.monotonic() < deadline, "tmux session never appeared"
        time.sleep(0.05)
    handle.close()

    # Restore: what TmuxSession.restore keeps as ptmx — an attached client.
    attached, err = tmux_pty.Pty().start(
        cmd_pkg.Cmd(["tmux", "-L", tmux_sock, "attach-session", "-t", "mf-engine"])
    )
    assert err is None, err
    try:
        out = _read_until(attached.fileno(), b"ENGINE-READY")
        assert b"ENGINE-READY" in out, out
        assert tmux_pty.set_size(attached, tmux_pty.Winsize(40, 120)) is None
    finally:
        attached.close()
    assert len(no_native_setsid) == 2  # both went through libc's posix_spawn
    assert _has_session(tmux_sock, "mf-engine")  # detaching kept the session
