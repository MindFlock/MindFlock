"""``core.speech`` (the optional server speech backends): the OpenAI-compatible
backend against a fake HTTP layer, the registry's ``select`` / ``choose`` /
``describe``, the local backend's refusals (not installed, memory low) and
the idle unload of a cached model.

Nothing here reaches the network or loads a real model: ``_http`` is
replaced, the lazy runtimes come from fake modules through ``_import``, and
the idle clock is ``_now``.
"""

from __future__ import annotations

import inspect
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
    # Background lookups / probes run inline, from a clean slate.
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    # A loopback host here is a WARM one (its cold start, when Voice Lab
    # stopped while idle, is test_speech_tts_budget.TestColdStart's).
    monkeypatch.setattr(speech.OpenAITTS, "starts_cold", lambda self: False)
    speech.forget_ffmpeg()
    speech.TTS_CHAIN.reset()
    speech.unload_all()
    yield
    speech.unload_all()
    speech.TTS_CHAIN.reset()
    speech.forget_ffmpeg()


def _set(**voice):
    S.update_settings(voice=voice)
    S.invalidate()


class _WithFinal:
    """The voice settings with ``stt_final`` — read with a default by the
    code, so it works before the settings model has the field."""

    def __init__(self, voice, final):
        self._voice, self.stt_final = voice, final

    def __getattr__(self, name):
        return getattr(self._voice, name)


def _set_final(monkeypatch, value):
    real = speech._voice
    monkeypatch.setattr(speech, "_voice", lambda: _WithFinal(real(), value))


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
        assert "Settings → Voice" in speech.NOT_INSTALLED
        with pytest.raises(speech.SpeechError, match="Settings"):
            speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        with pytest.raises(speech.SpeechError, match="Settings"):
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
        speech.forget_ffmpeg()  # cached until forgotten (or PATH changes)
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
            "cpu_threads": speech.whisper_threads("final"),
            "num_workers": 1,
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

    def test_the_wake_clip_uses_the_fast_model(self, whisper, monkeypatch):
        from backend.web.core import wake

        # The short encoder input hooks faster_whisper.transcribe (not in the fake).
        monkeypatch.setattr(speech, "_hook_pad", lambda: False)
        # The fake's transcribe takes **kw: every option counts as accepted.
        monkeypatch.setattr(speech, "_takes", lambda model, key: True)
        stt = speech.LocalSTT()
        assert stt.wake_transcribe(b"RIFF", "audio/wav", "en-US", "MindFlock") == (
            "Hello there."
        )
        assert whisper.loads[0][0] == "base.en"
        # Decoded as core.wake says: greedy, one temperature, no VAD, no
        # no-speech filter, a token cap, the word as the prompt.
        assert whisper.calls[0][1] == {
            "language": "en",
            **wake.decode_options("MindFlock"),
        }
        path = whisper.seen_paths[0][0]
        assert path.endswith(".wav") and not os.path.exists(path)
        stt.wake_transcribe(b"RIFF", "audio/wav", "de", "MindFlock")
        assert whisper.loads[1][0] == "base"  # the stream model for German
        # A phrase sent whole keeps the accurate model.
        stt.transcribe(b"RIFF", "audio/wav", "en")
        assert whisper.loads[2][0] == "small"
        assert whisper.calls[2][1]["beam_size"] == 5

    def test_the_wake_check_pass_has_no_prompt(self, whisper, monkeypatch):
        from backend.web.core import wake

        monkeypatch.setattr(speech, "_hook_pad", lambda: False)
        # The fake's transcribe takes **kw: every option counts as accepted.
        monkeypatch.setattr(speech, "_takes", lambda model, key: True)
        stt = speech.LocalSTT()
        assert stt.wake_verify_transcribe(b"RIFF", "audio/wav", "en-US") == (
            "Hello there."
        )
        assert whisper.loads[0][0] == "base.en"  # the same fast model
        assert whisper.calls[0][1] == {"language": "en", **wake.decode_options("")}
        assert "initial_prompt" not in whisper.calls[0][1]
        assert not os.path.exists(whisper.seen_paths[0][0])

    def test_the_base_wake_path_is_transcribe(self):
        seen: list = []

        class _B(speech.Backend):
            def transcribe(self, audio, mime, lang="", prompt=""):
                seen.append((audio, mime, lang, prompt))
                return "ok"

        assert _B().wake_transcribe(b"a", "audio/wav", "en", "MindFlock") == "ok"
        assert seen == [(b"a", "audio/wav", "en", "MindFlock")]
        # No un-hinted check pass: the wake stands on the first.
        assert _B().wake_verify_transcribe(b"a", "audio/wav", "en") is None
        assert len(seen) == 1

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


class TestLocalStream:
    """``LocalSTT.stream_transcriber``: the float32 window straight into the
    model with the streaming arguments, words back with their timestamps."""

    @pytest.fixture
    def model(self, monkeypatch):
        from array import array

        seen: dict = {"loads": 0, "calls": [], "names": []}

        class _Model:
            def __init__(self, name, **kw):
                seen["loads"] += 1
                seen["names"].append(name)

            def transcribe(self, audio, **kw):
                seen["calls"].append((audio, kw))
                w = SimpleNamespace
                segs = [
                    w(words=[w(word=" Hello", start=0.0, end=0.4)]),
                    w(words=[w(word=" there.", start=0.5, end=0.9)]),
                    w(words=None),
                ]
                return iter(segs), SimpleNamespace(language="en")

        fw = SimpleNamespace(WhisperModel=_Model)

        def frombuffer(raw, dtype):
            out = array("f")
            out.frombytes(raw)
            return ("np", dtype, list(out))

        np = SimpleNamespace(float32="float32", frombuffer=frombuffer)
        monkeypatch.setattr(speech, "_installed", lambda name: name == "faster_whisper")
        monkeypatch.setattr(
            speech, "_import", lambda name: {"faster_whisper": fw, "numpy": np}[name]
        )
        return seen

    def test_a_pass_is_greedy_once_with_its_tokens_capped(self, model, monkeypatch):
        from array import array

        seen: list = []

        class _Model:
            def transcribe(self, audio, temperature=None, max_new_tokens=None, **kw):
                seen.append((temperature, max_new_tokens))
                return iter(()), SimpleNamespace(language="en")

        monkeypatch.setattr(speech.LocalSTT, "_model", lambda self, name="": _Model())
        monkeypatch.setattr(
            speech,
            "_import",
            lambda name: SimpleNamespace(
                float32="f4", frombuffer=lambda raw, dtype: [0.0] * (len(raw) // 4)
            ),
        )
        run = speech.LocalSTT().stream_transcriber("en")
        assert run(array("f", [0.0] * 32000), "") == []  # a 2 s window
        assert seen == [(0.0, speech.STREAM_TOKENS + 2 * speech.STREAM_TOKENS_PER_S)]

    def test_the_fast_knobs_reach_the_model(self, model, monkeypatch):
        from array import array

        monkeypatch.setattr(speech, "STREAM_BEAM", 2)
        monkeypatch.setattr(speech, "STREAM_VOCAB", "Over. Disregard.")
        run = speech.LocalSTT().stream_transcriber("en")
        run(array("f", [0.0]), "earlier words")
        _audio, kw = model["calls"][0]
        assert kw["beam_size"] == 2
        # The vocabulary leads the prompt, never as hotwords (the fast tier).
        assert kw["initial_prompt"] == "Over. Disregard.. earlier words"
        assert "hotwords" not in kw

    def test_a_pass(self, model):
        from array import array

        stt = speech.LocalSTT()
        assert stt.streams is True
        run = stt.stream_transcriber("pt-BR")
        words = run(array("f", [0.0, 0.5]), "earlier words")
        # Raw words: the stream strips them.
        assert words == [(" Hello", 0.0, 0.4), (" there.", 0.5, 0.9)]
        audio, kw = model["calls"][0]
        assert audio == ("np", "float32", [0.0, 0.5])
        assert kw == {
            "language": "pt",
            "beam_size": 1,
            "word_timestamps": True,
            "initial_prompt": "earlier words",
            "condition_on_previous_text": False,
            "vad_filter": True,
        }
        run(array("f"), "")
        # Nothing committed yet and no hints: no prompt argument at all.
        assert "initial_prompt" not in model["calls"][1][1]
        assert "hotwords" not in model["calls"][1][1]
        assert model["loads"] == 1  # the cached model
        # The streaming model, not the one-shot "small": base for Portuguese.
        assert model["names"] == ["base"]
        assert speech.loaded() == ["whisper:base"]

    @pytest.mark.parametrize(
        "lang,setting,name",
        [
            ("en-US", "", "base.en"),
            ("", "", "base.en"),
            ("EN", "", "base.en"),
            ("de-DE", "", "base"),
            ("fr", "tiny", "tiny"),
            ("en-GB", "small.en", "small.en"),
            ("en", "whisper-1", "base.en"),  # a host's name is not a size
            ("en", "Systran/faster-whisper-tiny.en", "Systran/faster-whisper-tiny.en"),
        ],
    )
    def test_the_stream_model_by_language_and_setting(self, model, lang, setting, name):
        from array import array

        _set(stt_stream_model=setting)
        stt = speech.LocalSTT()
        assert stt.stream_model_name(lang) == name
        stt.stream_transcriber(lang)(array("f", [0.0]), "")
        assert model["names"] == [name]
        assert speech.loaded() == ["whisper:" + name]

    def test_the_one_shot_keeps_its_own_model(self, model):
        from array import array

        _set(stt_model="medium")
        stt = speech.LocalSTT()
        stt.stream_transcriber("en")(array("f", [0.0]), "")
        assert stt._model() is not None
        assert model["names"] == ["base.en", "medium"]
        assert speech.loaded() == ["whisper:base.en", "whisper:medium"]

    def test_refusals_are_speech_errors(self, model, monkeypatch):
        from array import array

        run = speech.LocalSTT().stream_transcriber("")
        monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: True)
        with pytest.raises(speech.SpeechError, match="held back"):
            run(array("f", [0.0]), "")
        assert model["calls"] == []

    def test_other_backends_do_not_stream(self):
        assert speech.OpenAISTT().streams is False
        with pytest.raises(speech.SpeechError, match="does not stream"):
            speech.OpenAISTT().stream_transcriber("en")
        assert speech.OpenAISTT().final_transcriber("en") is None


