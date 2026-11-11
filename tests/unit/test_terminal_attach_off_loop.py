"""A terminal websocket's attach never runs on the event loop (incident
2026-11-10: ``pty.fork()`` on the loop never returned and froze the server).

The websocket tests share ONE event loop between the socket and the plain
HTTP requests (a blocking portal handed to the TestClient, no lifespan), so
"another request was answered meanwhile" really means the loop was free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time

import anyio.from_thread
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.session import mux
from backend.web import server
from backend.web.core import terminal_ws as tw


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #
class _FakeProc:
    """A PTY client stand-in: the server reads/writes ``fd``; the test holds
    ``peer``."""

    def __init__(self) -> None:
        self.mine, self.peer = socket.socketpair()
        self.fd = self.mine.fileno()
        self.terminated = threading.Event()
        self.closed = threading.Event()

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        self.terminated.set()
        return True

    def close(self, force=True):
        self.closed.set()
        self.mine.close()


class _Inst:
    Program = "claude"

    def GetWorktreePath(self):
        return "/nonexistent/wt"


def _within(fn, timeout=5.0):
    """``fn()`` on a helper thread; fail (instead of hanging the suite) if it
    does not return in time — what a frozen loop looks like."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as err:  # noqa: BLE001
            box["error"] = err

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "no answer in %.1f s: the event loop is blocked" % timeout
    if "error" in box:
        raise box["error"]
    return box["value"]


# --------------------------------------------------------------------------- #
# the websocket                                                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def spawner(monkeypatch, tmp_path):
    """``mux.attach``'s spawn held until the test releases it; the test sees
    which thread it ran on and every client it produced."""
    from backend.web.core import guests, ownership

    state = {
        "started": threading.Event(),
        "release": threading.Event(),
        "threads": [],
        "procs": [],
        "make": _FakeProc,  # a test swaps in its own client
    }

    def spawn(argv, **kw):
        state["threads"].append(threading.current_thread().name)
        state["started"].set()
        state["release"].wait(30)
        state["procs"].append(state["make"]())
        return state["procs"][-1]

    monkeypatch.setattr(mux, "spawn_pty", spawn)
    monkeypatch.setattr(server.ENGINE, "instances", {"w": _Inst()})
    monkeypatch.setattr(server, "ensure_terminal_session", lambda i, t: ("mf_w", None))
    monkeypatch.setattr(
        server, "_ensure_shell_session", lambda t, wt: ("mf_w_sh", None)
    )
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_note_human_input", lambda *a, **k: None)
    monkeypatch.setattr(guests, "_live_instances", lambda: {})
    ownership._reset_for_tests()
    monkeypatch.setattr(ownership, "_path", lambda: str(tmp_path / "owners.json"))
    monkeypatch.setattr(ownership, "caller", lambda scope: ("", "laptop"))
    yield state
    state["release"].set()
    ownership._reset_for_tests()


@pytest.fixture
def one_loop():
    """A TestClient whose websockets and requests all run on ONE loop."""
    with anyio.from_thread.start_blocking_portal() as portal:
        client = TestClient(server.app)
        client.portal = portal
        yield client


def _answered(client) -> int:
    return _within(lambda: client.get("/api/instances/nope/prompt").status_code)


@pytest.mark.parametrize("pane", ["terminal", "shell"])
def test_the_attach_runs_on_a_worker_thread_while_requests_are_answered(
    spawner, one_loop, pane
):
    with one_loop.websocket_connect("/api/instances/w/" + pane) as ws:
        assert spawner["started"].wait(5)
        # The spawn is stuck — and the loop still answers.
        assert _answered(one_loop) == 404
        assert _answered(one_loop) == 404
        spawner["release"].set()
        assert _within(ws.receive_text)  # the who-drives frame: attached
    assert spawner["threads"] == ["pty-attach"]
    # Detached off the loop when the socket went.
    assert spawner["procs"][0].terminated.wait(5)


