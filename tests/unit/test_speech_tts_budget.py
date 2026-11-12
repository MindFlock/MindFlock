"""``core.speech.TtsChain``'s host budget against an OpenAI-compatible
voice host: a slow-but-alive host is never marked down, our lines go to a
serial host one at a time (its lane), time spent behind our own lines does
not eat a line's budget, and a dead host (refused, 5xx, reset, hung) is
still marked down at once.

Voice Lab's F5 renders one request at a time and its first byte comes after
the whole clip (synth ≈ max(1.4 s, 0.52 × audio)); :class:`_FakeHost`
models that with a floor per line, optionally serial.

Every budget here races a host's answer, so all of it runs on a VIRTUAL
clock (:class:`_VirtualClock`, handed to the chain as its
:class:`speech.LaneClock`): the fake host renders in virtual seconds and a
line gives up in virtual seconds, and time only moves once every thread of
the test is blocked on that clock — so the outcome depends on the budgets
and the host's floor alone, never on how loaded the runner is. A DEAD host
(5xx, reset) is a real one on loopback (port band 7230-7259): the
connection failing is what is under test there, and it races nothing.
"""

from __future__ import annotations

import itertools
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Callable, List, Optional

import pytest

from backend import memory_pressure
from backend.config import settings as S
from backend.web.core import speech

PORTS = range(7230, 7260)


# --------------------------------------------------------------------------- #
# The virtual clock
# --------------------------------------------------------------------------- #
class _Wait:
    """One thread blocked on the clock: until ``deadline`` (virtual; None =
    only a notify ends it), or a notify. ``notify`` wakes a waiter that
    sleeps on a condition of its own (a :class:`_VCond`); ``on_exit``: a
    join, woken whenever a clock thread ends."""

    __slots__ = ("deadline", "seq", "notify", "woken", "notified", "on_exit")

    def __init__(self, deadline: Optional[float], seq: int, notify) -> None:
        self.deadline, self.seq, self.notify = deadline, seq, notify
        self.woken = self.notified = self.on_exit = False


class _Thread:
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = False


class _VirtualClock(speech.LaneClock):
    """Virtual seconds for a test (:class:`speech.LaneClock`'s seam).

    Every thread that takes part is an ACTOR: the test's own (counted from
    the start), and each one started by :meth:`spawn` — the lines asked at
    once, the lane's drain (``speech._lane_thread``), the fake host's
    renders. An actor blocked on this clock (:meth:`sleep`, :meth:`join`,
    a :meth:`condition`'s wait) is idle; a notify makes its waiters busy
    again at once (before they even run). Time moves only while no actor is
    busy, and then straight to the earliest deadline, waking that ONE
    waiter (ties in the order they blocked). So events happen in the order
    of their virtual times, one at a time, whatever the runner's load."""

    def __init__(self, start: float = 100.0) -> None:
        self._lock = threading.Lock()
        self._kick = threading.Condition(self._lock)  # the advancer's
        self._ring = threading.Condition(self._lock)  # sleep / join waiters'
        self._t = start
        self._busy = 1  # the test's own thread
        self._live = 0  # spawned threads not yet done
        self._waits: List[_Wait] = []
        self._seq = itertools.count()
        self._closed = False
        threading.Thread(
            target=self._advance, name="virtual-clock", daemon=True
        ).start()

    # -- LaneClock ------------------------------------------------------- #
    def now(self) -> float:
        return self._t

    def condition(self, lock) -> "_VCond":
        return _VCond(self, lock)

    # -- actors ---------------------------------------------------------- #
    def spawn(self, target: Callable[[], object], name: str = "actor") -> _Thread:
        handle = _Thread()
        with self._lock:
            self._busy += 1
            self._live += 1

        def run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    handle.done = True
                    self._live -= 1
                    self._busy -= 1
                    for w in [w for w in self._waits if w.on_exit]:
                        self._wake(w)  # a join: busy again before we are not
                    self._ring.notify_all()
                    if self._busy == 0:
                        self._kick.notify()

        threading.Thread(target=run, name=name, daemon=True).start()
        return handle

    def sleep(self, seconds: float) -> None:
        with self._lock:
            w = self._block(self._t + max(0.0, seconds), None)
            while not w.woken and not self._closed:
                self._ring.wait()

    def join(self, threads: List[_Thread], timeout: float = 10.0) -> None:
        with self._lock:
            end = self._t + timeout
            while not all(t.done for t in threads) and self._t < end:
                w = self._block(end, None)
                w.on_exit = True
                while not w.woken and not self._closed:
                    self._ring.wait()

    def settle(self, limit: float = 3600.0) -> None:
        """Let every spawned thread finish (the orphans' renders, the
        drains): the test's thread idles in virtual time until none is
        left."""
        end = self._t + limit
        while self._live and self._t < end and not self._closed:
            self.sleep(1.0)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            outside = [w.notify for w in self._waits if w.notify]
            self._ring.notify_all()
            self._kick.notify_all()
        for fn in outside:
            fn()

    # -- the mechanism (under self._lock) -------------------------------- #
    def _block(self, deadline: Optional[float], notify) -> _Wait:
        w = _Wait(deadline, next(self._seq), notify)
        self._waits.append(w)
        self._busy -= 1
        if self._busy == 0:
            self._kick.notify()
        return w

    def _wake(self, w: _Wait) -> None:
        if not w.woken:
            w.woken = True
            self._waits.remove(w)
            self._busy += 1

    def _advance(self) -> None:
        with self._lock:
            while not self._closed:
                timed = [w for w in self._waits if w.deadline is not None]
                if self._busy > 0 or not timed:
                    self._kick.wait()
                    continue
                w = min(timed, key=lambda w: (w.deadline, w.seq))
                self._t = max(self._t, w.deadline)
                self._wake(w)
                if w.notify is None:
                    self._ring.notify_all()
                else:
                    self._lock.release()
                    try:
                        w.notify()
                    finally:
                        self._lock.acquire()


