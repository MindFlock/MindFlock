"""The speak prewarm (routes/speech.py ``Prewarm``): the session names a
switch says and a few fixed lines, pre-synthesised while the voice is idle
into ``SPEAK_CACHE``'s pinned set — and the serial lane's low priority
(core/speech.py ``TtsChain.send(low=True)``, ``idle_for``) that keeps it
from ever holding up a real line. Fakes only: nothing reaches a model or
the network.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.routes import speech as routes

client = TestClient(server.app)


class _Tts(speech.Backend):
    """A TTS engine: ``serial`` like Voice Lab's host or not like piper;
    ``hold`` (an Event) keeps a synthesis at the "host" until it is set."""

    kind = "tts"

    def __init__(self, backend_id: str, serial: bool, audio: bytes) -> None:
        self.id, self.label, self.serial = backend_id, backend_id, serial
        self.audio = audio
        self.calls: list = []
        self.hold: "threading.Event | None" = None
        self.at_host = threading.Event()

    def ready(self):
        return True, "%s ready" % self.id

    def speak(self, text, voice=""):
        self.calls.append(text)
        self.at_host.set()
        if self.hold is not None:
            self.hold.wait(5)
        return self.audio + b"|" + text.encode(), "audio/mpeg"


@pytest.fixture(autouse=True)
def _store(isolate_settings_store, monkeypatch):
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    monkeypatch.setattr(routes, "PREWARM_GAP_S", 0.0)
    monkeypatch.setattr(routes, "_PREWARM_POLL_S", 0.01)
    # Kicks from the routes start nothing here: each test runs its pass.
    monkeypatch.setattr(routes, "_prewarm_thread", lambda target, name: None)
    speech.TTS_CHAIN.reset()
    routes.SPEAK_CACHE.clear()
    routes.PREWARM.reset()
    yield
    speech.TTS_CHAIN.reset()
    routes.SPEAK_CACHE.clear()
    routes.PREWARM.reset()


@pytest.fixture
def pair(monkeypatch):
    """A serial host (the configured voice) and piper, both prewarmable,
    the voice idle; the live sessions are ``names``."""
    host = _Tts("openai", True, b"ID3host")
    piper = _Tts("local", False, b"RIFFpiper")
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host, piper]})
    S.update_settings(voice={"tts_backend": "openai"})
    S.invalidate()
    monkeypatch.setattr(routes, "_prewarmable", lambda engine: True)
    monkeypatch.setattr(routes, "PREWARM_QUIET_S", 0.0)
    names = ["hackathon-2", "c4-names"]
    monkeypatch.setattr(routes, "_session_names", lambda: list(names))
    monkeypatch.setattr(routes, "PREWARM_FIXED", ("Answer it on the screen.",))
    return SimpleNamespace(host=host, piper=piper, names=names)


def _shaped(text):
    return routes._spoken_text.shape(text, [])


def _speak(text, **kw):
    return client.post("/api/voice/speak", json=dict({"text": text}, **kw))


# --------------------------------------------------------------------------- #
# Filling the cache
# --------------------------------------------------------------------------- #
class TestFill:
    def test_names_are_hits_after_a_prewarm(self, pair):
        assert routes.PREWARM.run_once(wait=2) == 3
        # Said as the route says them: shaped for the voice.
        assert pair.host.calls == [
            _shaped("hackathon-2."),
            _shaped("c4-names."),
            "Answer it on the screen.",
        ]
        # What a switch says (voice_router.switch_line: the name and a full
        # stop), asked by either client, is now a hit — no synthesis.
        for text, kw in (("hackathon-2.", {}), ("c4-names.", {"client": "phone"})):
            r = _speak(text, **kw)
            assert r.status_code == 200, r.text
            assert r.headers["x-voice-cache"] == "hit"
            assert r.headers["x-voice-engine"] == "openai"
            assert r.content == b"ID3host|" + _shaped(text).encode()
        assert len(pair.host.calls) == 3

    def test_the_key_is_the_routes_own_shaping(self, pair, monkeypatch):
        """A name the voice shaping changes ("mcp-fences" → "M C P fences"
        or whatever ``spoken_text.shape`` makes of it) is pinned under the
        SHAPED text, exactly as ``/speak`` looks it up."""
        pair.names[:] = ["mcp-fences-order", "sitecheck-bot7"]
        routes.PREWARM.run_once(wait=2)
        for name in pair.names:
            r = _speak(name + ".")
            assert r.headers["x-voice-cache"] == "hit", name

    def test_a_second_run_with_nothing_new_says_nothing(self, pair):
        routes.PREWARM.run_once(wait=2)
        assert routes.PREWARM.run_once(wait=2) == 0
        assert routes.PREWARM.last["done"] is True
        assert len(pair.host.calls) == 3

    def test_a_new_session_is_added_and_a_gone_one_unpinned(self, pair):
        routes.PREWARM.run_once(wait=2)
        pair.names[:] = ["c4-names", "fresh-one"]
        assert routes.PREWARM.run_once(wait=2) == 1
        assert pair.host.calls[-1] == _shaped("fresh-one.")
        assert _speak("fresh-one.").headers["x-voice-cache"] == "hit"
        assert _speak("hackathon-2.").headers["x-voice-cache"] == "miss"

    def test_bounded(self, pair, monkeypatch):
        pair.names[:] = ["s%d" % i for i in range(100)]
        monkeypatch.setattr(routes, "PREWARM_MAX_NAMES", 5)
        engine, _sig, wanted = routes.PREWARM.plan()
        assert engine is pair.host
        assert [s for _k, s in wanted] == [_shaped("s%d." % i) for i in range(5)] + [
            "Answer it on the screen."
        ]
        monkeypatch.setattr(routes, "PREWARM_MAX_NAMES", 100)
        monkeypatch.setattr(routes.SPEAK_CACHE, "max_pinned", 7)
        assert len(routes.PREWARM.plan()[2]) == 7

    def test_nothing_for_an_engine_that_costs_money(self, pair, monkeypatch):
        monkeypatch.setattr(routes, "_prewarmable", lambda engine: False)
        assert routes.PREWARM.run_once(wait=1) == 0
        assert pair.host.calls == []

    def test_down_host_prewarms_the_fallback_that_would_speak(self, pair):
        speech.TTS_CHAIN.mark_down(pair.host, "refused")
        assert routes.PREWARM.plan()[0] is pair.piper
        routes.PREWARM.run_once(wait=2)
        assert pair.host.calls == [] and len(pair.piper.calls) == 3
        r = _speak("hackathon-2.")
        assert r.headers["x-voice-engine"] == "local"
        assert r.headers["x-voice-cache"] == "hit"

    def test_a_dead_host_stops_the_run_and_is_marked_down(self, pair, monkeypatch):
        def dead(text, voice=""):
            pair.host.calls.append(text)
            raise speech.SpeechError("refused", down=True)

        monkeypatch.setattr(pair.host, "speak", dead)
        assert routes.PREWARM.run_once(wait=2) == 0
        assert pair.host.calls == [_shaped("hackathon-2.")]  # one try, then stop
        assert speech.TTS_CHAIN.down_reason(pair.host) == "refused"


class TestInvalidation:
    def test_a_voice_change_invalidates_the_pins(self, pair):
        routes.PREWARM.run_once(wait=2)
        S.update_settings(voice={"tts_voice": "onyx"})
        S.invalidate()
        assert _speak("hackathon-2.").headers["x-voice-cache"] == "miss"
        before = len(pair.host.calls)
        assert routes.PREWARM.run_once(wait=2) == 2  # hackathon-2 is cached now
        assert len(pair.host.calls) == before + 2
        assert _speak("c4-names.").headers["x-voice-cache"] == "hit"

    def test_an_engine_change_invalidates_the_pins(self, pair):
        routes.PREWARM.run_once(wait=2)
        S.update_settings(voice={"tts_backend": "local"})
        S.invalidate()
        r = _speak("hackathon-2.")
        assert r.headers["x-voice-engine"] == "local"
        assert r.headers["x-voice-cache"] == "miss"

    def test_a_reset_chain_stops_a_run(self, pair):
        pair.host.hold = threading.Event()
        t = threading.Thread(target=routes.PREWARM.run_once, kwargs={"wait": 2})
        t.start()
        assert pair.host.at_host.wait(2)
        speech.TTS_CHAIN.reset()
        pair.host.hold.set()
        t.join(3)
        assert routes.PREWARM.last["why"] == "the voice changed"
        assert pair.host.calls == [_shaped("hackathon-2.")]


class TestPinnedStore:
    def test_pins_never_evict_recent_lines_and_vice_versa(self):
        c = routes.SpeakCache()
        sig = ("e", 1)
        for i in range(c.max_entries):
            c.put(sig, ("", "line %d." % i), b"x" * 10, "audio/wav")
        for i in range(c.max_pinned + 5):
            c.pin(sig, ("", "name %d." % i), b"y" * 10, "audio/wav")
        assert len(c) == c.max_entries
        assert c.get(sig, ("", "line 0.")) is not None
        assert c.pin_count() == c.max_pinned  # full: the rest refused
        for i in range(c.max_entries * 2):
            c.put(sig, ("", "more %d." % i), b"x" * 10, "audio/wav")
        assert c.pin_count() == c.max_pinned
        assert c.get(sig, ("", "name 0.")) == (b"y" * 10, "audio/wav")

    def test_byte_bound(self, monkeypatch):
        c = routes.SpeakCache()
        monkeypatch.setattr(c, "max_pinned_bytes", 25)
        assert c.pin(("e",), ("", "a."), b"1" * 10, "m")
        assert c.pin(("e",), ("", "b."), b"1" * 10, "m")
        assert not c.pin(("e",), ("", "c."), b"1" * 10, "m")
        assert c.pinned_bytes == 20

    def test_another_engines_lines_do_not_drop_pins(self):
        """A reply on the fallback voice (the LRU switching signature) keeps
        the configured voice's pins; they are served only for their own."""
        c = routes.SpeakCache()
        c.pin(("host",), ("", "a."), b"h", "m")
        c.put(("piper",), ("", "b."), b"p", "m")
        assert c.get(("piper",), ("", "a.")) is None
        assert c.get(("host",), ("", "a.")) == (b"h", "m")


