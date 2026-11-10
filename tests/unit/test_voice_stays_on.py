"""Voice stays on when memory runs short (owner, 2026-11-10: "I don't want
voice models off when memory gets low").

With ``memory_pressure`` reading ``critical`` (and ``is_low`` true):

* local recognition loads, streams and runs the accurate pass — no
  "local recognition held back" on every phrase;
* the boot/stream warm-up loads it;
* Kokoro (and piper) load and are ready;
* ``speech.host_held_back`` is "" and a local voice host (Voice Lab) is
  picked, called and warmed — nothing falls back for memory;
* the memory watch sheds no voice model;
* the memory shield's critical step never stops Voice Lab.

Fakes only: no model is loaded, no host is reached, no systemctl runs.
"""

from __future__ import annotations

import subprocess
from array import array
from types import SimpleNamespace

import pytest

from backend import memory_pressure
from backend.config import settings as S
from backend.web.core import memory_shield as shield
from backend.web.core import speech
from backend.web.core import speech_kokoro as kokoro

GiB = 1024**3


def _critical(fresh: bool = False) -> dict:
    return {
        "level": "critical",
        "available": int(0.4 * GiB),
        "total": 20 * GiB,
        "available_pct": 2.0,
        "swap_total": 0,
        "swap_free": 0,
        "psi": None,
        "at": 0.0,
    }


def _set(**voice):
    S.update_settings(voice=voice)
    S.invalidate()


@pytest.fixture(autouse=True)
def _memory_critical(isolate_settings_store, monkeypatch, tmp_path):
    """Memory reads critical everywhere it can be read; model files under a
    tmp voice dir, no sweep timer, inline background work, a clean cache."""
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(memory_pressure, "snapshot", _critical)
    monkeypatch.setattr(memory_pressure, "level", lambda **kw: "critical")
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: True)
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    shield._reset_for_tests()
    speech.unload_all()
    speech.TTS_CHAIN.reset()
    yield
    speech.unload_all()
    speech.TTS_CHAIN.reset()
    shield._reset_for_tests()


@pytest.fixture
def whisper(monkeypatch):
    """A fake faster-whisper (and numpy): loads and passes recorded."""
    seen: dict = {"names": [], "calls": []}

    class _Model:
        def __init__(self, name, **kw):
            seen["names"].append(name)

        def transcribe(self, audio, **kw):
            seen["calls"].append(kw)
            w = SimpleNamespace
            segs = [
                w(text=" Hello there.", words=[w(word=" Hello", start=0.0, end=0.4)])
            ]
            return iter(segs), SimpleNamespace(language="en")

    def frombuffer(raw, dtype):
        out = array("f")
        out.frombytes(raw)
        return list(out)

    mods = {
        "faster_whisper": SimpleNamespace(WhisperModel=_Model),
        "numpy": SimpleNamespace(
            float32="float32",
            frombuffer=frombuffer,
            zeros=lambda n, dtype=None: [0.0] * n,
        ),
    }
    monkeypatch.setattr(speech, "_installed", lambda name: name == "faster_whisper")
    monkeypatch.setattr(speech, "_import", lambda name: mods[name])
    monkeypatch.setattr(speech, "_shim_av_open", lambda: None)
    _set(stt_backend="local")
    return seen


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #
def test_local_recognition_is_ready_and_loads_while_critical(whisper):
    ok, detail = speech.LocalSTT().ready()
    assert ok is True and "held back" not in detail
    assert speech.LocalSTT()._model() is not None
    assert whisper["names"]  # a fresh load, not refused


def test_the_live_stream_runs_while_critical(whisper):
    run = speech.LocalSTT().stream_transcriber("en")
    words = run(array("f", [0.0] * 1600), "")
    assert words and words[0][0] == " Hello"
    assert len(whisper["calls"]) == 1


def test_the_accurate_pass_is_not_refused_while_critical(whisper):
    run = speech.LocalSTT().final_transcriber("en-US")
    assert run is not None
    # Before: SpeechError("local recognition held back: memory is critically
    # low …") on every phrase, since the accurate model had to load.
    assert run(array("f", [0.0] * 1600)).strip() == "Hello there."
    assert speech.stt_loaded() or speech.loaded()


def test_the_warm_up_loads_while_critical(whisper):
    assert speech.warm("stt") == "loaded"
    assert speech.loaded()


def test_the_memory_watch_sheds_no_voice_model(whisper):
    speech.warm("stt")
    held = speech.loaded()
    speech._LISTENING.clear()  # nobody listening: nothing is resident
    assert speech.shed_for_memory(min_idle_s=0) == []
    assert speech.loaded() == held


# --------------------------------------------------------------------------- #
# Local voices
# --------------------------------------------------------------------------- #
def test_kokoro_loads_and_is_ready_while_critical(monkeypatch):
    model = object()
    monkeypatch.setattr(kokoro, "_load", lambda: model)
    monkeypatch.setattr(kokoro, "unavailable_reason", lambda: "")
    assert kokoro.engine() is model
    assert kokoro.loaded()
    ok, detail = kokoro.KokoroTTS().ready()
    assert ok is True and "held back" not in detail


def test_piper_is_ready_while_critical(monkeypatch):
    monkeypatch.setattr(speech, "_installed", lambda name: name == "piper")
    ok, detail = speech.LocalTTS().ready()
    assert ok is True and "held back" not in detail


# --------------------------------------------------------------------------- #
# A local voice host (Voice Lab)
# --------------------------------------------------------------------------- #
LOCAL_HOST = "http://127.0.0.1:8770/v1"


def test_host_held_back_is_empty_while_critical():
    assert speech.host_held_back(LOCAL_HOST) == ""
    assert speech.host_held_back("https://api.openai.com/v1") == ""


def test_the_local_voice_host_is_picked_and_called_while_critical(monkeypatch):
    host = speech.OpenAITTS()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host]})
    _set(tts_backend="openai", tts_url=LOCAL_HOST)
    calls: list = []
    monkeypatch.setattr(
        speech,
        "_http",
        lambda *a, **kw: calls.append(a) or (200, "audio/mpeg", b"ID3voice"),
    )
    assert speech.TTS_CHAIN.memory_yield(host) == ""
    engine, fell_back, _why = speech.TTS_CHAIN.pick(host, probe=False)
    assert engine is host and fell_back is False
    audio, _mime = host.speak("All done.")
    assert audio == b"ID3voice" and calls
    assert speech.TTS_CHAIN.status(host, probe=False)["held"] == ""


# --------------------------------------------------------------------------- #
# The memory shield
# --------------------------------------------------------------------------- #
def test_the_shields_critical_step_never_stops_voice_lab(monkeypatch):
    ran: list = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: ran.append(a) or pytest.fail("ran %r" % (a,)),
    )
    for now in (1000.0, 1010.0, 1100.0):
        out = shield.tick(SimpleNamespace(), _critical(), now)
        assert "voicelab" not in out
    assert ran == []
    assert not hasattr(shield, "stop_voice_lab")
    assert not hasattr(shield, "voice_lab_yield")
    assert "voice_yield" not in shield.status()
