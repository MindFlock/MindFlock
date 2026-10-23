"""``/api/voice*`` (routes/speech.py): the shapes and every status code, with
the backend registry swapped for fakes and the settings store at a tmp file
(``isolate_settings_store``). Nothing here reaches a model or the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import auth as _auth
from backend.web.core import speech
from backend.web.routes import speech as routes

client = TestClient(server.app)


class _Fake(speech.Backend):
    def __init__(self, kind, ok=True, detail="fake is ready"):
        self.id, self.kind, self.label = "fake", kind, "fake"
        self.ok, self.detail = ok, detail
        self.text = "hello there"
        self.audio = (b"ID3fake-mp3", "audio/mpeg")
        self.fail: BaseException | None = None
        self.calls: list = []

    def ready(self):
        return self.ok, self.detail

    def transcribe(self, audio, mime, lang=""):
        self.calls.append(("transcribe", audio, mime, lang))
        if self.fail:
            raise self.fail
        return self.text

    def speak(self, text, voice=""):
        self.calls.append(("speak", text, voice))
        if self.fail:
            raise self.fail
        return self.audio


@pytest.fixture(autouse=True)
def _store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


@pytest.fixture
def fakes(monkeypatch):
    stt, tts = _Fake("stt"), _Fake("tts")
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [stt], "tts": [tts]})
    return SimpleNamespace(stt=stt, tts=tts)


def _voice(**fields):
    S.update_settings(voice=fields)
    S.invalidate()


def _transcribe(body=b"\x1aE\xdf\xa3clip", ctype="audio/webm;codecs=opus", query=""):
    return client.post(
        "/api/voice/transcribe" + query, content=body, headers={"Content-Type": ctype}
    )


# --------------------------------------------------------------------------- #
# GET /api/voice
# --------------------------------------------------------------------------- #
class TestGet:
    def test_defaults(self, fakes):
        r = client.get("/api/voice")
        assert r.status_code == 200
        body = r.json()
        assert body["stt"] == "phone" and body["tts"] == "phone"
        assert body["approve_dialogs"] is True
        assert body["agent_words"] is True
        assert body["backends"] == {"stt": ["fake"], "tts": ["fake"]}
        sr = body["server_ready"]
        assert sr["stt"] is True and sr["tts"] is True
        assert sr["stt_backend"] == "fake" and sr["stt_detail"] == "fake is ready"
        assert isinstance(sr["detail"], str) and "ready (fake)" in sr["detail"]

    def test_reflects_settings_and_readiness(self, fakes):
        _voice(stt="server", approve_dialogs=False, tts_backend="fake")
        S.update_settings(notifications={"ntfy_agent_words": False})
        S.invalidate()
        fakes.tts.ok, fakes.tts.detail = (
            False,
            "local models not installed: uv sync --group voice",
        )
        body = client.get("/api/voice").json()
        assert body["stt"] == "server" and body["tts"] == "phone"
        assert body["approve_dialogs"] is False
        assert body["agent_words"] is False
        sr = body["server_ready"]
        assert sr["stt"] is True
        assert sr["tts"] is False
        assert sr["tts_detail"] == "local models not installed: uv sync --group voice"

    def test_readable_without_configure_rights(self, fakes, monkeypatch):
        async def _refuse(scope):
            return False

        monkeypatch.setattr(_auth, "may_configure", _refuse)
        assert client.get("/api/voice").status_code == 200
        # …while the owner-only key clearing is refused for the same caller.
        r = client.post("/api/voice/clear-key", json={"kind": "stt"})
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# POST /api/voice/transcribe
# --------------------------------------------------------------------------- #
class TestTranscribe:
    def test_ok(self, fakes):
        _voice(stt="server")
        r = _transcribe(query="?lang=en")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["text"] == "hello there"
        assert body["backend"] == "fake"
        assert isinstance(body["ms"], int) and body["ms"] >= 0
        # The raw body reaches the backend with the bare media type and the hint.
        assert fakes.stt.calls == [
            ("transcribe", b"\x1aE\xdf\xa3clip", "audio/webm", "en")
        ]

    def test_every_audio_type_passes_the_415_gate(self, fakes):
        _voice(stt="server")
        for ctype in (
            "audio/webm",
            "audio/mp4",
            "audio/wav",
            "audio/ogg",
            "audio/mpeg",
        ):
            assert _transcribe(ctype=ctype).status_code == 200, ctype
        assert [c[2] for c in fakes.stt.calls] == [
            "audio/webm",
            "audio/mp4",
            "audio/wav",
            "audio/ogg",
            "audio/mpeg",
        ]

    def test_bad_lang_is_dropped_not_refused(self, fakes):
        _voice(stt="server")
        assert _transcribe(query="?lang=../etc").status_code == 200
        assert fakes.stt.calls[0][3] == ""
        assert _transcribe(query="?lang=pt-BR").status_code == 200
        assert fakes.stt.calls[1][3] == "pt-BR"

    def test_415_when_not_audio(self, fakes):
        _voice(stt="server")
        r = client.post("/api/voice/transcribe", json={"audio": "nope"})
        assert r.status_code == 415
        assert "audio/webm" in r.json()["error"]
        r = client.post("/api/voice/transcribe", content=b"x")  # no Content-Type at all
        assert r.status_code == 415
        assert fakes.stt.calls == []

    def test_413_over_the_cap(self, fakes, monkeypatch):
        _voice(stt="server")
        monkeypatch.setattr(routes, "MAX_AUDIO_BYTES", 16)
        assert _transcribe(body=b"x" * 16).status_code == 200
        r = _transcribe(body=b"x" * 17)
        assert r.status_code == 413
        assert "over" in r.json()["error"]
        assert len(fakes.stt.calls) == 1

    def test_400_empty_clip(self, fakes):
        _voice(stt="server")
        assert _transcribe(body=b"").status_code == 400

    def test_409_when_recognition_runs_on_the_phone(self, fakes):
        r = _transcribe()
        assert r.status_code == 409
        assert "Settings" in r.json()["error"]
        assert fakes.stt.calls == []

    def test_409_when_no_backend_is_ready(self, fakes):
        _voice(stt="server")
        fakes.stt.ok, fakes.stt.detail = (
            False,
            "local models not installed: uv sync --group voice",
        )
        r = _transcribe()
        assert r.status_code == 409
        assert r.json() == {
            "error": "no recognition backend is ready (local models not installed: uv sync --group voice)"
        }
        _voice(stt="server", stt_backend="fake")  # configured: its own detail, verbatim
        r = _transcribe()
        assert r.status_code == 409
        assert r.json() == {
            "error": "local models not installed: uv sync --group voice"
        }
        assert fakes.stt.calls == []

    def test_409_unknown_configured_backend(self, fakes):
        _voice(stt="server", stt_backend="deepgram")
        r = _transcribe()
        assert r.status_code == 409
        assert r.json()["error"].startswith("unknown recognition backend 'deepgram'")

    def test_502_on_backend_failure(self, fakes):
        _voice(stt="server")
        fakes.stt.fail = speech.SpeechError(
            "api.openai.com answered 401: Incorrect API key"
        )
        r = _transcribe()
        assert r.status_code == 502
        assert r.json() == {"error": "api.openai.com answered 401: Incorrect API key"}
        fakes.stt.fail = RuntimeError("boom")
        r = _transcribe()
        assert r.status_code == 502
        assert r.json() == {"error": "RuntimeError: boom"}


# --------------------------------------------------------------------------- #
# POST /api/voice/speak
# --------------------------------------------------------------------------- #
class TestSpeak:
    def test_ok(self, fakes):
        _voice(tts="server")
        r = client.post(
            "/api/voice/speak", json={"text": "  All done.  ", "voice": "nova"}
        )
        assert r.status_code == 200, r.text
        assert r.content == b"ID3fake-mp3"
        assert r.headers["content-type"].startswith("audio/mpeg")
        assert r.headers["x-voice-backend"] == "fake"
        assert r.headers["cache-control"] == "no-store"
        assert fakes.tts.calls == [("speak", "All done.", "nova")]

    def test_wav_mime_passes_through(self, fakes):
        _voice(tts="server")
        fakes.tts.audio = (b"RIFFwav", "audio/wav")
        r = client.post("/api/voice/speak", json={"text": "Hi"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/wav")
        assert fakes.tts.calls == [("speak", "Hi", "")]

    def test_400_without_text(self, fakes):
        _voice(tts="server")
        assert client.post("/api/voice/speak", json={}).status_code == 400
        assert client.post("/api/voice/speak", json={"text": "   "}).status_code == 400
        assert client.post("/api/voice/speak", json={"text": 5}).status_code == 400
        assert fakes.tts.calls == []

    def test_413_over_the_text_cap(self, fakes):
        _voice(tts="server")
        assert (
            client.post(
                "/api/voice/speak", json={"text": "x" * routes.MAX_SPEAK_CHARS}
            ).status_code
            == 200
        )
        r = client.post(
            "/api/voice/speak", json={"text": "x" * (routes.MAX_SPEAK_CHARS + 1)}
        )
        assert r.status_code == 413
        assert "1201" in r.json()["error"]
        assert len(fakes.tts.calls) == 1

    def test_409_when_the_voice_runs_on_the_phone(self, fakes):
        r = client.post("/api/voice/speak", json={"text": "Hi"})
        assert r.status_code == 409
        assert "Settings" in r.json()["error"]

    def test_409_when_no_backend_is_ready(self, fakes):
        _voice(tts="server")
        fakes.tts.ok, fakes.tts.detail = (
            False,
            "OpenAI-compatible host needs an API key for api.openai.com",
        )
        r = client.post("/api/voice/speak", json={"text": "Hi"})
        assert r.status_code == 409
        assert "needs an API key" in r.json()["error"]

    def test_502_on_backend_failure(self, fakes):
        _voice(tts="server")
        fakes.tts.fail = speech.SpeechError("piper failed: no voice")
        r = client.post("/api/voice/speak", json={"text": "Hi"})
        assert r.status_code == 502
        assert r.json() == {"error": "piper failed: no voice"}


# --------------------------------------------------------------------------- #
# POST /api/voice/clear-key
# --------------------------------------------------------------------------- #
class TestClearKey:
    def test_clears_one_key_and_keeps_the_other(self, fakes):
        _voice(
            stt_api_key="sk-stt",  # pragma: allowlist secret
            tts_api_key="sk-tts",  # pragma: allowlist secret
            stt_url="https://x.example/v1",
        )
        r = client.post("/api/voice/clear-key", json={"kind": "stt"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "kind": "stt"}
        S.invalidate()
        v = S.load_settings().voice
        assert (
            v.stt_api_key == ""  # pragma: allowlist secret
            and v.tts_api_key == "sk-tts"  # pragma: allowlist secret
            and v.stt_url == "https://x.example/v1"
        )
        # The settings API now reports the field as unset (no mask).
        assert (
            client.get("/api/settings").json()["settings"]["voice"]["stt_api_key"] == ""
        )

    def test_400_bad_kind(self, fakes):
        assert (
            client.post("/api/voice/clear-key", json={"kind": "github"}).status_code
            == 400
        )
        assert client.post("/api/voice/clear-key", json={}).status_code == 400

    def test_settings_post_cannot_clear_a_secret(self, fakes):
        """Why the route exists: a blank secret through ``POST /api/settings``
        means keep."""
        _voice(tts_api_key="sk-tts")  # pragma: allowlist secret
        r = client.post("/api/settings", json={"voice": {"tts_api_key": ""}})
        assert r.status_code == 200
        S.invalidate()
        kept = S.load_settings().voice.tts_api_key
        assert kept == "sk-tts"  # pragma: allowlist secret