class _VCond:
    """A :class:`threading.Condition` on ``lock`` whose ``wait(timeout)``
    runs in the clock's virtual seconds — what the chain's lanes wait on."""

    def __init__(self, clock: _VirtualClock, lock) -> None:
        self._clock = clock
        self._real = threading.Condition(lock)
        self._waits: List[_Wait] = []

    def _ring(self) -> None:
        with self._real:
            self._real.notify_all()

    def wait(self, timeout: Optional[float] = None) -> bool:  # lock held
        clock = self._clock
        with clock._lock:
            w = clock._block(
                None if timeout is None else clock._t + timeout, self._ring
            )
        self._waits.append(w)
        try:
            while not w.woken and not clock._closed:
                self._real.wait()
        finally:
            self._waits.remove(w)
        return w.notified

    def notify_all(self) -> None:  # lock held
        with self._clock._lock:
            for w in self._waits:
                if not w.woken:
                    w.notified = True
                    self._clock._wake(w)
        self._real.notify_all()

    def notify(self, n: int = 1) -> None:  # lock held
        with self._clock._lock:
            for w in [w for w in self._waits if not w.woken][:n]:
                w.notified = True
                self._clock._wake(w)
        self._real.notify_all()  # the others find themselves not woken


# --------------------------------------------------------------------------- #
# The hosts
# --------------------------------------------------------------------------- #
class _FakeHost:
    """``POST /v1/audio/speech`` → mp3 bytes after ``delay(text)`` VIRTUAL
    seconds, answered in-process (the fixture routes ``speech._http`` for
    its URL here; its port in the band is held, so nothing else gets it).

    ``mode``: "ok" (concurrent) or "serial" (one synthesis at a time, the
    rest queue inside the host — Voice Lab's shape). Each request renders
    on a thread of its own, so a client that stops waiting (its socket
    timeout: ``TimeoutError``, as urllib raises it) leaves the host
    rendering on, as a real one does. ``seen`` gets a request's
    ``(text, arrived, finished)`` BEFORE its client is answered."""

    virtual = True

    def __init__(
        self, clock: _VirtualClock, mode: str = "ok", floor: float = 0.0,
        per_char: float = 0.0,
    ) -> None:  # fmt: skip
        assert mode in ("ok", "serial"), mode
        self.clock = clock
        self.mode, self.floor, self.per_char = mode, floor, per_char
        self.seen: list = []  # (text, arrived, finished)
        self.active = 0
        self.max_active = 0  # concurrent requests the host held at once
        self._lock = threading.Lock()
        self._turn = clock.condition(self._lock)
        self._rendering = False  # serial: a synthesis holds the host
        self._sock = None
        for port in PORTS:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                s.close()
                continue
            self._sock, self.port = s, port
            break
        assert self._sock is not None, "no free port in 7230-7259"
        self.base = "http://127.0.0.1:%d/" % self.port

    def delay(self, text: str) -> float:
        return self.floor + self.per_char * len(text)

    def call(self, text: str, timeout: float) -> bytes:
        """The exchange as the client sees it (on the client's thread)."""
        clock = self.clock
        done: list = []

        def render() -> None:
            arrived = clock.now()
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.mode == "serial":
                    while self._rendering:
                        self._turn.wait()
                    self._rendering = True
            try:
                clock.sleep(self.delay(text))
            finally:
                with self._lock:
                    self.active -= 1
                    if self.mode == "serial":
                        self._rendering = False
                    self.seen.append((text, arrived, clock.now()))
                    done.append(True)
                    self._turn.notify_all()

        clock.spawn(render, "fake-voice-host")
        give_up = clock.now() + timeout
        with self._lock:
            while not done:
                left = give_up - clock.now()
                if left <= 0:
                    raise TimeoutError("timed out")  # nothing back in time
                self._turn.wait(left)
        return b"ID3" + text.encode()

    def texts(self) -> list:
        return [t for t, _a, _f in self.seen]

    def close(self) -> None:
        self._sock.close()


