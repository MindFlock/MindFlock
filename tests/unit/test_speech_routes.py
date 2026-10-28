"""``/api/voice*`` (routes/speech.py): the shapes and every status code, with
the backend registry swapped for fakes and the settings store at a tmp file
(``isolate_settings_store``). Nothing here reaches a model or the network.
"""

from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import auth as _auth
from backend.web.core import speech
from backend.web.core import speech_stream
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

    def transcribe(self, audio, mime, lang="", prompt=""):
        self.calls.append(("transcribe", audio, mime, lang, prompt))
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
        assert body["final"] == {
            "samples": 0,
            "typical_ms": 0,
            "p95_ms": 0,
            "expect_ms": speech_stream.FINAL_EXPECT_MS,
            "timeout_ms": int(speech_stream.FINAL_TIMEOUT_S * 1000),
        }
        assert sr["final_ms"] == speech_stream.FINAL_EXPECT_MS

    def test_reports_the_measured_end_waits(self, streaming):
        streaming.final_transcriber = lambda lang="", hints="": lambda a: "Hello."
        for _ in range(3):
            sid = _open().json()["id"]
            streaming.passes += [_words("hello world")] * 3
            _chunk(sid)
            _chunk(sid)
            assert "final" in client.post("/api/voice/stream/%s/end" % sid).json()
        body = client.get("/api/voice").json()
        final = body["final"]
        assert final["samples"] == 3
        assert final["timeout_ms"] == int(speech_stream.FINAL_TIMEOUT_S * 1000)
        assert 0 <= final["typical_ms"] <= final["p95_ms"] <= final["timeout_ms"]
        assert final["expect_ms"] == final["p95_ms"]  # measured, not the bench's
        assert body["server_ready"]["final_ms"] == final["expect_ms"]

    def test_reflects_settings_and_readiness(self, fakes):
        _voice(stt="server", approve_dialogs=False, tts_backend="fake")
        S.update_settings(notifications={"ntfy_agent_words": False})
        S.invalidate()
        fakes.tts.ok, fakes.tts.detail = (
            False,
            "local models not installed: Settings → Voice installs them",
        )
        body = client.get("/api/voice").json()
        assert body["stt"] == "server" and body["tts"] == "phone"
        assert body["approve_dialogs"] is False
        assert body["agent_words"] is False
        sr = body["server_ready"]
        assert sr["stt"] is True
        assert sr["tts"] is False
        assert (
            sr["tts_detail"]
            == "local models not installed: Settings → Voice installs them"
        )

    def test_serves_the_effective_words_and_ours(self, fakes):
        from backend.web.core import voice_words

        body = client.get("/api/voice").json()
        assert body["words"] == voice_words.effective({})
        assert body["words_default"] == voice_words.DEFAULTS
        assert body["words"]["send"] == ["over"]
        assert body["words"]["wake"] == "mindflock"
        _voice(words={"send": ["Go Go"], "wake": "Jarvis"})
        body = client.get("/api/voice").json()
        assert body["words"]["send"] == ["go go"]
        assert body["words"]["wake"] == "jarvis"
        assert body["words"]["end"] == ["over and out", "over, out"]
        assert body["words_default"]["send"] == ["over"]

    def test_reports_the_stream_model_and_warm_ups(self, fakes):
        body = client.get("/api/voice").json()
        assert body["stream_model"] == ""  # the fake does not stream
        assert body["warm"] == {"stt": "", "tts": ""}

    def test_warms_both_kinds(self, fakes, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            speech, "warm_in_background", lambda kind, delay_s=0.0: calls.append(kind)
        )
        assert client.get("/api/voice").status_code == 200
        assert calls == ["stt", "tts"]

    def test_boot_warms_both_kinds(self, fakes, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_VOICE_WARM", "1")  # the suite turns it off
        calls: list = []
        monkeypatch.setattr(
            speech,
            "warm_in_background",
            lambda kind, delay_s=0.0: calls.append((kind, delay_s)),
        )
        with TestClient(server.app):
            pass
        assert calls == [("stt", 3.0), ("tts", 6.0)]

    def test_the_suite_skips_the_boot_warm(self, fakes, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_VOICE_WARM", "0")
        calls: list = []
        monkeypatch.setattr(
            speech, "warm_in_background", lambda kind, delay_s=0.0: calls.append(kind)
        )
        with TestClient(server.app):
            pass
        assert calls == []

    def test_system_block(self, fakes, monkeypatch):
        from backend.web.core import speech_system

        found = speech_system.Probe(
            "wsl",
            stt_ok=True,
            stt_detail="Windows (from WSL) speech recognition (en-US)",
            tts_ok=True,
            tts_detail="Windows (from WSL) voices: Microsoft Zira Desktop",
            voices=["Microsoft Zira Desktop"],
            recognizers=["en-US"],
        )
        monkeypatch.setattr(speech_system, "probe", lambda: found)
        system = client.get("/api/voice").json()["system"]
        assert system == {
            "available": True,
            "platform": "wsl",
            "detail": "Recognition: Windows (from WSL) speech recognition (en-US)."
            " Voice: Windows (from WSL) voices: Microsoft Zira Desktop.",
            "voices": ["Microsoft Zira Desktop"],
            "recognizers": ["en-US"],
            "stt_quality": "basic",
        }

    def test_system_block_is_offline_under_pytest(self, fakes):
        # Nothing is spawned for a poll in the suite: the block is there, the
        # engine unprobed (or plain Linux's sentence).
        system = client.get("/api/voice").json()["system"]
        assert set(system) == {
            "available",
            "platform",
            "detail",
            "voices",
            "recognizers",
            "stt_quality",
        }
        assert system["available"] is False and system["voices"] == []

    def test_readable_without_configure_rights(self, fakes, monkeypatch):
        async def _refuse(scope):
            return False

        monkeypatch.setattr(_auth, "may_configure", _refuse)
        r = client.get("/api/voice")
        assert r.status_code == 200
        # It reports that this caller may not use the owner-only routes…
        assert r.json()["configure_allowed"] is False
        # …which refuse the same caller.
        r = client.post("/api/voice/clear-key", json={"kind": "stt"})
        assert r.status_code == 403
        r = client.post("/api/voice/install-local")
        assert r.status_code == 403
        assert client.get("/api/voice/install-local").status_code == 200
        r = client.post("/api/voice/say-all", json={"on": True})
        assert r.status_code == 403

    def test_configure_allowed_for_the_owner(self, fakes):
        assert client.get("/api/voice").json()["configure_allowed"] is True


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
            ("transcribe", b"\x1aE\xdf\xa3clip", "audio/webm", "en", "")
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

    def test_served_while_the_phone_prefers_its_own_recognizer(self, fakes):
        # voice.stt only says which engine the PHONE page prefers; the desktop
        # app's mic always transcribes on the server, so it is not a gate.
        _voice(stt="phone")
        r = _transcribe(body=b"RIFF\x00\x00\x00\x00WAVEfmt ", ctype="audio/wav")
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "hello there"
        assert fakes.stt.calls[0][2] == "audio/wav"

    def test_409_when_no_backend_is_ready(self, fakes):
        _voice(stt="server")
        fakes.stt.ok, fakes.stt.detail = (
            False,
            "local models not installed: Settings → Voice installs them",
        )
        r = _transcribe()
        assert r.status_code == 409
        assert r.json() == {
            "error": "no recognition backend is ready (local models not installed: Settings → Voice installs them)"
        }
        _voice(stt="server", stt_backend="fake")  # configured: its own detail, verbatim
        r = _transcribe()
        assert r.status_code == 409
        assert r.json() == {
            "error": "local models not installed: Settings → Voice installs them"
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

    def test_served_while_the_phone_prefers_its_own_voice(self, fakes):
        _voice(tts="phone")
        r = client.post("/api/voice/speak", json={"text": "Hi"})
        assert r.status_code == 200, r.text
        assert fakes.tts.calls == [("speak", "Hi", "")]

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

    def test_the_model_fields_are_owner_only(self):
        # The settings POST allow-list: a voice model is never an open field.
        from backend.web.addons import settings as addon

        assert addon._guarded_paths(
            {"voice": {"stt_model": "tiny", "stt_stream_model": "tiny.en"}}
        ) == ["voice.stt_model", "voice.stt_stream_model"]

    def test_the_words_post_roundtrips_normalised(self, fakes):
        r = client.post(
            "/api/settings",
            json={"voice": {"words": {"stop": [" HALT ", "halt", "x!"], "send": []}}},
        )
        assert r.status_code == 200
        S.invalidate()
        assert S.load_settings().voice.words == {"stop": ["halt"]}
        assert client.get("/api/voice").json()["words"]["stop"] == ["halt"]
        assert client.get("/api/settings").json()["settings"]["voice"]["words"] == {
            "stop": ["halt"]
        }
        # {} = back to all defaults.
        r = client.post("/api/settings", json={"voice": {"words": {}}})
        assert r.status_code == 200
        S.invalidate()
        assert S.load_settings().voice.words == {}
        assert "words" not in S.load_settings().voice.to_dict()

    def test_the_words_are_owner_only(self):
        from backend.web.addons import settings as addon

        assert addon._guarded_paths({"voice": {"words": {}}}) == ["voice.words"]

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


def test_413_holds_for_a_chunked_upload_without_content_length(fakes, monkeypatch):
    """A body sent in chunks carries no Content-Length: the cap must still stop
    the read, not hold the whole clip in memory first."""
    _voice(stt="server")
    monkeypatch.setattr(routes, "MAX_AUDIO_BYTES", 16)

    def chunks():
        for _ in range(4):
            yield b"x" * 8

    r = client.post(
        "/api/voice/transcribe",
        content=chunks(),
        headers={"Content-Type": "audio/webm", "Transfer-Encoding": "chunked"},
    )
    assert r.status_code == 413
    assert fakes.stt.calls == []


# --------------------------------------------------------------------------- #
# /api/voice/stream*
# --------------------------------------------------------------------------- #
@pytest.fixture
def streaming(fakes):
    """The fake recognizer streams: each pass answers with the next of
    ``stt.passes`` (an exception is raised)."""
    stt = fakes.stt
    stt.streams, stt.passes, stt.langs, stt.hints = True, [], [], None

    def stream_transcriber(lang="", hints=""):
        stt.langs.append(lang)
        stt.hints = hints  # the server sends the fast tier none

        def run(audio, prompt):
            stt.calls.append(("pass", len(audio), prompt))
            out = stt.passes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out

        return run

    stt.stream_transcriber = stream_transcriber
    speech_stream.clear()
    yield stt
    speech_stream.clear()


def _wav(seconds=0.5, rate=16000, channels=1, sample=b"\x00\x10"):
    """A WAV chunk; each sample ``sample`` (default 4096/32768: speech-loud,
    so the stream's silence check lets the pass run)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(sample * int(seconds * rate) * channels)
    return buf.getvalue()


def _words(text, step=0.2):
    """``[(word, start, end)]``, one word every ``step`` s — inside the
    audio fed (a word a pass places after the last loud frame is dropped)."""
    return [(w, i * step, i * step + 0.15) for i, w in enumerate(text.split())]


def _open(lang="en-US"):
    return client.post("/api/voice/stream", json={"lang": lang})


def _chunk(sid, body=None, ctype="audio/wav"):
    return client.post(
        "/api/voice/stream/%s/chunk" % sid,
        content=_wav() if body is None else body,
        headers={"Content-Type": ctype},
    )


class TestStream:
    def test_get_says_whether_the_ready_backend_streams(self, fakes):
        assert client.get("/api/voice").json()["server_ready"]["stt_stream"] is False
        fakes.stt.streams = True
        assert client.get("/api/voice").json()["server_ready"]["stt_stream"] is True

    def test_a_backend_that_does_not_stream_is_409(self, fakes):
        r = _open()
        assert r.status_code == 409
        assert r.json()["error"] == (
            "fake recognition does not stream: send one clip per phrase"
        )

    def test_no_ready_backend_is_409(self, streaming):
        streaming.ok, streaming.detail = False, "local models not installed"
        r = _open()
        assert r.status_code == 409
        assert r.json()["error"] == (
            "no recognition backend is ready (local models not installed)"
        )

    def test_open_chunk_end(self, streaming):
        r = _open("en-US")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True and isinstance(body["id"], str) and body["id"]
        # The accurate pass's hints; the fast tier gets none (its pass time).
        assert body["hints"] > 0 and streaming.hints == ""
        sid = body["id"]
        assert streaming.langs == ["en-US"]
        streaming.passes += [
            _words("hello world"),
            _words("Hello world how"),
            _words("Hello world how are you"),
        ]
        r = _chunk(sid)
        assert r.status_code == 200
        first = r.json()
        assert set(first) == {"committed", "partial", "ms", "window_s", "audio_s"}
        assert first["committed"] == "" and first["partial"] == "hello world"
        assert isinstance(first["ms"], int) and first["audio_s"] == 0.5
        assert first["window_s"] == 0.5
        second = _chunk(sid).json()
        assert (second["committed"], second["partial"]) == ("Hello world", "how")
        assert second["audio_s"] == 1.0
        assert streaming.calls[-1] == ("pass", 16000, "")
        r = client.post("/api/voice/stream/%s/end" % sid)
        assert r.status_code == 200
        assert r.json() == {"committed": "how are you", "partial": ""}
        assert streaming.calls[-1] == ("pass", 16000, "Hello world")
        # Gone afterwards.
        assert _chunk(sid).status_code == 404
        assert client.post("/api/voice/stream/%s/end" % sid).status_code == 404

    def test_a_bad_lang_is_dropped(self, streaming):
        assert (
            client.post("/api/voice/stream", json={"lang": "en US;"}).status_code == 200
        )
        assert client.post("/api/voice/stream").status_code == 200
        assert streaming.langs == ["", ""]

    def test_unknown_id_is_404(self, streaming):
        r = _chunk("nope")
        assert r.status_code == 404
        assert "no such voice stream" in r.json()["error"]
        assert client.post("/api/voice/stream/nope/end").status_code == 404

    def test_not_wav_is_415(self, streaming):
        sid = _open().json()["id"]
        r = _chunk(sid, b"\x1aE\xdf\xa3clip", "audio/webm")
        assert r.status_code == 415
        assert r.json()["error"] == "send the chunk as audio/wav, not audio/webm"
        r = _chunk(sid, b"not a wav at all")
        assert r.status_code == 415 and "as a WAV file" in r.json()["error"]
        r = _chunk(sid, _wav(rate=48000))
        assert r.status_code == 415
        assert r.json()["error"].startswith("send 16 kHz mono 16-bit WAV")
        assert [c for c in streaming.calls if c[0] == "pass"] == []

    def test_over_the_cap_is_413(self, streaming, monkeypatch):
        sid = _open().json()["id"]
        monkeypatch.setattr(routes, "MAX_AUDIO_BYTES", 64)
        r = _chunk(sid)
        assert r.status_code == 413
        assert streaming.calls == []

    def test_a_failed_pass_is_502_and_keeps_the_chunk(self, streaming):
        sid = _open().json()["id"]
        streaming.passes += [speech.SpeechError("faster-whisper failed: boom"), []]
        r = _chunk(sid)
        assert r.status_code == 502
        assert r.json()["error"] == "faster-whisper failed: boom"
        assert _chunk(sid).json()["audio_s"] == 1.0

    def test_a_quiet_chunk_runs_no_pass(self, streaming):
        sid = _open().json()["id"]
        r = _chunk(sid, body=_wav(sample=b"\x00\x00"))
        assert r.status_code == 200
        assert r.json() == {
            "committed": "",
            "partial": "",
            "ms": 0,
            "window_s": 0.0,
            "audio_s": 0.5,
        }
        assert [c for c in streaming.calls if c[0] == "pass"] == []
        streaming.passes.append(_words("hi"))
        body = _chunk(sid).json()
        assert body["partial"] == "hi" and body["window_s"] == 1.0

    def test_end_adds_the_accurate_final(self, streaming):
        heard: list = []

        def final_transcriber(lang="", hints=""):
            heard.append(("open", lang))

            def run(audio):
                heard.append(("final", len(audio)))
                return "Run the tests."

            return run

        streaming.final_transcriber = final_transcriber
        sid = _open("en-GB").json()["id"]
        assert heard == [("open", "en-GB")]
        streaming.passes += [_words("run the tick")] * 3
        _chunk(sid)
        _chunk(sid)
        r = client.post("/api/voice/stream/%s/end" % sid)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "committed",
            "partial",
            "final",
            "final_ms",
            "final_wait_ms",
            "final_early",
            "same",
        }
        assert body["committed"] == "" and body["partial"] == ""
        assert body["final"] == "Run the tests."
        assert body["same"] is False and isinstance(body["final_ms"], int)
        assert body["final_early"] is False  # no quiet chunk came first
        assert isinstance(body["final_wait_ms"], int) and body["final_wait_ms"] >= 0
        assert isinstance(body["final_early"], bool)
        assert body["final_wait_ms"] == body["final_ms"]
        # The whole phrase: both chunks.
        assert heard[-1] == ("final", 16000)

    def test_end_says_same_when_the_final_agrees(self, streaming):
        streaming.final_transcriber = (
            lambda lang="", hints="": lambda a: "Hello, world."
        )
        sid = _open().json()["id"]
        streaming.passes += [_words("hello world")] * 3
        _chunk(sid)
        _chunk(sid)
        body = client.post("/api/voice/stream/%s/end" % sid).json()
        assert body["final"] == "Hello, world." and body["same"] is True

    def test_a_failed_final_leaves_the_fast_body(self, streaming):
        def boom(audio):
            raise speech.SpeechError("faster-whisper failed: boom")

        streaming.final_transcriber = lambda lang="", hints="": boom
        sid = _open().json()["id"]
        streaming.passes += [_words("hello")] * 3
        _chunk(sid)
        _chunk(sid)
        r = client.post("/api/voice/stream/%s/end" % sid)
        assert r.status_code == 200
        assert r.json() == {"committed": "", "partial": ""}

    def test_open_builds_the_hints_from_the_words_and_the_sessions(
        self, streaming, monkeypatch
    ):
        monkeypatch.setattr(speech, "session_titles", lambda: ["sitecheck-bot7", "b"])
        _voice(words={"send": ["over", "send it"], "wake": "hey flock"})
        heard: list = []
        streaming.final_transcriber = lambda lang="", hints="": heard.append(hints)
        r = _open()
        assert streaming.hints == ""  # the fast tier hears bare
        (hints,) = heard
        assert hints.startswith(
            "hey flock, over, send it, over and out, over out, disregard, stop"
        )
        assert "sitecheck bot 7, b, MindFlock" in hints
        assert len(hints) <= speech.HINT_CHARS
        assert r.json()["hints"] == len(hints)

    def test_open_survives_a_hint_builder_failure(self, streaming, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("no words today")

        monkeypatch.setattr(speech, "recognition_hints", boom)
        heard: list = []
        streaming.final_transcriber = lambda lang="", hints="": heard.append(hints)
        r = _open()
        assert r.status_code == 200
        assert r.json()["hints"] == 0 and r.json()["ready"] is True
        assert heard == [""] and streaming.hints == ""

    def test_a_quiet_tail_starts_the_final_early(self, streaming):
        heard: list = []

        def final_transcriber(lang="", hints=""):
            def run(audio):
                heard.append(len(audio))
                return "Over."

            return run

        streaming.final_transcriber = final_transcriber
        sid = _open().json()["id"]
        streaming.passes += [_words("over")] * 2
        _chunk(sid)  # loud: "over" is the tail
        _chunk(sid, body=_wav(0.3, sample=b"\x00\x00"))  # quiet: settles it
        assert heard == [int(0.8 * 16000)]  # the pass started on that chunk
        body = client.post("/api/voice/stream/%s/end" % sid).json()
        assert body["final"] == "Over." and body["final_early"] is True
        assert heard == [int(0.8 * 16000)]  # not run again
        assert [c for c in streaming.calls if c[0] == "pass"][-1][1] == 12800
        # No fast pass at the end either: nothing new to hear.
        assert len([c for c in streaming.calls if c[0] == "pass"]) == 2

    def test_delete_abandons(self, streaming):
        sid = _open().json()["id"]
        r = client.delete("/api/voice/stream/" + sid)
        assert r.status_code == 204
        assert _chunk(sid).status_code == 404
        assert client.delete("/api/voice/stream/" + sid).status_code == 204
        assert [c for c in streaming.calls if c[0] == "pass"] == []

    def test_at_most_four_streams(self, streaming):
        ids = [_open().json()["id"] for _ in range(speech_stream.MAX_STREAMS)]
        r = _open()
        assert r.status_code == 409
        assert r.json()["error"] == "4 voice streams are already open: end one first"
        client.delete("/api/voice/stream/" + ids[0])
        assert _open().status_code == 200

    def test_a_stream_expires_a_minute_after_its_last_chunk(
        self, streaming, monkeypatch
    ):
        clock = [5000.0]
        monkeypatch.setattr(speech_stream, "_now", lambda: clock[0])
        sid = _open().json()["id"]
        streaming.passes += [[], []]
        clock[0] += speech_stream.EXPIRE_SECONDS - 1
        assert _chunk(sid).status_code == 200
        clock[0] += speech_stream.EXPIRE_SECONDS - 1
        assert _chunk(sid).status_code == 200
        clock[0] += speech_stream.EXPIRE_SECONDS
        assert _chunk(sid).status_code == 404
        # An expired stream no longer counts against the cap.
        for _ in range(speech_stream.MAX_STREAMS):
            assert _open().status_code == 200


# --------------------------------------------------------------------------- #
# /api/voice/install-local
# --------------------------------------------------------------------------- #
class TestInstallLocal:
    @pytest.fixture
    def install(self, monkeypatch):
        from backend.web.core import speech_install as si

        box = {"running": False, "starts": 0}
        state = {
            "state": "idle",
            "step": "",
            "log": [],
            "started": None,
            "finished": None,
            "error": "",
        }

        def start():
            if box["running"]:
                return False
            box["running"] = True
            box["starts"] += 1
            state.update(state="running", step="installing packages", started=1.0)
            return True

        monkeypatch.setattr(si, "start", start)
        monkeypatch.setattr(si, "status", lambda: dict(state))
        return SimpleNamespace(box=box, state=state)

    def test_get_is_the_status(self, install):
        r = client.get("/api/voice/install-local")
        assert r.status_code == 200
        assert r.json() == {
            "state": "idle",
            "step": "",
            "log": [],
            "started": None,
            "finished": None,
            "error": "",
        }

    def test_post_starts_it_once(self, install):
        r = client.post("/api/voice/install-local")
        assert r.status_code == 200
        assert r.json()["state"] == "running"
        assert r.json()["step"] == "installing packages"
        r = client.post("/api/voice/install-local")
        assert r.status_code == 409
        assert r.json()["state"] == "running"
        assert r.json()["error"] == "the install is already running"
        assert install.box["starts"] == 1

    def test_post_is_owner_only(self, install, monkeypatch):
        async def _refuse(scope):
            return False

        monkeypatch.setattr(_auth, "may_configure", _refuse)
        r = client.post("/api/voice/install-local")
        assert r.status_code == 403
        assert install.box["starts"] == 0


# --------------------------------------------------------------------------- #
# POST /api/voice/say-all
# --------------------------------------------------------------------------- #
class TestSayAll:
    def test_sets_and_clears_the_marker_and_the_get_reports_it(
        self, fakes, monkeypatch, tmp_path
    ):
        from backend.web import server
        from backend.web.core import voice

        monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "flags"))
        pins = []
        monkeypatch.setattr(server, "repin_voice_hooks", lambda: pins.append(1) or 1)
        assert client.get("/api/voice").json()["say_all"] is False
        r = client.post("/api/voice/say-all", json={"on": True})
        assert r.status_code == 200 and r.json() == {"say_all": True}
        assert voice.say_all() is True and pins == [1]
        assert client.get("/api/voice").json()["say_all"] is True
        r = client.post("/api/voice/say-all", json={"on": False})
        assert r.status_code == 200 and r.json() == {"say_all": False}
        assert voice.say_all() is False and pins == [1]  # off needs no re-pin
        assert client.post("/api/voice/say-all", json={"on": "yes"}).status_code == 400
        assert client.post("/api/voice/say-all", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# The effective voice words the routes judge by
# --------------------------------------------------------------------------- #
class TestWords:
    """``_words``: the effective voice words, ours when the settings can't be
    read — the wake route and the hints still answer."""

    def test_the_persons_words_then_ours_when_unreadable(self, fakes, monkeypatch):
        from backend.web.core import voice_words

        _voice(words={"wake": "Hey Jarvis"})
        assert routes._words()["wake"] == "hey jarvis"
        assert routes._wake_word() == "hey jarvis"

        def boom():
            raise OSError("settings.json unreadable")

        monkeypatch.setattr(routes, "_settings", boom)
        assert routes._words() == voice_words.effective({})
        assert routes._wake_word() == voice_words.DEFAULTS["wake"] == "mindflock"
        assert routes._hints().startswith("mindflock, over, over and out, over out")
        # The wake route judges against ours, not the unreadable person's.
        fakes.stt.text = "Hey, Jarvis!"
        r = client.post(
            "/api/voice/wake?lang=en",
            content=b"RIFFclip",
            headers={"Content-Type": "audio/wav"},
        )
        assert r.status_code == 200 and r.json()["wake"] is False
        fakes.stt.text = "Mind flock."
        body = client.post(
            "/api/voice/wake?lang=en",
            content=b"RIFFclip",
            headers={"Content-Type": "audio/wav"},
        ).json()
        assert body["wake"] is True and body["word"] == "mindflock"
        assert fakes.stt.calls[-1][3:] == ("en", "MindFlock")


# --------------------------------------------------------------------------- #
# ?hint= and POST /api/voice/wake
# --------------------------------------------------------------------------- #
class TestWake:
    def test_the_hint_reaches_the_backend_capped_and_printable(self, fakes):
        assert _transcribe(query="?hint=MindFlock").status_code == 200
        assert fakes.stt.calls[-1][4] == "MindFlock"
        assert _transcribe(query="?hint=" + "x" * 200).status_code == 200
        assert len(fakes.stt.calls[-1][4]) == 80
        assert _transcribe(query="?hint=%01bad").status_code == 200
        assert fakes.stt.calls[-1][4] == ""
        assert _transcribe().status_code == 200
        assert fakes.stt.calls[-1][4] == ""

    def _wake(self, fakes, heard):
        fakes.stt.text = heard
        return client.post(
            "/api/voice/wake?lang=en",
            content=b"RIFFclip",
            headers={"Content-Type": "audio/wav"},
        )

    def test_the_word_alone_wakes_a_sentence_does_not(self, fakes):
        r = self._wake(fakes, "Mind flock.")
        assert r.status_code == 200
        body = r.json()
        assert body["wake"] is True and body["word"] == "mindflock"
        assert body["text"] == "Mind flock." and body["backend"] == "fake"
        # Recognised with the word as the decoder's hint, in the language asked.
        assert fakes.stt.calls[-1][3:] == ("en", "MindFlock")
        assert self._wake(fakes, "MindFlock is great").json()["wake"] is False
        assert self._wake(fakes, "").json()["wake"] is False

    def test_the_persons_wake_word(self, fakes):
        _voice(words={"wake": "Hey Jarvis"})
        body = self._wake(fakes, "Hey, Jarvis!").json()
        assert body["wake"] is True and body["word"] == "hey jarvis"
        assert fakes.stt.calls[-1][3:] == ("en", "Hey Jarvis")
        assert self._wake(fakes, "MindFlock").json()["wake"] is False

    def test_the_wake_check_takes_the_fast_path(self, fakes, monkeypatch):
        def fast(audio, mime, lang="", word=""):
            fakes.stt.calls.append(("wake", audio, mime, lang, word))
            return "MindFlock"

        monkeypatch.setattr(fakes.stt, "wake_transcribe", fast, raising=False)
        body = self._wake(fakes, "unused").json()
        assert body["wake"] is True and isinstance(body["ms"], int)
        assert fakes.stt.calls == [
            ("wake", b"RIFFclip", "audio/wav", "en", "MindFlock")
        ]
        # A phrase sent whole keeps the accurate one-shot path.
        assert _transcribe().status_code == 200
        assert fakes.stt.calls[-1][0] == "transcribe"

    def test_the_clip_rules_are_transcribes(self, fakes):
        r = client.post(
            "/api/voice/wake", content=b"x", headers={"Content-Type": "text/plain"}
        )
        assert r.status_code == 415
        r = client.post(
            "/api/voice/wake", content=b"", headers={"Content-Type": "audio/wav"}
        )
        assert r.status_code == 400