# --------------------------------------------------------------------------- #
# Never ahead of a real line
# --------------------------------------------------------------------------- #
class TestPriority:
    def test_a_real_line_queues_ahead_of_waiting_prewarm_lines(self, pair):
        chain = speech.TTS_CHAIN
        pair.host.hold = threading.Event()
        order = pair.host.calls
        first = threading.Thread(
            target=chain.prewarm_line, args=(pair.host, "Warm one.")
        )
        first.start()
        assert pair.host.at_host.wait(2)
        queued = threading.Thread(
            target=chain.prewarm_line, args=(pair.host, "Warm two.")
        )
        queued.start()
        for _ in range(200):
            if len(chain._lanes[pair.host.health_key()].queue) == 2:
                break
            time.sleep(0.005)
        real = threading.Thread(target=chain.send, args=(pair.host, "Real line.", 5.0))
        real.start()
        for _ in range(200):
            if len(chain._lanes[pair.host.health_key()].queue) == 3:
                break
            time.sleep(0.005)
        pair.host.hold.set()
        for t in (first, queued, real):
            t.join(3)
        assert order == ["Warm one.", "Real line.", "Warm two."]

    def test_a_prewarm_line_at_the_host_does_not_stretch_a_real_lines_budget(
        self, pair, monkeypatch
    ):
        """The prewarm's line is nobody's reply: a real line behind it gets
        only its own budget, then piper — never the prewarm's time too."""
        monkeypatch.setattr(speech, "HOST_BUDGET", 0.2)
        monkeypatch.setattr(speech, "HOST_BUDGET_PER_CHAR", 0.0)
        pair.host.hold = threading.Event()
        warm = threading.Thread(
            target=speech.TTS_CHAIN.prewarm_line, args=(pair.host, "Warm one.")
        )
        warm.start()
        assert pair.host.at_host.wait(2)
        t0 = time.monotonic()
        r = _speak("Hello there.")
        took = time.monotonic() - t0
        pair.host.hold.set()
        warm.join(3)
        assert r.status_code == 200, r.text
        assert r.headers["x-voice-engine"] == "local"
        assert took < 1.0

    def test_the_prewarm_waits_for_quiet(self, pair, monkeypatch):
        """Nothing is said while a line plays (``note_line``) or is in
        flight: the run gives up after its wait, having said nothing."""
        monkeypatch.setattr(routes, "PREWARM_QUIET_S", 5.0)
        speech.TTS_CHAIN.note_line("A reply that is still playing.", "phone")
        assert routes.PREWARM.run_once(wait=0.2) == 0
        assert routes.PREWARM.last["why"] == "never quiet"
        assert pair.host.calls == []

    def test_the_prewarm_waits_while_a_phrase_is_heard(self, pair, monkeypatch):
        from backend.web.core import speech_stream

        monkeypatch.setattr(routes, "PREWARM_QUIET_S", 5.0)
        monkeypatch.setattr(
            speech_stream,
            "_STREAMS",
            {"s1": SimpleNamespace(touched=speech_stream._now())},
        )
        monkeypatch.setattr(speech, "_now", lambda: 1e12)  # the voice: long idle
        assert routes._hearing()
        assert routes.PREWARM.run_once(wait=0.2) == 0
        assert pair.host.calls == []
        monkeypatch.setattr(speech_stream, "_STREAMS", {})
        assert not routes._hearing()
        assert routes.PREWARM.run_once(wait=2) == 3

    def test_idle_for(self, pair, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        chain = speech.TTS_CHAIN
        assert chain.idle_for() == 1000.0  # nothing ever said
        chain.note_line("x" * 28, "phone")  # 2 s of audio
        assert chain.idle_for() == 0.0
        now[0] += 5
        assert chain.idle_for() == pytest.approx(3.0)
        chain.note_line("x" * 28, speech.PREWARM_LISTENER)  # not a listener
        assert chain.idle_for() == pytest.approx(3.0)

    def test_a_real_line_joins_the_prewarms_synthesis_of_it(self, pair):
        pair.host.hold = threading.Event()
        run = threading.Thread(target=routes.PREWARM.run_once, kwargs={"wait": 2})
        run.start()
        assert pair.host.at_host.wait(2)

        async def main():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                task = asyncio.create_task(
                    ac.post("/api/voice/speak", json={"text": "hackathon-2."})
                )
                await asyncio.sleep(0.1)
                pair.host.hold.set()
                return await task

        r = asyncio.run(main())
        run.join(3)
        assert r.status_code == 200, r.text
        assert r.headers["x-voice-shared"] == "1"
        assert r.headers["x-voice-engine"] == "openai"
        assert pair.host.calls.count(_shaped("hackathon-2.")) == 1


# --------------------------------------------------------------------------- #
# Kicks, names, engines
# --------------------------------------------------------------------------- #
class TestKick:
    def test_get_voice_and_speak_kick_a_run_rate_limited(self, pair, monkeypatch):
        started: list = []
        monkeypatch.setattr(
            routes, "_prewarm_thread", lambda target, name: started.append(name)
        )
        assert client.get("/api/voice").status_code == 200
        assert started == ["mindflock-voice-prewarm"]
        routes.PREWARM._running = False  # the recorded run "finished"
        client.get("/api/voice")
        _speak("Hello.")
        assert len(started) == 1  # within PREWARM_RECHECK_S
        assert routes.PREWARM.kick(force=True)
        assert len(started) == 2

    def test_one_run_at_a_time(self, pair, monkeypatch):
        started: list = []
        monkeypatch.setattr(
            routes, "_prewarm_thread", lambda target, name: started.append(name)
        )
        assert routes.PREWARM.kick()
        assert not routes.PREWARM.kick(force=True)
        assert len(started) == 1


class TestNames:
    def test_session_names(self, monkeypatch):
        from backend.web.core import aliases, events, remote
        from backend.web.routes import peer_sessions

        monkeypatch.setattr(
            events,
            "sessions_snapshot",
            lambda: [
                {"title": "old", "branch": ""},
                {"title": "new", "branch": ""},
                {"title": "verify-x", "hidden_by": "verify"},
                {"title": ""},
            ],
        )
        monkeypatch.setattr(
            aliases,
            "overlay",
            lambda rows: [
                r.__setitem__("name", "renamed") for r in rows if r["title"] == "old"
            ],
        )
        monkeypatch.setattr(speech, "session_titles", lambda: ["new", "old"])
        monkeypatch.setattr(
            remote, "merged_instances", lambda: [{"title": "mac::a", "name": "a"}]
        )
        monkeypatch.setattr(
            peer_sessions,
            "peer_rows",
            lambda: [{"title": "bob::b", "display_title": "b"}],
        )
        assert routes._session_names() == ["new", "renamed", "a", "bob::b", "b"]

    def test_prewarmable(self, monkeypatch):
        host = speech.OpenAITTS()
        S.update_settings(voice={"tts_url": "http://127.0.0.1:8770/v1"})
        S.invalidate()
        assert routes._prewarmable(host)
        S.update_settings(voice={"tts_url": "https://api.openai.com/v1"})
        S.invalidate()
        assert not routes._prewarmable(host)
        assert routes._prewarmable(speech.LocalTTS())
        assert not routes._prewarmable(_Tts("fake", False, b""))


# --------------------------------------------------------------------------- #
# The chain's low priority, unit by unit
# --------------------------------------------------------------------------- #
def _lane_with(chain, engine, specs):
    """``engine``'s lane holding ``specs`` — ``(text, low, started)`` — and
    marked busy, so a ``send`` only queues (no drain thread says anything)."""
    key = engine.health_key()
    lane = speech._Lane(key)
    lane.busy = True
    for text, low, started in specs:
        ln = speech._Line(engine, text, "", 5.0)
        ln.low, ln.started, ln.gen = low, started, chain.generation
        if started:
            ln.sent_at = time.monotonic()
        lane.queue.append(ln)
    chain._lanes[key] = lane
    return lane


def _wait_len(lane, n):
    for _ in range(400):
        if len(lane.queue) == n:
            return True
        time.sleep(0.005)
    return False


def _finish(chain, lane):
    with chain._lock:
        for ln in lane.queue:
            ln.done, ln.result = True, (b"", "audio/wav")
        chain._cond.notify_all()


class TestChainLow:
    def test_real_lines_go_ahead_of_every_waiting_low_line_in_fifo(self, pair):
        chain = speech.TTS_CHAIN
        lane = _lane_with(
            chain,
            pair.host,
            [("A", True, True), ("B", True, False), ("C", True, False)],
        )
        senders = []
        for n, text in enumerate(("R1", "R2"), start=4):
            t = threading.Thread(target=chain.send, args=(pair.host, text, 5.0))
            t.start()
            senders.append(t)
            assert _wait_len(lane, n)
        try:
            assert [ln.text for ln in lane.queue] == ["A", "R1", "R2", "B", "C"]
        finally:
            _finish(chain, lane)
            for t in senders:
                t.join(3)

    def test_a_low_line_queues_last(self, pair):
        chain = speech.TTS_CHAIN
        lane = _lane_with(chain, pair.host, [("A", True, True), ("R", False, False)])
        t = threading.Thread(
            target=chain.send,
            args=(pair.host, "W", 5.0),
            kwargs={"listener": speech.PREWARM_LISTENER, "low": True},
        )
        t.start()
        assert _wait_len(lane, 3)
        try:
            assert [ln.text for ln in lane.queue] == ["A", "R", "W"]
        finally:
            _finish(chain, lane)
            t.join(3)

    def test_eta_skips_waiting_low_lines_only_for_a_real_line(self, pair):
        chain = speech.TTS_CHAIN
        key = pair.host.health_key()
        with chain._lock:
            chain._learn(key, "a sample line.", 1.0)
        now = time.monotonic()
        bare = _lane_with(chain, pair.host, [])
        with chain._lock:
            alone = chain._eta(key, bare, "Real.", now)
        waiting = _lane_with(
            chain, pair.host, [("Warm one.", True, False), ("Warm two.", True, False)]
        )
        with chain._lock:
            assert chain._eta(key, waiting, "Real.", now) == pytest.approx(alone)
            # A low line itself waits behind everything ahead of it.
            assert chain._eta(key, waiting, "Real.", now, low=True) > alone
        started = _lane_with(chain, pair.host, [("Warm one.", True, True)])
        started.queue[0].sent_at = now
        with chain._lock:
            # What is left of the one at the host counts: it is not stopped.
            assert chain._eta(key, started, "Real.", now) > alone

    def test_idle_for_counts_every_lane_and_real_listener(self, pair, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        chain = speech.TTS_CHAIN
        assert chain.idle_for() == 1000.0
        # A warm or probe holds its lane: not idle.
        _lane_with(chain, pair.host, [(speech.WARM_TEXT, False, True)])
        assert chain.idle_for() == 0.0
        chain._lanes.clear()
        # A real listener's line in flight: not idle.
        with chain._lock:
            chain._reply("id:phone").inflight = 1
        assert chain.idle_for() == 0.0
        with chain._lock:
            chain._reply("id:phone").inflight = 0
            # The prewarm's own line in flight is nobody's.
            chain._reply(speech.PREWARM_LISTENER).inflight = 1
            chain._reply(speech.PREWARM_LISTENER).quiet_at = now[0] + 50
        assert chain.idle_for() == 1000.0
        # A served line still playing: not idle until it ends.
        with chain._lock:
            chain._reply("id:phone").quiet_at = now[0] + 2
        assert chain.idle_for() == 0.0
        now[0] += 5
        assert chain.idle_for() == pytest.approx(3.0)

    def test_reset_forgets_the_last_real_line_and_bumps_the_generation(
        self, pair, monkeypatch
    ):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        chain = speech.TTS_CHAIN
        gen = chain.generation
        assert chain.generation == gen  # reading it changes nothing
        chain.speak(pair.piper, "Hello.", listener="id:phone")
        now[0] += 100
        # Idle since "Hello." finished playing (note_line), under 100 s.
        assert 99.0 < chain.idle_for() < 100.0
        chain.reset()
        assert chain.generation == gen + 1
        assert chain.idle_for() == 1100.0
        chain.reset()
        assert chain.generation == gen + 2

    def test_speak_stamps_real_at_on_the_way_out_even_when_it_fails(
        self, pair, monkeypatch
    ):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        chain = speech.TTS_CHAIN

        def boom(text, voice=""):
            assert chain._real_at == 1000.0  # stamped on the way in
            now[0] += 30  # the line took 30 s, then failed
            raise speech.SpeechError("bad request")

        monkeypatch.setattr(pair.piper, "speak", boom)
        with pytest.raises(speech.SpeechError):
            chain.speak(pair.piper, "Hello.", listener="id:phone")
        assert chain._real_at == 1030.0
        assert chain._replies["id:phone"].inflight == 0

    def test_speak_stamps_real_at_when_it_falls_back(self, pair, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        chain = speech.TTS_CHAIN

        def dead(text, voice=""):
            now[0] += 7
            raise speech.SpeechError("refused", down=True)

        monkeypatch.setattr(pair.host, "speak", dead)
        _audio, _mime, by, fell_back = chain.speak(pair.host, "Hello.", "", "id:p")
        assert by is pair.piper and fell_back
        assert chain._real_at == 1007.0


class TestPrewarmLine:
    def test_a_non_serial_engine_is_called_directly(self, pair):
        chain = speech.TTS_CHAIN
        audio, _mime = chain.prewarm_line(pair.piper, "Warm.")
        assert audio == b"RIFFpiper|Warm."
        assert pair.piper.calls == ["Warm."]
        assert chain._lanes == {} and chain._replies == {}

    def test_a_serial_host_makes_no_reply_for_anyone(self, pair):
        chain = speech.TTS_CHAIN
        audio, _mime = chain.prewarm_line(pair.host, "Warm.")
        assert audio == b"ID3host|Warm."
        assert all(who == speech.PREWARM_LISTENER for who in chain._replies)
        assert chain._lanes[pair.host.health_key()].queue == []

    @pytest.mark.parametrize("serial", [True, False])
    def test_a_failure_is_the_callers(self, pair, monkeypatch, serial):
        engine = pair.host if serial else pair.piper

        def dead(text, voice=""):
            engine.calls.append(text)
            raise speech.SpeechError("refused", down=True)

        monkeypatch.setattr(engine, "speak", dead)
        with pytest.raises(speech.SpeechError):
            speech.TTS_CHAIN.prewarm_line(engine, "Warm.")
        # No fallback said it, and the chain marked nothing down itself.
        assert pair.piper.calls == ([] if serial else ["Warm."])
        assert speech.TTS_CHAIN.down_reason(engine) == ""

    def test_its_answer_is_a_speed_sample(self, pair):
        chain = speech.TTS_CHAIN
        assert chain.speed(pair.host)["samples"] == 0
        chain.prewarm_line(pair.host, "Warm.")
        assert chain.speed(pair.host)["samples"] == 1

    def test_a_slow_prewarm_answer_makes_the_host_too_slow_for_real_lines(
        self, pair, monkeypatch
    ):
        """Pinned as a choice: a prewarm line is the host's real pace (it is
        not a cold load), so a slow one sends the next reply to piper."""
        monkeypatch.setattr(speech, "host_budget", lambda text: 0.05)
        speak = pair.host.speak

        def slow(text, voice=""):
            time.sleep(0.2)
            return speak(text, voice)

        monkeypatch.setattr(pair.host, "speak", slow)
        speech.TTS_CHAIN.prewarm_line(pair.host, "Warm.")
        assert speech.TTS_CHAIN.too_slow(pair.host)
        engine, fell_back, _why = speech.TTS_CHAIN.pick(pair.host, "id:phone")
        assert engine is pair.piper and fell_back


# --------------------------------------------------------------------------- #
# The pinned store, edge by edge
# --------------------------------------------------------------------------- #
class TestPinnedStoreEdges:
    def test_a_pin_is_served_only_for_its_own_signature(self):
        c = routes.SpeakCache()
        c.pin(("A",), ("", "a."), b"a", "m")
        assert c.get(("B",), ("", "a.")) is None  # B's LRU is empty too
        assert c.get(("A",), ("", "a.")) == (b"a", "m")

    def test_a_new_lru_signature_keeps_the_pins_and_clear_drops_both(self):
        c = routes.SpeakCache()
        c.pin(("host",), ("", "a."), b"12345", "m")
        c.put(("host",), ("", "b."), b"x", "m")
        c.put(("piper",), ("", "c."), b"y", "m")  # _sync: a new LRU signature
        assert c.get(("host",), ("", "b.")) is None  # the LRU was dropped
        assert c.pinned(("host",), ("", "a."))
        assert c.pinned_bytes == 5
        c.clear()
        assert c.pin_count() == 0 and c.pinned_bytes == 0 and len(c) == 0
        assert c.get(("host",), ("", "a.")) is None

    def test_repinning_adjusts_the_bytes(self):
        c = routes.SpeakCache()
        assert c.pin(("e",), ("", "a."), b"1" * 10, "m")
        assert c.pin(("e",), ("", "a."), b"1" * 30, "m")
        assert c.pinned_bytes == 30
        assert c.pin(("e",), ("", "a."), b"1" * 4, "m")
        assert c.pinned_bytes == 4 and c.pin_count() == 1

    def test_a_clip_too_big_is_refused(self, monkeypatch):
        c = routes.SpeakCache()
        monkeypatch.setattr(c, "max_entry_bytes", 8)
        assert not c.pin(("e",), ("", "a."), b"1" * 9, "m")
        assert c.pin_count() == 0 and c.pinned_bytes == 0

    def test_a_full_set_refuses_a_new_key_but_repins_an_old_one(self):
        c = routes.SpeakCache()
        for i in range(c.max_pinned):
            assert c.pin(("e",), ("", "n%d." % i), b"x", "m")
        assert not c.pin(("e",), ("", "one more."), b"x", "m")
        assert c.pin(("e",), ("", "n0."), b"xy", "m")
        assert c.pin_count() == c.max_pinned
        assert c.pinned_bytes == c.max_pinned + 1

    def test_keep_pins(self):
        c = routes.SpeakCache()
        c.pin(("e",), ("", "a."), b"1" * 3, "m")
        c.pin(("e",), ("", "b."), b"1" * 5, "m")
        c.keep_pins(("e",), {("", "a.")})
        assert c.pinned(("e",), ("", "a.")) and not c.pinned(("e",), ("", "b."))
        assert c.pinned_bytes == 3
        c.keep_pins(("other",), {("", "a.")})  # a new signature: all gone
        assert c.pin_count() == 0 and c.pinned_bytes == 0

    def test_peek_does_not_touch_the_order(self):
        c = routes.SpeakCache()
        c.put(("e",), ("", "old."), b"o", "m")
        c.put(("e",), ("", "new."), b"n", "m")
        assert c.peek(("e",), ("", "old.")) == (b"o", "m")
        assert list(c._items) == [("", "old."), ("", "new.")]
        assert c.peek(("x",), ("", "old.")) is None
        assert c._sig == ("e",)  # a peek for another signature drops nothing


# --------------------------------------------------------------------------- #
# Prewarm runs: plan, peek, stop reasons
# --------------------------------------------------------------------------- #
class TestRun:
    def test_a_line_already_spoken_is_pinned_without_a_synthesis(self, pair):
        _engine, sig, wanted = routes.PREWARM.plan()
        key, spoken = wanted[0]
        routes.SPEAK_CACHE.put(sig, key, b"spoken-already", "audio/mpeg")
        assert routes.PREWARM.run_once(wait=2) == 2
        assert spoken not in pair.host.calls
        assert routes.SPEAK_CACHE.pinned(sig, key)
        routes.SPEAK_CACHE.clear()  # the LRU gone: the pin still answers
        routes.SPEAK_CACHE.pin(sig, key, b"spoken-already", "audio/mpeg")
        assert _speak("hackathon-2.").content == b"spoken-already"

    def test_too_slow_host_prewarms_the_fallback(self, pair):
        chain = speech.TTS_CHAIN
        key = pair.host.health_key()
        with chain._lock:
            chain._learn(key, "a line.", 10 * speech.host_budget("a line."))
        assert chain.too_slow(pair.host)
        assert chain.down_reason(pair.host) == ""
        assert routes.PREWARM.plan()[0] is pair.piper
        routes.PREWARM.run_once(wait=2)
        assert pair.host.calls == [] and len(pair.piper.calls) == 3

    def test_nothing_to_prewarm_for(self, pair, monkeypatch):
        monkeypatch.setattr(speech, "select", lambda kind: (None, "no voice"))
        assert routes.PREWARM.plan() == (None, None, [])
        assert routes.PREWARM.run_once(wait=1) == 0
        assert routes.PREWARM.last["why"] == "nothing to prewarm for"

    def test_a_down_host_with_no_fallback_prewarms_nothing(self, pair, monkeypatch):
        monkeypatch.setattr(
            speech.TtsChain, "fallback_for", staticmethod(lambda p: None)
        )
        speech.TTS_CHAIN.mark_down(pair.host, "refused")
        assert routes.PREWARM.plan() == (None, None, [])

    def test_names_that_shape_alike_are_one_line(self, pair, monkeypatch):
        pair.names[:] = ["same", "same.", "Answer it on the screen"]
        _e, _s, wanted = routes.PREWARM.plan()
        assert [s for _k, s in wanted] == [_shaped("same."), "Answer it on the screen."]

    def test_capped_at_the_pinned_set_even_with_every_name_and_line(
        self, pair, monkeypatch
    ):
        pair.names[:] = ["s%d" % i for i in range(100)]
        monkeypatch.setattr(routes, "PREWARM_MAX_NAMES", 100)
        monkeypatch.setattr(
            routes, "PREWARM_FIXED", tuple("Line %d." % i for i in range(8))
        )
        wanted = routes.PREWARM.plan()[2]
        assert len(wanted) == routes.SpeakCache.max_pinned
        assert wanted[-1][1] == _shaped("s%d." % (routes.SpeakCache.max_pinned - 1))

    def test_the_engine_swapped_mid_run_stops_it(self, pair, monkeypatch):
        """The registry's engine is replaced (Settings rebuilt it) after the
        first line: the run stops before the second."""
        other = _Tts("openai", True, b"other")
        real_get = speech.get

        def get(kind, backend_id):
            if kind == "tts" and pair.host.calls:
                return other
            return real_get(kind, backend_id)

        monkeypatch.setattr(speech, "get", get)
        assert routes.PREWARM.run_once(wait=2) == 1
        assert routes.PREWARM.last["why"] == "the voice changed"
        assert pair.host.calls == [_shaped("hackathon-2.")] and other.calls == []

    def test_a_failure_that_is_not_down_stops_without_marking_down(
        self, pair, monkeypatch
    ):
        def bad(text, voice=""):
            raise speech.SpeechError("bad voice name")

        monkeypatch.setattr(pair.host, "speak", bad)
        assert routes.PREWARM.run_once(wait=2) == 0
        assert routes.PREWARM.last["why"] == "SpeechError: bad voice name"
        assert speech.TTS_CHAIN.down_reason(pair.host) == ""

    def test_pins_stay_consistent_beside_real_traffic(self, pair):
        """The run on its own thread while ``/speak`` churns the LRU with
        other lines: nothing raises, and the pinned bytes add up."""
        pair.names[:] = ["s%d" % i for i in range(20)]
        errors: list = []

        def run():
            try:
                routes.PREWARM.run_once(wait=5)
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        t = threading.Thread(target=run)
        t.start()
        for i in range(30):
            assert _speak("Churn line %d." % i).status_code == 200
        t.join(10)
        assert not t.is_alive() and errors == []
        c = routes.SPEAK_CACHE
        with c._plock:
            assert c.pinned_bytes == sum(len(a) for a, _m in c._pins.values())
        assert c.pin_count() == 21


# --------------------------------------------------------------------------- #
# /speak joining the prewarm: timeout, failure, headers
# --------------------------------------------------------------------------- #
class TestJoin:
    def test_a_join_past_the_budget_falls_through_and_the_pin_still_lands(
        self, pair, monkeypatch
    ):
        monkeypatch.setattr(speech, "HOST_BUDGET", 0.2)
        monkeypatch.setattr(speech, "HOST_BUDGET_PER_CHAR", 0.0)
        pair.host.hold = threading.Event()
        run = threading.Thread(target=routes.PREWARM.run_once, kwargs={"wait": 2})
        run.start()
        assert pair.host.at_host.wait(2)
        _e, sig, wanted = routes.PREWARM.plan()
        key = wanted[0][0]
        fut = routes.PREWARM.joining(sig, key)
        assert fut is not None
        r = _speak("hackathon-2.")
        pair.host.hold.set()
        run.join(3)
        assert r.status_code == 200, r.text
        assert "x-voice-shared" not in r.headers
        assert r.headers["x-voice-engine"] == "local"  # its own line: piper
        assert not fut.cancelled()
        assert fut.result()[0] == b"ID3host|" + _shaped("hackathon-2.").encode()
        assert routes.SPEAK_CACHE.pinned(sig, key)

    def test_a_join_on_a_failed_prewarm_says_it_as_usual(self, pair, monkeypatch):
        pair.host.hold = threading.Event()
        speak = pair.host.speak
        first = [True]

        def once_bad(text, voice=""):
            if first[0]:
                first[0] = False
                pair.host.at_host.set()
                pair.host.hold.wait(5)
                raise speech.SpeechError("bad request")
            return speak(text, voice)

        monkeypatch.setattr(pair.host, "speak", once_bad)
        run = threading.Thread(target=routes.PREWARM.run_once, kwargs={"wait": 2})
        run.start()
        assert pair.host.at_host.wait(2)

        async def main():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                task = asyncio.create_task(
                    ac.post("/api/voice/speak", json={"text": "hackathon-2."})
                )
                await asyncio.sleep(0.1)
                pair.host.hold.set()
                return await task

        r = asyncio.run(main())
        run.join(3)
        assert r.status_code == 200, r.text
        assert "x-voice-shared" not in r.headers
        assert r.headers["x-voice-engine"] == "openai"
        assert r.content == b"ID3host|" + _shaped("hackathon-2.").encode()
        assert routes.PREWARM.last["why"] == "SpeechError: bad request"

    def test_a_joined_line_is_a_miss_on_the_engine_and_noted_for_the_listener(
        self, pair
    ):
        pair.host.hold = threading.Event()
        run = threading.Thread(target=routes.PREWARM.run_once, kwargs={"wait": 2})
        run.start()
        assert pair.host.at_host.wait(2)

        async def main():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                task = asyncio.create_task(
                    ac.post(
                        "/api/voice/speak",
                        json={"text": "hackathon-2.", "client": "phone"},
                    )
                )
                await asyncio.sleep(0.1)
                pair.host.hold.set()
                return await task

        r = asyncio.run(main())
        run.join(3)
        assert r.headers["x-voice-shared"] == "1"
        assert r.headers["x-voice-backend"] == "openai"
        # Not served from the cache: the synthesis was shared, not stored.
        assert r.headers["x-voice-cache"] == "miss"
        assert speech.TTS_CHAIN._replies["id:phone"].quiet_at > 0


# --------------------------------------------------------------------------- #
# Helpers: names, ends, engines, hearing, kicks
# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_a_broken_source_leaves_the_others(self, monkeypatch):
        from backend.web.core import events, remote
        from backend.web.routes import peer_sessions

        def broken():
            raise RuntimeError("no server")

        monkeypatch.setattr(events, "sessions_snapshot", broken)
        monkeypatch.setattr(remote, "merged_instances", lambda: [{"title": "m::a"}])
        monkeypatch.setattr(peer_sessions, "peer_rows", broken)
        assert routes._session_names() == ["m::a"]
        monkeypatch.setattr(
            events, "sessions_snapshot", lambda: [{"title": "local", "hidden_by": ""}]
        )
        monkeypatch.setattr(speech, "session_titles", lambda: [])
        monkeypatch.setattr(remote, "merged_instances", broken)
        monkeypatch.setattr(
            peer_sessions, "peer_rows", lambda: [{"title": "p::b", "hidden_by": "x"}]
        )
        assert routes._session_names() == ["local"]

    @pytest.mark.parametrize(
        "text, want",
        [
            ("a", "a."),
            ("a.", "a."),
            ("a!", "a!"),
            ("a?", "a?"),
            ("  a  ", "a."),
            ("", ""),
            (None, ""),
        ],
    )
    def test_end(self, text, want):
        assert routes._end(text) == want

    @pytest.mark.parametrize(
        "url, ok",
        [
            ("http://127.0.0.1:8770/v1", True),
            ("https://192.168.1.20/v1", True),
            ("https://voice.tail1234.ts.net/v1", True),
            ("https://api.openai.com/v1", False),
        ],
    )
    def test_prewarmable_host(self, url, ok):
        S.update_settings(voice={"tts_url": url})
        S.invalidate()
        assert routes._prewarmable(speech.OpenAITTS()) is ok

    def test_prewarmable_unreadable_host_and_system_voice(self, monkeypatch):
        from backend.web.core import speech_system

        host = speech.OpenAITTS()

        def broken():
            raise RuntimeError("bad settings")

        monkeypatch.setattr(host, "_cfg", broken)
        assert not routes._prewarmable(host)
        assert routes._prewarmable(speech_system.SystemTTS())

    def test_hearing(self, monkeypatch):
        from backend.web.core import speech_stream

        monkeypatch.setattr(speech_stream, "_now", lambda: 100.0)
        old = 100.0 - routes.PREWARM_QUIET_S - 1
        monkeypatch.setattr(
            speech_stream, "_STREAMS", {"s": SimpleNamespace(touched=old)}
        )
        assert not routes._hearing()
        monkeypatch.setattr(
            speech_stream, "_STREAMS", {"s": SimpleNamespace(touched=99.5)}
        )
        assert routes._hearing()
        monkeypatch.setattr(speech_stream, "_STREAMS", {"s": object()})
        assert not routes._hearing()  # a gate: never raises
        monkeypatch.delattr(speech_stream, "_STREAMS")
        assert not routes._hearing()

    def test_a_thread_that_will_not_start_frees_the_next_kick(self, monkeypatch):
        def no_thread(target, name):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(routes, "_prewarm_thread", no_thread)
        assert not routes.PREWARM.kick()
        assert routes.PREWARM._running is False
        started: list = []
        monkeypatch.setattr(
            routes, "_prewarm_thread", lambda target, name: started.append(target)
        )
        assert routes.PREWARM.kick(force=True)
        assert len(started) == 1

    def test_run_swallows_a_failure_and_frees_the_next_kick(self, monkeypatch):
        def broken(wait=0):
            raise RuntimeError("boom")

        monkeypatch.setattr(routes.PREWARM, "run_once", broken)
        monkeypatch.setattr(routes, "_prewarm_thread", lambda target, name: target())
        assert routes.PREWARM.kick()
        assert routes.PREWARM._running is False
        assert routes.PREWARM.kick(force=True)


# --------------------------------------------------------------------------- #
# Drift guard: the fixed lines are the clients' own
# --------------------------------------------------------------------------- #
_CLIENT_SOURCES = (
    "backend/web/static/mobile.js",
    "frontend/src/lib/voiceDesktop.ts",
    "frontend/src/components/TopBarMic.tsx",
    "backend/web/core/voice_router.py",
)


def test_every_fixed_line_is_still_said_by_a_client():
    """A client line reworded without :data:`routes.PREWARM_FIXED` loses its
    prewarm hit silently: each fixed line must appear, quoted, in a client."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = "\n".join((root / p).read_text(encoding="utf-8") for p in _CLIENT_SOURCES)
    missing = [
        line
        for line in routes.PREWARM_FIXED
        if not any(q + line + q in text for q in ("'", '"', "`"))
    ]
    assert missing == []