class _DeadHost:
    """A REAL host on loopback that fails every ``POST``: "500" (a CUDA
    error) or "reset" (the connection torn down with an RST)."""

    virtual = False

    def __init__(self, mode: str) -> None:
        assert mode in ("500", "reset"), mode
        self.mode = mode
        self.seen: list = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                text = json.loads(self.rfile.read(n) or b"{}").get("input", "")
                host.seen.append(text)
                if host.mode == "reset":
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    self.connection.close()
                    return
                body = b'{"error": "CUDA error: unknown error"}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = None
        for port in PORTS:
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        assert self.server is not None, "no free port in 7230-7259"
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _Piper(speech.Backend):
    id, kind, label = "local", "tts", "piper on this computer"

    def __init__(self):
        self.ok = True
        self.spoken: list = []

    def ready(self):
        return self.ok, "piper voice en_US-lessac-medium on this computer"

    def engine_name(self):
        return "piper"

    def speak(self, text, voice=""):
        self.spoken.append(text)
        return b"RIFFpiper", "audio/wav"

    def warm(self, prime=True):
        return "loaded"


def _free_band_port() -> int:
    """A port in the band nobody listens on (connect → refused)."""
    for port in PORTS:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    pytest.skip("no free port in 7230-7259")


@pytest.fixture
def chain(isolate_settings_store, monkeypatch, tmp_path):
    """The configured voice is an OpenAI-compatible host on loopback, piper
    the fallback; the health clock (``_now``) is ours, by hand, and the
    lanes' is a :class:`_VirtualClock` (``clock``) on a chain of this test's
    own; probes run inline, lanes and lines on the clock's threads."""
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    # The host is WARM unless a test says otherwise (TestColdStart: Voice Lab
    # stopped while idle and starts on the next request).
    monkeypatch.setattr(speech.OpenAITTS, "starts_cold", lambda self: False)
    piper = _Piper()
    host = speech.OpenAITTS()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host, piper]})
    now = [1000.0]
    monkeypatch.setattr(speech, "_now", lambda: now[0])
    monkeypatch.setattr(speech, "_WARMED", {})  # a warm here stays here
    clock = _VirtualClock()
    tts_chain = speech.TtsChain(clock=clock)
    monkeypatch.setattr(speech, "TTS_CHAIN", tts_chain)
    monkeypatch.setattr(speech, "_lane_thread", clock.spawn)
    made: list = []
    real_http = speech._http

    def routed(method, url, headers, body, timeout):
        for fake in made:
            if fake.virtual and url.startswith(fake.base):
                text = json.loads(body or b"{}").get("input", "")
                return 200, "audio/mpeg", fake.call(text, timeout)
        return real_http(method, url, headers, body, timeout)

    monkeypatch.setattr(speech, "_http", routed)

    def serve(mode: str = "ok", **kw):
        if mode in ("500", "reset"):
            fake = _DeadHost(mode)
        else:
            fake = _FakeHost(clock, mode, **kw)
        made.append(fake)
        point(fake.port)
        return fake

    def point(port: int) -> None:
        S.update_settings(
            voice={
                "tts_backend": "openai",
                "tts_url": "http://127.0.0.1:%d/v1" % port,
                "tts_voice": "Donna",
            }
        )
        S.invalidate()

    yield SimpleNamespace(
        c=tts_chain, host=host, piper=piper, now=now, serve=serve, point=point,
        clock=clock,
    )  # fmt: skip
    clock.settle()  # the orphans' renders and the drains finish
    clock.close()
    for fake in made:
        fake.close()


def _budget(monkeypatch, base: float, per_char: float = 0.0) -> None:
    monkeypatch.setattr(speech, "HOST_BUDGET", base)
    monkeypatch.setattr(speech, "HOST_BUDGET_PER_CHAR", per_char)


def _at_once(clock: _VirtualClock, fn, args_list, stagger: float = 0.02) -> list:
    """Run ``fn(*args)`` for each, on threads started ``stagger`` (virtual)
    seconds apart (in order, as a client pipelining a reply's sentences):
    the results in the same order."""
    out: list = [None] * len(args_list)

    def run(i, args):
        try:
            out[i] = fn(*args)
        except Exception as err:  # noqa: BLE001
            out[i] = err

    threads = []
    for i, args in enumerate(args_list):
        threads.append(clock.spawn(lambda i=i, args=args: run(i, args), "line"))
        clock.sleep(stagger)
    clock.join(threads)
    return out


def _wait(clock: _VirtualClock, cond, timeout: float = 5.0) -> None:
    end = clock.now() + timeout
    while not cond():
        assert clock.now() < end, "timed out waiting"
        clock.sleep(0.02)