@pytest.mark.parametrize("pane", ["terminal", "shell"])
def test_a_spawn_that_hangs_past_the_timeout_closes_the_socket(
    spawner, one_loop, monkeypatch, pane
):
    monkeypatch.setattr(tw, "ATTACH_TIMEOUT_S", 1.0)
    with one_loop.websocket_connect("/api/instances/w/" + pane) as ws:
        assert spawner["started"].wait(5)
        # While it waits out the deadline, the loop still answers.
        assert _answered(one_loop) == 404
        # The reason comes first, as a frame, then the "try again later" close.
        frame = _within(ws.receive_json)
        assert frame["type"] == "error"
        assert "timed out" in frame["message"]
        assert "did not start" in frame["message"]
        with pytest.raises(WebSocketDisconnect) as closed:
            _within(ws.receive_text)
        assert closed.value.code == tw.TIMEOUT_CLOSE
    # The server is still there.
    assert _answered(one_loop) == 404
    # The spawn returns at last: its client is nobody's, so it is reaped.
    spawner["release"].set()
    deadline = time.time() + 5
    while not spawner["procs"] and time.time() < deadline:
        time.sleep(0.02)
    assert spawner["procs"][0].terminated.wait(5)
    assert spawner["procs"][0].closed.wait(5)


@pytest.mark.parametrize("pane", ["terminal", "shell"])
def test_a_failing_spawn_still_closes_with_its_error(
    spawner, one_loop, monkeypatch, pane
):
    def boom(argv, **kw):
        raise FileNotFoundError(
            "The command was not found or was not executable: tmux."
        )

    monkeypatch.setattr(mux, "spawn_pty", boom)
    with one_loop.websocket_connect("/api/instances/w/" + pane) as ws:
        frame = _within(ws.receive_json)
        assert frame == {
            "type": "error",
            "message": "The command was not found or was not executable: tmux.",
        }
        with pytest.raises(WebSocketDisconnect) as closed:
            _within(ws.receive_text)
        assert closed.value.code == 4500


def test_keystrokes_reach_the_client_through_the_writer(spawner, one_loop):
    spawner["release"].set()
    with one_loop.websocket_connect("/api/instances/w/terminal") as ws:
        _within(ws.receive_text)
        ws.send_bytes(b"\x1b[1;1R")  # a terminal reply: always written
        proc = spawner["procs"][0]
        proc.peer.settimeout(5)
        assert proc.peer.recv(64) == b"\x1b[1;1R"


def test_a_guests_keys_go_by_send_keys_and_terminal_replies_by_the_writer(
    spawner, one_loop, monkeypatch
):
    from backend.web.core import guest_terminal, guests

    typed: list = []
    got_keys = threading.Event()

    def send_keys(name, data):
        typed.append((name, data))
        got_keys.set()

    monkeypatch.setattr(guests, "scope_guest", lambda ws: {"label": "guest"})
    monkeypatch.setattr(guest_terminal, "send_keys", send_keys)
    spawner["release"].set()
    with one_loop.websocket_connect("/api/instances/w/terminal") as ws:
        _within(ws.receive_text)
        proc = spawner["procs"][0]
        proc.peer.settimeout(5)
        ws.send_bytes(b"ls\r")  # a person typing: send-keys, never the client
        assert got_keys.wait(5)
        assert typed == [("mf_w", b"ls\r")]
        ws.send_bytes(b"\x1b[1;1R")  # tmux's probe answered: the client
        assert proc.peer.recv(64) == b"\x1b[1;1R"
    assert spawner["threads"] == ["pty-attach"]


class _SlowProc(_FakeProc):
    """ptyprocess's sleeps, exaggerated: a detach ON the loop would hold it."""

    events: list = []

    def terminate(self, force=False):
        time.sleep(1.0)
        _SlowProc.events.append("terminate")
        return super().terminate(force)

    def close(self, force=True):
        time.sleep(1.0)
        _SlowProc.events.append("close")
        super().close(force)


def test_the_terminal_detaches_through_reap_off_the_loop(
    spawner, one_loop, monkeypatch
):
    reaped: list = []
    real = tw.reap
    monkeypatch.setattr(
        tw,
        "reap",
        lambda proc, writer=None: (reaped.append((proc, writer)), real(proc, writer)),
    )
    monkeypatch.setattr(_SlowProc, "events", [])
    spawner["make"] = _SlowProc
    spawner["release"].set()
    t0 = time.monotonic()
    with one_loop.websocket_connect("/api/instances/w/terminal") as ws:
        _within(ws.receive_text)
        t0 = time.monotonic()
    # Two seconds of terminate + close, and the loop answers at once.
    assert _within(lambda: _answered(one_loop), timeout=1.5) == 404
    assert time.monotonic() - t0 < 1.5
    proc = spawner["procs"][0]
    assert proc.closed.wait(5)
    assert _SlowProc.events == ["terminate", "close"]
    assert len(reaped) == 1
    assert reaped[0][0] is proc
    assert isinstance(reaped[0][1], tw.PtyWriter)


