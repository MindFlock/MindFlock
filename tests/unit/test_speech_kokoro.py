"""The built-in Kokoro voice (``core.speech_kokoro``) and its wiring: the
voice list and ids, readiness (package + model files), the backend's voice
choice and WAV output, its place in the registry and the fallback order,
the model download (size + sha256, nothing half-written left), the
one-click install's Kokoro steps and their release-survival record, the
doctor row, ``GET /api/voice``'s ``kokoro`` block, ``GET /api/voice/voices``
and ``POST /api/voice/speak``'s ``"as": "assistant"``.

The model is a fake (:class:`_FakeModel`), the download a fake response, the
installer's subprocesses a fake spawner: nothing is downloaded or loaded.
"""

from __future__ import annotations

import hashlib
import io
import wave
from types import SimpleNamespace

import importlib.util

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.core import speech_install as si
from backend.web.core import speech_kokoro as k
from backend.web.routes import speech as routes

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    speech.unload_all()
    speech.TTS_CHAIN.reset()
    routes.SPEAK_CACHE.clear()
    routes.ASSISTANT_CACHE.clear()
    si.wait(5)
    si.reset()
    yield
    speech.unload_all()
    speech.TTS_CHAIN.reset()
    routes.SPEAK_CACHE.clear()
    routes.ASSISTANT_CACHE.clear()
    si.wait(5)
    si.reset()


def _voice(**fields):
    S.update_settings(voice=fields)
    S.invalidate()


#: Synthesis converts the model's samples with numpy, which comes with the
#: voice packages (kokoro-onnx needs it), not with the base install CI runs.
needs_numpy = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None,
    reason="numpy comes with the voice packages (uv sync --group voice)",
)


class _FakeModel:
    """``kokoro_onnx.Kokoro``'s ``create``: a 0.1 s tone, the call recorded."""

    def __init__(self):
        self.calls: list = []

    def create(self, text, voice, speed=1.0, lang="en-us"):
        import numpy as np

        self.calls.append((text, voice, speed, lang))
        return np.full(2400, 0.25, dtype=np.float32), 24000


@pytest.fixture
def installed(monkeypatch):
    """The package importable and the model files on disk (at their pinned
    sizes, as sparse files), the loader returning a fake model."""
    real = speech._installed
    monkeypatch.setattr(
        speech, "_installed", lambda m: True if m == k.MODULE else real(m)
    )
    folder = k.model_dir()
    folder.mkdir(parents=True)
    for name, (size, _sha) in k.FILES.items():
        with open(folder / name, "wb") as f:
            f.truncate(size)
    model = _FakeModel()
    monkeypatch.setattr(k, "_load", lambda: k.KokoroEngine(model, 0.5, 2))
    return model