class TestSerialHostQueue:
    def test_queued_short_lines_never_trip_down(self, chain, monkeypatch):
        """R3: three short lines at once to a serial host taking 0.4 s each,
        each line's own budget 0.6 s. The third waits 0.8 s behind the
        other two: before, that wait ate its budget (cut at 0.6 s, host
        down, piper for 45 s). Now the wait behind our own lines is ours:
        all three in the host's voice, the host never down."""
        _budget(monkeypatch, 0.6)
        fake = chain.serve("serial", floor=0.4)
        lines = [(chain.host, "Yes."), (chain.host, "Okay."), (chain.host, "Done.")]
        out = _at_once(chain.clock, chain.c.speak, lines)
        for r in out:
            assert not isinstance(r, Exception), r
            assert r[2] is chain.host and r[3] is False
        assert chain.piper.spoken == []
        assert chain.c.down_reason(chain.host) == ""
        assert sorted(fake.texts()) == ["Done.", "Okay.", "Yes."]
        assert fake.max_active == 1  # one at a time: nothing hidden in its queue

    def test_lines_reach_the_host_in_the_order_asked(self, chain, monkeypatch):
        _budget(monkeypatch, 1.0)
        fake = chain.serve("serial", floor=0.15)
        lines = [(chain.host, "Line %d." % i) for i in range(4)]
        out = _at_once(chain.clock, chain.c.speak, lines, stagger=0.03)
        assert all(r[2] is chain.host for r in out)
        assert fake.texts() == ["Line 0.", "Line 1.", "Line 2.", "Line 3."]

    def test_the_warm_and_the_probe_queue_in_the_lane(self, chain, monkeypatch):
        """A boot warm is a request of ours too: lines wait for it in the
        lane instead of piling onto the host beside it."""
        _budget(monkeypatch, 1.0)
        fake = chain.serve("serial", floor=0.3)
        out = _at_once(
            chain.clock,
            lambda f, *a: f(*a),
            [(speech.warm, "tts"), (chain.c.speak, chain.host, "Hi."),
             (chain.c.speak, chain.host, "There.")],
        )  # fmt: skip
        assert out[0] == "loaded"
        assert out[1][2] is chain.host and out[2][2] is chain.host
        assert fake.texts()[0] == speech.WARM_TEXT
        assert fake.max_active == 1


class TestSlowIsNotDown:
    def test_a_slow_line_falls_back_alone_and_the_host_stays_up(
        self, chain, monkeypatch
    ):
        """A line past its budget is said by piper — and so is the rest of
        that reply, without sending it to the host — but the host is not
        marked down: the next reply is the host's again."""
        _budget(monkeypatch, 0.3)
        fake = chain.serve("ok", floor=0.8)
        t0 = chain.clock.now()
        _a, _m, engine, fb = chain.c.speak(chain.host, "One.")
        took = chain.clock.now() - t0
        assert engine is chain.piper and fb is True
        assert 0.25 <= took < 0.7  # the budget, not the host's 0.8 s
        assert chain.c.down_reason(chain.host) == ""  # slow is not down
        _a, _m, engine, fb = chain.c.speak(chain.host, "Two.")  # same reply
        assert engine is chain.piper and fb is True
        # The orphan finished at the host:
        _wait(chain.clock, lambda: len(fake.seen) == 1)
        assert fake.texts() == ["One."]  # "Two." never went there
        assert chain.c.down_reason(chain.host) == ""
        # The reply plays out, the pause between replies passes …
        chain.now[0] += 10 + speech.REPLY_GAP_SECONDS
        fake.floor = 0.05
        _a, _m, engine, fb = chain.c.speak(chain.host, "A new reply.")
        assert engine is chain.host and fb is False
        assert chain.piper.spoken == ["One.", "Two."]

    def test_lines_waiting_behind_a_slow_line_leave_with_it(self, chain, monkeypatch):
        """One voice per reply: the reply's lines still waiting in the lane
        go to piper the moment one line does — none reaches the host after
        the switch."""
        _budget(monkeypatch, 0.3)
        fake = chain.serve("serial", floor=1.5)
        t0 = chain.clock.now()
        lines = [
            (chain.host, "First."),
            (chain.host, "Second."),
            (chain.host, "Third."),
        ]
        out = _at_once(chain.clock, chain.c.speak, lines, stagger=0.02)
        took = chain.clock.now() - t0
        assert [r[2] for r in out] == [chain.piper] * 3
        assert all(r[3] is True for r in out)
        assert took < 1.0  # the first line's budget, not 3 × 0.3 s, not 1.5 s
        assert chain.c.down_reason(chain.host) == ""
        _wait(chain.clock, lambda: len(fake.seen) == 1)
        assert fake.texts() == ["First."]

    def test_a_line_behind_an_orphan_goes_to_piper_at_once(self, chain, monkeypatch):
        """The orphan is not being played, so waiting behind it IS silence:
        the host already missed a budget, so the next reply's line is not
        sent behind it — piper at once (it used to wait its own budget
        first). Still not down."""
        _budget(monkeypatch, 0.3)
        monkeypatch.setattr(speech, "_spawn", lambda target, name: None)  # no probe
        fake = chain.serve("serial", floor=1.2)
        chain.c.speak(chain.host, "Slow one.")  # gives up at 0.3 s
        chain.now[0] += 10 + speech.REPLY_GAP_SECONDS  # a new reply
        t0 = chain.clock.now()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Next.")
        took = chain.clock.now() - t0
        assert engine is chain.piper
        assert took < 0.15
        assert chain.c.down_reason(chain.host) == ""
        _wait(chain.clock, lambda: len(fake.seen) == 1)
        chain.clock.sleep(0.1)
        assert fake.texts() == ["Slow one."]  # "Next." never went there

    def test_no_fallback_waits_for_a_slow_host(self, chain, monkeypatch):
        """Waiting for the owner's voice beats a 502 when there is nothing
        else to say the line with."""
        chain.piper.ok = False
        _budget(monkeypatch, 0.2)
        chain.serve("ok", floor=0.6)
        audio, _m, engine, fb = chain.c.speak(chain.host, "Patience.")
        assert engine is chain.host and fb is False
        assert audio == b"ID3Patience."
        assert chain.c.down_reason(chain.host) == ""


