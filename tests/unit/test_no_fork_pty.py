"""Nothing in the server forks to start a PTY child (incident 2026-11-10).

CPython forks while holding the GIL, so a slow ``fork()`` of a big server
stalls every thread, the event loop included. ``mux.spawn_pty`` starts PTY
children with ``posix_spawn``; these tests pin that every PTY spawn site goes
through it — the provider/``gh`` login terminals (``core.pty_run``), the
ticket log stream (``core.terminal.spawn_tail``) and the engine's own PTY
(``session/tmux/pty.py``) — that ``spawn_pty(cwd=…)`` lands the child in that
directory with the env it was given, and that ``pump_pty`` keeps blocking
PTY writes and the teardown off the loop.

Real spawns run throwaway commands (python, tail), never tmux.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import pytest

from backend import cmd as cmd_pkg
from backend.session import mux
from backend.session.tmux import pty as tmux_pty
from backend.web.core import pty_run, terminal, terminal_ws

needs_posix_spawn = pytest.mark.skipif(
    not mux._POSIX_SPAWN_PTY, reason="Linux posix_spawn PTY"
)


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(extra)
    return env


def _read_all(fd, timeout=10.0) -> bytes:
    """Everything the child prints until it exits (EIO once the slave closes)."""
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


@pytest.fixture
def no_ptyprocess_spawn(monkeypatch):
    """``ptyprocess.PtyProcess.spawn`` (a fork) is never reached."""
    import ptyprocess

    def _forks(*a, **k):
        raise AssertionError("ptyprocess.PtyProcess.spawn forks the server")

    monkeypatch.setattr(ptyprocess.PtyProcess, "spawn", _forks)


@pytest.fixture
def no_fork(monkeypatch):
    """A real spawn that forks fails the test instead."""
    import pty

    def _forks(*a, **k):
        raise AssertionError("fork() of the server")

    monkeypatch.setattr(os, "fork", _forks)
    monkeypatch.setattr(os, "forkpty", _forks)
    monkeypatch.setattr(pty, "fork", _forks)


@pytest.fixture
def spawned(monkeypatch, no_ptyprocess_spawn):
    """``mux.spawn_pty`` recorded instead of run."""
    calls: list = []

    def _spawn(argv, **kw):
        calls.append((list(argv), kw))
        return _Proc()

    monkeypatch.setattr(mux, "spawn_pty", _spawn)
    return calls


class _Proc:
    fd = None

    def read(self, size=1024):
        return b""

    def isalive(self):
        return False

    def wait(self):
        return 0


# --------------------------------------------------------------------------- #
# Each spawn site goes through mux.spawn_pty                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY path")
def test_login_terminal_pty_run_spawns_through_spawn_pty(spawned, tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "MF_PROBE": "1"}
    run = pty_run.PtyRun(["gh", "auth", "login"], str(tmp_path), env)
    assert run.proc is not None
    assert spawned == [
        (
            ["gh", "auth", "login"],
            {
                "cwd": str(tmp_path),
                "env": {**env, "TERM": "xterm-256color"},
                "dimensions": (24, 80),
            },
        )
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY path")
def test_ticket_log_stream_spawns_through_spawn_pty(spawned, tmp_path):
    log = tmp_path / "mindflock.log"
    terminal.spawn_tail(log, lines=500, dimensions=(30, 120))
    assert spawned == [
        (
            ["tail", "-F", "-n", "500", str(log)],
            {
                "dimensions": (30, 120),
                "env": {**os.environ, "TERM": "xterm-256color"},
            },
        )
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY path")
@pytest.mark.parametrize("cwd", [None, "/some/dir"])
def test_engine_pty_spawns_through_spawn_pty(spawned, cwd):
    handle, err = tmux_pty.Pty().start(
        cmd_pkg.Cmd(["tmux", "new-session", "-d"], dir=cwd)
    )
    assert err is None and handle is not None
    assert spawned == [(["tmux", "new-session", "-d"], {"cwd": cwd})]


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY path")
def test_engine_pty_returns_a_spawn_failure_as_its_error(monkeypatch):
    boom = FileNotFoundError("nope")

    def _spawn(argv, **kw):
        raise boom

    monkeypatch.setattr(mux, "spawn_pty", _spawn)
    assert tmux_pty.Pty().start(cmd_pkg.Cmd(["x"])) == (None, boom)


# --------------------------------------------------------------------------- #
# The real thing: no fork, the cwd, the env                                    #
# --------------------------------------------------------------------------- #
_PROBE = (
    "import os,sys;"
    "os.close(os.open('/dev/tty', os.O_RDWR));"  # ENXIO without a ctty
    "print('CWD', os.getcwd());"
    "print('ENV', os.environ.get('MF_PROBE'));"
    "print('PWD', os.environ.get('PWD'));"
    "print('ARGV', sys.argv[1:]);"
    "print('SIZE', os.get_terminal_size(1));"
    "print('LEADER', os.getsid(0) == os.getpid());"
    "print('PID', os.getpid());"
    "sys.stdout.flush()"
)


@needs_posix_spawn
def test_a_real_spawn_with_a_cwd_lands_there_with_the_env(tmp_path, no_fork):
    # A directory whose name a shell would expand, split or execute if it
    # were ever pasted into the wrapper script.
    cwd = tmp_path / "it's a $(touch PWNED) `x` dir"
    cwd.mkdir()
    args = ["a b", "$HOME", "'q'", '"d"', "-c"]
    env = _env(MF_PROBE="probe-value")
    proc = mux.spawn_pty(
        [sys.executable, "-c", _PROBE] + args, dimensions=(31, 97), env=env, cwd=cwd
    )
    try:
        out = _read_all(proc.fd).decode()
    finally:
        proc.close(force=True)
    assert "CWD %s" % cwd in out, out
    assert "ENV probe-value" in out, out
    assert "PWD %s" % cwd in out, out  # the shell's cd: never a stale PWD
    assert "ARGV %r" % args in out, out
    assert "SIZE os.terminal_size(columns=97, lines=31)" in out, out
    assert "LEADER True" in out, out
    # exec replaced the shell: the pid is the command's own.
    assert "PID %d" % proc.pid in out, out
    assert not list(tmp_path.rglob("PWNED"))
    # As ptyprocess with a cwd: argv[0] is the resolved command.
    assert proc.argv == [sys.executable, "-c", _PROBE] + args
    assert proc.launch_dir == str(cwd)
    assert proc.env is env


@needs_posix_spawn
def test_a_relative_cwd_is_the_servers_relative_one(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    proc = mux.spawn_pty([sys.executable, "-c", _PROBE], env=_env(), cwd="sub")
    try:
        out = _read_all(proc.fd).decode()
    finally:
        proc.close(force=True)
    assert "CWD %s" % (tmp_path / "sub") in out, out


@needs_posix_spawn
def test_a_missing_cwd_raises_before_anything_spawns(tmp_path, monkeypatch):
    spawns: list = []
    real = mux._spawn_session_leader
    monkeypatch.setattr(
        mux,
        "_spawn_session_leader",
        lambda *a, **k: (spawns.append(a), real(*a, **k))[1],
    )
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(FileNotFoundError):
        mux.spawn_pty([sys.executable, "-c", "pass"], cwd=tmp_path / "gone")
    assert spawns == []
    assert set(os.listdir("/proc/self/fd")) <= before


@needs_posix_spawn
def test_a_missing_command_with_a_cwd_raises_like_ptyprocess(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found or was not executable"):
        mux.spawn_pty(["definitely-not-a-command-xyz"], cwd=tmp_path)


def test_off_linux_a_cwd_goes_to_ptyprocess(monkeypatch):
    import ptyprocess

    calls = []
    monkeypatch.setattr(mux, "_POSIX_SPAWN_PTY", False)
    monkeypatch.setattr(
        ptyprocess.PtyProcess,
        "spawn",
        lambda argv, **kw: calls.append((argv, kw)) or "proc",
    )
    assert mux.spawn_pty(["x"], cwd="/w") == "proc"
    assert calls == [(["x"], {"dimensions": (24, 80), "env": None, "cwd": "/w"})]


@needs_posix_spawn
def test_a_real_login_terminal_run_never_forks(tmp_path, no_fork):
    run = pty_run.PtyRun(
        [sys.executable, "-c", _PROBE], str(tmp_path), _env(MF_PROBE="login")
    )
    try:
        end = time.time() + 10
        while run.alive() and time.time() < end:
            time.sleep(0.05)
        out = run.attach(lambda _d: None).decode()
    finally:
        run.terminate()
    assert "CWD %s" % tmp_path in out, out
    assert "ENV login" in out, out


@needs_posix_spawn
def test_a_real_log_stream_never_forks(tmp_path, no_fork):
    log = tmp_path / "x.log"
    log.write_text("first\nsecond\n")
    proc = terminal.spawn_tail(log, lines=1)
    try:
        out = _read_until(proc.fd, b"second")
        assert b"second" in out and b"first" not in out, out
        with log.open("a") as f:
            f.write("third\n")
        assert b"third" in _read_until(proc.fd, b"third")
    finally:
        proc.terminate(force=True)
        proc.close(force=True)


@needs_posix_spawn
def test_a_real_engine_pty_never_forks(tmp_path, no_fork):
    handle, err = tmux_pty.Pty().start(
        cmd_pkg.Cmd([sys.executable, "-c", _PROBE], dir=str(tmp_path))
    )
    assert err is None, err
    try:
        out = _read_all(handle.fileno()).decode()
    finally:
        handle.close()
    assert "CWD %s" % tmp_path in out, out


# --------------------------------------------------------------------------- #
# pump_pty: blocking writes and the teardown stay off the loop                 #
# --------------------------------------------------------------------------- #
class _WS:
    """Scripted client frames; then waits for ``release`` and disconnects."""

    def __init__(self, frames, release: "asyncio.Event"):
        self._frames = list(frames)
        self._release = release
        self.closed = False

    async def receive(self):
        if self._frames:
            await asyncio.sleep(0)
            return self._frames.pop(0)
        await self._release.wait()
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        pass

    async def send_text(self, data):
        pass

    async def close(self):
        self.closed = True


class _StuckProc:
    """A client that is not reading (a write blocks until it is killed) and
    whose ``terminate``/``close`` sleep like ptyprocess's."""

    def __init__(self, fd):
        self.fd = fd
        self.killed = threading.Event()
        self.events: list = []

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        time.sleep(0.3)
        self.events.append("terminate")
        self.killed.set()
        return True

    def close(self, force=True):
        time.sleep(0.3)
        self.events.append("close")