def _wav_info(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


# --------------------------------------------------------------------------- #
# Voices
# --------------------------------------------------------------------------- #
class TestVoices:
    def test_the_english_voices(self):
        vs = k.voices()
        ids = [v["id"] for v in vs]
        assert ids[0] == k.DEFAULT_VOICE == "af_heart"
        for want in (
            "af_bella",
            "af_nicole",
            "am_michael",
            "am_fenrir",
            "am_puck",
            "bf_emma",
            "bm_george",
        ):
            assert want in ids
        assert len(ids) == len(set(ids)) == len(k.ids())
        assert {v["lang"] for v in vs} == {"en-us", "en-gb"}
        assert all(v["lang"] == ("en-gb" if v["id"][0] == "b" else "en-us") for v in vs)
        assert vs[0] == {
            "id": "af_heart",
            "label": "Heart (US, female)",
            "lang": "en-us",
        }

    def test_voice_id(self):
        assert k.voice_id("bm_george") == "bm_george"
        # A host's voice name is not a Kokoro voice.
        assert k.voice_id("alloy") == "af_heart"
        assert k.voice_id("alloy", "bf_emma") == "bf_emma"
        assert k.voice_id("", "") == "af_heart"

    def test_the_assistant_voice_setting(self):
        assert k.assistant_voice() == "af_heart"
        _voice(assistant_voice="am_puck")
        assert k.assistant_voice() == "am_puck"
        _voice(assistant_voice="not-a-voice")
        assert k.assistant_voice() == "af_heart"


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #
class TestReadiness:
    def test_not_installed(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda m: False)
        assert "kokoro-onnx is not installed" in k.unavailable_reason()
        ok, why = speech.get("tts", "kokoro").ready()
        assert ok is False and "kokoro-onnx" in why

    def test_package_without_the_model(self, monkeypatch, tmp_path):
        monkeypatch.setattr(speech, "_installed", lambda m: m == k.MODULE)
        why = k.unavailable_reason()
        assert "not downloaded" in why and k.MODEL_FILE in why and k.VOICES_FILE in why
        assert k.model_dir() == tmp_path / "voice" / "models" / "kokoro"

    def test_a_short_file_is_missing(self, installed):
        assert k.available()
        with open(k.model_dir() / k.VOICES_FILE, "wb") as f:
            f.truncate(10)
        assert k.missing_files() == [k.VOICES_FILE]
        assert not k.available()

    def test_ready_names_the_voice(self, installed):
        assert speech.get("tts", "kokoro").ready() == (
            True,
            "Kokoro voice af_heart on this computer",
        )

    def test_never_held_back_while_memory_is_low(self, installed, monkeypatch):
        """Voice is exempt from memory gating (owner, 2026-11-10)."""
        from backend import memory_pressure

        monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: True)
        monkeypatch.setattr(memory_pressure, "level", lambda **kw: "critical")
        ok, why = speech.get("tts", "kokoro").ready()
        assert ok is True and "held back" not in why
        assert k.engine().model is installed  # loads, not refused
        assert k.loaded()


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #
@needs_numpy
class TestSpeak:
    def test_wav_at_the_models_rate(self, installed):
        audio, mime = speech.get("tts", "kokoro").speak("Hello there.", "bf_emma")
        assert mime == "audio/wav"
        assert _wav_info(audio) == (1, 2, 24000, 2400)
        assert installed.calls == [("Hello there.", "bf_emma", k.SPEED, "en-gb")]

    def test_the_voice_choice(self, installed):
        tts = speech.get("tts", "kokoro")
        # A host's voice (it is the fallback) → the assistant's voice.
        _voice(tts_voice="alloy", assistant_voice="am_michael")
        tts.speak("a", "alloy")
        # voice.tts_voice naming a Kokoro voice wins over the assistant's.
        _voice(tts_voice="bm_george")
        tts.speak("b", "")
        # A per-call Kokoro voice wins over both.
        tts.speak("c", "af_bella")
        assert [c[1] for c in installed.calls] == [
            "am_michael",
            "bm_george",
            "af_bella",
        ]

    def test_loaded_once_and_cached(self, installed, monkeypatch):
        loads = []
        monkeypatch.setattr(
            k, "_load", lambda: loads.append(1) or k.KokoroEngine(installed)
        )
        assert not k.loaded()
        k.synthesize("one")
        k.synthesize("two")
        assert loads == [1] and k.loaded()
        assert k.CACHE_KEY in speech.loaded()

    def test_warm(self, installed):
        assert k.warm() == "loaded"
        assert installed.calls == [(speech.WARM_TEXT, "af_heart", k.SPEED, "en-us")]
        assert k.warm() == "already"
        assert speech.get("tts", "kokoro").warm_keys() == [k.CACHE_KEY]

    def test_warm_in_background(self, installed):
        t = k.warm_in_background()
        assert t is not None
        t.join(5)
        assert k.loaded()
        assert k.warm_in_background() is None  # already loaded

    def test_no_background_warm_when_unavailable(self):
        assert k.warm_in_background() is None

    def test_a_model_failure_is_a_speech_error(self, installed):
        installed.create = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        with pytest.raises(speech.SpeechError, match="Kokoro failed: boom"):
            k.synthesize("x")

    def test_threads(self, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_KOKORO_THREADS", raising=False)
        monkeypatch.setattr(k.os, "cpu_count", lambda: 8)
        assert k.threads() == k.THREADS == 2
        monkeypatch.setenv("MINDFLOCK_KOKORO_THREADS", "4")
        assert k.threads() == 4
        monkeypatch.setenv("MINDFLOCK_KOKORO_THREADS", "99")
        assert k.threads() == 8
        monkeypatch.setenv("MINDFLOCK_KOKORO_THREADS", "x")
        assert k.threads() == 2


# --------------------------------------------------------------------------- #
# The registry and the fallback order
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_just_ahead_of_piper(self):
        assert speech.ids("tts") == ["system", "openai", "kokoro", "local"]
        assert "kokoro" not in speech.ids("stt")
        assert speech.FALLBACK_TTS == ("kokoro", "local")

    def test_registering_twice_adds_nothing(self):
        before = speech.ids("tts")
        k._register()
        assert speech.ids("tts") == before

    def test_the_backend_setting_accepts_kokoro(self, installed):
        _voice(tts_backend="kokoro")
        backend, detail = speech.select("tts")
        assert backend.id == "kokoro" and "af_heart" in detail
        assert speech.choose("tts") == "kokoro"

    def test_the_fallback_prefers_kokoro_then_piper(self, monkeypatch, installed):
        host = SimpleNamespace(id="openai")
        piper = speech.get("tts", "local")
        monkeypatch.setattr(piper, "ready", lambda: (True, "piper"))
        assert speech.TtsChain.fallback_for(host).id == "kokoro"
        monkeypatch.setattr(speech.get("tts", "kokoro"), "ready", lambda: (False, "no"))
        assert speech.TtsChain.fallback_for(host) is piper

    def test_describe_reports_kokoro(self, installed):
        assert speech.describe(probe=False)["kokoro"] == {
            "available": True,
            "reason": "",
            "loaded": False,
            "voice": "af_heart",
        }


# --------------------------------------------------------------------------- #
# The download
# --------------------------------------------------------------------------- #
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def release(monkeypatch):
    """Two tiny "release files" with their pinned size and sha256."""
    blobs = {k.MODEL_FILE: b"m" * 300_000, k.VOICES_FILE: b"v" * 1000}
    monkeypatch.setattr(
        k,
        "FILES",
        {n: (len(b), hashlib.sha256(b).hexdigest()) for n, b in blobs.items()},
    )
    got: list = []

    def fake_open(url, timeout=60.0):
        got.append(url)
        return _Resp(blobs[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(k, "_open_url", fake_open)
    return SimpleNamespace(blobs=blobs, urls=got)


class TestFetch:
    def test_downloads_both_with_progress(self, release):
        lines: list = []
        where = k.fetch_model(lines.append)
        assert where == str(k.model_dir())
        for name, blob in release.blobs.items():
            assert (k.model_dir() / name).read_bytes() == blob
        assert release.urls == [
            k.RELEASE_URL + k.MODEL_FILE,
            k.RELEASE_URL + k.VOICES_FILE,
        ]
        assert lines[0].startswith("downloading " + k.MODEL_FILE)
        assert any("100%" in line for line in lines)
        assert not list(k.model_dir().glob("*.part"))

    def test_skips_what_is_there(self, release):
        k.fetch_model(lambda line: None)
        release.urls.clear()
        lines: list = []
        k.fetch_model(lines.append)
        assert release.urls == []
        assert lines == [
            k.MODEL_FILE + " already downloaded",
            k.VOICES_FILE + " already downloaded",
        ]

    def test_a_checksum_mismatch_leaves_nothing(self, release, monkeypatch):
        size, _sha = k.FILES[k.MODEL_FILE]
        monkeypatch.setitem(k.FILES, k.MODEL_FILE, (size, "0" * 64))
        with pytest.raises(speech.SpeechError, match="checksum mismatch"):
            k.fetch_model(lambda line: None)
        assert not (k.model_dir() / k.MODEL_FILE).exists()
        assert not list(k.model_dir().glob("*.part"))

    def test_a_short_download_fails(self, release, monkeypatch):
        size, sha = k.FILES[k.VOICES_FILE]
        monkeypatch.setitem(k.FILES, k.VOICES_FILE, (size + 1, sha))
        with pytest.raises(speech.SpeechError, match="expected"):
            k.fetch_model(lambda line: None)

    def test_offline_says_how_to_do_it_by_hand(self, release, monkeypatch):
        def refuse(url, timeout=60.0):
            raise OSError("network is unreachable")

        monkeypatch.setattr(k, "_open_url", refuse)
        with pytest.raises(speech.SpeechError, match="network is unreachable.*by hand"):
            k.fetch_model(lambda line: None)

    def test_the_installers_helper_runs_it(self, monkeypatch, capsys):
        monkeypatch.setattr(k, "fetch_model", lambda: "/x/kokoro")
        si._download_main(["kokoro", "v1.0"])
        assert capsys.readouterr().out.strip().endswith("saved to /x/kokoro")
        with pytest.raises(SystemExit):
            si._download_main(["kokoro", "v9"])


# --------------------------------------------------------------------------- #
# The one-click install, the carry and the doctor
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, lines):
        self._lines = list(lines)
        self.stdout = self

    def readline(self):
        return self._lines.pop(0) + "\n" if self._lines else ""

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def spawn(monkeypatch):
    calls: list = []

    def fake(argv):
        calls.append(list(argv))
        return _Proc(["ok"])

    monkeypatch.setattr(si, "_spawn", fake)
    monkeypatch.setattr(si, "_which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(speech, "invalidate", lambda: None)
    return calls


class TestInstall:
    def test_kokoro_alone(self, spawn):
        from backend.web.core import self_update

        before = S.load_settings().voice
        assert si.start(what="kokoro") is True
        si.wait(5)
        st = si.status()
        assert st["state"] == "done" and st["what"] == "kokoro"
        packages, model = spawn
        assert packages[-1] == "kokoro-onnx>=0.6.1"
        assert model[3:] == ["kokoro", "v1.0"]
        assert self_update.recorded_voice_packages() == ["kokoro-onnx"]
        # It survives a release: the engine rebuild carries it.
        assert "kokoro-onnx" in self_update.carried_packages()
        S.invalidate()
        after = S.load_settings().voice
        assert (after.stt_backend, after.tts_backend) == (
            before.stt_backend,
            before.tts_backend,
        )

    def test_the_local_models_include_it(self, spawn):
        from backend.web.core import self_update

        assert si.start() is True
        si.wait(5)
        assert si.status()["state"] == "done"
        assert "kokoro-onnx>=0.6.1" in spawn[0]
        assert spawn[-1][3:] == ["kokoro", "v1.0"]
        assert "kokoro-onnx" in self_update.recorded_voice_packages()

    def test_the_heal_knows_its_module(self, monkeypatch):
        from backend.web.core import self_update

        self_update.record_voice_packages(["kokoro-onnx"])
        monkeypatch.setattr(speech, "_installed", lambda m: m != "kokoro_onnx")
        assert si.missing_recorded_packages() == ["kokoro-onnx"]
        monkeypatch.setattr(speech, "_installed", lambda m: True)
        assert si.missing_recorded_packages() == []

    def test_the_route_takes_what_kokoro(self, spawn):
        r = client.post("/api/voice/install-local", json={"what": "kokoro"})
        assert r.status_code == 200 and r.json()["what"] == "kokoro"
        si.wait(5)
        bad = client.post("/api/voice/install-local", json={"what": "parakeet"})
        assert bad.status_code == 400 and "'kokoro'" in bad.json()["error"]

    def test_the_doctor_row(self, installed):
        from backend import doctor
        from backend.web.core import self_update

        self_update.record_voice_packages(
            ["faster-whisper", "piper-tts", "kokoro-onnx"]
        )
        row = doctor.check_voice_packages()
        assert row.status == "info"
        (k.model_dir() / k.MODEL_FILE).unlink()
        row = doctor.check_voice_packages()
        assert row.status == "warn"
        assert "the built-in voice cannot speak" in row.detail
        assert "Install the built-in voice" in row.fix


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #
class _Host(speech.Backend):
    """The configured voice (a host): what speaks when Kokoro can't."""

    id, kind, label = "fake", "tts", "fake host"

    def __init__(self):
        self.calls: list = []

    def ready(self):
        return True, "fake is ready"

    def speak(self, text, voice=""):
        self.calls.append((text, voice))
        return b"ID3host", "audio/mpeg"


@pytest.fixture
def host(monkeypatch):
    fake = _Host()
    tts = [fake, speech.get("tts", "kokoro")]
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": tts})
    _voice(tts_backend="fake")
    return fake


class TestRoutes:
    def test_voices(self, installed):
        body = client.get("/api/voice/voices?engine=kokoro").json()
        assert body["ready"] is True and body["reason"] == ""
        assert body["voices"] == k.voices() and body["voice"] == "af_heart"
        assert client.get("/api/voice/voices").json()["engine"] == "kokoro"
        assert client.get("/api/voice/voices?engine=piper").status_code == 400

    def test_voices_not_ready(self):
        body = client.get("/api/voice/voices?engine=kokoro").json()
        assert body["ready"] is False and body["reason"]
        assert body["voices"] == k.voices()

    def test_status_reports_kokoro(self, installed):
        assert client.get("/api/voice?warm=0").json()["kokoro"]["available"] is True

    def test_status_can_warm_the_assistants_voice(self, installed, monkeypatch):
        warmed: list = []
        monkeypatch.setattr(k, "warm_in_background", lambda: warmed.append(1))
        client.get("/api/voice?warm=0")
        assert warmed == []
        client.get("/api/voice?warm=0&assistant=1")
        assert warmed == [1]

    @needs_numpy
    def test_the_assistant_speaks_with_kokoro(self, installed, host):
        _voice(assistant_voice="bm_george")
        r = client.post(
            "/api/voice/speak",
            json={"text": "Two sessions need you.", "as": "assistant"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.headers["X-Voice-Assistant"] == "kokoro"
        assert r.headers["X-Voice-Backend"] == "kokoro"
        assert r.headers["X-Voice-Cache"] == "miss"
        assert _wav_info(r.content)[2] == 24000
        assert installed.calls[-1][1:] == ("bm_george", k.SPEED, "en-gb")
        assert host.calls == []
        # A repeat is a cache hit — the assistant's own cache.
        again = client.post(
            "/api/voice/speak",
            json={"text": "Two sessions need you.", "as": "assistant"},
        )
        assert again.headers["X-Voice-Cache"] == "hit" and again.content == r.content
        assert len(routes.SPEAK_CACHE) == 0

    @needs_numpy
    def test_an_explicit_kokoro_voice_wins(self, installed, host):
        r = client.post(
            "/api/voice/speak",
            json={"text": "Hi.", "as": "assistant", "voice": "am_puck"},
        )
        assert r.headers["X-Voice-Assistant"] == "kokoro"
        assert installed.calls[-1][1] == "am_puck"
        # The cache key includes the voice.
        r2 = client.post(
            "/api/voice/speak",
            json={"text": "Hi.", "as": "assistant", "voice": "af_sky"},
        )
        assert (
            r2.headers["X-Voice-Cache"] == "miss" and installed.calls[-1][1] == "af_sky"
        )

    def test_falls_back_to_the_chain_when_kokoro_is_not_ready(self, host):
        r = client.post(
            "/api/voice/speak",
            json={"text": "Hi.", "as": "assistant", "voice": "am_puck"},
        )
        assert r.status_code == 200
        assert r.headers["X-Voice-Assistant"] == "fallback"
        assert r.headers["X-Voice-Backend"] == "fake"
        # The Kokoro voice id is not passed to the host.
        assert host.calls == [("Hi.", "")]

    def test_falls_back_when_kokoro_fails(self, installed, host):
        installed.create = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        r = client.post("/api/voice/speak", json={"text": "Hi.", "as": "assistant"})
        assert r.status_code == 200 and r.headers["X-Voice-Assistant"] == "fallback"
        assert host.calls == [("Hi.", "")]

    def test_a_plain_speak_is_unchanged(self, installed, host):
        r = client.post("/api/voice/speak", json={"text": "Hi."})
        assert r.headers["X-Voice-Backend"] == "fake"
        assert "X-Voice-Assistant" not in r.headers

    def test_an_unknown_as_is_a_400(self, host):
        r = client.post("/api/voice/speak", json={"text": "Hi.", "as": "narrator"})
        assert r.status_code == 400 and "assistant" in r.json()["error"]
