"""Server speech: ``/api/voice*`` (docs/web-ui.md, "Voice"; docs/web-api.md).

``GET /api/voice`` tells a page where speech runs and what the server can do;
``transcribe`` and ``speak`` are the server-side engines of ``core.speech``
behind HTTP — the built-in ``system`` engine first (Windows / macOS speech,
nothing to install), then ``openai`` and ``local``. Any signed-in client may
call them whenever a backend of that kind is ready: the desktop app's mic
always does (Electron has no working Web Speech recognition), the phone page
does when ``voice.stt`` / ``voice.tts`` is ``"server"`` — those two settings
only say which engine the PHONE prefers, they no longer gate these routes.
Pages record ``audio/wav`` (the one format every backend, the built-in
recognizer included, accepts). Only the owner clears a saved key. The engine
calls run in a worker thread (``asyncio.to_thread``) — a local model, a
PowerShell round trip or a slow host never stalls the event loop.

Status codes, in the order they are checked:

* ``transcribe`` — 415 body is not ``audio/*``; 413 over
  :data:`MAX_AUDIO_BYTES`; 400 empty; 409 no recognition backend is ready
  (``{"error"}`` is :func:`core.speech.select`'s reason); 502 the backend
  failed (the built-in recognizer refuses a non-WAV clip this way); 200
  ``{"text", "backend", "ms"}``.
* ``speak`` — 400 no text; 413 over :data:`MAX_SPEAK_CHARS`; 409 / 502 as
  above; 200 the audio bytes (``audio/mpeg`` or ``audio/wav``) with
  ``X-Voice-Backend``.
* ``stream`` (open) — 409 no recognition backend is ready, the ready one
  does not stream (only ``local`` does: the client sends one clip per phrase
  instead), or :data:`speech_stream.MAX_STREAMS` are open; 200
  ``{"id", "ready": true}``.
* ``stream/{id}/chunk`` — 404 unknown or expired id; 415 not ``audio/wav``,
  or a WAV that is not 16 kHz mono 16-bit; 413 over
  :data:`MAX_AUDIO_BYTES`; 502 the pass failed (the chunk is kept); 200
  ``{"committed", "partial", "ms", "audio_s"}``.
* ``stream/{id}/end`` — 404 as above; 502; 200 ``{"committed", "partial":
  ""}`` and the stream is gone. ``DELETE stream/{id}`` — 204, always.
* ``install-local`` — ``POST`` 403 not the owner; 409 already running; 200
  the status (``core.speech_install``). ``GET`` the status, for anyone.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from backend.web.core import auth as _auth
from backend.web.core import speech as _speech
from backend.web.core import speech_install as _install
from backend.web.core import speech_stream as _stream

router = APIRouter()

#: The raw clip cap (413 above). A minute of webm/opus is well under 1 MB;
#: 10 MB leaves room for an uncompressed WAV of the same length.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
#: The ``/speak`` text cap (413 above): a Say line is ≤ 320 chars and a
#: dialog read-out a few hundred, so this is generous without being a job.
MAX_SPEAK_CHARS = 1200
#: The ``Content-Type`` values a stream chunk may carry.
_WAV_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})
#: ``?lang=`` as a BCP-47-ish tag (``en``, ``pt-BR``); anything else is dropped.
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


def _settings():
    from backend.config import settings as settings_store

    return settings_store.load_settings()


def _refusal(kind: str) -> Tuple[Optional[_speech.Backend], Optional[JSONResponse]]:
    """The backend a ``kind`` request may use, or the 409 that says why not
    (no backend of that kind is ready). ``voice.stt`` / ``voice.tts`` are
    not consulted: they are the phone page's preference, not a gate."""
    backend, detail = _speech.select(kind)
    if backend is None:
        return None, JSONResponse({"error": detail}, status_code=409)
    return backend, None