def test_the_shell_detaches_through_reap_off_the_loop(spawner, one_loop, monkeypatch):
    reaped: list = []
    real = tw.reap
    monkeypatch.setattr(
        tw,
        "reap",
        lambda proc, writer=None: (reaped.append((proc, writer)), real(proc, writer)),
    )
    monkeypatch.setattr(_SlowProc, "events", [])
    spawner["make"] = _SlowProc
    spawner["release"].set()
    with one_loop.websocket_connect("/api/instances/w/shell") as ws:
        _within(ws.receive_text)
        t0 = time.monotonic()
    assert _within(lambda: _answered(one_loop), timeout=1.5) == 404
    assert time.monotonic() - t0 < 1.5
    proc = spawner["procs"][0]
    assert proc.closed.wait(5)
    assert _SlowProc.events == ["terminate", "close"]
    # pump_pty's proc.terminate(force=True) went through detaching: reap got
    # the client itself, not the wrapper.
    assert [p for p, _ in reaped] == [proc]


# --------------------------------------------------------------------------- #
# _attach_pty                                                                  #
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self, gone: bool = False) -> None:
        self.gone = gone
        self.frames: list = []
        self.closed = None

    async def send_text(self, text):
        if self.gone:
            raise RuntimeError("the browser left")
        self.frames.append(json.loads(text))

    async def close(self, code=1000):
        if self.gone:
            raise RuntimeError("the browser left")
        self.closed = code


@pytest.mark.parametrize("guest", [False, True])
def test_attach_pty_spawns_the_right_client_on_a_worker_thread(monkeypatch, guest):
    calls: list = []

    def attach(name, **kw):
        calls.append(("attach", name, kw, threading.current_thread().name))
        return "owner-proc"

    def readonly(name, rows, cols):
        calls.append(("readonly", name, (rows, cols), threading.current_thread().name))
        return "guest-proc"

    monkeypatch.setattr(server.mux, "attach", attach)
    monkeypatch.setattr(server._guest_terminal, "spawn_readonly_attach", readonly)
    ws = _FakeWS()
    got = asyncio.run(server._attach_pty(ws, "mf_w", guest))
    if guest:
        assert got == "guest-proc"
        assert calls == [("readonly", "mf_w", (24, 80), "pty-attach")]
    else:
        assert got == "owner-proc"
        assert calls == [("attach", "mf_w", {"dimensions": (24, 80)}, "pty-attach")]
    assert ws.frames == [] and ws.closed is None


@pytest.mark.parametrize("guest", [False, True])
@pytest.mark.parametrize(
    "err, code",
    [
        (tw.AttachTimeout("terminal attach timed out after 15 s: busy"), 1013),
        (OSError("out of ptys"), 4500),
    ],
)
def test_attach_pty_maps_a_timeout_to_1013_and_any_other_failure_to_4500(
    monkeypatch, guest, err, code
):
    async def failing(spawn, timeout=None):
        raise err

    monkeypatch.setattr(tw, "attach_off_loop", failing)
    ws = _FakeWS()
    assert asyncio.run(server._attach_pty(ws, "mf_w", guest)) is None
    assert ws.frames == [{"type": "error", "message": str(err)}]
    assert ws.closed == code
    assert tw.TIMEOUT_CLOSE == 1013


def test_attach_pty_shrugs_off_a_browser_that_already_left(monkeypatch):
    async def failing(spawn, timeout=None):
        raise tw.AttachTimeout("timed out")

    monkeypatch.setattr(tw, "attach_off_loop", failing)
    assert asyncio.run(server._attach_pty(_FakeWS(gone=True), "mf_w", False)) is None