class TestHints:
    """The vocabulary hints: built by ``recognition_hints`` and fed to both
    tiers as faster-whisper's ``hotwords`` (or the prompt on an old one)."""

    model = TestLocalStream.model
    WORDS = {
        "send": ["over"],
        "end": ["over and out", "over, out"],
        "clear": ["disregard"],
        "stop": ["stop", "stop listening"],
        "hush": ["hush"],
        "wake": "mindflock",
    }

    @pytest.mark.parametrize(
        "title,spoken",
        [
            ("sitecheck-bot7", "sitecheck bot 7"),
            ("voice_stt", "voice stt"),
            ("feature/mcp-fences-order", "feature mcp fences order"),
            ("mapUx", "map Ux"),
            ("shortcut-21129", "shortcut 21129"),
            ("MindFlock", "Mind Flock"),
            ("v2beta", "v 2 beta"),
            ("  ", ""),
            ("plain", "plain"),
        ],
    )
    def test_speech_form(self, title, spoken):
        assert speech.speech_form(title) == spoken

    def test_the_words_come_first_then_titles_then_the_vocabulary(self):
        hints = speech.recognition_hints(self.WORDS, ["sitecheck-bot7", "voice-stt"])
        parts = hints.split(", ")
        assert parts[:8] == [
            "mindflock",
            "over",
            "over and out",
            "over out",  # its own comma dropped: the list is comma-separated
            "disregard",
            "stop",
            "stop listening",
            "hush",
        ]
        # The commands Moonshine mishears bare, then the titles.
        n = len(speech.HINT_COMMANDS)
        assert parts[8 : 8 + n] == list(speech.HINT_COMMANDS)
        assert parts[8 + n : 10 + n] == ["sitecheck bot 7", "voice stt"]
        assert parts[10 + n] == "session"  # the vocabulary follows
        assert "pull request" in parts and "pytest" in parts
        # "mindflock" is the wake word already: the vocabulary's MindFlock is
        # a repeat (compared lower-cased) and is not added twice.
        assert [p.lower() for p in parts].count("mindflock") == 1
        assert len(hints) <= speech.HINT_CHARS

    def test_bounded(self):
        titles = ["session-number-%d-with-a-long-name" % i for i in range(60)]
        hints = speech.recognition_hints(self.WORDS, titles)
        assert len(hints) <= speech.HINT_CHARS
        # At most HINT_TITLES titles, the first (newest) ones.
        assert "session number 0 with a long name" in hints
        assert "session number %d" % speech.HINT_TITLES not in hints
        # A phrase that does not fit is skipped; shorter ones after it go in.
        tight = speech.recognition_hints(self.WORDS, ["x" * 300], limit=80)
        assert len(tight) <= 80 and "xxx" not in tight and "disregard" in tight

    def test_odd_input_is_fine(self):
        assert speech.recognition_hints({}, []) == ", ".join(
            speech.HINT_COMMANDS + speech.HINT_VOCABULARY
        )
        assert speech.recognition_hints(None, None).startswith("scroll, ")
        assert speech.recognition_hints({"send": "over", "wake": 3}, [""]).startswith(
            "scroll, "
        )

    def test_session_titles_from_the_engine(self, monkeypatch):
        import datetime as dt
        import sys

        import backend.web as web_pkg

        def inst(title, at):
            return SimpleNamespace(
                Title=title,
                UpdatedAt=dt.datetime(2026, 10, 27, 12, at, tzinfo=dt.timezone.utc),
            )

        engine = SimpleNamespace(
            instances={
                "old": inst("old", 1),
                "new": inst("new", 9),
                "mid": inst("mid", 5),
                "blank": inst("", 7),
            }
        )
        fake = SimpleNamespace(ENGINE=engine)
        monkeypatch.setitem(sys.modules, "backend.web.server", fake)
        monkeypatch.setattr(web_pkg, "server", fake, raising=False)
        assert speech.session_titles() == ["new", "mid", "old"]

    def test_session_titles_without_an_engine(self, monkeypatch):
        import sys

        import backend.web as web_pkg

        monkeypatch.setitem(sys.modules, "backend.web.server", SimpleNamespace())
        monkeypatch.setattr(web_pkg, "server", SimpleNamespace(), raising=False)
        assert speech.session_titles() == []

    def test_the_stream_pass_opens_its_prompt_with_the_hints(self, model):
        from array import array

        # Never hotwords on the fast tier (3x the time on base.en, and a
        # lone word can loop): the hints lead the prompt instead.
        run = speech.LocalSTT().stream_transcriber("en", "mindflock, over, disregard")
        run(array("f", [0.0]), "earlier words")
        kw = model["calls"][0][1]
        assert "hotwords" not in kw
        assert kw["initial_prompt"] == "mindflock, over, disregard. earlier words"
        run(array("f", [0.0]), "")
        kw = model["calls"][1][1]
        assert "hotwords" not in kw
        assert kw["initial_prompt"] == "mindflock, over, disregard"

    def test_without_the_vocabulary(self):
        hints = speech.recognition_hints(self.WORDS, (), vocabulary=False)
        assert hints == (
            "mindflock, over, over and out, over out, disregard, stop,"
            " stop listening, hush"
        )
        assert speech.recognition_hints({}, (), vocabulary=False) == ""

    def test_the_final_pass_sends_hotwords_only(self, model):
        from array import array

        speech.LocalSTT().final_transcriber("en", "over,  disregard")(array("f", [0.0]))
        kw = model["calls"][0][1]
        assert kw["hotwords"] == "over, disregard"  # whitespace collapsed
        assert "initial_prompt" not in kw  # the fast words may be wrong
        assert kw["beam_size"] == 1 and kw["vad_filter"] is True

    def test_an_old_faster_whisper_gets_the_hints_in_the_prompt(self, monkeypatch):
        from array import array

        calls: list = []

        class _Old:
            def __init__(self, name, **kw):
                pass

            def transcribe(  # noqa: PLR0913 — the 1.0 signature, no hotwords
                self,
                audio,
                language=None,
                beam_size=5,
                word_timestamps=False,
                initial_prompt=None,
                condition_on_previous_text=True,
                vad_filter=False,
            ):
                calls.append(dict(initial_prompt=initial_prompt))
                return iter([]), None

        fw = SimpleNamespace(WhisperModel=_Old)
        np = SimpleNamespace(float32="float32", frombuffer=lambda raw, dtype: raw)
        monkeypatch.setattr(speech, "_installed", lambda name: name == "faster_whisper")
        monkeypatch.setattr(
            speech, "_import", lambda name: {"faster_whisper": fw, "numpy": np}[name]
        )
        speech._HOTWORDS.clear()
        stt = speech.LocalSTT()
        stt.stream_transcriber("en", "over, disregard")(array("f", [0.0]), "earlier")
        stt.stream_transcriber("en", "over, disregard")(array("f", [0.0]), "")
        stt.final_transcriber("en", "over, disregard")(array("f", [0.0]))
        stt.stream_transcriber("en", "")(array("f", [0.0]), "earlier")
        assert [c["initial_prompt"] for c in calls] == [
            "over, disregard. earlier",
            "over, disregard",
            "over, disregard",
            "earlier",
        ]
        assert speech._HOTWORDS[_Old] is False

    def test_at_most_twelve_titles_the_newest_first(self):
        assert speech.HINT_TITLES == 12
        titles = ["t%d" % i for i in range(13)]
        parts = speech.recognition_hints(self.WORDS, titles).split(", ")
        n = 8 + len(speech.HINT_COMMANDS)
        assert parts[n : n + 12] == ["t %d" % i for i in range(12)]
        assert "t 12" not in parts  # the 13th
        assert parts[n + 12] == "session"  # the vocabulary still follows

    def test_a_title_that_is_a_voice_phrase_is_one_entry(self):
        titles = ["Disregard", "Over", "stop", "Window"]
        parts = speech.recognition_hints(self.WORDS, titles).split(", ")
        low = [p.lower() for p in parts]
        assert low.count("disregard") == 1 and low.count("over") == 1
        assert low.count("stop") == 1 and low.count("window") == 1
        # The first spelling in (the voice phrase's) stands.
        assert "disregard" in parts and "Disregard" not in parts
        assert "Window" in parts and "window" not in parts  # the title came first

    def test_a_phrase_that_does_not_fit_is_skipped_not_the_rest(self):
        words = {"send": ["a", "x" * 30, "b"]}
        assert speech.recognition_hints(words, (), limit=10, vocabulary=False) == "a, b"
        assert speech.recognition_hints(words, (), limit=0, vocabulary=False) == ""

    def test_session_titles_fall_back_on_created_at_and_drop_repeats(self, monkeypatch):
        import datetime as dt
        import sys

        import backend.web as web_pkg

        def at(minute):
            return dt.datetime(2026, 10, 27, 12, minute, tzinfo=dt.timezone.utc)

        inst = SimpleNamespace
        engine = SimpleNamespace(
            instances={
                # Never updated: CreatedAt stands in.
                "a": inst(Title="created", UpdatedAt=None, CreatedAt=at(8)),
                "b": inst(Title="updated", UpdatedAt=at(5), CreatedAt=at(1)),
                # Not a datetime: counts as the oldest.
                "c": inst(Title="odd", UpdatedAt="yesterday"),
                # A copy window on the same title: once, at its newest.
                "d": inst(Title="updated", UpdatedAt=at(9)),
                "e": inst(Title=None, UpdatedAt=at(7)),
                "f": inst(Title="  padded  ", UpdatedAt=at(6)),
                "g": inst(UpdatedAt=at(4)),  # no Title at all
            }
        )
        fake = SimpleNamespace(ENGINE=engine)
        monkeypatch.setitem(sys.modules, "backend.web.server", fake)
        monkeypatch.setattr(web_pkg, "server", fake, raising=False)
        assert speech.session_titles() == ["updated", "created", "padded", "odd"]


