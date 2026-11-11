"""Regressions from the 0.14.7 review of the spawn and git-index code.

1. ``scratch_index_env`` copied the index with ``shutil.copyfile``: the copy's
   fresh mtime switched off git's racy-git check, so a same-size edit made in
   the same second as the last ``git add`` vanished from every scratch diff.
2. With ``core.splitIndex`` (or ``GIT_TEST_SPLIT_INDEX``) on, the scratch
   ``git add -N .`` wrote a new ``sharedindex.<sha>`` into the REAL gitdir on
   every poll.
3. ``os.posix_spawn`` holds the GIL until the child execs; the PTY spawn now
   goes through libc's own ``posix_spawn`` (ctypes, GIL released).
4. The Assistant pane, the ticket log stream and the helper terminals
   (``pty_run.serve``) spawned their PTY client ON the event loop.
5. ``posix_spawn`` passed every INHERITABLE fd to the child (ptyprocess
   closed every fd ≥ 3).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import anyio.from_thread
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from backend.session import mux
from backend.session.git.diff import scratch_index_env

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux")
needs_posix_spawn = pytest.mark.skipif(
    not mux._POSIX_SPAWN_PTY, reason="Linux posix_spawn PTY"
)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _git(repo, *args, env=None):
    cp = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )
    return cp


def _repo(tmp_path, name="r"):
    r = tmp_path / name
    r.mkdir()
    assert _git(r, "init", "-q").returncode == 0
    _git(r, "config", "user.name", "a")
    _git(r, "config", "user.email", "a@b")
    return r


def _commit(r):
    assert _git(r, "commit", "-qm", "c").returncode == 0


def _scratch_names(r, *args):
    with scratch_index_env(str(r)) as env:
        return _git(r, "diff", "--name-only", *args, env=env).stdout.split()


# --------------------------------------------------------------------------- #
# 1. racy-git: the scratch copy keeps the index's mtime                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("untracked", [True, False])
def test_a_racily_clean_edit_shows_in_the_scratch_diff(tmp_path, untracked):
    """``f`` is staged, then rewritten with the same size and (as within one
    second) the same mtime: only the racy-git check — entry mtime ≥ index
    mtime — can tell it changed. Timestamps are pinned, so no sleeping and
    no flake: ctime is left out of the stat check (``trustctime=false``,
    ``checkStat=minimal``), which leaves size + mtime, both unchanged."""
    r = _repo(tmp_path)
    _git(r, "config", "core.trustctime", "false")
    _git(r, "config", "core.checkStat", "minimal")
    then = time.time_ns() - 1000 * 10**9  # well before "now" (the copy's)
    f = r / "f"
    f.write_text("aaaa\n")
    os.utime(f, ns=(then, then))
    _git(r, "add", "f")
    _commit(r)
    f.write_text("bbbb\n")  # same size …
    os.utime(f, ns=(then, then))  # … same mtime as the index entry
    index = r / ".git" / "index"
    os.utime(index, ns=(then, then))  # the index written that same instant
    if untracked:
        (r / "untracked").write_text("u\n")  # add -N rewrites the scratch copy

    scratch = _scratch_names(r, "HEAD")

    assert "f" in scratch
    if untracked:
        assert "untracked" in scratch
    # The real index was never touched.
    assert os.stat(index).st_mtime_ns == then


# --------------------------------------------------------------------------- #
# 2. split index: nothing new in the real gitdir                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("how", ["config", "env"])
def test_a_split_index_repo_gets_no_new_sharedindex_from_polls(
    tmp_path, monkeypatch, how
):
    r = _repo(tmp_path)
    if how == "config":
        _git(r, "config", "core.splitIndex", "true")
    else:
        monkeypatch.setenv("GIT_TEST_SPLIT_INDEX", "1")
    for i in range(200):
        (r / ("f%d" % i)).write_text("%d\n" % i)
    _git(r, "add", ".")
    _commit(r)
    gitdir = r / ".git"
    before = sorted(p.name for p in gitdir.iterdir())
    assert any(n.startswith("sharedindex.") for n in before), before
    index_mtime = os.stat(gitdir / "index").st_mtime_ns
    for i in range(60):
        (r / ("new%d" % i)).write_text("n\n")

    for _ in range(4):  # several polls
        names = _scratch_names(r, "HEAD")
        assert "new0" in names and "new59" in names  # still the add -N diff

    assert sorted(p.name for p in gitdir.iterdir()) == before
    assert os.stat(gitdir / "index").st_mtime_ns == index_mtime


# --------------------------------------------------------------------------- #
# 3. the spawn releases the GIL                                                 #
# --------------------------------------------------------------------------- #
@needs_posix_spawn
def test_a_blocking_spawn_does_not_freeze_other_threads(tmp_path, monkeypatch):
    """The child's tty open is a FIFO opened read-only: it blocks (and glibc
    holds the spawning thread in vfork) until a writer turns up 0.6 s later.
    Another Python thread must keep running meanwhile."""
    fifo = str(tmp_path / "fifo")
    os.mkfifo(fifo)
    monkeypatch.setattr(mux, "_TTY_OPEN_FLAGS", os.O_RDONLY)
    ticks = [0]
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            ticks[0] += 1
            time.sleep(0.001)

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    time.sleep(0.05)
    writer = subprocess.Popen(["sh", "-c", "sleep 0.6; exec 3>'%s'" % fifo])
    try:
        before = ticks[0]
        t0 = time.monotonic()
        pid = mux._spawn_session_leader(
            "/bin/true", ["/bin/true"], dict(os.environ), fifo
        )
        took = time.monotonic() - t0
        during = ticks[0] - before
    finally:
        stop.set()
        t.join(5)
        writer.wait(5)
    os.waitpid(pid, 0)
    assert took > 0.4, "the spawn did not block on the FIFO: %.2f s" % took
    # ~0.6 s of 1 ms sleeps: hundreds of ticks; a held GIL gives ~0.
    assert during > 50, "other threads froze during the spawn (%d ticks)" % during


@needs_posix_spawn
def test_on_linux_the_spawn_always_goes_through_libc(monkeypatch):
    """Even on a CPython whose ``os.posix_spawn`` has ``setsid=``."""
    calls: list = []
    real = mux._libc_spawn_session_leader

    def _libc(path, argv, env, tty):
        calls.append(argv)
        return real(path, argv, env, tty)

    def _no_os_spawn(*a, **k):
        raise AssertionError("os.posix_spawn with libc usable")

    monkeypatch.setattr(mux, "_libc_spawn_session_leader", _libc)
    monkeypatch.setattr(os, "posix_spawn", _no_os_spawn)
    proc = mux.spawn_pty([sys.executable, "-c", "print('ok')"])
    try:
        assert b"ok" in _read_all(proc.fd)
    finally:
        proc.close(force=True)
    assert len(calls) == 1


@needs_posix_spawn
def test_without_a_usable_libc_os_posix_spawn_is_the_fallback(monkeypatch):
    spawns: list = []
    real = os.posix_spawn

    def _spy(*a, **k):
        spawns.append(k)
        return real(*a, **k)

    monkeypatch.setattr(mux, "_LIBC", mux._NoLibc("no ctypes here"))
    monkeypatch.setattr(os, "posix_spawn", _spy)
    proc = mux.spawn_pty([sys.executable, "-c", "print('ok')"])
    try:
        assert b"ok" in _read_all(proc.fd)
    finally:
        proc.close(force=True)
    assert len(spawns) == 1 and spawns[0]["setsid"] is True


# --------------------------------------------------------------------------- #
# 5. no inheritable fd reaches the child                                        #
# --------------------------------------------------------------------------- #
def _read_all(fd, timeout=10.0) -> bytes:
    import select

    buf = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            d = os.read(fd, 4096)
        except OSError:  # EIO: the child is gone
            break
        if not d:
            break
        buf += d
    return buf


_LIST_FDS = "import os; print('FDS', ' '.join(sorted(os.listdir('/proc/self/fd'))))"


def _child_fds(argv=None) -> set:
    proc = mux.spawn_pty(argv or [sys.executable, "-c", _LIST_FDS])
    try:
        out = _read_all(proc.fd).decode()
    finally:
        proc.close(force=True)
    for line in out.splitlines():
        if line.startswith("FDS"):
            return {int(x) for x in line.split()[1:]}
    raise AssertionError(out)


@needs_posix_spawn
@pytest.mark.parametrize("path", ["libc-closefrom", "libc-per-fd", "os-posix-spawn"])
def test_an_inheritable_fd_never_reaches_the_child(monkeypatch, path):
    if path == "libc-per-fd":  # a glibc < 2.34 (no addclosefrom_np)
        monkeypatch.setattr(mux, "_LIBC_CLOSEFROM", "mf_no_such_symbol")
    elif path == "os-posix-spawn":
        monkeypatch.setattr(mux, "_LIBC", mux._NoLibc("no ctypes here"))
    rd, wr = os.pipe()
    os.set_inheritable(wr, True)
    try:
        fds = _child_fds()
    finally:
        os.close(rd)
        os.close(wr)
    assert wr not in fds, fds
    # 0/1/2 (the PTY) plus the listdir's own fd: nothing else of ours.
    assert {0, 1, 2} <= fds and len(fds) <= 4, fds


@linux
def test_inheritable_fds_lists_only_inheritable_ones_above_2():
    rd, wr = os.pipe()
    os.set_inheritable(wr, True)
    try:
        fds = mux._inheritable_fds()
    finally:
        os.close(rd)
        os.close(wr)
    assert wr in fds and rd not in fds
    assert all(fd > 2 for fd in fds)


# --------------------------------------------------------------------------- #
# 4. the remaining PTY spawns run off the event loop                            #
# --------------------------------------------------------------------------- #
class _Blocker:
    """A spawn held until the test releases it; records its thread."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.threads: list = []

    def __call__(self, *a, **k):
        self.threads.append(threading.current_thread().name)
        self.started.set()
        self.release.wait(30)
        return "proc"


