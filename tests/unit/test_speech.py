"""``core.speech`` (the optional server speech backends): the OpenAI-compatible
backend against a fake HTTP layer, the registry's ``select`` / ``choose`` /
``describe``, the local backend's refusals (not installed, memory low) and
the idle unload of a cached model.

Nothing here reaches the network or loads a real model: ``_http`` is
replaced, the lazy runtimes come from fake modules through ``_import``, and
the idle clock is ``_now``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import wave
from types import SimpleNamespace

import pytest

from backend import memory_pressure
from backend.config import settings as S
from backend.web.core import speech


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, tmp_path):
    """Settings at a tmp file, model files under a tmp voice dir, no sweep
    timer thread, and an empty model cache on both sides of each test."""
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    speech.unload_all()
    yield
    speech.unload_all()


def _set(**voice):
    S.update_settings(voice=voice)
    S.invalidate()


class _Http:
    """A recording stand-in for ``speech._http``."""

    def __init__(
        self, status=200, ctype="application/json", body=b'{"text": "hello there"}'
    ):
        self.status, self.ctype, self.body = status, ctype, body
        self.raise_ = None
        self.calls: list = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            SimpleNamespace(
                method=method, url=url, headers=headers, body=body, timeout=timeout
            )
        )
        if self.raise_:
            raise self.raise_
        return self.status, self.ctype, self.body


@pytest.fixture
def http(monkeypatch):
    h = _Http()
    monkeypatch.setattr(speech, "_http", h)
    return h


# --------------------------------------------------------------------------- #
# openai — recognition
# --------------------------------------------------------------------------- #
class TestOpenAISTT:
    def test_not_ready_without_a_key(self, http):
        _set(stt_backend="openai")
        ok, detail = speech.OpenAISTT().ready()
        assert ok is False
        assert "API key" in detail and "api.openai.com" in detail
        assert http.calls == []  # ready() is offline

    def test_ready_with_a_key_is_offline(self, http):
        _set(stt_api_key="sk-test")  # pragma: allowlist secret
        http.raise_ = AssertionError("ready() must not touch the network")
        ok, detail = speech.OpenAISTT().ready()
        assert ok is True
        assert detail == "gpt-transcribe at api.openai.com"

    def test_transcribe_posts_multipart_with_bearer(self, http):
        _set(
            stt_api_key="gsk_abc",  # pragma: allowlist secret
            stt_url="https://api.groq.com/openai/v1/",
            stt_model="whisper-large-v3-turbo",
        )
        clip = b"\x1aE\xdf\xa3webm-bytes"
        text = speech.OpenAISTT().transcribe(clip, "audio/webm;codecs=opus", "en")
        assert text == "hello there"
        call = http.calls[0]
        assert call.method == "POST"
        assert call.url == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert call.headers["Authorization"] == "Bearer gsk_abc"
        assert call.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert call.timeout == speech.STT_TIMEOUT == 30.0
        body = call.body
        assert b'name="model"\r\n\r\nwhisper-large-v3-turbo\r\n' in body
        assert b'name="language"\r\n\r\nen\r\n' in body
        assert (
            b'name="file"; filename="clip.webm"\r\nContent-Type: audio/webm\r\n\r\n'
            in body
        )
        assert clip in body
        boundary = call.headers["Content-Type"].split("boundary=")[1].encode()
        assert body.endswith(b"--" + boundary + b"--\r\n")

    def test_default_url_and_model_and_no_language(self, http):
        _set(stt_api_key="sk-test")  # pragma: allowlist secret
        speech.OpenAISTT().transcribe(b"RIFF", "audio/wav", "")
        call = http.calls[0]
        assert call.url == "https://api.openai.com/v1/audio/transcriptions"
        assert b'name="model"\r\n\r\ngpt-transcribe\r\n' in call.body
        assert b'name="language"' not in call.body
        assert b'filename="clip.wav"' in call.body

    def test_http_error_carries_the_hosts_reason(self, http):
        _set(stt_api_key="sk-bad")  # pragma: allowlist secret
        http.status, http.body = (
            401,
            json.dumps({"error": {"message": "Incorrect API key provided"}}).encode(),
        )
        with pytest.raises(speech.SpeechError) as err:
            speech.OpenAISTT().transcribe(b"x", "audio/wav", "")
        assert (
            str(err.value) == "api.openai.com answered 401: Incorrect API key provided"
        )

    def test_network_failure_is_a_speech_error(self, http):
        _set(
            stt_api_key="sk-test",  # pragma: allowlist secret
            stt_url="http://10.255.255.1:9/v1",
        )
        http.raise_ = urllib.error.URLError("timed out")
        with pytest.raises(speech.SpeechError) as err:
            speech.OpenAISTT().transcribe(b"x", "audio/wav", "")
        assert str(err.value).startswith("could not reach 10.255.255.1:9: ")

    def test_non_json_answer(self, http):
        _set(stt_api_key="sk-test")  # pragma: allowlist secret
        http.ctype, http.body = "text/html", b"<html>oops"
        with pytest.raises(speech.SpeechError, match="other than JSON"):
            speech.OpenAISTT().transcribe(b"x", "audio/wav", "")

    def test_no_key_refuses_before_any_request(self, http):
        with pytest.raises(speech.SpeechError, match="API key"):
            speech.OpenAISTT().transcribe(b"x", "audio/wav", "")
        assert http.calls == []


# --------------------------------------------------------------------------- #
# openai — the voice
# --------------------------------------------------------------------------- #
class TestOpenAITTS:
    def test_speak_posts_json_and_returns_mp3(self, http):
        _set(tts_api_key="sk-test")  # pragma: allowlist secret
        http.ctype, http.body = "audio/mpeg", b"ID3mp3-bytes"
        audio, mime = speech.OpenAITTS().speak("All done.", "")
        assert (audio, mime) == (b"ID3mp3-bytes", "audio/mpeg")
        call = http.calls[0]
        assert call.url == "https://api.openai.com/v1/audio/speech"
        assert call.headers["Authorization"] == "Bearer sk-test"
        assert call.headers["Content-Type"] == "application/json"
        assert call.timeout == speech.TTS_TIMEOUT == 20.0
        assert json.loads(call.body) == {
            "model": "gpt-4o-mini-tts",
            "input": "All done.",
            "voice": "alloy",
            "response_format": "mp3",
        }

    def test_configured_model_voice_and_per_call_override(self, http):
        _set(
            tts_api_key="k",  # pragma: allowlist secret
            tts_url="https://tts.example.com/v1",
            tts_model="m1",
            tts_voice="nova",
        )
        http.ctype, http.body = "audio/mpeg", b"x"
        speech.OpenAITTS().speak("Hi", "")
        assert json.loads(http.calls[0].body)["voice"] == "nova"
        assert json.loads(http.calls[0].body)["model"] == "m1"
        assert http.calls[0].url == "https://tts.example.com/v1/audio/speech"
        speech.OpenAITTS().speak("Hi", "onyx")
        assert json.loads(http.calls[1].body)["voice"] == "onyx"

    def test_ready_names_model_voice_host(self, http):
        assert speech.OpenAITTS().ready()[0] is False
        _set(tts_api_key="k", tts_voice="nova")  # pragma: allowlist secret
        assert speech.OpenAITTS().ready() == (
            True,
            "gpt-4o-mini-tts (nova) at api.openai.com",
        )

    def test_json_back_instead_of_audio(self, http):
        _set(tts_api_key="k")  # pragma: allowlist secret
        http.ctype, http.body = "application/json", b'{"error": "nope"}'
        with pytest.raises(speech.SpeechError, match="no audio"):
            speech.OpenAITTS().speak("Hi", "")

    def test_http_error(self, http):
        _set(tts_api_key="k")  # pragma: allowlist secret
        http.status, http.ctype, http.body = (
            429,
            "application/json",
            b'{"error": {"message": "Rate limit"}}',
        )
        with pytest.raises(speech.SpeechError, match="answered 429: Rate limit"):
            speech.OpenAITTS().speak("Hi", "")


# --------------------------------------------------------------------------- #
# local — refusals
# --------------------------------------------------------------------------- #
class TestLocalRefusals:
    def test_not_installed(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda name: False)
        monkeypatch.setattr(
            speech, "_import", lambda name: pytest.fail("must not import " + name)
        )
        assert speech.LocalSTT().ready() == (False, speech.NOT_INSTALLED)
        assert speech.LocalTTS().ready() == (False, speech.NOT_INSTALLED)
        assert "uv sync --group voice" in speech.NOT_INSTALLED
        with pytest.raises(speech.SpeechError, match="uv sync --group voice"):
            speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        with pytest.raises(speech.SpeechError, match="uv sync --group voice"):
            speech.LocalTTS().speak("Hi", "")
        assert speech.loaded() == []

    def test_low_memory_holds_both_back(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        monkeypatch.setattr(
            speech, "_import", lambda name: pytest.fail("must not import " + name)
        )
        monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: True)
        monkeypatch.setattr(
            memory_pressure,
            "describe",
            lambda snap=None: "memory is low: 1.2 GB free of 8.0 GB (15%)",
        )
        ok, detail = speech.LocalSTT().ready()
        assert ok is False
        assert (
            detail
            == "local recognition held back: memory is low: 1.2 GB free of 8.0 GB (15%)"
        )
        ok, detail = speech.LocalTTS().ready()
        assert ok is False and detail.startswith("local voice held back: memory is low")
        with pytest.raises(speech.SpeechError, match="held back"):
            speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        with pytest.raises(speech.SpeechError, match="held back"):
            speech.LocalTTS().speak("Hi", "")
        assert speech.loaded() == []

    def test_ready_mentions_ffmpeg_and_the_model(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        monkeypatch.setattr(speech.shutil, "which", lambda name: None)
        ok, detail = speech.LocalSTT().ready()
        assert ok is True
        assert detail.startswith("faster-whisper small on this computer")
        assert "ffmpeg" in detail
        monkeypatch.setattr(speech.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        _set(stt_model="base")
        assert speech.LocalSTT().ready() == (
            True,
            "faster-whisper base on this computer",
        )

    def test_tts_ready_says_when_the_voice_downloads(self, monkeypatch, tmp_path):
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        ok, detail = speech.LocalTTS().ready()
        assert ok is True
        assert (
            detail
            == "piper voice en_US-lessac-medium on this computer (downloads on first use, ~60 MB)"
        )
        folder = speech.voice_dir() / "piper"
        folder.mkdir(parents=True)
        (folder / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
        assert speech.LocalTTS().ready() == (
            True,
            "piper voice en_US-lessac-medium on this computer",
        )


# --------------------------------------------------------------------------- #
# local — fake runtimes, the cache and the idle unload
# --------------------------------------------------------------------------- #
class _FakeWhisper:
    """A stand-in ``faster_whisper`` module."""

    def __init__(self):
        self.loads: list = []
        self.calls: list = []
        self.seen_paths: list = []

    @property
    def WhisperModel(self):  # noqa: N802 — the real class name
        fake = self

        class _Model:
            def __init__(self, name, **kw):
                fake.loads.append((name, kw))

            def transcribe(self, path, **kw):
                fake.calls.append((path, kw))
                fake.seen_paths.append(
                    (path, os.path.exists(path), os.path.getsize(path))
                )
                segs = [
                    SimpleNamespace(text=" Hello "),
                    SimpleNamespace(text="there. "),
                ]
                return iter(segs), SimpleNamespace(language="en")

        return _Model


@pytest.fixture
def whisper(monkeypatch):
    fake = _FakeWhisper()
    monkeypatch.setattr(speech, "_installed", lambda name: name == "faster_whisper")
    monkeypatch.setattr(
        speech,
        "_import",
        lambda name: fake if name == "faster_whisper" else pytest.fail(name),
    )
    return fake


class TestLocalSTT:
    def test_transcribe_loads_once_and_cleans_the_clip(self, whisper, tmp_path):
        stt = speech.LocalSTT()
        text = stt.transcribe(b"\x1aE\xdf\xa3", "audio/webm;codecs=opus", "en")
        assert text == "Hello there."
        name, kw = whisper.loads[0]
        assert name == "small"
        assert kw == {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(tmp_path / "voice" / "whisper"),
        }
        assert (tmp_path / "voice" / "whisper").is_dir()
        path, existed, size = whisper.seen_paths[0]
        assert path.endswith(".webm") and existed and size == 4
        assert not os.path.exists(path)  # the temp clip is gone after the call
        assert whisper.calls[0][1] == {"language": "en", "beam_size": 5}
        assert speech.loaded() == ["whisper:small"]
        stt.transcribe(b"RIFF", "audio/wav", "")
        assert len(whisper.loads) == 1  # cached
        assert whisper.calls[1][1]["language"] is None

    def test_configured_model_size(self, whisper):
        _set(stt_model="medium")
        speech.LocalSTT().transcribe(b"x", "audio/mp4", "")
        assert whisper.loads[0][0] == "medium"
        assert speech.loaded() == ["whisper:medium"]

    def test_idle_unload(self, whisper, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(speech, "_now", lambda: clock[0])
        speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        assert speech.loaded() == ["whisper:small"]
        clock[0] += speech.IDLE_SECONDS - 1
        assert speech.sweep_idle() == []
        assert speech.loaded() == ["whisper:small"]
        speech.LocalSTT().transcribe(b"x", "audio/wav", "")  # a call restarts the clock
        clock[0] += speech.IDLE_SECONDS - 1
        assert speech.sweep_idle() == []
        clock[0] += 1
        assert speech.sweep_idle() == ["whisper:small"]
        assert speech.loaded() == []
        speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        assert len(whisper.loads) == 2  # reloaded after the unload

    def test_sweep_timer_is_armed_on_load_and_fires_a_sweep(self, whisper, monkeypatch):
        armed: list = []
        monkeypatch.setattr(speech, "_arm_timer", lambda: armed.append(1))
        speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        assert armed == [1]
        clock = [speech._now() + speech.IDLE_SECONDS + 5]
        monkeypatch.setattr(speech, "_now", lambda: clock[0])
        speech._timer_fire()
        assert speech.loaded() == []
        assert armed == [1]  # nothing left loaded → no re-arm

    def test_model_load_failure_is_a_speech_error(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda name: True)

        class _Broken:
            class WhisperModel:  # noqa: D106
                def __init__(self, *a, **kw):
                    raise RuntimeError("no AVX")

        monkeypatch.setattr(speech, "_import", lambda name: _Broken())
        with pytest.raises(
            speech.SpeechError, match="could not load faster-whisper small: no AVX"
        ):
            speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        assert speech.loaded() == []


class _FakePiper:
    """Stand-ins for ``piper.download_voices`` and ``piper.voice``."""

    def __init__(self):
        self.downloads: list = []
        self.loads: list = []
        self.spoken: list = []
        fake = self

        class _Voice:
            @staticmethod
            def load(path):
                fake.loads.append(path)
                return _Voice()

            def synthesize_wav(self, text, wav):
                fake.spoken.append(text)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\x00\x01" * 200)

        def download_voice(name, folder):
            fake.downloads.append((name, folder))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (name + ".onnx")).write_bytes(b"onnx")
            (folder / (name + ".onnx.json")).write_text("{}")

        self.modules = {
            "piper.download_voices": SimpleNamespace(download_voice=download_voice),
            "piper.voice": SimpleNamespace(PiperVoice=_Voice),
        }


@pytest.fixture
def piper(monkeypatch):
    fake = _FakePiper()
    monkeypatch.setattr(speech, "_installed", lambda name: name == "piper")
    monkeypatch.setattr(speech, "_import", lambda name: fake.modules[name])
    return fake


class TestLocalTTS:
    def test_speak_downloads_once_and_returns_wav(self, piper, tmp_path):
        audio, mime = speech.LocalTTS().speak("All done.", "")
        assert mime == "audio/wav"
        assert audio.startswith(b"RIFF")
        with wave.open(__import__("io").BytesIO(audio)) as wav:
            assert wav.getframerate() == 22050 and wav.getnframes() == 200
        folder = tmp_path / "voice" / "piper"
        assert piper.downloads == [("en_US-lessac-medium", folder)]
        assert piper.loads == [str(folder / "en_US-lessac-medium.onnx")]
        assert piper.spoken == ["All done."]
        assert speech.loaded() == ["piper:en_US-lessac-medium"]
        speech.LocalTTS().speak("Again.", "")
        assert len(piper.downloads) == 1 and len(piper.loads) == 1

    def test_voice_name_from_settings_and_per_call(self, piper):
        _set(tts_voice="en_GB-alan-medium")
        speech.LocalTTS().speak("Hi", "")
        assert piper.downloads[0][0] == "en_GB-alan-medium"
        speech.LocalTTS().speak("Hi", "de_DE-thorsten-medium")
        assert piper.downloads[1][0] == "de_DE-thorsten-medium"
        assert speech.loaded() == [
            "piper:de_DE-thorsten-medium",
            "piper:en_GB-alan-medium",
        ]

    def test_download_failure_names_the_command(self, piper, monkeypatch):
        def _offline(name, folder):
            raise OSError("no route to huggingface.co")

        piper.modules["piper.download_voices"] = SimpleNamespace(
            download_voice=_offline
        )
        with pytest.raises(speech.SpeechError) as err:
            speech.LocalTTS().speak("Hi", "")
        msg = str(err.value)
        assert msg.startswith(
            "could not download piper voice en_US-lessac-medium: no route"
        )
        assert "python -m piper.download_voices en_US-lessac-medium --data-dir" in msg
        assert speech.loaded() == []


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
class _Fake(speech.Backend):
    def __init__(self, id, kind, ok, detail):
        self.id, self.kind, self.label = id, kind, id
        self._ok, self._detail = ok, detail

    def ready(self):
        return self._ok, self._detail


@pytest.fixture
def registry(monkeypatch):
    reg = {
        "stt": [
            _Fake("a", "stt", False, "a needs a key"),
            _Fake("b", "stt", True, "b is fine"),
        ],
        "tts": [
            _Fake("a", "tts", False, "a needs a key"),
            _Fake("b", "tts", False, "b not installed"),
        ],
    }
    monkeypatch.setattr(speech, "BACKENDS", reg)
    return reg


class TestRegistry:
    def test_ids(self, registry):
        assert speech.ids("stt") == ["a", "b"]
        assert speech.ids("nope") == []
        assert speech.get("stt", "b") is registry["stt"][1]
        assert speech.get("stt", "zzz") is None

    def test_select_first_ready_when_unset(self, registry):
        backend, detail = speech.select("stt")
        assert backend is registry["stt"][1] and detail == "b is fine"
        assert speech.choose("stt") == "b"

    def test_select_nothing_ready_lists_every_reason(self, registry):
        backend, detail = speech.select("tts")
        assert backend is None
        assert detail == "no voice backend is ready (a needs a key; b not installed)"
        assert speech.choose("tts") == ""

    def test_configured_backend_is_the_only_candidate(self, registry):
        _set(stt_backend="a")
        backend, detail = speech.select("stt")
        assert backend is None and detail == "a needs a key"
        assert speech.choose("stt") == "a"  # configured wins even while not ready
        _set(stt_backend="b")
        assert speech.select("stt")[0] is registry["stt"][1]

    def test_unknown_configured_backend(self, registry):
        _set(tts_backend="elevenlabs")
        backend, detail = speech.select("tts")
        assert backend is None
        assert detail == "unknown voice backend 'elevenlabs' (one of a, b)"
        assert speech.choose("tts") == ""

    def test_describe_shape(self, registry):
        d = speech.describe()
        assert d["backends"] == {"stt": ["a", "b"], "tts": ["a", "b"]}
        sr = d["server_ready"]
        assert (
            sr["stt"] is True
            and sr["stt_backend"] == "b"
            and sr["stt_detail"] == "b is fine"
        )
        assert sr["tts"] is False and sr["tts_backend"] == ""
        assert sr["tts_detail"].startswith("no voice backend is ready")
        assert sr["detail"] == (
            "Server recognition ready (b): b is fine. "
            "Server voice not ready: no voice backend is ready (a needs a key; b not installed)."
        )
        assert set(d["system"]) == {
            "available",
            "platform",
            "detail",
            "voices",
            "recognizers",
        }

    def test_select_follows_the_registry_order(self, registry):
        registry["stt"][0]._ok = True
        assert speech.select("stt") == (registry["stt"][0], "a needs a key")

    def test_real_registry_defaults(self):
        # The built-in engine first: "" (the first ready one) lands on it
        # wherever the OS has one — the fresh-install default.
        assert speech.ids("stt") == ["system", "openai", "local"]
        assert speech.ids("tts") == ["system", "openai", "local"]
        assert speech.get("stt", "system").label == "This computer's speech engine"
        for kind in ("stt", "tts"):
            for b in speech.backends(kind):
                assert b.kind == kind
        assert speech.mime_of("audio/webm;codecs=opus") == "audio/webm"
        assert speech.mime_of("") == ""
        assert (
            speech._ext("audio/mp4") == ".mp4"
            and speech._ext("audio/x-weird") == ".bin"
        )