class TestDeadIsDown:
    @pytest.mark.parametrize("mode", ["refused", "500", "reset"])
    def test_a_dead_host_is_down_at_once(self, chain, monkeypatch, mode):
        _budget(monkeypatch, 2.0)
        if mode == "refused":
            chain.point(_free_band_port())
        else:
            chain.serve(mode)
        t0 = chain.clock.now()
        _a, _m, engine, fb = chain.c.speak(chain.host, "Hello.")
        assert chain.clock.now() - t0 < 1.0  # no budget spent
        assert engine is chain.piper and fb is True
        assert chain.c.down_reason(chain.host)
        # Down: the next line goes straight to piper, not to the host.
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Again.")
        assert engine is chain.piper

    def test_a_hung_host_is_down_after_host_hung_after(self, chain, monkeypatch):
        """Nothing back for a line it had to itself for host_hung_after: the
        line went to piper at its budget, the orphan marks the host down."""
        _budget(monkeypatch, 0.2)
        monkeypatch.setattr(speech, "TTS_TIMEOUT", 0.7)
        chain.serve("ok", floor=3.0)
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Hello.")
        assert engine is chain.piper
        assert chain.c.down_reason(chain.host) == ""
        _wait(chain.clock, lambda: bool(chain.c.down_reason(chain.host)))
        assert "sent nothing back" in chain.c.down_reason(chain.host)


_REAL_STARTS_COLD = speech.OpenAITTS.starts_cold