def _within(fn, timeout=5.0):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as err:  # noqa: BLE001
            box["error"] = err

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    assert not th.is_alive(), "no answer in %.1f s: the event loop is blocked" % timeout
    if "error" in box:
        raise box["error"]
    return box["value"]


async def _fake_pump(ws, proc, allow_input=True, **kw):
    await ws.send_json({"type": "bridged", "proc": proc})


def _one_loop(app):
    """A TestClient whose websockets and requests share ONE loop."""
    portal_cm = anyio.from_thread.start_blocking_portal()
    portal = portal_cm.__enter__()
    client = TestClient(app)
    client.portal = portal
    return client, portal_cm


def _with_ping(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def _assert_off_loop(app, ws_path, blocker):
    client, portal_cm = _one_loop(app)
    try:
        with client.websocket_connect(ws_path) as ws:
            assert blocker.started.wait(5), "the spawn never ran"
            # The spawn is stuck — and the loop still answers.
            assert _within(lambda: client.get("/ping").status_code) == 200
            blocker.release.set()
            assert _within(ws.receive_json) == {"type": "bridged", "proc": "proc"}
        assert blocker.threads == ["pty-attach"]
    finally:
        blocker.release.set()
        portal_cm.__exit__(None, None, None)


def test_the_assistant_pane_spawns_off_the_loop(monkeypatch):
    from backend.web.addons import assistant

    blocker = _Blocker()
    monkeypatch.setattr(assistant, "_ensure_assistant_session", lambda: ("mf_a", None))
    monkeypatch.setattr(assistant, "spawn_tmux_attach", blocker)
    monkeypatch.setattr(assistant, "pump_pty", _fake_pump)
    app = _with_ping(assistant.AssistantAddon(None).router)
    _assert_off_loop(app, "/api/assistant/terminal", blocker)


def test_the_ticket_log_stream_spawns_off_the_loop(monkeypatch, tmp_path):
    from backend.web.addons import ticket_ingestion as ti

    blocker = _Blocker()
    monkeypatch.setattr(ti, "spawn_tail", blocker)
    monkeypatch.setattr(ti, "pump_pty", _fake_pump)
    addon = ti.TicketIngestionAddon.__new__(ti.TicketIngestionAddon)

    class _Ctrl:
        def log_path(self):
            return tmp_path / "logs" / "mindflock.log"

    addon.ctrl = _Ctrl()
    app = _with_ping(addon._build_router())
    _assert_off_loop(app, "/api/mindflock/logs", blocker)


def test_a_helper_terminal_spawns_off_the_loop(monkeypatch):
    from backend.web.core import pty_run
    from backend.web.core import terminal as web_terminal

    blocker = _Blocker()
    monkeypatch.setattr(pty_run, "get", lambda session: None)  # tmux, no PtyRun
    monkeypatch.setattr(web_terminal, "spawn_tmux_attach", blocker)
    monkeypatch.setattr(web_terminal, "pump_pty", _fake_pump)

    from fastapi import APIRouter

    router = APIRouter()

    @router.websocket("/helper")
    async def helper(ws: WebSocket) -> None:
        await ws.accept()
        await pty_run.serve(ws, "mf_helper")

    _assert_off_loop(_with_ping(router), "/helper", blocker)


def test_a_helper_terminal_spawn_error_still_closes_4500(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from backend.web.core import pty_run
    from backend.web.core import terminal as web_terminal

    def boom(*a, **k):
        raise FileNotFoundError("no tmux")

    monkeypatch.setattr(pty_run, "get", lambda session: None)
    monkeypatch.setattr(web_terminal, "spawn_tmux_attach", boom)

    from fastapi import APIRouter

    router = APIRouter()

    @router.websocket("/helper")
    async def helper(ws: WebSocket) -> None:
        await ws.accept()
        await pty_run.serve(ws, "mf_helper")

    client = TestClient(_with_ping(router))
    with client.websocket_connect("/helper") as ws:
        assert _within(ws.receive_json) == {"type": "error", "message": "no tmux"}
        with pytest.raises(WebSocketDisconnect) as closed:
            _within(ws.receive_text)
        assert closed.value.code == 4500