class TestHotwords:
    """``_hotwords_supported`` and ``_bias``: how the hints reach a
    ``transcribe`` call, by the installed faster-whisper's signature."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(speech, "_HOTWORDS", {})

    def test_a_named_or_a_star_kwargs_parameter_counts(self):
        class _Named:
            def transcribe(self, audio, hotwords=None):
                pass

        class _Star:
            def transcribe(self, audio, **kw):
                pass

        class _Old:
            def transcribe(self, audio, initial_prompt=None):
                pass

        assert speech._hotwords_supported(_Named()) is True
        assert speech._hotwords_supported(_Star()) is True
        assert speech._hotwords_supported(_Old()) is False

    def test_an_unreadable_signature_reads_as_no(self, monkeypatch):
        class _Opaque:
            def transcribe(self, audio, **kw):
                pass

        def boom(obj):
            raise ValueError("no signature found for builtin")

        monkeypatch.setattr(
            speech,
            "inspect",
            SimpleNamespace(signature=boom, Parameter=inspect.Parameter),
        )
        model = _Opaque()
        assert speech._hotwords_supported(model) is False
        assert speech._HOTWORDS == {_Opaque: False}
        # The hints ride the prompt instead.
        assert speech._bias(model, "over, out", "earlier") == {
            "initial_prompt": "over, out. earlier"
        }

    def test_the_answer_is_cached_per_class(self, monkeypatch):
        seen: list = []

        def counted(obj):
            seen.append(obj)
            return inspect.signature(obj)

        monkeypatch.setattr(
            speech,
            "inspect",
            SimpleNamespace(signature=counted, Parameter=inspect.Parameter),
        )

        class _New:
            def transcribe(self, audio, hotwords=None):
                pass

        class _Old:
            def transcribe(self, audio):
                pass

        assert speech._hotwords_supported(_New()) is True
        assert speech._hotwords_supported(_New()) is True
        assert len(seen) == 1  # once per class, not per model or call
        assert speech._hotwords_supported(_Old()) is False
        assert speech._hotwords_supported(_Old()) is False
        assert len(seen) == 2
        assert speech._HOTWORDS == {_New: True, _Old: False}

    @pytest.mark.parametrize(
        "hints,prompt,hotwords,want",
        [
            (
                "over, out",
                "earlier",
                True,
                {"hotwords": "over, out", "initial_prompt": "earlier"},
            ),
            ("over, out", "", True, {"hotwords": "over, out"}),
            ("over, out", "earlier", False, {"initial_prompt": "over, out. earlier"}),
            ("over, out", "", False, {"initial_prompt": "over, out"}),
            ("", "earlier", True, {"initial_prompt": "earlier"}),
            ("", "earlier", False, {"initial_prompt": "earlier"}),
            ("", "", True, {}),  # no keys at all, never None
            ("", "", False, {}),
        ],
    )
    def test_bias(self, hints, prompt, hotwords, want):
        class _New:
            def transcribe(self, audio, hotwords=None, initial_prompt=None):
                pass

        assert speech._bias(_New(), hints, prompt, hotwords) == want


class TestLocalFinal:
    """``LocalSTT.final_transcriber``: the accurate pass over the whole
    phrase at a stream's end, on the one-shot model by default."""

    model = TestLocalStream.model

    def test_the_accurate_pass(self, model):
        from array import array

        run = speech.LocalSTT().final_transcriber("en-US")
        # The fake's segments carry words, no text: nothing heard.
        assert run(array("f", [0.0, 0.5])) == ""
        audio, kw = model["calls"][0]
        assert audio == ("np", "float32", [0.0, 0.5])
        assert kw == {
            "language": "en",
            "beam_size": 1,
            "condition_on_previous_text": False,
            "vad_filter": True,
        }
        assert "initial_prompt" not in kw  # the fast words may be wrong
        assert model["names"] == ["small"]
        assert speech.loaded() == ["whisper:small"]

    def test_the_text_of_its_segments(self, model, monkeypatch):
        from array import array

        class _Model:
            def __init__(self, name, **kw):
                pass

            def transcribe(self, audio, **kw):
                segs = [
                    SimpleNamespace(text=" Run the"),
                    SimpleNamespace(text=" tests. "),
                ]
                return iter(segs + [SimpleNamespace(text="")]), None

        fw = SimpleNamespace(WhisperModel=_Model)
        np = SimpleNamespace(float32="float32", frombuffer=lambda raw, dtype: raw)
        monkeypatch.setattr(
            speech, "_import", lambda name: {"faster_whisper": fw, "numpy": np}[name]
        )
        run = speech.LocalSTT().final_transcriber("")
        assert run(array("f", [0.1])) == "Run the tests."

    def test_a_failure_is_a_speech_error(self, model, monkeypatch):
        from array import array

        class _Model:
            def __init__(self, name, **kw):
                pass

            def transcribe(self, audio, **kw):
                raise RuntimeError("bad audio")

        fw = SimpleNamespace(WhisperModel=_Model)
        np = SimpleNamespace(float32="float32", frombuffer=lambda raw, dtype: raw)
        monkeypatch.setattr(
            speech, "_import", lambda name: {"faster_whisper": fw, "numpy": np}[name]
        )
        with pytest.raises(speech.SpeechError, match="faster-whisper failed: bad"):
            speech.LocalSTT().final_transcriber("")(array("f", [0.1]))

    @pytest.mark.parametrize(
        "final,stt_model,name",
        [
            ("", "", "small"),
            ("", "medium", "medium"),
            ("", "whisper-1", "small"),  # a host's name is not a size
            ("off", "medium", ""),
            ("OFF", "", ""),
            ("medium.en", "", "medium.en"),
            ("large-v3-turbo", "tiny", "large-v3-turbo"),
            ("org/whisper-x", "", "org/whisper-x"),
            ("nonsense", "base", "base"),
        ],
    )
    def test_final_whisper_model(self, final, stt_model, name):
        assert speech.final_whisper_model(final, stt_model) == name

    def test_the_setting_picks_the_model(self, model, monkeypatch):
        from array import array

        _set_final(monkeypatch, "medium.en")
        stt = speech.LocalSTT()
        assert stt.final_model_name() == "medium.en"
        stt.final_transcriber("en")(array("f", [0.0]))
        assert model["names"] == ["medium.en"]

    def test_off_means_no_accurate_pass(self, model, monkeypatch):
        _set_final(monkeypatch, "off")
        stt = speech.LocalSTT()
        assert stt.final_model_name() == ""
        assert stt.final_transcriber("en") is None
        assert "whisper:" not in stt.warm_keys()
        assert stt.warm_keys() == ["whisper:base.en", "whisper:small"]

    def test_unset_is_the_one_shot_model(self, model):
        # Also with a settings model that has no stt_final field yet.
        assert speech.LocalSTT().final_model_name() == "small"
        _set(stt_model="medium")
        assert speech.LocalSTT().final_model_name() == "medium"


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