class TestColdStart:
    """Voice Lab off while idle (owner, 2026-11-04): its socket proxy stops
    it after 10 idle minutes and the next connection starts it again, the
    first synthesis loading its model (~40 s for F5 on a CPU). That first
    line WAITS for it, in the owner's voice, instead of going to piper at
    its budget or marking the host down — and status reads never send it
    anything (``probe=False``), so nothing but speech wakes it."""

    @pytest.fixture(autouse=True)
    def _cold(self, chain, monkeypatch):  # after chain, which turns it off
        monkeypatch.setattr(speech.OpenAITTS, "starts_cold", _REAL_STARTS_COLD)

    def test_a_loopback_host_starts_cold_and_a_remote_api_never(self, chain):
        chain.point(_free_band_port())
        assert chain.host.starts_cold() is True
        assert chain.c.cold(chain.host) is True  # never answered yet
        S.update_settings(
            voice={"tts_url": "https://api.openai.com/v1"}
        )  # a remote API
        S.invalidate()
        assert chain.host.starts_cold() is False
        assert chain.c.cold(chain.host) is False
        assert chain.c.cold(chain.piper) is False  # not a serial host

    def test_the_first_line_waits_for_a_starting_host(self, chain, monkeypatch):
        _budget(monkeypatch, 0.2)  # a warm line would go to piper at 0.2 s
        fake = chain.serve("serial", floor=1.0)  # starting + loading
        _a, _m, engine, fb = chain.c.speak(chain.host, "Hello.")
        assert engine is chain.host and fb is False
        assert chain.piper.spoken == []
        assert chain.c.down_reason(chain.host) == ""
        assert fake.texts() == ["Hello."]
        # Not a speed sample: a model loading is not the host's pace.
        assert chain.c.speed(chain.host)["samples"] == 0
        # Up now: the next line is warm (its budget applies again).
        assert chain.c.cold(chain.host) is False
        fake.floor = 0.0
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Again.")
        assert engine is chain.host

    def test_lines_asked_while_it_starts_wait_with_it(self, chain, monkeypatch):
        _budget(monkeypatch, 0.2)
        chain.serve("serial", floor=0.6)
        out = _at_once(
            chain.clock, chain.c.speak, [(chain.host, "One."), (chain.host, "Two.")]
        )
        assert [r[2] for r in out] == [chain.host, chain.host]
        assert chain.piper.spoken == []

    def test_cold_again_after_ten_idle_minutes(self, chain, monkeypatch):
        _budget(monkeypatch, 0.2)
        chain.serve("ok", floor=0.0)
        chain.c.speak(chain.host, "Hello.")
        assert chain.c.cold(chain.host) is False
        chain.now[0] += speech.COLD_AFTER_SECONDS - 1
        assert chain.c.cold(chain.host) is False
        chain.now[0] += 1
        assert chain.c.cold(chain.host) is True

    def test_a_refused_host_is_still_down_at_once(self, chain, monkeypatch):
        """Waiting is for a host that is STARTING (the connection is held);
        one that refuses is down, as before."""
        chain.point(_free_band_port())
        t0 = chain.clock.now()
        _a, _m, engine, fb = chain.c.speak(chain.host, "Hello.")
        assert chain.clock.now() - t0 < 1.0
        assert engine is chain.piper and fb is True
        assert chain.c.down_reason(chain.host)

    def test_past_the_cold_start_timeout_it_is_down(self, chain, monkeypatch):
        monkeypatch.setattr(speech, "COLD_START_TIMEOUT", 0.4)
        monkeypatch.setattr(speech, "TTS_TIMEOUT", 0.4)
        chain.serve("ok", floor=3.0)
        t0 = chain.clock.now()
        _a, _m, engine, fb = chain.c.speak(chain.host, "Hello.")
        assert 0.3 < chain.clock.now() - t0 < 2.5
        assert engine is chain.piper and fb is True
        assert "sent nothing back" in chain.c.down_reason(chain.host)

    def test_a_status_read_never_probes_a_down_host(self, chain, monkeypatch):
        started = _no_probe(monkeypatch)
        chain.point(_free_band_port())
        chain.c.mark_down(chain.host, "could not reach it")
        chain.now[0] += speech.HOST_DOWN_SECONDS + 1  # the re-check is due
        st = chain.c.status(chain.host, probe=False)
        assert st["why"] == "could not reach it"
        assert started == []
        speech.describe(probe=False)
        assert started == []
        # A line (or a warm read) does re-check it.
        chain.c.status(chain.host)
        assert started == ["mindflock-voice-probe"]

    def test_a_status_read_never_probes_a_slow_host(self, chain, monkeypatch):
        started = _no_probe(monkeypatch)
        chain.point(_free_band_port())
        with chain.c._lock:
            chain.c._learn(chain.host.health_key(), "Hello.", 60.0)
        chain.now[0] += speech.SLOW_RECHECK_SECONDS + 1
        assert chain.c.too_slow(chain.host, probe=False)
        assert chain.c.status(chain.host, probe=False)["slow"]
        assert started == []
        chain.c.too_slow(chain.host)
        assert started == ["mindflock-voice-pace"]

    def test_describe_reports_a_cold_voice(self, chain):
        chain.point(_free_band_port())
        ready = speech.describe(probe=False)["server_ready"]
        assert ready["tts_cold"] is True


class TestBudgetMath:
    def test_hung_after_scales_with_the_line(self):
        assert speech.host_hung_after("") == speech.TTS_TIMEOUT
        long = "x" * 400  # budget 20 s (capped) → hung after 40 s
        assert speech.host_hung_after(long) == 2 * speech.host_budget(long)

    def test_a_line_budget_chains_off_the_live_line_ahead(self, chain, monkeypatch):
        """budget = host_budget(this) + the budgets of the live lines ahead:
        three 0.5 s lines on a serial host with a 0.55 s budget each. Alone,
        the third (answered at 1.5 s) would be cut at 0.59 s; chained it has
        until 1.65 s, and all three come back in the host's voice. (50 ms of
        slack per line is enough: the clock is virtual, no runner eats it.)"""
        _budget(monkeypatch, 0.55)
        fake = chain.serve("serial", floor=0.5)
        out = _at_once(
            chain.clock,
            chain.c.speak,
            [(chain.host, "A."), (chain.host, "B."), (chain.host, "C.")],
        )
        assert [r[2] for r in out] == [chain.host] * 3
        assert fake.max_active == 1


def _no_probe(monkeypatch) -> list:
    """Background probes recorded, not run (the window stays as it is)."""
    started: list = []
    monkeypatch.setattr(speech, "_spawn", lambda target, name: started.append(name))
    return started


