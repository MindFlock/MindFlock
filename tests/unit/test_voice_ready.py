"""Recognition is READY before the mic says "talk" (core/speech.py
``make_ready`` / ``stt_hot`` / ``keep_hot``, ``POST /api/voice/ready``).

"In memory" was not "answering": a cached model's pages are swapped out on a
machine short of memory, and the first pass after a quiet minute took 10 to
40 s while ``stt_loaded`` still said true — the mic was red and nothing was
typed. Fakes only: nothing reaches a model.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.routes import speech as routes

client = TestClient(server.app)


class _Local(speech.Backend):
    """``local`` recognition whose readiness pass is counted, can fail and
    can be held."""

    id = "local"
    kind = "stt"
    label = "fake local"

    def __init__(self) -> None:
        self.passes = 0
        self.error: "Exception | None" = None
        self.hold: "threading.Event | None" = None
        self.started = threading.Event()

    def ready(self):
        return True, "fake local ready"

    def warm_keys(self):
        return ["whisper:fake"]

    def make_ready(self):
        self.started.set()
        if self.hold is not None:
            self.hold.wait(5)
        if self.error is not None:
            raise self.error
        self.passes += 1


class _Cloud(speech.Backend):
    id = "openai"
    kind = "stt"
    label = "fake cloud"

    def ready(self):
        return True, "fake cloud ready"


@pytest.fixture
def local(isolate_settings_store, monkeypatch):
    backend = _Local()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [backend, _Cloud()], "tts": []})
    monkeypatch.setattr(speech, "_CACHE", {"whisper:fake": object()})
    monkeypatch.setattr(speech, "_HOT_AT", [0.0])
    S.update_settings(voice={"stt_backend": "local"})
    S.invalidate()
    return backend


def test_loaded_is_not_hot_until_a_real_pass(local):
    assert speech.stt_loaded() is True
    assert speech.stt_hot() is False
    speech.note_hot()
    assert speech.stt_hot() is True


def test_hot_lapses_after_hot_seconds(local, monkeypatch):
    speech.note_hot()
    at = speech._HOT_AT[0]
    monkeypatch.setattr(speech.time, "monotonic", lambda: at + speech.HOT_SECONDS + 1)
    assert speech.stt_hot() is False


def test_hot_needs_the_models_in_memory(local, monkeypatch):
    speech.note_hot()
    monkeypatch.setattr(speech, "_CACHE", {})
    assert speech.stt_hot() is False


def test_make_ready_runs_a_pass_then_is_hot(local):
    got = speech.make_ready()
    assert got["ready"] is True and got["hot"] is False
    assert local.passes == 1 and speech.stt_hot() is True
    # Hot: the next call runs nothing.
    again = speech.make_ready()
    assert again["hot"] is True and local.passes == 1


def test_make_ready_failure_raises_and_stays_cold(local):
    local.error = speech.SpeechError("faster-whisper failed: no memory")
    with pytest.raises(speech.SpeechError, match="no memory"):
        speech.make_ready()
    assert speech.stt_hot() is False
    local.error = RuntimeError("boom")
    with pytest.raises(speech.SpeechError, match="did not get ready: boom"):
        speech.make_ready()


def test_two_callers_share_one_pass(local):
    local.hold = threading.Event()
    out: list = []
    first = threading.Thread(target=lambda: out.append(speech.make_ready()))
    first.start()
    assert local.started.wait(5)
    second = threading.Thread(target=lambda: out.append(speech.make_ready()))
    second.start()
    local.hold.set()
    first.join(5)
    second.join(5)
    assert len(out) == 2 and all(o["ready"] for o in out)
    assert local.passes == 1


def test_another_backend_is_ready_at_once(local):
    S.update_settings(voice={"stt_backend": "openai"})
    S.invalidate()
    assert speech.stt_hot() is True
    assert speech.make_ready()["hot"] is True
    assert local.passes == 0
    assert speech.keep_hot() is None


def test_keep_hot_runs_only_when_the_last_pass_is_old(local, monkeypatch):
    # Never passed: a renewal readies it.
    t = speech.keep_hot()
    assert t is not None
    t.join(5)
    assert local.passes == 1
    # Fresh: nothing.
    assert speech.keep_hot() is None
    # Old: again.
    at = speech._HOT_AT[0]
    monkeypatch.setattr(speech, "HOT_SECONDS", speech.KEEP_HOT_SECONDS)
    monkeypatch.setattr(
        speech.time, "monotonic", lambda: at + speech.KEEP_HOT_SECONDS + 1
    )
    t = speech.keep_hot()
    assert t is not None
    t.join(5)
    assert local.passes == 2


def test_keep_hot_does_not_load_an_unloaded_model(local, monkeypatch):
    monkeypatch.setattr(speech, "_CACHE", {})
    assert speech.keep_hot() is None
    assert local.passes == 0


def test_keep_hot_survives_a_failing_pass(local):
    local.error = RuntimeError("boom")
    t = speech.keep_hot()
    assert t is not None
    t.join(5)
    assert speech.stt_hot() is False


# ---- the routes ---------------------------------------------------------- #
def test_ready_route_waits_for_the_pass(local):
    r = client.post("/api/voice/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True and body["hot"] is False and body["ms"] >= 0
    assert local.passes == 1
    assert client.post("/api/voice/ready").json()["hot"] is True
    assert local.passes == 1


def test_ready_route_says_why_it_is_not(local):
    local.error = speech.SpeechError("faster-whisper failed: no memory")
    r = client.post("/api/voice/ready")
    assert r.status_code == 503
    assert r.json() == {"ready": False, "error": "faster-whisper failed: no memory"}


def test_ready_route_without_recognition(local, monkeypatch):
    monkeypatch.setattr(local, "ready", lambda: (False, "local models not installed"))
    r = client.post("/api/voice/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert "not installed" in r.json()["error"]
    assert local.passes == 0


def test_listen_reports_hot_and_keeps_it(local, monkeypatch):
    kept: list = []
    monkeypatch.setattr(routes._speech, "keep_hot", lambda: kept.append(1))
    monkeypatch.setattr(routes._speech, "warm_in_background", lambda kind: None)
    body = client.post("/api/voice/listen", json={"on": True, "client": "t"}).json()
    assert body["stt_hot"] is False and kept == [1]
    speech.note_hot()
    off = client.post("/api/voice/listen", json={"on": False, "client": "t"}).json()
    assert off["stt_hot"] is True and kept == [1]


def test_strict_prime_raises_and_quiet_prime_does_not(monkeypatch):
    from backend.web.core import speech_stream

    class _Lane:
        def run(self, fn, kind):
            raise RuntimeError("lane full")

    monkeypatch.setattr(speech_stream, "lane", lambda name: _Lane())
    monkeypatch.setattr(speech, "_import", lambda name: __import__("array"))
    speech.LocalSTT._prime(object(), "whisper:x")  # a warm-up: quiet
    with pytest.raises(speech.SpeechError, match="lane full"):
        speech.LocalSTT._prime(object(), "whisper:x", strict=True)
