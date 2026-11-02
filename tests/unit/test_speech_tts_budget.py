"""``core.speech.TtsChain``'s host budget against a REAL OpenAI-compatible
voice host (a fake one on loopback, port band 7230-7259): a slow-but-alive
host is never marked down, our lines go to a serial host one at a time (its
lane), time spent behind our own lines does not eat a line's budget, and a
dead host (refused, 5xx, reset, hung) is still marked down at once.

Voice Lab's F5 renders one request at a time and its first byte comes after
the whole clip (synth ≈ max(1.4 s, 0.52 × audio)); :class:`_FakeHost`
models that with a floor per line, optionally serial.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend import memory_pressure
from backend.config import settings as S
from backend.web.core import speech

PORTS = range(7230, 7260)


class _FakeHost:
    """``POST /v1/audio/speech`` → mp3 bytes after ``delay(text)`` seconds.

    ``mode``: "ok" (concurrent), "serial" (one synthesis at a time, the
    rest queue inside the host — Voice Lab's shape), "500", "reset" (the
    connection is torn down with an RST)."""

    def __init__(self, mode: str = "ok", floor: float = 0.0, per_char: float = 0.0):
        self.mode, self.floor, self.per_char = mode, floor, per_char
        self.seen: list = []  # (text, arrived, finished)
        self.active = 0
        self.max_active = 0  # concurrent requests the host held at once
        self._gate = threading.Lock()
        self._count = threading.Lock()
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                text = json.loads(self.rfile.read(n) or b"{}").get("input", "")
                arrived = time.monotonic()
                with host._count:
                    host.active += 1
                    host.max_active = max(host.max_active, host.active)
                try:
                    if host.mode == "reset":
                        self.connection.setsockopt(
                            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                        )
                        self.connection.close()
                        return
                    if host.mode == "500":
                        body = b'{"error": "CUDA error: unknown error"}'
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if host.mode == "serial":
                        with host._gate:
                            time.sleep(host.delay(text))
                    else:
                        time.sleep(host.delay(text))
                    body = b"ID3" + text.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with host._count:
                        host.active -= 1
                    host.seen.append((text, arrived, time.monotonic()))

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

    def delay(self, text: str) -> float:
        return self.floor + self.per_char * len(text)

    def texts(self) -> list:
        return [t for t, _a, _f in self.seen]

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
    the fallback; the health clock (``_now``) is ours, the lane's is real;
    probes run inline, lanes on real threads."""
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    piper = _Piper()
    host = speech.OpenAITTS()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host, piper]})
    now = [1000.0]
    monkeypatch.setattr(speech, "_now", lambda: now[0])
    monkeypatch.setattr(speech, "_WARMED", {})  # a warm here stays here
    speech.TTS_CHAIN.reset()
    made: list = []

    def serve(*a, **kw) -> _FakeHost:
        fake = _FakeHost(*a, **kw)
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
        c=speech.TTS_CHAIN, host=host, piper=piper, now=now, serve=serve, point=point
    )
    speech.TTS_CHAIN.reset()
    for fake in made:
        fake.close()


def _budget(monkeypatch, base: float, per_char: float = 0.0) -> None:
    monkeypatch.setattr(speech, "HOST_BUDGET", base)
    monkeypatch.setattr(speech, "HOST_BUDGET_PER_CHAR", per_char)


def _at_once(fn, args_list, stagger: float = 0.02) -> list:
    """Run ``fn(*args)`` for each, on threads started ``stagger`` apart (in
    order, as a client pipelining a reply's sentences): the results in the
    same order."""
    out: list = [None] * len(args_list)

    def run(i, args):
        try:
            out[i] = fn(*args)
        except Exception as err:  # noqa: BLE001
            out[i] = err

    threads = []
    for i, args in enumerate(args_list):
        t = threading.Thread(target=run, args=(i, args))
        t.start()
        threads.append(t)
        time.sleep(stagger)
    for t in threads:
        t.join(10)
    return out