class TestLearnedSpeed:
    """N1 (cycle-2 judge): a host that is alive but slower than its budget
    (Voice Lab's F5 on a CPU) used to cost every reply its first line's
    budget in silence before piper. Its pace is learned now: once it is
    known too slow, a reply goes to piper at once; a probe brings it back."""

    def _slow_reply(self, chain, fake):
        _a, _m, engine, _fb = chain.c.speak(chain.host, "One.")
        assert engine is chain.piper
        # The orphan's answer: a sample.
        _wait(chain.clock, lambda: len(fake.seen) == 1)
        chain.clock.sleep(0.05)

    def test_a_slow_host_costs_one_reply_then_none(self, chain, monkeypatch):
        _budget(monkeypatch, 0.3)
        probes = _no_probe(monkeypatch)
        fake = chain.serve("serial", floor=0.8)
        self._slow_reply(chain, fake)
        sp = chain.c.speed(chain.host)
        assert sp["slow"] is True and sp["ratio"] > speech.SLOW_RATIO
        assert chain.c.down_reason(chain.host) == ""  # too slow is not down
        for n in range(3):  # the next replies: piper at once, nothing sent
            chain.now[0] += 5 + speech.REPLY_GAP_SECONDS
            t0 = chain.clock.now()
            _a, _m, engine, fb = chain.c.speak(chain.host, "Reply %d." % n)
            assert chain.clock.now() - t0 < 0.1
            assert engine is chain.piper and fb is True
        assert fake.texts() == ["One."]
        assert probes  # the recheck came due and asked for a probe
        assert chain.c.status(chain.host)["slow"]

    def test_no_probe_before_the_recheck(self, chain, monkeypatch):
        _budget(monkeypatch, 0.3)
        probes = _no_probe(monkeypatch)
        fake = chain.serve("serial", floor=0.8)
        self._slow_reply(chain, fake)
        chain.now[0] += speech.SLOW_RECHECK_SECONDS / 2
        chain.c.speak(chain.host, "Soon after.")
        assert probes == []

    def test_one_probe_that_keeps_up_ends_the_window(self, chain, monkeypatch):
        """slow → fast: the first probe after the host speeds up brings the
        configured voice back for the next reply (the probe runs inline
        here, so for this very line)."""
        _budget(monkeypatch, 0.3)
        fake = chain.serve("serial", floor=0.8)
        self._slow_reply(chain, fake)
        assert chain.c.speed(chain.host)["slow"]
        fake.floor = 0.05
        chain.now[0] += speech.SLOW_RECHECK_SECONDS + speech.REPLY_GAP_SECONDS + 5
        _a, _m, engine, fb = chain.c.speak(chain.host, "Back.")
        assert engine is chain.host and fb is False
        assert fake.texts() == ["One.", speech.WARM_TEXT, "Back."]
        assert chain.c.speed(chain.host)["slow"] is False

    def test_a_probe_still_slow_keeps_the_window(self, chain, monkeypatch):
        _budget(monkeypatch, 0.3)
        fake = chain.serve("serial", floor=0.8)
        self._slow_reply(chain, fake)
        chain.now[0] += speech.SLOW_RECHECK_SECONDS + speech.REPLY_GAP_SECONDS + 5
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Still slow.")
        assert engine is chain.piper
        assert fake.texts() == ["One.", speech.WARM_TEXT]
        assert chain.c.speed(chain.host)["slow"]
        assert chain.c.down_reason(chain.host) == ""

    def test_the_warm_measures_a_slow_host_before_the_first_reply(
        self, chain, monkeypatch
    ):
        """A warm past its budget may be a cold load, so one more word is
        timed: a host that is just slow is known before anyone asks, and
        even the FIRST reply starts on piper at once."""
        _budget(monkeypatch, 0.3)
        _no_probe(monkeypatch)
        fake = chain.serve("serial", floor=0.8)
        assert chain.host.warm() == "loaded"
        assert fake.texts() == [speech.WARM_TEXT, speech.WARM_TEXT]
        assert chain.c.speed(chain.host)["slow"]
        t0 = chain.clock.now()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "First reply.")
        assert chain.clock.now() - t0 < 0.1 and engine is chain.piper

    def test_a_cold_warm_on_a_fast_host_is_not_held_against_it(
        self, chain, monkeypatch
    ):
        _budget(monkeypatch, 0.3)
        fake = chain.serve("serial", floor=0.8)  # a cold load …
        orig = fake.delay
        fake.delay = lambda text: (orig(text) if len(fake.seen) == 0 else 0.05)
        assert chain.host.warm() == "loaded"
        assert len(fake.seen) == 2  # … then the timed word: fast
        assert chain.c.speed(chain.host)["slow"] is False
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Hello.")
        assert engine is chain.host

    def test_a_fast_warm_is_the_first_sample(self, chain, monkeypatch):
        _budget(monkeypatch, 0.6)
        fake = chain.serve("serial", floor=0.1)
        assert chain.host.warm() == "loaded"
        assert fake.texts() == [speech.WARM_TEXT]  # no second word needed
        sp = chain.c.speed(chain.host)
        assert sp["samples"] == 1 and sp["slow"] is False

    def test_a_measured_healthy_host_keeps_every_line(self, chain, monkeypatch):
        """Prediction never costs a host that keeps up its voice: three
        queued lines, each within its chained budget, all the host's."""
        _budget(monkeypatch, 0.6)
        fake = chain.serve("serial", floor=0.4)
        chain.host.warm()
        out = _at_once(
            chain.clock,
            chain.c.speak,
            [(chain.host, "A."), (chain.host, "B."), (chain.host, "C.")],
        )
        assert [r[2] for r in out] == [chain.host] * 3
        assert fake.texts()[1:] == ["A.", "B.", "C."]

    def test_a_line_that_cannot_make_it_behind_the_probe_is_not_sent(
        self, chain, monkeypatch
    ):
        """Known pace, but a probe is at the host: the line would arrive
        past its budget, so it goes to piper at once rather than after
        waiting its budget out."""
        _budget(monkeypatch, 0.5)
        fake = chain.serve("serial", floor=0.4)
        chain.host.warm()  # ratio ≈ 0.8: keeps up
        probe = chain.clock.spawn(
            lambda: chain.c.send(
                chain.host, speech.WARM_TEXT, speech.host_hung_after("")
            ),
            "probe",
        )
        _wait(chain.clock, lambda: fake.active == 1)
        t0 = chain.clock.now()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Now.")
        assert chain.clock.now() - t0 < 0.1 and engine is chain.piper
        chain.clock.join([probe], 5)
        assert "Now." not in fake.texts()

    def test_learning_math(self, chain, monkeypatch):
        _budget(monkeypatch, 1.0)
        c = chain.c
        with c._lock:
            c._learn("h", "", 0.5)  # first sample: as is
            assert c._speed["h"].ratio == pytest.approx(0.5)
            c._learn("h", "", 2.5)  # EWMA → 1.5: too slow
            assert c._speed["h"].slow
            c._learn("h", "", 0.2, at_least=True)  # a lower bound never lowers
            assert c._speed["h"].ratio == pytest.approx(1.5)
            c._learn("h", "", 0.9)  # one answer within budget ends it
            assert c._speed["h"].ratio == pytest.approx(0.9)
            assert not c._speed["h"].slow
            assert c._predict("h", "") == pytest.approx(0.9)
            assert c._predict("other", "") is None