class TestLocalNames:
    """``voice.stt_model`` / ``voice.tts_voice`` are shared with the other
    backends: a host's model or an OS voice must not break ``local``."""

    def test_whisper_model(self):
        assert speech.whisper_model("") == "small"
        assert speech.whisper_model(" medium.en ") == "medium.en"
        assert speech.whisper_model("large-v3-turbo") == "large-v3-turbo"
        assert speech.whisper_model("Systran/faster-whisper-tiny") == (
            "Systran/faster-whisper-tiny"
        )
        assert speech.whisper_model("whisper-1") == "small"
        assert speech.whisper_model("whisper-large-v3-turbo") == "small"

    def test_piper_voice(self):
        assert speech.piper_voice("") == "en_US-lessac-medium"
        assert speech.piper_voice("en_GB-alan-low") == "en_GB-alan-low"
        assert speech.piper_voice("de_DE-thorsten-high") == "de_DE-thorsten-high"
        for other in ("alloy", "Microsoft Zira Desktop", "Samantha", "en_US"):
            assert speech.piper_voice(other) == "en_US-lessac-medium"

    def test_an_os_voice_in_settings_speaks_the_default_piper_voice(self, piper):
        _set(tts_voice="Microsoft Zira Desktop")
        speech.LocalTTS().speak("Hi", "")
        assert piper.downloads[0][0] == "en_US-lessac-medium"

    def test_a_host_model_in_settings_loads_the_default_size(self):
        _set(stt_model="whisper-1")
        assert speech.LocalSTT()._model_name() == "small"

    def test_invalidate_drops_the_import_caches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            speech.importlib, "invalidate_caches", lambda: calls.append(1)
        )
        speech.invalidate()
        assert calls == [1]


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
            "stt_quality",
        }

    def test_stt_stream_follows_the_ready_backend(self, registry):
        assert speech.describe()["server_ready"]["stt_stream"] is False
        registry["stt"][1].streams = True  # "b", the ready one, streams
        assert speech.describe()["server_ready"]["stt_stream"] is True
        registry["stt"][1]._ok = False  # nothing ready: nothing streams
        assert speech.describe()["server_ready"]["stt_stream"] is False

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


# --- the PyAV 15 shim --------------------------------------------------------- #
def _fake_av(monkeypatch, version: str):
    import sys
    import types

    calls: list = []

    def open_(*args, **kwargs):
        if "metadata_errors" in kwargs:
            raise TypeError(
                "open() got an unexpected keyword argument 'metadata_errors'"
            )
        calls.append((args, kwargs))
        return "container"

    av = types.ModuleType("av")
    av.__version__ = version
    av.open = open_
    fw = types.ModuleType("faster_whisper")
    fw_audio = types.ModuleType("faster_whisper.audio")
    fw_audio.av = av
    fw.audio = fw_audio
    monkeypatch.setitem(sys.modules, "av", av)
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", fw_audio)
    return av, fw_audio, calls


def test_av_open_shim_drops_metadata_errors_on_pyav_15(monkeypatch):
    av, fw_audio, calls = _fake_av(monkeypatch, "19.0.2")
    speech._shim_av_open()
    assert (
        fw_audio.av.open("/tmp/x.wav", mode="r", metadata_errors="ignore")
        == "container"
    )
    assert calls == [(("/tmp/x.wav",), {"mode": "r"})]
    first = fw_audio.av.open
    speech._shim_av_open()  # idempotent: never wrapped twice
    assert fw_audio.av.open is first


def test_av_open_shim_leaves_older_pyav_alone(monkeypatch):
    av, fw_audio, _calls = _fake_av(monkeypatch, "14.4.0")
    before = fw_audio.av.open
    speech._shim_av_open()
    assert fw_audio.av.open is before


# --- language tags ------------------------------------------------------------ #
@pytest.mark.parametrize(
    "tag,code",
    [
        ("en-US", "en"),
        ("pt_BR", "pt"),
        ("yue", "yue"),
        ("EN", "en"),
        ("", None),
        ("123", None),
        ("x", None),
    ],
)
def test_whisper_language_is_the_primary_subtag(tag, code):
    # navigator.language is BCP-47 ("en-US"); faster-whisper rejects anything
    # but its own two/three-letter codes — the owner saw "'en-US' is not a
    # valid language code" on the first live clip.
    assert speech.whisper_language(tag) == code


class _FakeNumpy:
    float32 = "float32"

    @staticmethod
    def zeros(n, dtype=None):
        return ("zeros", n, dtype)