# --------------------------------------------------------------------------- #
# attach_off_loop                                                              #
# --------------------------------------------------------------------------- #
def test_the_concurrency_cap(monkeypatch):
    monkeypatch.setattr(tw, "_SLOTS", threading.BoundedSemaphore(2))
    lock = threading.Lock()
    now = {"in": 0, "peak": 0}

    def spawn():
        with lock:
            now["in"] += 1
            now["peak"] = max(now["peak"], now["in"])
        time.sleep(0.1)
        with lock:
            now["in"] -= 1
        return "proc"

    async def main():
        return await asyncio.gather(*(tw.attach_off_loop(spawn) for _ in range(7)))

    assert asyncio.run(main()) == ["proc"] * 7
    assert now["peak"] == 2


def test_waiting_for_a_slot_counts_against_the_timeout(monkeypatch):
    monkeypatch.setattr(tw, "_SLOTS", threading.BoundedSemaphore(1))
    hold = threading.Event()
    ran = []

    def stuck():
        hold.wait(10)
        return _FakeProc()

    def quick():
        ran.append(1)
        return _FakeProc()

    async def main():
        first = asyncio.ensure_future(tw.attach_off_loop(stuck, timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(tw.AttachTimeout, match="busy starting other terminals"):
            await tw.attach_off_loop(quick, timeout=0.3)
        hold.set()
        return await first

    proc = asyncio.run(main())
    assert isinstance(proc, _FakeProc)
    time.sleep(0.2)
    assert ran == []  # its waiter gave up before it got a slot: never spawned


def test_the_loop_keeps_running_while_a_spawn_blocks():
    hold = threading.Event()

    async def main():
        task = asyncio.ensure_future(
            tw.attach_off_loop(lambda: hold.wait(10) and "proc")
        )
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1
        hold.set()
        return ticks, await task

    assert asyncio.run(main()) == (10, "proc")


def test_a_cancelled_attach_reaps_the_client_that_comes_later():
    hold = threading.Event()
    proc = _FakeProc()

    async def main():
        task = asyncio.ensure_future(tw.attach_off_loop(lambda: hold.wait(10) and proc))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    hold.set()
    assert proc.terminated.wait(5)
    assert proc.closed.wait(5)


def test_the_spawns_own_error_is_raised():
    def boom():
        raise OSError("no pty")

    async def main():
        await tw.attach_off_loop(boom)

    with pytest.raises(OSError, match="no pty"):
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# writes and detach                                                            #
# --------------------------------------------------------------------------- #
def test_a_write_to_a_client_that_is_not_reading_never_blocks():
    proc = _FakeProc()
    proc.mine.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    writer = tw.PtyWriter(proc)
    big = bytes(range(256)) * 4096  # 1 MiB: far past any socket buffer
    t0 = time.monotonic()
    writer.write(big)
    writer.write(b"END")
    assert time.monotonic() - t0 < 0.1
    got = b""
    proc.peer.settimeout(5)
    while not got.endswith(b"END"):
        got += proc.peer.recv(65536)
    assert got == big + b"END"  # every byte, in order
    writer.close()
    assert writer.join(5)


def test_reap_returns_at_once_and_closes_after_the_writer():
    proc = _FakeProc()
    slow = proc.terminate

    def terminate(force=False):
        time.sleep(0.3)  # ptyprocess sleeps 0.1 s per signal it tries
        return slow(force)

    proc.terminate = terminate
    writer = tw.PtyWriter(proc)
    writer.write(b"x")
    t0 = time.monotonic()
    tw.reap(proc, writer)
    assert time.monotonic() - t0 < 0.05
    assert proc.closed.wait(5)
    assert proc.terminated.is_set()
    assert writer.join(1)


def test_detaching_passes_through_and_terminates_off_loop():
    proc = _FakeProc()
    wrapped = tw.detaching(proc)
    assert wrapped.fd == proc.fd
    wrapped.setwinsize(10, 20)
    assert wrapped.terminate(force=True) is True
    assert proc.terminated.wait(5)
    assert proc.closed.wait(5)


# --------------------------------------------------------------------------- #
# attach_off_loop: slots, phases, give-ups                                     #
# --------------------------------------------------------------------------- #
def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def _free(slots) -> int:
    """How many slots are free right now (taken and given straight back)."""
    n = 0
    while slots.acquire(blocking=False):
        n += 1
    for _ in range(n):
        slots.release()
    return n


def _attach_threads(before) -> list:
    return [
        t
        for t in threading.enumerate()
        if t.name == "pty-attach" and t not in before and t.is_alive()
    ]


@pytest.fixture
def slots(monkeypatch):
    sem = threading.BoundedSemaphore(tw.MAX_CONCURRENT_ATTACHES)
    monkeypatch.setattr(tw, "_SLOTS", sem)
    return sem


def test_a_waiter_cancelled_while_queued_never_spawns_and_frees_its_slot(slots):
    held = [slots.acquire(blocking=False) for _ in range(tw.MAX_CONCURRENT_ATTACHES)]
    assert all(held)  # other spawns hold every slot
    ran = []
    before = set(threading.enumerate())

    async def main():
        task = asyncio.ensure_future(
            tw.attach_off_loop(lambda: ran.append(1) or _FakeProc(), timeout=5)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    queued = _attach_threads(before)
    assert len(queued) == 1
    slots.release()  # a holder finishes: the queued thread gets the slot …
    queued[0].join(5)
    assert not queued[0].is_alive()
    for _ in range(tw.MAX_CONCURRENT_ATTACHES - 1):
        slots.release()
    assert ran == []  # … sees its waiter left, never spawns, and gives it back
    assert _free(slots) == tw.MAX_CONCURRENT_ATTACHES


def test_a_spawn_that_raises_gives_its_slot_back(slots):
    def boom():
        raise OSError("no pty")

    async def main():
        for _ in range(tw.MAX_CONCURRENT_ATTACHES + 1):
            with pytest.raises(OSError, match="no pty"):
                await tw.attach_off_loop(boom, timeout=2)

    asyncio.run(main())  # none of them timed out waiting for a slot
    _until(lambda: _free(slots) == tw.MAX_CONCURRENT_ATTACHES)


def test_a_timeout_says_which_phase_it_was_in(monkeypatch):
    monkeypatch.setattr(tw, "_SLOTS", threading.BoundedSemaphore(1))
    hold = threading.Event()
    made = []

    def stuck():
        hold.wait(10)
        made.append(_FakeProc())
        return made[-1]

    async def main():
        with pytest.raises(tw.AttachTimeout) as spawning:
            await tw.attach_off_loop(stuck, timeout=0.3)
        # The stuck spawn still holds the only slot: the next one queues.
        with pytest.raises(tw.AttachTimeout) as queued:
            await tw.attach_off_loop(_FakeProc, timeout=0.3)
        return spawning.value, queued.value

    try:
        spawning, queued = asyncio.run(main())
    finally:
        hold.set()
    assert "timed out" in str(spawning)
    assert "the terminal client did not start" in str(spawning)
    assert "the server is busy starting other terminals" in str(queued)
    for err in (spawning, queued):
        assert err.__cause__ is None
        assert err.__suppress_context__  # raised "from None"
    _until(lambda: bool(made))
    assert made[0].terminated.wait(5)  # the late client was nobody's


class _Bail(BaseException):
    pass


def test_a_spawn_raising_a_base_exception_hands_it_over_and_frees_the_slot(slots):
    def bail():
        raise _Bail("out")

    async def main():
        await tw.attach_off_loop(bail, timeout=2)

    with pytest.raises(_Bail):
        asyncio.run(main())
    _until(lambda: _free(slots) == tw.MAX_CONCURRENT_ATTACHES)


def test_a_client_that_starts_after_its_loop_closed_is_reaped(slots):
    hold = threading.Event()
    proc = _FakeProc()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(
            tw.attach_off_loop(lambda: hold.wait(10) and proc, timeout=5)
        )
        loop.run_until_complete(asyncio.sleep(0.05))  # spawning now
    finally:
        loop.close()  # the waiter never gave up: _post finds the loop gone
    hold.set()
    assert proc.terminated.wait(5)
    assert proc.closed.wait(5)
    _until(lambda: _free(slots) == tw.MAX_CONCURRENT_ATTACHES)
    task._log_destroy_pending = False
    task.get_coro().close()


def test_a_zero_timeout_gives_up_at_once_and_leaks_nothing(slots):
    hold = threading.Event()
    made = []

    def spawn():
        hold.wait(10)
        made.append(_FakeProc())
        return made[-1]

    async def main():
        await tw.attach_off_loop(spawn, timeout=0)

    t0 = time.monotonic()
    try:
        with pytest.raises(tw.AttachTimeout):
            asyncio.run(main())
        assert time.monotonic() - t0 < 1.0
    finally:
        hold.set()
    _until(lambda: _free(slots) == tw.MAX_CONCURRENT_ATTACHES)
    for p in made:  # if it got as far as spawning, its client is reaped
        assert p.terminated.wait(5)


def test_stuck_spawns_keep_their_slots_and_later_attaches_do_not_pile_up(slots):
    hold = threading.Event()
    before = set(threading.enumerate())

    def stuck():
        hold.wait(30)
        return _FakeProc()

    async def main():
        stuck_ones = [
            asyncio.ensure_future(tw.attach_off_loop(stuck, timeout=0.2))
            for _ in range(tw.MAX_CONCURRENT_ATTACHES)
        ]
        await asyncio.sleep(0.05)
        for _ in range(3):
            with pytest.raises(tw.AttachTimeout, match="busy starting other terminals"):
                await tw.attach_off_loop(_FakeProc, timeout=0.2)
        for got in await asyncio.gather(*stuck_ones, return_exceptions=True):
            assert isinstance(got, tw.AttachTimeout)

    try:
        asyncio.run(main())
        # The queued threads left with their waiters; only the stuck remain.
        _until(lambda: len(_attach_threads(before)) == tw.MAX_CONCURRENT_ATTACHES)
        assert _free(slots) == 0
    finally:
        hold.set()
    _until(lambda: not _attach_threads(before))
    _until(lambda: _free(slots) == tw.MAX_CONCURRENT_ATTACHES)


# --------------------------------------------------------------------------- #
# PtyWriter, _terminate, detaching                                             #
# --------------------------------------------------------------------------- #
def test_many_small_writes_arrive_in_order():
    proc = _FakeProc()
    writer = tw.PtyWriter(proc)
    want = b"".join(b"%05d," % i for i in range(3000))
    for i in range(3000):
        writer.write(b"%05d," % i)
    got = b""
    proc.peer.settimeout(5)
    while len(got) < len(want):
        got += proc.peer.recv(65536)
    assert got == want
    writer.close()
    assert writer.join(5)


def test_a_writer_whose_client_is_gone_stops_and_drops_later_writes(monkeypatch):
    import errno

    calls = []

    def gone(proc, data):
        calls.append(data)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(tw, "pty_write", gone)
    writer = tw.PtyWriter(_FakeProc())
    writer.write(b"a")
    thread = writer._thread
    assert writer.join(5)
    assert writer._closed
    writer.write(b"b")  # dropped: no new thread, no write
    assert writer._thread is thread and not thread.is_alive()
    assert calls == [b"a"]


def test_a_writer_closed_before_any_write_never_starts_a_thread():
    writer = tw.PtyWriter(_FakeProc())
    writer.close()
    assert writer.join(0)
    writer.write(b"late")
    assert writer._thread is None


class _BlockedWriter:
    def __init__(self) -> None:
        self.closed = False
        self.waited = None

    def close(self):
        self.closed = True

    def join(self, timeout):
        self.waited = timeout
        return False  # still stuck in a write


def test_terminate_leaves_the_fd_open_while_the_writer_is_blocked(caplog):
    proc = _FakeProc()
    writer = _BlockedWriter()
    with caplog.at_level(logging.WARNING, logger="mindflock.terminal"):
        tw._terminate(proc, writer)
    assert proc.terminated.is_set()
    assert writer.closed and writer.waited == 2.0
    assert not proc.closed.is_set()  # a reused fd number never gets its write
    assert "still blocked" in caplog.text


def test_terminate_swallows_a_failing_client_and_needs_no_close():
    class Broken:
        def terminate(self, force=False):
            raise OSError("already gone")

        def close(self, force=True):
            raise OSError("already closed")

    class NoClose:
        def __init__(self):
            self.terminated = False

        def terminate(self, force=False):
            self.terminated = True

    tw._terminate(Broken())
    no_close = NoClose()
    tw._terminate(no_close)
    assert no_close.terminated


def test_detaching_reaps_off_loop_for_a_gentle_terminate_too():
    proc = _FakeProc()
    proc.isalive = lambda: True
    wrapped = tw.detaching(proc)
    assert wrapped.isalive() is True
    assert wrapped.fd == proc.fd
    t0 = time.monotonic()
    assert wrapped.terminate() is True  # force=False: still a full detach
    assert time.monotonic() - t0 < 0.05
    assert proc.terminated.wait(5)
    assert proc.closed.wait(5)