async def _max_stall(task) -> float:
    """The longest the loop went without running a tick while ``task`` ran."""
    worst, last = 0.0, time.monotonic()
    while not task.done():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        worst = max(worst, now - last)
        last = now
    await task
    return worst


async def test_pump_pty_writes_and_tears_down_off_the_loop(monkeypatch):
    r, w = os.pipe()
    proc = _StuckProc(r)
    written: list = []
    writing = threading.Event()

    def _blocking_write(fd, data):
        writing.set()
        proc.killed.wait(10)  # a client that never reads, until it is killed
        written.append(data)
        return len(data)

    monkeypatch.setattr(terminal.os, "write", _blocking_write)
    release = asyncio.Event()
    frames = [
        {"type": "websocket.receive", "bytes": b"typed"},
        {"type": "websocket.receive", "text": "more"},
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "resize", "rows": 3, "cols": 4}),
        },
    ]
    task = asyncio.ensure_future(
        terminal.pump_pty(_WS(frames, release), proc, allow_input=True)
    )

    async def _disconnect_once_writing():
        while not writing.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # the write is wedged; the loop runs on
        release.set()

    asyncio.ensure_future(_disconnect_once_writing())
    worst = await _max_stall(task)
    # 0.6 s of terminate + close and a wedged write: none of it on the loop.
    assert worst < 0.2, worst
    # Done by the time pump_pty returns: terminated, input flushed in order,
    # then closed (only once the writer let go of the fd).
    assert proc.events == ["terminate", "close"]
    assert written == [b"typed", b"more"]
    os.close(r)
    os.close(w)