class TestWarm:
    """warm() / warm_in_background(): the configured local model is loaded
    (and primed with one silent pass) ahead of the first press."""

    @pytest.fixture
    def local(self, whisper, monkeypatch):
        monkeypatch.setattr(
            speech,
            "_import",
            lambda name: whisper if name == "faster_whisper" else _FakeNumpy,
        )
        _set(stt_backend="local")
        return whisper

    def test_warm_loads_and_primes_both_models_once(self, local):
        assert speech.warm("stt") == "loaded"
        # The one-shot model and the live transcript's.
        assert speech.loaded() == ["whisper:base.en", "whisper:small"]
        assert sorted(n for n, _kw in local.loads) == ["base.en", "small"]
        # One priming pass each: a short silent array, greedy, no VAD.
        assert len(local.calls) == 2
        for path, kw in local.calls:
            assert path == ("zeros", 8000, "float32")
            assert kw == {"language": "en", "beam_size": 1, "vad_filter": False}
        # Resident already: nothing loaded, nothing primed.
        assert speech.warm("stt") == "already"
        assert len(local.loads) == 2 and len(local.calls) == 2
        assert speech.describe()["warm"]["stt"] == "loaded"

    def test_warm_loads_the_one_missing_model(self, local):
        speech.LocalSTT().transcribe(b"x", "audio/wav", "")
        assert speech.loaded() == ["whisper:small"]
        assert speech.warm("stt") == "loaded"
        assert speech.loaded() == ["whisper:base.en", "whisper:small"]
        assert len(local.loads) == 2

    def test_a_third_final_model_is_warmed_too(self, local, monkeypatch):
        _set_final(monkeypatch, "medium.en")
        assert speech.LocalSTT().warm_keys() == [
            "whisper:base.en",
            "whisper:medium.en",
            "whisper:small",
        ]
        assert speech.warm("stt") == "loaded"
        assert speech.loaded() == [
            "whisper:base.en",
            "whisper:medium.en",
            "whisper:small",
        ]
        assert len(local.loads) == 3
        # Resident while local recognition is configured, like the others.
        assert "whisper:medium.en" in speech.LocalSTT().resident_keys()

    def test_one_model_for_both_loads_once(self, local):
        _set(stt_backend="local", stt_model="base.en")
        assert speech.warm("stt") == "loaded"
        assert speech.loaded() == ["whisper:base.en"]
        assert len(local.loads) == 1

    def test_warm_skips_unless_the_backend_is_local(self, whisper, monkeypatch):
        monkeypatch.setattr(
            speech,
            "_import",
            lambda name: whisper if name == "faster_whisper" else _FakeNumpy,
        )
        _set(stt_backend="")
        assert speech.warm("stt").startswith("skipped: stt backend is not local")
        _set(stt_backend="openai")
        assert speech.warm("stt").startswith("skipped:")
        assert speech.loaded() == [] and whisper.loads == []

    def test_warm_skips_when_not_installed_or_memory_is_low(self, monkeypatch):
        _set(stt_backend="local")
        monkeypatch.setattr(speech, "_installed", lambda name: False)
        assert speech.warm("stt").startswith("skipped:")
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        monkeypatch.setattr(speech, "_memory_block", lambda: "memory is low")
        assert "memory is low" in speech.warm("stt")
        assert speech.loaded() == []

    def test_warm_in_background_runs_once_and_is_a_no_op_when_resident(self, local):
        t = speech.warm_in_background("stt")
        assert t is not None
        t.join(5)
        assert not t.is_alive()
        assert speech.loaded() == ["whisper:base.en", "whisper:small"]
        assert len(local.loads) == 2
        # Resident: nothing to do.
        assert speech.warm_in_background("stt") is None
        speech.unload_all()
        _set(stt_backend="")
        assert speech.warm_in_background("stt") is None
        assert speech.loaded() == []

    def test_warm_in_background_dedupes_a_running_warm_up(self, local, monkeypatch):
        import threading

        gate = threading.Event()
        real_warm = speech.warm

        def slow_warm(kind="stt", prime=True):
            gate.wait(5)
            return real_warm(kind, prime)

        monkeypatch.setattr(speech, "warm", slow_warm)
        t = speech.warm_in_background("stt")
        assert t is not None
        assert speech.warm_in_background("stt") is None  # already warming
        assert speech.describe()["warm"]["stt"] == "loading"
        gate.set()
        t.join(5)
        assert speech.loaded() == ["whisper:base.en", "whisper:small"]


def test_a_prompt_biases_the_local_decoder(whisper):
    stt = speech.LocalSTT()
    stt.transcribe(b"RIFF", "audio/wav", "en", prompt="MindFlock")
    assert whisper.calls[-1][1] == {
        "language": "en",
        "beam_size": 5,
        "initial_prompt": "MindFlock",
    }
    stt.transcribe(b"RIFF", "audio/wav", "en")
    assert "initial_prompt" not in whisper.calls[-1][1]


class TestLocalHostNeedsNoKey:
    """A local model server that speaks the OpenAI shape (Voice Lab,
    Kokoro-FastAPI, a whisper server) wants no key: loopback, private and
    plain-http hosts are ready without one; a public https host still is not."""

    @pytest.mark.parametrize(
        "url,local",
        [
            ("http://127.0.0.1:8770/v1", True),
            ("http://localhost:8880/v1", True),
            ("http://192.168.1.20:8000/v1", True),
            ("http://10.0.0.5/v1", True),
            ("https://mac-mini.local/v1", True),
            ("https://box.tail1234.ts.net/v1", True),
            ("http://speech.example.com/v1", True),  # clear text: a key would not help
            ("https://api.openai.com/v1", False),
            ("https://api.groq.com/openai/v1", False),
            ("", False),
        ],
    )
    def test_local_host(self, url, local):
        assert speech._local_host(url) is local

    def test_tts_ready_and_speaks_without_a_key_on_a_local_host(self, monkeypatch):
        _set(
            tts_backend="openai",
            tts_url="http://127.0.0.1:8770/v1",
            tts_api_key="",
            tts_voice="harvey",
        )
        http = _Http(ctype="audio/wav", body=b"RIFFwav")
        monkeypatch.setattr(speech, "_http", http)
        tts = speech.OpenAITTS()
        ok, detail = tts.ready()
        assert ok and "127.0.0.1" in detail
        data, mime = tts.speak("Hello")
        assert data == b"RIFFwav" and mime.startswith("audio/wav")
        call = http.calls[0]
        assert call.url == "http://127.0.0.1:8770/v1/audio/speech"
        assert "Authorization" not in call.headers  # no bearer header without a key
        assert json.loads(call.body)["voice"] == "harvey"

    def test_a_public_host_still_needs_a_key(self):
        _set(tts_backend="openai", tts_url="https://api.openai.com/v1", tts_api_key="")
        ok, detail = speech.OpenAITTS().ready()
        assert not ok and "needs an API key" in detail
        with pytest.raises(speech.SpeechError):
            speech.OpenAITTS().speak("Hello")

    def test_stt_without_a_key_on_a_local_host(self, monkeypatch):
        _set(stt_backend="openai", stt_url="http://localhost:9000/v1", stt_api_key="")
        http = _Http()
        monkeypatch.setattr(speech, "_http", http)
        assert speech.OpenAISTT().ready()[0]
        assert (
            speech.OpenAISTT().transcribe(b"RIFF", "audio/wav", "en") == "hello there"
        )
        assert "Authorization" not in http.calls[0].headers


# --------------------------------------------------------------------------- #
# Resident recognition, the voice's warm-up, describe()'s additions
# --------------------------------------------------------------------------- #
@pytest.fixture
def both(monkeypatch):
    """faster-whisper and piper both installed (fakes), numpy faked."""
    w, p = _FakeWhisper(), _FakePiper()
    mods = dict(p.modules, faster_whisper=w, numpy=_FakeNumpy)
    monkeypatch.setattr(
        speech, "_installed", lambda n: n in ("faster_whisper", "piper")
    )
    monkeypatch.setattr(speech, "_import", lambda n: mods[n])
    return SimpleNamespace(whisper=w, piper=p)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(speech, "_now", lambda: now[0])
    return now


