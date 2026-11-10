"""The Moonshine streaming lane wired into the voice feature: which engine a
live transcript runs (``core.speech.stream_engine`` and ``voice.stt_stream_model``),
the model-cache keys it warms / keeps / sheds, what ``GET /api/voice``
reports, ``POST /api/voice/stream`` opening the right stream (and falling
back to whisper), and the doctor's line. Moonshine itself is faked: its
availability is a flag and its streams are the registry's plain ones.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend import memory_pressure
from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.core import speech_moonshine as sm
from backend.web.core import speech_stream
from backend.web.routes import speech as routes
from tests._bundle import in_bundle

client = TestClient(server.app)
KEY = sm.CACHE_KEY


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    speech.unload_all()
    speech_stream.clear()
    yield
    speech.unload_all()
    speech_stream.clear()


@pytest.fixture
def moon(monkeypatch):
    """Moonshine installed or not (``moon.on``), its engine a placeholder
    loaded through the real model cache."""
    box = SimpleNamespace(on=True, loads=0)

    def reason():
        return "" if box.on else "moonshine-voice is not installed"

    def engine():
        def load():
            box.loads += 1
            return object()

        return speech._cached(KEY, load)

    monkeypatch.setattr(sm, "unavailable_reason", reason)
    monkeypatch.setattr(sm, "available", lambda: box.on)
    monkeypatch.setattr(sm, "engine", engine)
    return box


@pytest.fixture
def whisper(monkeypatch):
    """faster-whisper "installed": loads go straight into the model cache."""
    loads: list = []

    def model(self, name=""):
        name = name or self._model_name()
        loads.append(name)
        return speech._cached("whisper:" + name, lambda: object())

    monkeypatch.setattr(speech, "_installed", lambda n: n == "faster_whisper")
    monkeypatch.setattr(speech.LocalSTT, "_model", model)
    monkeypatch.setattr(speech.LocalSTT, "_prime", staticmethod(lambda m, k="": None))
    return loads


def _set(**voice):
    S.update_settings(voice=voice)
    S.invalidate()


# --------------------------------------------------------------------------- #
# The engine choice
# --------------------------------------------------------------------------- #
class TestStreamEngine:
    @pytest.mark.parametrize(
        "raw, lang, on, engine",
        [
            ("", "", True, "moonshine"),  # the default: Moonshine when installed
            ("", "en-US", True, "moonshine"),
            ("", "en-GB", True, "moonshine"),
            ("", "", False, "whisper"),  # …else base.en
            ("moonshine", "", True, "moonshine"),
            (" Moonshine ", "en", True, "moonshine"),
            ("moonshine", "", False, "whisper"),  # asked for, not installed
            ("base.en", "", True, "whisper"),  # an explicit whisper name keeps whisper
            ("tiny.en", "en-US", True, "whisper"),
            ("org/some-whisper", "", True, "whisper"),
            ("", "de-DE", True, "whisper"),  # English only
            ("moonshine", "fr", True, "whisper"),
        ],
    )
    def test_the_choice(self, moon, raw, lang, on, engine):
        moon.on = on
        assert speech.stream_engine(raw, lang) == engine

    def test_a_broken_check_reads_as_whisper(self, moon, monkeypatch):
        def boom():
            raise RuntimeError("broken install")

        monkeypatch.setattr(sm, "available", boom)
        assert speech.stream_engine("", "") == "whisper"

    def test_moonshine_keeps_the_whisper_stream_model_for_its_name(self):
        # "moonshine" is not a whisper size: a whisper stream (the fallback,
        # the wake check) still runs base.en / base.
        assert speech.stream_whisper_model("moonshine", "") == "base.en"
        assert speech.stream_whisper_model("moonshine", "de") == "base"

    def test_settings_default_and_explicit_value_round_trip(self):
        assert S.VoiceSettings().stt_stream_model == ""
        assert (
            S.VoiceSettings.from_dict(
                {"stt_stream_model": " moonshine "}
            ).stt_stream_model
            == "moonshine"
        )
        _set(stt_stream_model="moonshine")
        assert S.load_settings().voice.stt_stream_model == "moonshine"


# --------------------------------------------------------------------------- #
# Keys: warm, resident, wake, shedding
# --------------------------------------------------------------------------- #
class TestKeys:
    def test_moonshine_replaces_base_en_in_the_warm_keys(self, moon):
        _set(stt_backend="local")
        stt = speech.LocalSTT()
        assert stt.stream_engine() == "moonshine"
        assert stt.stream_key() == KEY
        assert stt.stream_label() == "Moonshine"
        assert stt.warm_keys() == sorted([KEY, "whisper:small.en"])
        # The wake check still runs whisper's English streaming model.
        assert stt.wake_keys() == ["whisper:base.en"]
        # Another language's models too: its stream's base, its small.
        assert stt.resident_keys() == sorted(
            [
                KEY,
                "whisper:small.en",
                "whisper:base.en",
                "whisper:base",
                "whisper:small",
            ]
        )

    def test_without_moonshine_the_keys_are_todays(self, moon):
        moon.on = False
        _set(stt_backend="local")
        stt = speech.LocalSTT()
        assert stt.stream_key() == "whisper:base.en"
        assert stt.stream_label() == "Whisper base.en"
        assert stt.warm_keys() == ["whisper:base.en", "whisper:small.en"]
        assert stt.wake_keys() == []
        assert stt.resident_keys() == [
            "whisper:base",
            "whisper:base.en",
            "whisper:small",
            "whisper:small.en",
        ]

    def test_an_explicit_whisper_stream_keeps_whisper_keys(self, moon):
        _set(stt_backend="local", stt_stream_model="tiny.en")
        stt = speech.LocalSTT()
        assert stt.stream_key() == "whisper:tiny.en"
        assert KEY not in stt.warm_keys()

    def test_nobody_listening_keeps_only_the_moonshine_model(self, moon):
        _set(stt_backend="local")
        assert speech._resident() == frozenset({KEY})

    def test_the_boot_warm_loads_moonshine_not_base_en(self, moon, whisper):
        _set(stt_backend="local")
        assert speech.warm("stt") == "loaded"
        assert moon.loads == 1
        assert whisper == ["small.en"]
        assert speech.loaded() == sorted([KEY, "whisper:small.en"])
        assert speech.stt_loaded() is True
        assert speech.warm("stt") == "already"
        assert moon.loads == 1

    def test_a_moonshine_that_fails_to_load_still_warms_whisper(
        self, moon, whisper, monkeypatch
    ):
        def broken():
            raise speech.SpeechError("Moonshine failed to load: bad model")

        monkeypatch.setattr(sm, "engine", broken)
        _set(stt_backend="local")
        assert speech.warm("stt") == "loaded"
        assert speech.loaded() == ["whisper:small.en"]

    def test_a_listening_warm_also_loads_the_wake_model(self, moon, whisper):
        _set(stt_backend="local")
        speech.listen("standby-page")
        assert speech._warm_due("stt") is True
        speech.warm("stt")
        assert set(speech.loaded()) == {KEY, "whisper:small.en", "whisper:base.en"}
        assert speech._warm_due("stt") is False

    def test_low_memory_sheds_no_voice_model(self, moon, monkeypatch):
        """Owner 2026-11-10: voice models stay on when memory gets low."""
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        _set(stt_backend="system")  # nothing resident
        sm.engine()
        speech._WARMED["stt"] = ("loaded", "local:x", 0.0)
        speech._WARMED["tts"] = ("loaded", "local:piper", 0.0)
        now[0] += 500
        assert speech.shed_for_memory(min_idle_s=120) == []
        assert "stt" in speech._WARMED
        assert "tts" in speech._WARMED

    def test_shedding_keeps_the_resident_moonshine_model(self, moon, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: now[0])
        _set(stt_backend="local")
        sm.engine()
        now[0] += 500
        assert speech.shed_for_memory(min_idle_s=120) == []
        assert speech.loaded() == [KEY]

    def test_stt_keys(self):
        assert speech.is_stt_key("whisper:small")
        assert speech.is_stt_key(KEY)
        assert not speech.is_stt_key("piper:en_US-lessac-medium")
        assert not speech.is_stt_key("kokoro")


# --------------------------------------------------------------------------- #
# GET /api/voice
# --------------------------------------------------------------------------- #
class TestDescribe:
    def test_moonshine_live(self, moon, whisper):
        _set(stt_backend="local")
        body = client.get("/api/voice?warm=0").json()
        assert body["server_ready"]["stt_stream"] is True
        assert body["stream_engine"] == "moonshine"
        assert body["stream_model"] == sm.ARCH
        assert body["stream_label"] == "Moonshine"
        assert body["moonshine"] == {"available": True, "reason": ""}

    def test_whisper_live_says_why_not_moonshine(self, moon, whisper):
        moon.on = False
        _set(stt_backend="local")
        body = client.get("/api/voice?warm=0").json()
        assert body["stream_engine"] == "whisper"
        assert body["stream_model"] == "base.en"
        assert body["stream_label"] == "Whisper base.en"
        assert body["moonshine"] == {
            "available": False,
            "reason": "moonshine-voice is not installed",
        }

    def test_a_backend_that_does_not_stream_names_no_engine(self, moon):
        _set(stt_backend="system")
        body = client.get("/api/voice?warm=0").json()
        assert body["stream_engine"] == "" and body["stream_label"] == ""
        assert body["moonshine"]["available"] is True

    def test_the_served_bundle_offers_moonshine(self):
        """The committed build carries Settings → Voice's Moonshine row
        (rebuilt with ``npm run build``): the live-words hint, the install
        button, and an install POST that names ``moonshine`` while the
        local-models one still sends no body."""
        js = client.get("/app.js").text
        assert '"voice-live-words"' in js
        assert '"voice-moonshine-btn"' in js
        assert '"Install fast recognition (Moonshine, ~200 MB)"' in js
        assert '"Live words: Moonshine"' in js
        # One install POST for every kind (Moonshine, Kokoro): it names
        # `what`, and the local models' install still sends no body.
        assert in_bundle(
            '{ method: "POST", json: { what: opts.what } } : { method: "POST" }',
            js,
        )
        assert 'opts.what !== "local"' in js


# --------------------------------------------------------------------------- #
# POST /api/voice/stream
# --------------------------------------------------------------------------- #
class _Local(speech.Backend):
    """A ready streaming backend that picks its engine like LocalSTT."""

    id, kind, label, streams = "local", "stt", "local", True

    def __init__(self):
        self.engine = "moonshine"
        self.whisper_opens: list = []

    def ready(self):
        return True, "ready"

    def stream_engine(self, lang=""):
        return self.engine if sm.supports(lang) else "whisper"

    def stream_transcriber(self, lang="", hints=""):
        self.whisper_opens.append(lang)
        return lambda audio, prompt: []

    def final_transcriber(self, lang="", hints=""):
        def accurate(audio):
            return "whisper text"

        accurate.lane = "whisper:small"
        return accurate


@pytest.fixture
def local(monkeypatch):
    backend = _Local()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [backend], "tts": []})
    monkeypatch.setattr(speech, "stt_loaded", lambda: True)
    opened: list = []

    def open_stream(hints="", accurate=None, client="", engine_=None):
        if backend.engine == "boom":
            raise speech.SpeechError("Moonshine failed to load: bad model")
        opened.append((hints, accurate, client))
        return speech_stream.open_stream(lambda a, p: [], None, client)

    monkeypatch.setattr(routes._moonshine, "open_stream", open_stream)
    return SimpleNamespace(backend=backend, opened=opened)


class TestStreamOpen:
    def test_english_opens_moonshine_with_the_whisper_final(self, local):
        r = client.post("/api/voice/stream", json={"lang": "en-US"})
        assert r.status_code == 200
        body = r.json()
        assert body["engine"] == "moonshine"
        assert speech_stream.get_stream(body["id"]) is not None
        assert len(local.opened) == 1 and local.backend.whisper_opens == []
        hints, accurate, _client = local.opened[0]
        assert hints and getattr(accurate, "lane", "") == "whisper:small"

    def test_another_language_opens_whisper(self, local):
        r = client.post("/api/voice/stream", json={"lang": "de-DE"})
        assert r.json()["engine"] == "whisper"
        assert local.opened == [] and local.backend.whisper_opens == ["de-DE"]

    def test_whisper_chosen_opens_whisper(self, local):
        local.backend.engine = "whisper"
        r = client.post("/api/voice/stream", json={"lang": "en-US"})
        assert r.json()["engine"] == "whisper"
        assert local.opened == [] and local.backend.whisper_opens == ["en-US"]

    def test_a_moonshine_that_fails_to_load_falls_back_to_whisper(self, local):
        local.backend.engine = "boom"

        def pick(lang=""):
            return "moonshine"

        local.backend.stream_engine = pick
        r = client.post("/api/voice/stream", json={"lang": "en-US"})
        assert r.status_code == 200
        assert r.json()["engine"] == "whisper"
        assert local.backend.whisper_opens == ["en-US"]

    def test_a_backend_without_a_choice_streams_on_whisper(self, local):
        def broken(lang=""):
            raise RuntimeError("unreadable settings")

        local.backend.stream_engine = broken
        r = client.post("/api/voice/stream", json={"lang": "en-US"})
        assert r.json()["engine"] == "whisper"

    def test_the_cap_still_applies_to_moonshine(self, local, monkeypatch):
        monkeypatch.setattr(speech_stream, "MAX_STREAMS", 1)
        assert client.post("/api/voice/stream", json={"client": "a"}).status_code == 200
        r = client.post("/api/voice/stream", json={"client": "b"})
        assert r.status_code == 409


# --------------------------------------------------------------------------- #
# mindflock doctor
# --------------------------------------------------------------------------- #
class TestDoctor:
    def test_no_row_unless_local_recognition(self, moon):
        _set(stt_backend="system")
        assert doctor.check_fast_recognition() is None
        assert doctor.CHECKS_BY_ID["fast-recognition"] is doctor.check_fast_recognition
        assert doctor.group_of("fast-recognition") == "optional"

    def test_missing_moonshine_is_a_warning_with_the_install(self, moon):
        moon.on = False
        _set(stt_backend="local")
        c = doctor.check_fast_recognition()
        assert (c.id, c.status, c.group) == ("fast-recognition", "warn", "optional")
        assert c.detail == (
            "live words: Whisper base.en — moonshine-voice is not installed"
        )
        assert "Install fast recognition (Moonshine, ~200 MB)" in c.fix
        assert not c.cmd and not c.install

    def test_moonshine_live_is_ok(self, moon):
        _set(stt_backend="local")
        c = doctor.check_fast_recognition()
        assert c.status == "ok"
        assert c.detail == "live words: Moonshine tiny-streaming"

    def test_an_explicit_whisper_stream_is_info(self, moon):
        moon.on = False
        _set(stt_backend="local", stt_stream_model="small.en")
        c = doctor.check_fast_recognition()
        assert c.status == "info"
        assert (
            c.detail == "live words: Whisper small.en (voice.stt_stream_model picks it)"
        )


class TestStreamEnd:
    def test_end_carries_whispers_final_and_moonshines_heard(self):
        from tests.unit import test_speech_moonshine as tsm

        fake = tsm.FakeTranscriber([[(1, "Send the report over.", True)]])
        sid = sm.open_stream(
            "",
            lambda audio: "Send the reports, okay.",
            client="t-end",
            engine_=sm.MoonshineEngine(fake, threads=1),
        )
        try:
            stream = speech_stream.get_stream(sid)
            stream.feed(tsm.loud(1.0))
            stream.feed(tsm.quiet(0.1))
            body = client.post("/api/voice/stream/%s/end" % sid).json()
        finally:
            speech_stream.close_stream(sid)
        assert body["heard"] == "Send the report over."
        assert body["final"] == "Send the reports over."
        assert body["final_by"] == "whisper"