async def test_pump_pty_leaves_the_fd_open_while_the_writer_is_wedged(monkeypatch):
    r, w = os.pipe()
    proc = _StuckProc(r)
    proc.terminate = lambda force=False: proc.events.append("terminate")
    stuck = threading.Event()
    monkeypatch.setattr(terminal, "_WRITER_JOIN_S", 0.2)
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: stuck.wait(10))
    release = asyncio.Event()
    release.set()
    await terminal.pump_pty(
        _WS([{"type": "websocket.receive", "bytes": b"x"}], release), proc
    )
    stuck.set()
    assert proc.events == ["terminate"]  # never closed under a live write
    os.close(r)
    os.close(w)


async def test_pump_pty_hands_a_detaching_client_and_its_writer_to_reap(
    monkeypatch,
):
    r, w = os.pipe()
    inner = _StuckProc(r)
    reaped: list = []
    monkeypatch.setattr(
        terminal_ws, "reap", lambda proc, writer=None: reaped.append((proc, writer))
    )
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: len(data))
    release = asyncio.Event()
    release.set()
    await terminal.pump_pty(
        _WS([{"type": "websocket.receive", "bytes": b"x"}], release),
        terminal_ws.detaching(inner),
    )
    assert [p for p, _ in reaped] == [inner]
    assert isinstance(reaped[0][1], terminal_ws.PtyWriter)
    assert inner.events == []  # the reaper's job, not the pump's
    os.close(r)
    os.close(w)


