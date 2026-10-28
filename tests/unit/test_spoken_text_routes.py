"""The speech boundary (routes/speech.py): ``POST /api/voice/speak`` shapes
the text for the voice (``core.spoken_text``) — never the caption, which
the client keeps — honours ``shape: false`` (the Settings "Try" button
hears a respelling as written), applies the person's ``voice.pronounce``
first, and serves repeats from the speak cache (``X-Voice-Cache: hit|miss``,
keyed by voice and shaped text, dropped when the backend or voice changes,
bounded in entries and bytes); ``GET /api/voice`` serves ``pronounce``.
Fakes for the engines; the settings store at a tmp file
(``isolate_settings_store`` — never the owner's ``~/.mindflock/settings.json``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.routes import speech as routes

client = TestClient(server.app)


class _Fake(speech.Backend):
    def __init__(self, kind="tts", ok=True, detail="fake is ready"):
        self.id, self.kind, self.label = "fake", kind, "fake"
        self.ok, self.detail = ok, detail
        self.audio = (b"ID3fake-mp3", "audio/mpeg")
        self.fail: BaseException | None = None
        self.calls: list = []

    def ready(self):
        return self.ok, self.detail

    def speak(self, text, voice=""):
        self.calls.append((text, voice))
        if self.fail:
            raise self.fail
        return self.audio


@pytest.fixture(autouse=True)
def _store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""
    routes.SPEAK_CACHE.clear()
    yield
    routes.SPEAK_CACHE.clear()


@pytest.fixture
def tts(monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [_Fake("stt")], "tts": [fake]})
    return fake


def _voice(**fields):
    S.update_settings(voice=fields)
    S.invalidate()


def _pronounce(monkeypatch, rows):
    """``voice.pronounce`` as the route reads it. The settings model
    (``backend/config/settings.py`` VoiceSettings) does not carry the field
    yet — see test_pronounce_persists_once_the_model_carries_it — so the
    rows ride on the loaded settings object."""
    real = routes._settings

    def with_rows():
        s = real()
        s.voice.pronounce = rows
        return s

    monkeypatch.setattr(routes, "_settings", with_rows)


def _speak(text, **extra):
    return client.post("/api/voice/speak", json={"text": text, **extra})


# --------------------------------------------------------------------------- #
# Shaping at the boundary
# --------------------------------------------------------------------------- #
class TestShaping:
    def test_the_voice_hears_the_shaped_text(self, tts):
        r = _speak("Once it is done everything goes live. See PR #275.")
        assert r.status_code == 200
        assert tts.calls == [("Once it is done everything goes lyve. See P R 275.", "")]
        assert r.headers["X-Voice-Backend"] == "fake"
        assert r.headers["X-Voice-Cache"] == "miss"

    def test_shape_false_sends_the_text_as_written(self, tts):
        r = _speak("goes live, lyve, SQL", shape=False)
        assert r.status_code == 200
        assert tts.calls == [("goes live, lyve, SQL", "")]

    def test_the_persons_pronunciations_come_first(self, tts, monkeypatch):
        _pronounce(
            monkeypatch,
            [{"say": "MindFlock", "as": "mind flock"}, {"say": "live", "as": "liv"}],
        )
        _speak("MindFlock goes live with JSON")
        assert tts.calls == [("Mind flock goes liv with jayson", "")]

    def test_the_cap_is_on_the_text_as_sent(self, tts):
        r = _speak("x" * (routes.MAX_SPEAK_CHARS + 1))
        assert r.status_code == 413
        assert tts.calls == []

    def test_a_line_that_shapes_to_nothing_is_a_400(self, tts):
        r = _speak("🚀 ✅")
        assert r.status_code == 400
        assert tts.calls == []

    def test_whitespace_only_text_is_refused_before_the_engine(self, tts):
        assert _speak("   ").status_code == 400
        assert tts.calls == []


# --------------------------------------------------------------------------- #
# The speak cache
# --------------------------------------------------------------------------- #
class TestCache:
    def test_a_repeat_is_a_hit_and_costs_no_engine_call(self, tts):
        a = _speak("Sent.")
        b = _speak("Sent.")
        assert (a.headers["X-Voice-Cache"], b.headers["X-Voice-Cache"]) == (
            "miss",
            "hit",
        )
        assert b.content == a.content and b.headers["content-type"].startswith(
            "audio/mpeg"
        )
        assert b.headers["X-Voice-Backend"] == "fake"
        assert len(tts.calls) == 1

    def test_the_key_is_the_shaped_text(self, tts):
        _speak("goes live")
        r = _speak("goes  live ")
        assert r.headers["X-Voice-Cache"] == "hit"
        assert len(tts.calls) == 1
        # ``shape: false`` text is its own key: "goes live" as written differs.
        assert _speak("goes live", shape=False).headers["X-Voice-Cache"] == "miss"

    def test_another_voice_is_another_entry(self, tts):
        _speak("Sent.")
        assert _speak("Sent.", voice="Donna").headers["X-Voice-Cache"] == "miss"
        assert _speak("Sent.", voice="Donna").headers["X-Voice-Cache"] == "hit"
        assert _speak("Sent.").headers["X-Voice-Cache"] == "hit"
        assert len(tts.calls) == 2

    def test_a_changed_backend_or_voice_setting_drops_the_cache(self, tts):
        _speak("Sent.")
        _voice(tts_voice="Donna")
        assert _speak("Sent.").headers["X-Voice-Cache"] == "miss"
        assert _speak("Sent.").headers["X-Voice-Cache"] == "hit"
        _voice(tts_voice="Donna", tts_url="http://127.0.0.1:8770/v1")
        assert _speak("Sent.").headers["X-Voice-Cache"] == "miss"
        assert len(tts.calls) == 3

    def test_a_failure_is_not_cached(self, tts):
        tts.fail = speech.SpeechError("host down")
        assert _speak("Sent.").status_code == 502
        tts.fail = None
        r = _speak("Sent.")
        assert r.status_code == 200 and r.headers["X-Voice-Cache"] == "miss"

    def test_entries_are_bounded_oldest_out(self, tts):
        n = routes.SPEAK_CACHE.max_entries
        for i in range(n + 1):
            _speak("line %d" % i)
        assert len(routes.SPEAK_CACHE) == n
        assert _speak("line 0").headers["X-Voice-Cache"] == "miss"  # the oldest went
        assert _speak("line %d" % n).headers["X-Voice-Cache"] == "hit"
        assert len(routes.SPEAK_CACHE) == n

    def test_bytes_are_bounded_and_a_huge_clip_is_not_kept(self, tts):
        tts.audio = (b"x" * routes.SPEAK_CACHE.max_entry_bytes, "audio/wav")
        _speak("big one")
        assert _speak("big one").headers["X-Voice-Cache"] == "hit"
        # Four more pass the byte budget: the first goes.
        for i in range(4):
            _speak("big %d" % i)
        assert routes.SPEAK_CACHE.bytes <= routes.SPEAK_CACHE.max_bytes
        assert _speak("big one").headers["X-Voice-Cache"] == "miss"
        tts.audio = (b"x" * (routes.SPEAK_CACHE.max_entry_bytes + 1), "audio/wav")
        _speak("too big")
        assert _speak("too big").headers["X-Voice-Cache"] == "miss"

    def test_a_hit_is_served_no_store_like_a_miss(self, tts):
        _speak("Sent.")
        assert _speak("Sent.").headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------- #
# GET /api/voice → pronounce
# --------------------------------------------------------------------------- #
class TestGet:
    def test_pronounce_is_empty_by_default(self, tts):
        body = client.get("/api/voice").json()
        assert body["pronounce"] == []

    def test_pronounce_serves_the_normalised_rows(self, tts, monkeypatch):
        _pronounce(
            monkeypatch,
            [{"say": " Live ", "as": "lyve"}, {"say": "live", "as": "again"}, "junk"],
        )
        body = client.get("/api/voice").json()
        assert body["pronounce"] == [{"say": "live", "as": "lyve"}]

    @pytest.mark.xfail(
        reason="VoiceSettings (backend/config/settings.py, outside this change) does not carry "
        "voice.pronounce yet; settings_sync.SYNCED needs it too",
        strict=True,
    )
    def test_pronounce_persists_once_the_model_carries_it(self, tts):
        _voice(pronounce=[{"say": "live", "as": "lyve"}])
        body = client.get("/api/voice").json()
        assert body["pronounce"] == [{"say": "live", "as": "lyve"}]