class TestResident:
    """While recognition is ``local`` AND a page listens (the wake-word
    standby's lease, :func:`speech.listen`) its whisper models are never
    idle-unloaded; with nobody listening they unload like everything else
    (piper voices always do)."""

    WHISPERS = ["whisper:base.en", "whisper:small"]

    @pytest.fixture(autouse=True)
    def _listening(self, clock):
        # A page in standby the whole test (its lease renewed: the clock
        # jumps past it in these tests).
        speech.listen("standby-page")
        speech._LISTENING["standby-page"] = float("inf")
        yield
        speech._LISTENING.clear()

    def test_local_recognition_stays_voices_unload(self, both, clock):
        _set(stt_backend="local", tts_backend="local")
        assert speech.warm("stt") == "loaded"
        assert speech.warm("tts") == "loaded"
        assert speech.loaded() == ["piper:en_US-lessac-medium"] + self.WHISPERS
        clock[0] += speech.IDLE_SECONDS + 1
        assert speech.sweep_idle() == ["piper:en_US-lessac-medium"]
        assert speech.loaded() == self.WHISPERS

    def test_another_languages_stream_model_stays_too(self, both, clock):
        _set(stt_backend="local")
        speech.LocalSTT()._model("base")  # a German stream's model
        clock[0] += speech.IDLE_SECONDS + 1
        assert speech.sweep_idle() == []
        assert speech.loaded() == ["whisper:base"]

    def test_not_local_any_more_unloads(self, both, clock):
        _set(stt_backend="local")
        speech.warm("stt")
        _set(stt_backend="system")
        clock[0] += speech.IDLE_SECONDS + 1
        assert speech.sweep_idle() == self.WHISPERS
        assert speech.loaded() == []

    def test_a_model_from_an_old_setting_unloads(self, both, clock):
        _set(stt_backend="local")
        speech.warm("stt")
        _set(stt_backend="local", stt_model="medium")
        clock[0] += speech.IDLE_SECONDS + 1
        assert speech.sweep_idle() == ["whisper:small"]
        assert speech.loaded() == ["whisper:base.en"]

    def test_the_timer_keeps_running_and_unload_all_still_works(
        self, both, clock, monkeypatch
    ):
        armed: list = []
        monkeypatch.setattr(speech, "_arm_timer", lambda: armed.append(1))
        _set(stt_backend="local")
        speech.warm("stt")
        armed.clear()
        clock[0] += speech.IDLE_SECONDS + 1
        speech._timer_fire()
        assert speech.loaded() == self.WHISPERS and armed == [1]
        speech.unload_all()
        assert speech.loaded() == []

    def test_nobody_listening_unloads_all_but_the_stream_model(self, both, clock):
        speech._LISTENING.clear()
        _set(stt_backend="local")
        speech.warm("stt")
        clock[0] += speech.IDLE_SECONDS - 1
        assert speech.sweep_idle() == []  # a recent phrase keeps it a while
        clock[0] += 2
        # The streaming model (~150 MB) stays: reloading it cost ~2 s.
        assert speech.sweep_idle() == ["whisper:small"]
        assert speech.loaded() == ["whisper:base.en"]

    def test_a_lapsed_lease_unloads_it(self, both, clock):
        speech._LISTENING.clear()
        _set(stt_backend="local")
        assert speech.listen("page") is True
        speech.warm("stt")
        assert speech.stt_loaded() is True
        clock[0] += speech.LISTEN_LEASE_SECONDS - 1
        assert speech.listening() is True
        clock[0] += speech.IDLE_SECONDS  # the page went away long ago
        assert speech.listening() is False
        assert speech.sweep_idle() == ["whisper:small"]
        assert speech.stt_loaded() is False
        assert speech.loaded() == ["whisper:base.en"]

    def test_a_lease_ends_on_off_and_on_unload_all(self, both):
        speech._LISTENING.clear()
        assert speech.listen("a") is True
        assert speech.listen("b") is True
        assert speech.listen("a", on=False) is True
        assert speech.listen("b", on=False) is False
        speech.listen("c")
        speech.unload_all()
        assert speech.listening() is False

    def test_stt_loaded_only_for_local(self, both):
        _set(stt_backend="system")
        assert speech.stt_loaded() is False
        _set(stt_backend="local")
        assert speech.stt_loaded() is False
        speech.warm("stt")
        assert speech.stt_loaded() is True

    def test_the_memory_guard_still_refuses_a_load(self, both, monkeypatch):
        _set(stt_backend="local")
        monkeypatch.setattr(speech, "_memory_block", lambda: "memory is low")
        assert "memory is low" in speech.warm("stt")
        assert speech.loaded() == []
        assert speech.LocalSTT().ready()[0] is False

    def test_the_memory_guard_never_refuses_a_loaded_model(self, both, monkeypatch):
        """Under pressure every fast pass raised, so the client dropped to
        clip mode — whose clips were refused the same way — though nothing
        needed loading."""
        _set(stt_backend="local")
        speech.warm("stt")
        monkeypatch.setattr(speech, "_memory_block", lambda: "memory is low")
        stt = speech.LocalSTT()
        assert stt.ready()[0] is True
        assert stt._model("base.en") is not None
        with pytest.raises(speech.SpeechError, match="memory is low"):
            stt._model("medium")  # not loaded: the guard holds

    def test_shedding_keeps_the_stream_model(self, both, clock):
        speech._LISTENING.clear()
        _set(stt_backend="local")
        speech.warm("stt")
        clock[0] += 500
        assert speech.shed_for_memory() == ["whisper:small"]
        assert speech.loaded() == ["whisper:base.en"]