def _too_big() -> JSONResponse:
    return JSONResponse(
        {"error": "the clip is over %d MB" % (MAX_AUDIO_BYTES // (1024 * 1024))},
        status_code=413,
    )


async def _capped_body(request: Request) -> Optional[bytes]:
    """The raw body, or ``None`` once it passes :data:`MAX_AUDIO_BYTES`.
    Read in chunks and stopped at the cap: a chunked upload carries no
    Content-Length, and ``request.body()`` would hold all of it first."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_AUDIO_BYTES:
        return None
    chunks: list = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_AUDIO_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _failed(err: BaseException) -> JSONResponse:
    if isinstance(err, _speech.SpeechError):
        return JSONResponse({"error": str(err)}, status_code=502)
    return JSONResponse(
        {
            "error": (
                "%s: %s" % (type(err).__name__, err) if str(err) else type(err).__name__
            )
        },
        status_code=502,
    )


@router.get("/api/voice")
def voice_get(allowed: bool = Depends(_auth.configure_allowed)) -> JSONResponse:
    """Where speech runs and what the server offers::

        {"stt": "phone"|"server", "tts": "phone"|"server",
         "approve_dialogs": bool, "agent_words": bool, "ask_popup": bool,
         "backends": {"stt": [ids], "tts": [ids]},
         "server_ready": {"stt": bool, "tts": bool, "detail": str, …},
         "system": {"available": bool, "platform": str, "detail": str,
                    "voices": [names], "recognizers": [culture tags],
                    "stt_quality": "basic"|"good"|"none"},
         "configure_allowed": bool}

    Readable by any signed-in client (the phone page and the desktop app
    read it on load) — no owner check. ``configure_allowed`` only REPORTS
    whether this caller may use the owner-only routes (``install-local``,
    ``clear-key``), so the page can disable those buttons. ``server_ready``
    and ``system`` are :func:`core.speech.describe`'s offline verdict on the
    saved fields (no network, no model load; the built-in engine is probed
    once per process)."""
    s = _settings()
    body = {
        "stt": s.voice.stt,
        "tts": s.voice.tts,
        "approve_dialogs": bool(s.voice.approve_dialogs),
        "agent_words": bool(s.notifications.ntfy_agent_words),
        "ask_popup": bool(s.notifications.ask_popup),
        "configure_allowed": bool(allowed),
    }
    body.update(_speech.describe())
    # A voice client is here: have the local model resident before its first
    # press (a no-op unless voice.stt_backend is local and the model is out).
    _speech.warm_in_background("stt")
    return JSONResponse(body)


@router.post("/api/voice/transcribe")
async def voice_transcribe(request: Request, lang: str = "") -> JSONResponse:
    """Recognise one recorded clip. The RAW audio is the body — not multipart,
    so no form parser is needed — and ``Content-Type`` names its format
    (``audio/wav`` from every page today; ``audio/webm``, ``audio/mp4``,
    ``audio/ogg``, ``audio/mpeg`` for the other backends; a ``;codecs=``
    suffix is fine). ``?lang=en`` is a hint for the backend (the built-in
    recognizer maps it to an installed culture, ``en`` → ``en-US``). → ``{"text", "backend", "ms"}``; see the module doc for
    the status codes."""
    mime = _speech.mime_of(request.headers.get("content-type", ""))
    if not mime.startswith("audio/"):
        return JSONResponse(
            {
                "error": "send the clip as its audio type (audio/webm, audio/mp4, "
                "audio/wav, audio/ogg or audio/mpeg), not %s" % (mime or "nothing")
            },
            status_code=415,
        )
    audio = await _capped_body(request)
    if audio is None:
        return _too_big()
    if not audio:
        return JSONResponse({"error": "the clip is empty"}, status_code=400)
    backend, refused = _refusal("stt")
    if refused is not None or backend is None:
        return refused or _failed(RuntimeError("no backend"))
    hint = lang.strip()
    hint = hint if _LANG.match(hint) else ""
    started = time.monotonic()
    try:
        text = await asyncio.to_thread(backend.transcribe, audio, mime, hint)
    except Exception as err:  # noqa: BLE001 — every failure is the backend's
        return _failed(err)
    return JSONResponse(
        {
            "text": text,
            "backend": backend.id,
            "ms": int((time.monotonic() - started) * 1000),
        }
    )


@router.post("/api/voice/stream")
def voice_stream_open(payload: Optional[dict] = None) -> JSONResponse:
    """Open a streaming recognition (``core.speech_stream``):
    ``{"lang": "en-US"}`` → ``{"id", "ready": true}``. Only a backend that
    streams (``local``) can; ``GET /api/voice``'s ``server_ready.stt_stream``
    says so beforehand. See the module doc for the status codes."""
    body = payload if isinstance(payload, dict) else {}
    lang = body.get("lang")
    lang = lang.strip() if isinstance(lang, str) else ""
    lang = lang if _LANG.match(lang) else ""
    backend, refused = _refusal("stt")
    if refused is not None or backend is None:
        return refused or _failed(RuntimeError("no backend"))
    if not backend.streams:
        return JSONResponse(
            {
                "error": "%s recognition does not stream: send one clip per phrase"
                % backend.id
            },
            status_code=409,
        )
    sid = _stream.open_stream(backend.stream_transcriber(lang))
    if sid is None:
        return JSONResponse(
            {
                "error": "%d voice streams are already open: end one first"
                % _stream.MAX_STREAMS
            },
            status_code=409,
        )
    return JSONResponse({"id": sid, "ready": True})


def _no_stream() -> JSONResponse:
    return JSONResponse(
        {"error": "no such voice stream (it ended or expired): open another"},
        status_code=404,
    )


@router.post("/api/voice/stream/{sid}/chunk")
async def voice_stream_chunk(sid: str, request: Request) -> JSONResponse:
    """Feed one ``audio/wav`` chunk (16 kHz mono 16-bit PCM, the raw body)
    → ``{"committed": newly committed words, "partial": the unstable tail,
    "ms": inference ms, "audio_s": seconds held}``. Send the next chunk only
    after this one returned. See the module doc for the status codes."""
    stream = _stream.get_stream(sid)
    if stream is None:
        return _no_stream()
    mime = _speech.mime_of(request.headers.get("content-type", ""))
    if mime not in _WAV_TYPES:
        return JSONResponse(
            {"error": "send the chunk as audio/wav, not %s" % (mime or "nothing")},
            status_code=415,
        )
    audio = await _capped_body(request)
    if audio is None:
        return _too_big()
    try:
        samples = _stream.decode_wav(audio)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=415)
    try:
        committed, partial = await asyncio.to_thread(stream.feed, samples)
    except Exception as err:  # noqa: BLE001 — every failure is the backend's
        return _failed(err)
    return JSONResponse(
        {
            "committed": committed,
            "partial": partial,
            "ms": stream.last_ms,
            "audio_s": round(stream.held_seconds, 2),
        }
    )


@router.post("/api/voice/stream/{sid}/end")
async def voice_stream_end(sid: str) -> JSONResponse:
    """Finish a stream: one last pass, all of it committed →
    ``{"committed": the words not committed before, "partial": ""}``. The
    stream is gone afterwards (404 for a second ``end``)."""
    stream = _stream.close_stream(sid)
    if stream is None:
        return _no_stream()
    try:
        rest = await asyncio.to_thread(stream.finish)
    except Exception as err:  # noqa: BLE001
        return _failed(err)
    return JSONResponse({"committed": rest, "partial": ""})


@router.delete("/api/voice/stream/{sid}")
def voice_stream_abandon(sid: str) -> Response:
    """Abandon a stream without a last pass → 204 (unknown ids too)."""
    _stream.close_stream(sid)
    return Response(status_code=204)


@router.post("/api/voice/speak")
async def voice_speak(payload: Optional[dict] = None) -> Response:
    """Say ``{"text", "voice"?}`` → the audio bytes, ``Content-Type`` the
    backend's (``audio/mpeg`` for a host, ``audio/wav`` for piper and the
    built-in engine) and ``X-Voice-Backend`` its id. ``voice`` overrides ``voice.tts_voice`` for
    this one call. See the module doc for the status codes."""
    body = payload if isinstance(payload, dict) else {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"error": "text must be a non-empty string"}, status_code=400
        )
    if len(text) > MAX_SPEAK_CHARS:
        return JSONResponse(
            {
                "error": "text is %d characters; the limit is %d"
                % (len(text), MAX_SPEAK_CHARS)
            },
            status_code=413,
        )
    voice = body.get("voice")
    voice = voice.strip()[:64] if isinstance(voice, str) else ""
    backend, refused = _refusal("tts")
    if refused is not None or backend is None:
        return refused or _failed(RuntimeError("no backend"))
    try:
        audio, mime = await asyncio.to_thread(backend.speak, text.strip(), voice)
    except Exception as err:  # noqa: BLE001
        return _failed(err)
    return Response(
        content=audio,
        media_type=mime or "application/octet-stream",
        headers={"X-Voice-Backend": backend.id, "Cache-Control": "no-store"},
    )


@router.post("/api/voice/clear-key")
def voice_clear_key(
    payload: Optional[dict] = None, allowed: bool = Depends(_auth.configure_allowed)
) -> JSONResponse:
    """Forget a saved API key: ``{"kind": "stt"|"tts"}`` → ``{"ok": true}``.

    ``POST /api/settings`` keeps a secret whenever its field arrives blank
    (the app-wide convention, so a form resubmit never wipes a token), which
    leaves no way back to *no key* — this is it. Owner only (403 for a caller
    :func:`core.auth.may_configure` refuses); 400 for any other ``kind``."""
    if not allowed:
        return _auth.configure_refused()
    kind = str((payload or {}).get("kind") or "").strip().lower()
    if kind not in ("stt", "tts"):
        return JSONResponse({"error": 'kind must be "stt" or "tts"'}, status_code=400)
    from backend.config import settings as settings_store

    try:
        settings_store.update_settings(voice={kind + "_api_key": ""})
    except Exception as err:  # noqa: BLE001 — an unreadable settings file
        return JSONResponse({"error": str(err)}, status_code=500)
    return JSONResponse({"ok": True, "kind": kind})


@router.get("/api/voice/install-local")
def voice_install_local_status() -> JSONResponse:
    """The one-click local-models install (``core.speech_install``)::

        {"state": "idle"|"running"|"done"|"failed",
         "step": "installing packages"|"downloading recognition model (small)"
                 |"downloading voice (en_US-lessac-medium)"|"done"|"",
         "log": [the last 20 output lines], "started": epoch|null,
         "finished": epoch|null, "error": str}

    Readable by any signed-in client (the Settings screen polls it)."""
    return JSONResponse(_install.status())


@router.post("/api/voice/install-local")
def voice_install_local(
    allowed: bool = Depends(_auth.configure_allowed),
) -> JSONResponse:
    """Start the one-click install of the local models: faster-whisper and
    piper into the server's own interpreter, then the recognition model and
    the voice; on success ``voice.stt_backend`` becomes ``"local"``. Runs
    in the background — poll ``GET`` for the progress. Owner only (403);
    409 while one is already running; 200 the status (``state:
    "running"``)."""
    if not allowed:
        return _auth.configure_refused()
    if not _install.start():
        body = _install.status()
        body["error"] = body.get("error") or "the install is already running"
        return JSONResponse(body, status_code=409)
    return JSONResponse(_install.status())