class TestListeners:
    """Two pages hearing one reply (the phone and the desktop): each has
    its own reply in the chain."""

    def test_another_pages_lines_ahead_do_not_extend_a_budget(self, chain, monkeypatch):
        """The desktop's first line behind the phone's three is not chained
        off them (they are not being played to the desktop): it falls back
        on its own budget — and the phone's reply stays in the host's
        voice, its waiting lines untouched by the desktop's switch."""
        _budget(monkeypatch, 0.6)
        fake = chain.serve("serial", floor=0.4)
        calls = [
            (chain.host, "Phone one.", "", "phone"),
            (chain.host, "Phone two.", "", "phone"),
            (chain.host, "Phone three.", "", "phone"),
            (chain.host, "The desktop's whole reply.", "", "desktop"),
        ]
        out = _at_once(chain.clock, chain.c.speak, calls, stagger=0.03)
        assert [r[2] for r in out[:3]] == [chain.host] * 3
        assert out[3][2] is chain.piper and out[3][3] is True
        assert "The desktop's whole reply." not in fake.texts()
        # The desktop's reply is on piper; the phone's next line is not.
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Phone four.", "", "phone")
        assert engine is chain.host
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Desk two.", "", "desktop")
        assert engine is chain.piper

    def test_down_moves_every_listener(self, chain, monkeypatch):
        chain.c.pick(chain.host, "phone")
        chain.c.pick(chain.host, "desktop")
        chain.c.mark_down(chain.host, "refused")
        for who in ("phone", "desktop", "new page"):
            assert chain.c.pick(chain.host, who)[0] is chain.piper

    def test_a_gone_page_on_the_fallback_is_forgotten(self, chain):
        # Every page that heard a line while the host was down got a sticky
        # reply. A page that went away never picks again, so its reply stays
        # sticky: once quiet it must still be forgotten past MAX_LISTENERS,
        # or every page load during an outage is kept for good.
        chain.c.mark_down(chain.host, "refused")
        gone = ["page %d" % i for i in range(speech.MAX_LISTENERS)]
        for who in gone:
            assert chain.c.pick(chain.host, who)[0] is chain.piper
        chain.now[0] += speech.HOST_DOWN_SECONDS + speech.REPLY_GAP_SECONDS + 1
        for i in range(speech.MAX_LISTENERS):
            chain.c.pick(chain.host, "later %d" % i)
        assert len(chain.c._replies) <= speech.MAX_LISTENERS
        assert not set(gone) & set(chain.c._replies)