async def test_a_read_only_pump_starts_no_writer_thread(monkeypatch):
    r, w = os.pipe()
    os.close(w)
    proc = _StuckProc(r)
    proc.terminate = lambda force=False: None
    proc.close = lambda force=True: None
    names: list = []
    real = threading.Thread.start

    def _start(self):
        names.append(self.name)
        return real(self)

    monkeypatch.setattr(threading.Thread, "start", _start)
    release = asyncio.Event()
    release.set()
    await terminal.pump_pty(
        _WS([{"type": "websocket.receive", "bytes": b"x"}], release),
        proc,
        allow_input=False,
    )
    assert "pty-writer" not in names
    assert names == ["pty-detach"]
    os.close(r)


# --------------------------------------------------------------------------- #
# core.pty_run: the plain-PTY login terminal's bridge and kill                 #
# --------------------------------------------------------------------------- #
class _SlowRunProc:
    def __init__(self, fd):
        self.fd = fd
        self.killed = threading.Event()

    def terminate(self, force=False):
        time.sleep(0.3)
        self.killed.set()
        return True

    def wait(self):
        return 0


def test_a_pty_run_kill_returns_at_once(monkeypatch):
    r, w = os.pipe()
    proc = _SlowRunProc(r)
    monkeypatch.setattr(pty_run, "_spawn", lambda argv, cwd, env: proc)
    run = pty_run.PtyRun(["x"], "/")
    t0 = time.monotonic()
    run.terminate()
    assert time.monotonic() - t0 < 0.1
    assert proc.killed.wait(5)
    os.close(w)  # EOF: the drain thread ends
    os.close(r)


async def test_a_pty_run_bridge_writes_off_the_loop(monkeypatch):
    r, w = os.pipe()
    proc = _SlowRunProc(r)
    monkeypatch.setattr(pty_run, "_spawn", lambda argv, cwd, env: proc)
    run = pty_run.PtyRun(["x"], "/")
    written: list = []
    unblock = threading.Event()

    def _blocking_write(fd, data):
        unblock.wait(10)  # the installer is not reading
        written.append(data)
        return len(data)

    monkeypatch.setattr(terminal.os, "write", _blocking_write)
    release = asyncio.Event()
    frames = [
        {"type": "websocket.receive", "bytes": b"y\r"},
        {"type": "websocket.receive", "text": "n"},
    ]
    task = asyncio.ensure_future(pty_run.bridge(_WS(frames, release), run))

    async def _later():
        await asyncio.sleep(0.2)
        release.set()

    asyncio.ensure_future(_later())
    assert await _max_stall(task) < 0.15
    unblock.set()
    for _ in range(200):
        if len(written) == 2:
            break
        await asyncio.sleep(0.01)
    assert written == [b"y\r", b"n"]  # in order, after the socket left
    monkeypatch.undo()
    os.close(w)
    os.close(r)