def _wait(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out waiting"
        time.sleep(0.02)


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
        out = _at_once(chain.c.speak, lines)
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
        out = _at_once(chain.c.speak, lines, stagger=0.03)
        assert all(r[2] is chain.host for r in out)
        assert fake.texts() == ["Line 0.", "Line 1.", "Line 2.", "Line 3."]

    def test_the_warm_and_the_probe_queue_in_the_lane(self, chain, monkeypatch):
        """A boot warm is a request of ours too: lines wait for it in the
        lane instead of piling onto the host beside it."""
        _budget(monkeypatch, 1.0)
        fake = chain.serve("serial", floor=0.3)
        out = _at_once(
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
        t0 = time.monotonic()
        _a, _m, engine, fb = chain.c.speak(chain.host, "One.")
        took = time.monotonic() - t0
        assert engine is chain.piper and fb is True
        assert 0.25 <= took < 0.7  # the budget, not the host's 0.8 s
        assert chain.c.down_reason(chain.host) == ""  # slow is not down
        _a, _m, engine, fb = chain.c.speak(chain.host, "Two.")  # same reply
        assert engine is chain.piper and fb is True
        _wait(lambda: len(fake.seen) == 1)  # the orphan finished at the host
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
        t0 = time.monotonic()
        lines = [
            (chain.host, "First."),
            (chain.host, "Second."),
            (chain.host, "Third."),
        ]
        out = _at_once(chain.c.speak, lines, stagger=0.02)
        took = time.monotonic() - t0
        assert [r[2] for r in out] == [chain.piper] * 3
        assert all(r[3] is True for r in out)
        assert took < 1.0  # the first line's budget, not 3 × 0.3 s, not 1.5 s
        assert chain.c.down_reason(chain.host) == ""
        _wait(lambda: len(fake.seen) == 1)
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
        t0 = time.monotonic()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Next.")
        took = time.monotonic() - t0
        assert engine is chain.piper
        assert took < 0.15
        assert chain.c.down_reason(chain.host) == ""
        _wait(lambda: len(fake.seen) == 1)
        time.sleep(0.1)
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
        t0 = time.monotonic()
        _a, _m, engine, fb = chain.c.speak(chain.host, "Hello.")
        assert time.monotonic() - t0 < 1.0  # no budget spent
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
        _wait(lambda: bool(chain.c.down_reason(chain.host)))
        assert "sent nothing back" in chain.c.down_reason(chain.host)


class TestBudgetMath:
    def test_hung_after_scales_with_the_line(self):
        assert speech.host_hung_after("") == speech.TTS_TIMEOUT
        long = "x" * 400  # budget 20 s (capped) → hung after 40 s
        assert speech.host_hung_after(long) == 2 * speech.host_budget(long)

    def test_a_line_budget_chains_off_the_live_line_ahead(self, chain, monkeypatch):
        """budget = host_budget(this) + the budgets of the live lines ahead:
        three 0.5 s lines on a serial host with a 0.8 s budget each. Alone,
        the third (answered at ~1.5 s) would be cut at 0.8 s; chained it has
        2.4 s, and all three come back in the host's voice. (0.8, not 0.55:
        a slow CI runner ate the 50 ms of slack per line.)"""
        _budget(monkeypatch, 0.8)
        fake = chain.serve("serial", floor=0.5)
        out = _at_once(
            chain.c.speak, [(chain.host, "A."), (chain.host, "B."), (chain.host, "C.")]
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
        _wait(lambda: len(fake.seen) == 1)  # the orphan's answer: a sample
        time.sleep(0.05)

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
            t0 = time.monotonic()
            _a, _m, engine, fb = chain.c.speak(chain.host, "Reply %d." % n)
            assert time.monotonic() - t0 < 0.1
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
        t0 = time.monotonic()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "First reply.")
        assert time.monotonic() - t0 < 0.1 and engine is chain.piper

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
            chain.c.speak, [(chain.host, "A."), (chain.host, "B."), (chain.host, "C.")]
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
        probe = threading.Thread(
            target=chain.c.send,
            args=(chain.host, speech.WARM_TEXT, speech.host_hung_after("")),
        )
        probe.start()
        _wait(lambda: fake.active == 1)
        t0 = time.monotonic()
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Now.")
        assert time.monotonic() - t0 < 0.1 and engine is chain.piper
        probe.join(5)
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
        out = _at_once(chain.c.speak, calls, stagger=0.03)
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