class TestWarmVoice:
    """warm("tts"): the voice that speaks is ready before its first line."""

    def test_local_piper_loads_and_primes_once(self, both):
        _set(tts_backend="local")
        assert speech.warm("tts") == "loaded"
        assert speech.loaded() == ["piper:en_US-lessac-medium"]
        assert both.piper.spoken == [speech.WARM_TEXT]
        assert speech.warm("tts") == "already"
        assert both.piper.spoken == [speech.WARM_TEXT]
        assert speech.describe()["warm"]["tts"] == "loaded"

    def test_a_local_host_says_one_word(self, monkeypatch):
        _set(
            tts_backend="openai",
            tts_url="http://127.0.0.1:8770/v1",
            tts_voice="harvey",
        )
        http = _Http(ctype="audio/wav", body=b"RIFFwav")
        monkeypatch.setattr(speech, "_http", http)
        assert speech.warm("tts") == "loaded"
        (call,) = http.calls
        assert call.url == "http://127.0.0.1:8770/v1/audio/speech"
        assert call.timeout == speech.WARM_TIMEOUT
        body = json.loads(call.body)
        assert body["input"] == speech.WARM_TEXT and body["voice"] == "harvey"
        assert speech.describe()["warm"]["tts"] == "loaded"

    def test_a_paid_api_is_never_warmed(self, http):
        _set(
            tts_backend="openai",
            tts_url="",
            tts_api_key="sk-test",  # pragma: allowlist secret
        )
        result = speech.warm("tts")
        assert result.startswith("skipped:") and "api.openai.com" in result
        assert http.calls == []

    def test_a_host_that_fails_is_skipped_not_raised(self, http):
        _set(tts_backend="openai", tts_url="http://localhost:8770/v1")
        http.raise_ = urllib.error.URLError("refused")
        result = speech.warm("tts")
        assert result.startswith("skipped: could not reach localhost:8770")
        assert speech.describe()["warm"]["tts"] == result

    def test_system_runs_the_probe(self, monkeypatch):
        from backend.web.core import speech_system as sysmod

        probes: list = []
        found = [sysmod.Probe("windows", True, "ok", True, "Windows voices")]

        def probe():
            probes.append(1)
            return found[0]

        monkeypatch.setattr(sysmod, "probe", probe)
        _set(tts_backend="system")
        assert speech.warm("tts") == "loaded"
        assert probes
        found[0] = sysmod.Probe("windows", True, "ok", False, "no voices installed")
        assert speech.warm("tts") == "skipped: no voices installed"

    def test_nothing_ready_is_skipped(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda name: False)
        _set(tts_backend="")
        assert speech.warm("tts").startswith("skipped: no voice backend is ready")

    def test_in_the_background_a_host_is_warmed_again_only_when_due(
        self, monkeypatch, clock
    ):
        _set(tts_backend="openai", tts_url="http://127.0.0.1:8770/v1")
        http = _Http(ctype="audio/wav", body=b"RIFFwav")
        monkeypatch.setattr(speech, "_http", http)
        speech.warm_in_background("tts").join(5)
        assert len(http.calls) == 1
        assert speech.warm_in_background("tts") is None  # warmed just now
        _set(tts_backend="openai", tts_url="http://127.0.0.1:8770/v1", tts_voice="x")
        speech.warm_in_background("tts").join(5)  # another voice: due
        assert len(http.calls) == 2
        clock[0] += speech.REWARM_SECONDS + 1
        speech.warm_in_background("tts").join(5)  # a while later: due
        assert len(http.calls) == 3

    def test_in_the_background_a_resident_voice_is_left_alone(self, both):
        _set(tts_backend="local")
        speech.warm_in_background("tts").join(5)
        assert speech.loaded() == ["piper:en_US-lessac-medium"]
        assert speech.warm_in_background("tts") is None

    def test_a_delayed_warm_decides_on_its_thread(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(speech, "_warm_due", lambda kind: calls.append(kind))
        t = speech.warm_in_background("tts", delay_s=0.01)
        assert t is not None and calls == []  # nothing probed on the caller
        t.join(5)
        assert calls == ["tts"]


class TestDescribeAdditions:
    def test_stream_model_and_warm(self, whisper):
        d = speech.describe()
        assert d["warm"] == {"stt": "", "tts": ""}
        _set(stt_backend="local")
        assert speech.describe()["stream_model"] == "base.en"
        _set(stt_backend="local", stt_stream_model="tiny.en")
        assert speech.describe()["stream_model"] == "tiny.en"
        _set(stt_backend="openai", stt_url="https://api.openai.com/v1")
        assert speech.describe()["stream_model"] == ""  # it does not stream


# --------------------------------------------------------------------------- #
# The voice chain (TtsChain): fallback, health, one voice per reply
# --------------------------------------------------------------------------- #
class _Piper(speech.Backend):
    """A stand-in for the local piper voice: always ready, says WAV."""

    id, kind, label = "local", "tts", "piper on this computer"

    def __init__(self, ok=True):
        self.ok = ok
        self.spoken: list = []
        self.warmed = 0
        self.during = None  # called inside speak (sees the chain mid-flight)

    def ready(self):
        return self.ok, "piper voice en_US-lessac-medium on this computer"

    def engine_name(self):
        return "piper"

    def speak(self, text, voice=""):
        self.spoken.append(text)
        if self.during:
            self.during()
        return b"RIFFpiper", "audio/wav"

    def warm(self, prime=True):
        self.warmed += 1
        return "loaded"


def _refused_port() -> int:
    """A loopback port nobody listens on (connect → refused at once)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def chain(monkeypatch):
    """The configured voice is a local OpenAI-compatible host (Voice Lab's
    shape), piper is the fallback; the clock is ours."""
    piper = _Piper()
    host = speech.OpenAITTS()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host, piper]})
    now = [1000.0]
    monkeypatch.setattr(speech, "_now", lambda: now[0])
    _set(tts_backend="openai", tts_url="http://127.0.0.1:8770/v1", tts_voice="Donna")
    return SimpleNamespace(host=host, piper=piper, now=now, c=speech.TTS_CHAIN)


class TestTtsChain:
    def test_connect_refused_falls_back_to_piper_at_once(self, chain):
        import time as _time

        _set(tts_backend="openai", tts_url="http://127.0.0.1:%d/v1" % _refused_port())
        t0 = _time.monotonic()
        audio, mime, engine, fell_back = chain.c.speak(chain.host, "All done.")
        took = _time.monotonic() - t0
        assert (audio, mime, engine, fell_back) == (
            b"RIFFpiper",
            "audio/wav",
            chain.piper,
            True,
        )
        assert took < 1.0
        assert chain.piper.spoken == ["All done."]
        assert "could not reach 127.0.0.1" in chain.c.down_reason(chain.host)

    def test_a_slow_host_is_cut_at_its_budget(self, chain, monkeypatch):
        """A host that accepts the connection and never answers (F5 on a
        CPU) costs the budget, not TTS_TIMEOUT — and the line alone does not
        mark it down (slow); nothing back for host_hung_after does (hung)."""
        import socket
        import time as _time

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)  # the kernel completes the handshake; nobody answers
        try:
            monkeypatch.setattr(speech, "HOST_BUDGET", 0.3)
            monkeypatch.setattr(speech, "HOST_BUDGET_PER_CHAR", 0.0)
            monkeypatch.setattr(speech, "TTS_TIMEOUT", 0.8)  # hung after this
            _set(
                tts_backend="openai",
                tts_url="http://127.0.0.1:%d/v1" % srv.getsockname()[1],
            )
            t0 = _time.monotonic()
            _a, _m, engine, fell_back = chain.c.speak(chain.host, "Hello there.")
            took = _time.monotonic() - t0
            assert engine is chain.piper and fell_back is True
            assert 0.25 <= took < 0.75
            assert chain.c.down_reason(chain.host) == ""  # slow, not down
            deadline = _time.monotonic() + 5
            while not chain.c.down_reason(chain.host):
                assert _time.monotonic() < deadline, "never marked hung"
                _time.sleep(0.05)
        finally:
            srv.close()
        assert "sent nothing back in 1 s" in chain.c.down_reason(chain.host)

    def test_the_budget_grows_with_the_line_and_is_capped(self, chain, monkeypatch):
        assert speech.host_budget("") == speech.HOST_BUDGET == 1.5
        assert speech.host_budget("x" * 100) == pytest.approx(1.5 + 6.0)
        assert speech.host_budget("x" * 1200) == speech.TTS_TIMEOUT
        http = _Http(ctype="audio/mpeg", body=b"ID3")
        monkeypatch.setattr(speech, "_http", http)
        _a, _m, engine, fell_back = chain.c.speak(chain.host, "All done.")
        assert engine is chain.host and fell_back is False
        # The line's budget is the lane's wait; the socket waits for "hung".
        assert http.calls[0].timeout == speech.host_hung_after("All done.")
        assert speech.host_hung_after("x") == speech.TTS_TIMEOUT
        assert speech.host_hung_after("x" * 1200) == 2 * speech.TTS_TIMEOUT

    def test_a_dead_host_costs_one_attempt_per_window(self, chain, monkeypatch):
        http = _Http()
        http.raise_ = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        monkeypatch.setattr(speech, "_http", http)
        for line in ("One.", "Two.", "Three.", "Four."):
            _a, _m, engine, fell_back = chain.c.speak(chain.host, line)
            assert engine is chain.piper and fell_back is True
        assert len(http.calls) == 1
        assert chain.piper.spoken == ["One.", "Two.", "Three.", "Four."]

    def test_a_5xx_or_no_audio_fails_over_a_4xx_does_not(self, chain, monkeypatch):
        http = _Http(status=500, body=b'{"error": "CUDA error: unknown error"}')
        monkeypatch.setattr(speech, "_http", http)
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Hi.")
        assert engine is chain.piper
        assert "answered 500" in chain.c.down_reason(chain.host)
        chain.c.reset()
        http.status, http.ctype, http.body = 200, "application/json", b"{}"
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Hi.")
        assert engine is chain.piper  # no audio back: the engine's fault
        chain.c.reset()
        http.status, http.body = 400, b'{"error": "unknown voice Donna"}'
        with pytest.raises(speech.SpeechError, match="answered 400") as err:
            chain.c.speak(chain.host, "Hi.")
        assert err.value.down is False
        assert chain.c.down_reason(chain.host) == ""
        assert chain.piper.spoken == ["Hi.", "Hi."]

    def test_health_window_then_a_background_probe(self, chain, monkeypatch):
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        chain.c.speak(chain.host, "One.")
        assert len(http.calls) == 1
        chain.now[0] += speech.HOST_DOWN_SECONDS - 1  # inside the window
        assert chain.c.down_reason(chain.host)
        chain.c.speak(chain.host, "Two.")
        assert len(http.calls) == 1  # no probe yet, no attempt
        chain.now[0] += 2  # past it: the probe runs (inline here), still down
        chain.c.speak(chain.host, "Three.")
        assert len(http.calls) == 2
        probe = http.calls[1]
        assert json.loads(probe.body)["input"] == speech.WARM_TEXT
        assert probe.timeout == speech.PROBE_TIMEOUT
        assert chain.piper.spoken == ["One.", "Two.", "Three."]
        chain.now[0] += speech.HOST_DOWN_SECONDS - 1  # a new window
        assert chain.c.down_reason(chain.host)
        assert len(http.calls) == 2
        http.raise_, http.ctype, http.body = None, "audio/mpeg", b"ID3"
        chain.now[0] += 2
        chain.c.down_reason(chain.host)  # the probe passes …
        assert chain.c.down_reason(chain.host) == ""  # … healthy again

    def test_the_probe_never_runs_on_the_request(self, chain, monkeypatch):
        started: list = []
        monkeypatch.setattr(speech, "_spawn", lambda target, name: started.append(name))
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        chain.c.speak(chain.host, "One.")
        chain.now[0] += speech.HOST_DOWN_SECONDS + 1
        _a, _m, engine, _fb = chain.c.speak(chain.host, "Two.")
        assert engine is chain.piper
        assert len(http.calls) == 1  # the line did not wait on a probe
        assert started == ["mindflock-voice-probe"]
        chain.c.speak(chain.host, "Three.")
        assert started == ["mindflock-voice-probe"]  # one probe at a time

    def test_one_voice_per_reply(self, chain, monkeypatch):
        """Back on the host only once it is healthy AND the reply on the
        fallback has finished (nothing in flight, the gap after its last
        line's playback)."""
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        seen: list = []

        def mid_line():  # the host recovers while piper says the line
            chain.c.mark_up(chain.host)
            seen.append(chain.c.pick(chain.host))

        chain.piper.during = mid_line
        chain.c.speak(chain.host, "The first line of a reply.")
        assert seen[0][0] is chain.piper  # a line in flight holds the voice
        chain.piper.during = None
        line = "And another line of the same reply."  # 35 chars ≈ 2.5 s
        _a, _m, engine, fb = chain.c.speak(chain.host, line)
        assert engine is chain.piper and fb is True  # the reply keeps piper
        assert len(http.calls) == 1
        chain.now[0] += len(line) / speech.SPOKEN_CHARS_PER_SECOND  # it plays
        chain.now[0] += speech.REPLY_GAP_SECONDS - 0.5
        assert chain.c.pick(chain.host)[0] is chain.piper  # not quiet yet
        chain.now[0] += 1  # the pause between replies
        http.raise_, http.ctype, http.body = None, "audio/mpeg", b"ID3"
        _a, _m, engine, fb = chain.c.speak(chain.host, "A new reply.")
        assert engine is chain.host and fb is False
        assert chain.c.pick(chain.host) == (chain.host, False, "")

    def test_no_fallback_ready_keeps_the_failure(self, chain, monkeypatch):
        chain.piper.ok = False
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        with pytest.raises(speech.SpeechError, match="could not reach"):
            chain.c.speak(chain.host, "One.")
        with pytest.raises(speech.SpeechError, match="no fallback voice is ready"):
            chain.c.speak(chain.host, "Two.")
        assert len(http.calls) == 1  # the second line cost nothing
        assert chain.piper.spoken == []

    def test_a_fallback_never_falls_back_to_itself(self, chain, monkeypatch):
        _set(tts_backend="local")
        assert chain.c.fallback_for(chain.piper) is None

    def test_a_warm_that_fails_marks_the_host_and_warms_piper(self, chain, monkeypatch):
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        assert speech.warm("tts").startswith("skipped: could not reach")
        assert chain.c.down_reason(chain.host)
        assert speech.warm("tts") == "loaded"  # the fallback that speaks now
        assert chain.piper.warmed == 1
        assert len(http.calls) == 1


class TestDescribeTruthfulVoice:
    def test_healthy_host(self, chain):
        r = speech.describe()["server_ready"]
        assert r["tts"] is True and r["tts_backend"] == "openai"
        assert r["tts_engine"] == "openai" and r["tts_fallback"] is False
        assert r["tts_down"] == ""

    def test_a_down_host_reports_the_fallback(self, chain):
        chain.c.mark_down(chain.host, "could not reach 127.0.0.1:8770: refused")
        r = speech.describe()["server_ready"]
        assert r["tts"] is True
        assert r["tts_backend"] == "openai"  # Settings keys its rows on it
        assert r["tts_engine"] == "piper (fallback)" and r["tts_fallback"] is True
        assert "could not reach" in r["tts_down"]
        assert "speaking with piper voice" in r["tts_detail"]
        assert "while it is down (could not reach" in r["tts_detail"]

    def test_a_down_host_without_a_fallback_is_not_ready(self, chain):
        chain.piper.ok = False
        chain.c.mark_down(chain.host, "could not reach 127.0.0.1:8770: refused")
        r = speech.describe()["server_ready"]
        assert r["tts"] is False and r["tts_engine"] == ""
        assert "no fallback voice is ready" in r["tts_detail"]
        assert "Server voice not ready" in r["detail"]

    def test_a_too_slow_host_says_so_and_is_not_down(self, chain, monkeypatch):
        monkeypatch.setattr(speech, "_spawn", lambda target, name: None)
        with chain.c._lock:
            chain.c._learn(chain.host.health_key(), "Hello.", 9.0)
        r = speech.describe()["server_ready"]
        assert r["tts"] is True and r["tts_down"] == ""
        assert r["tts_engine"] == "piper (fallback)" and r["tts_fallback"] is True
        assert "while it is too slow (openai http://127.0.0.1:8770" in r["tts_detail"]


class TestFfmpegLookup:
    def test_looked_up_once(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        monkeypatch.setattr(speech, "_which", lambda name: calls.append(name))
        for _ in range(25):
            ok, detail = speech.LocalSTT().ready()
            assert ok is True and "ffmpeg is not on PATH" in detail
        assert calls == ["ffmpeg"]

    def test_never_looked_up_on_the_caller(self, monkeypatch):
        calls, spawned = [], []
        monkeypatch.setattr(speech, "_installed", lambda name: True)
        monkeypatch.setattr(speech, "_which", lambda name: calls.append(name))
        monkeypatch.setattr(
            speech, "_spawn", lambda target, name: spawned.append(target)
        )
        ok, detail = speech.LocalSTT().ready()
        assert ok is True and "ffmpeg" not in detail  # not known yet
        speech.LocalSTT().ready()
        assert calls == [] and len(spawned) == 1  # one background lookup
        spawned[0]()
        assert calls == ["ffmpeg"]
        assert "ffmpeg is not on PATH" in speech.LocalSTT().ready()[1]

    def test_again_after_forget_a_path_change_or_the_recheck(self, monkeypatch, clock):
        calls: list = []
        monkeypatch.setattr(speech, "_which", lambda name: calls.append(name) or "/x")
        assert speech.ffmpeg_path() == "/x" and speech.ffmpeg_path() == "/x"
        assert len(calls) == 1
        speech.forget_ffmpeg()
        speech.ffmpeg_path()
        assert len(calls) == 2
        monkeypatch.setenv("PATH", os.environ.get("PATH", "") + ":/opt/new")
        speech.ffmpeg_path()
        assert len(calls) == 3
        clock[0] += speech.FFMPEG_RECHECK_SECONDS + 1
        speech.ffmpeg_path()
        assert len(calls) == 4
        speech.invalidate()  # after the one-click install
        speech.ffmpeg_path()
        assert len(calls) == 5


class TestRewarmAfterAFailure:
    def test_a_failed_host_warm_is_due_after_the_down_window(self, chain, monkeypatch):
        """Not REWARM_SECONDS: a Voice Lab started a minute after a failed
        warm is warmed before a real line meets its cold load."""
        chain.piper.ok = False  # nothing to warm meanwhile
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        assert speech.warm("tts").startswith("skipped: could not reach")
        chain.c.mark_up(chain.host)  # e.g. the user restarted Voice Lab
        assert speech._warm_due("tts") is False  # just tried
        chain.now[0] += speech.HOST_DOWN_SECONDS + 1
        assert speech._warm_due("tts") is True  # long before REWARM_SECONDS
        http.raise_, http.ctype, http.body = None, "audio/wav", b"RIFF"
        assert speech.warm("tts") == "loaded"
        chain.now[0] += speech.HOST_DOWN_SECONDS + 1
        assert speech._warm_due("tts") is False  # a good warm holds 600 s
        chain.now[0] += speech.REWARM_SECONDS
        assert speech._warm_due("tts") is True

    def test_a_passing_probe_counts_as_a_warm(self, chain, monkeypatch):
        http = _Http()
        http.raise_ = urllib.error.URLError("refused")
        monkeypatch.setattr(speech, "_http", http)
        chain.c.speak(chain.host, "One.")
        http.raise_, http.ctype, http.body = None, "audio/mpeg", b"ID3"
        chain.now[0] += speech.HOST_DOWN_SECONDS + 1
        chain.c.down_reason(chain.host)  # the probe runs (inline) and passes
        assert chain.c.down_reason(chain.host) == ""
        assert speech.describe()["warm"]["tts"] == "loaded"
        assert speech._warm_due("tts") is False
        assert len(http.calls) == 2  # the line's attempt + the probe


def test_the_undo_phrases_and_the_commands_are_hints():
    from backend.web.core import voice_words

    titles = ["hackathon-2", "sitecheck-bot7", "voice-ears", "login-fix"]
    hints = speech.recognition_hints(voice_words.effective({}), titles)
    parts = hints.split(", ")
    # After every other voice phrase (the radio words that act at once lead).
    assert parts.index("scratch that") == parts.index("enough") + 1
    assert parts.index("undo that") == parts.index("scratch that") + 1
    # The commands before the titles, the titles before the vocabulary.
    assert parts.index("scroll") == parts.index("undo that") + 1
    assert parts.index("status") < parts.index("hackathon 2") < parts.index("session")
    assert len(hints) <= speech.HINT_CHARS
    # Without the vocabulary, no commands either (the fast tier's lone words).
    bare = speech.recognition_hints(voice_words.effective({}), (), vocabulary=False)
    assert "scroll" not in bare and bare.endswith("scratch that, undo that")
