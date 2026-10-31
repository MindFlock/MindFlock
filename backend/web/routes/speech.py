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
PowerShell round trip or a slow host never stalls the event loop; the
recognition calls (a clip, a wake check, a stream's chunk and end) on a
pool of their own (:data:`STT_THREADS`), so a model that stays busy can
never take the threads the rest of the server runs on.

Status codes, in the order they are checked:

* ``transcribe`` — 415 body is not ``audio/*``; 413 over
  :data:`MAX_AUDIO_BYTES`; 400 empty; 409 no recognition backend is ready
  (``{"error"}`` is :func:`core.speech.select`'s reason); 502 the backend
  failed (the built-in recognizer refuses a non-WAV clip this way); 200
  ``{"text", "backend", "ms"}``.
* ``speak`` — 400 no text (or nothing left once shaped); 413 over
  :data:`MAX_SPEAK_CHARS`; 409 / 502 as above; 200 the audio bytes
  (``audio/mpeg`` or ``audio/wav``) with ``X-Voice-Backend`` (the id of the
  engine that actually spoke), ``X-Voice-Engine`` (its short name:
  ``piper``, ``openai``, ``system``), ``X-Voice-Fallback`` (``1`` when the
  configured voice was down or a reply that began on the fallback was
  still going: ``core.speech.TtsChain``), ``X-Voice-Cache`` (``hit`` |
  ``miss``: :data:`SPEAK_CACHE`), ``X-Voice-Ms`` (the server's time for
  the line) and ``X-Voice-Shared: 1`` when an identical request already in
  flight was joined (:func:`_speak_once`). A configured host that refuses,
  times out (``core.speech.host_budget``) or answers 5xx falls back to
  piper for that line and the next :data:`core.speech.HOST_DOWN_SECONDS`;
  the 502 comes only when no engine could say it. The text is
  shaped for the voice first (``core.spoken_text``) unless ``shape`` is
  false; the caller's text — the caption — is never changed.
* ``stream`` (open) — 409 no recognition backend is ready, the ready one
  does not stream (only ``local`` does: the client sends one clip per phrase
  instead), or :data:`speech_stream.MAX_STREAMS` are open; 200
  ``{"id", "ready": true, "hints": chars}``. A client opening more than
  :data:`speech_stream.CLIENT_STREAMS` replaces its own oldest stream
  rather than counting against the cap: the client is ``{"client": id}``
  when the page sends one (a page that may hold two voice streams at once
  should), else its address and user agent (:func:`_client_key`). The new stream's accurate pass
  is biased towards the voice words, the live session titles and the
  MindFlock vocabulary (:func:`core.speech.recognition_hints`, built here
  per open so a new session's title is heard right away); its fast tier
  hears bare (see ``core.speech.HINT_CHARS``).
* ``stream/{id}/chunk`` — 404 unknown or expired id; 415 not ``audio/wav``,
  or a WAV that is not 16 kHz mono 16-bit; 413 over
  :data:`MAX_AUDIO_BYTES`; 502 the pass failed (the chunk is kept); 200
  ``{"committed", "partial", "ms", "window_s", "audio_s"}``.
* ``stream/{id}/end`` — 404 as above; 502 the last fast pass failed; 200
  ``{"committed", "partial": ""}`` plus ``{"final", "final_ms",
  "final_wait_ms", "final_early", "same"}`` when the accurate pass ran
  (``core.speech_stream``'s two tiers), and the stream is gone.
  ``DELETE stream/{id}`` — 204, always.
* ``install-local`` — ``POST`` 403 not the owner; 409 already running; 200
  the status (``core.speech_install``). ``GET`` the status, for anyone.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from backend.web.core import auth as _auth
from backend.web.core import speech as _speech
from backend.web.core import speech_install as _install
from backend.web.core import speech_stream as _stream
from backend.web.core import spoken_text as _spoken_text
from backend.web.core import voice as _voice
from backend.web.core import voice_words as _voice_words
from backend.web.core import wake as _wake

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
#: The recognition calls' own threads (see the module doc). Every wait in
#: them is bounded (``speech_stream.LANE_WAIT_S``), so a full pool drains.
STT_THREADS = 8
_STT_POOL: Optional[ThreadPoolExecutor] = None


async def _stt(fn, *args):
    """``fn(*args)`` on the recognition pool (made on first use)."""
    global _STT_POOL
    if _STT_POOL is None:
        _STT_POOL = ThreadPoolExecutor(STT_THREADS, thread_name_prefix="voice-stt")
    return await asyncio.get_running_loop().run_in_executor(
        _STT_POOL, lambda: fn(*args)
    )


def _client_key(request: Request, body: dict) -> str:
    """Who opens a stream (``speech_stream.open_stream``'s ``client``): the
    page's own ``client`` id when it sends one (≤ 64 chars), else a digest
    of its address and user agent."""
    own = body.get("client")
    if isinstance(own, str) and own.strip():
        return "id:" + own.strip()[:64]
    host = request.client.host if request.client else ""
    agent = request.headers.get("user-agent", "")
    return "ua:" + hashlib.sha256(("%s|%s" % (host, agent)).encode()).hexdigest()[:16]


def _settings():
    from backend.config import settings as settings_store

    return settings_store.load_settings()


def _pronounce(settings) -> list:
    """The person's pronunciations (``voice.pronounce``), normalised
    (``core.spoken_text.normalize_pronounce``); ``[]`` on a settings model
    that does not carry the field."""
    try:
        return _spoken_text.normalize_pronounce(
            getattr(settings.voice, "pronounce", None)
        )
    except Exception:  # noqa: BLE001 — the voice must still speak
        return []


class SpeakCache:
    """The speak cache: ``(voice, shaped text)`` → ``(audio, mime)`` for one
    engine signature (backend id, host, model, voice setting) — a repeat
    ("Sent.", "Listening.", a session's name) costs no engine call, which on
    a neural host is a second or more. LRU in :attr:`max_entries` entries
    and :attr:`max_bytes` in all; a clip over :attr:`max_entry_bytes` is
    served but not kept; a new signature (the backend, its host, its voice
    changed in Settings) drops everything."""

    max_entries = 64
    max_bytes = 8 * 1024 * 1024
    max_entry_bytes = 2 * 1024 * 1024

    def __init__(self) -> None:
        self._items: "OrderedDict[tuple, Tuple[bytes, str]]" = OrderedDict()
        self._sig: Optional[tuple] = None
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._sig = None
        self.bytes = 0

    def _sync(self, sig: tuple) -> None:
        if sig != self._sig:
            self.clear()
            self._sig = sig

    def get(self, sig: tuple, key: tuple) -> Optional[Tuple[bytes, str]]:
        self._sync(sig)
        got = self._items.get(key)
        if got is not None:
            self._items.move_to_end(key)
        return got

    def put(self, sig: tuple, key: tuple, audio: bytes, mime: str) -> None:
        self._sync(sig)
        if len(audio) > self.max_entry_bytes:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self.bytes -= len(old[0])
        self._items[key] = (audio, mime)
        self.bytes += len(audio)
        while self._items and (
            len(self._items) > self.max_entries or self.bytes > self.max_bytes
        ):
            _, (gone, _mime) = self._items.popitem(last=False)
            self.bytes -= len(gone)


#: The one speak cache (``POST /api/voice/speak``, ``X-Voice-Cache``).
SPEAK_CACHE = SpeakCache()


def _engine_signature(backend: _speech.Backend, settings) -> tuple:
    """What a cached clip depends on besides its text and per-call voice:
    the backend object itself (a rebuilt registry is a new engine), its
    host, model and voice settings."""
    v = settings.voice
    return (backend.id, id(backend), v.tts_url, v.tts_model, v.tts_voice)


def _words() -> dict:
    """The effective voice words (``core.voice_words``); ours when the
    settings can't be read."""
    try:
        return _voice_words.effective(_settings())
    except Exception:  # noqa: BLE001 — the voice routes must still answer
        return _voice_words.effective({})


def _wake_word() -> str:
    """The effective wake word (``core.voice_words``)."""
    return _words()["wake"]


def _hints() -> str:
    """The accurate pass's hints for a stream opened now
    (:func:`core.speech.recognition_hints`: the voice words, the live
    sessions' titles newest first, the fixed vocabulary); ``""`` when even
    that fails (the stream still works). The fast tier gets none: any
    prompt beyond the committed text triples its pass time
    (``core.speech.HINT_CHARS``)."""
    try:
        return _speech.recognition_hints(_words(), _speech.session_titles())
    except Exception:  # noqa: BLE001 — a hint, never a failure
        return ""


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


async def _capped_body(request: Request) -> "bytes | Response":
    """The raw body — or the response that ends the request: 413 once it
    passes :data:`MAX_AUDIO_BYTES`, 400 when the client hung up mid-upload
    (an aborted chunk or clip: Starlette's ``ClientDisconnect``, which
    escaped as a 500 with a traceback in the log; nobody reads the answer).
    Read in chunks and stopped at the cap: a chunked upload carries no
    Content-Length, and ``request.body()`` would hold all of it first."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_AUDIO_BYTES:
        return _too_big()
    chunks: list = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_AUDIO_BYTES:
                return _too_big()
            chunks.append(chunk)
    except ClientDisconnect:
        return JSONResponse(
            {"error": "the client hung up before the audio arrived"},
            status_code=400,
        )
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
         "words": {"send": [phrases], "end": […], "clear": […],
                   "stop": […], "hush": […], "wake": str},
         "words_default": {… the same shape: ours …},
         "backends": {"stt": [ids], "tts": [ids]},
         "server_ready": {"stt": bool, "tts": bool, "detail": str, …},
         "system": {"available": bool, "platform": str, "detail": str,
                    "voices": [names], "recognizers": [culture tags],
                    "stt_quality": "basic"|"good"|"none"},
         "final": {"samples": int, "typical_ms": int, "p95_ms": int,
                   "expect_ms": int, "timeout_ms": int},
         "configure_allowed": bool}

    Readable by any signed-in client (the phone page and the desktop app
    read it on load) — no owner check. ``configure_allowed`` only REPORTS
    whether this caller may use the owner-only routes (``install-local``,
    ``clear-key``), so the page can disable those buttons. ``server_ready``
    and ``system`` are :func:`core.speech.describe`'s offline verdict on the
    saved fields (no network, no model load; the built-in engine is probed
    once per process). ``words`` is :func:`core.voice_words.effective` —
    every kind present, the person's own where they changed it — and every
    client builds its matchers from it (``voice_words.phrase_regex``'s
    contract); ``words_default`` is ours, for Settings' "reset". ``final``
    is :func:`core.speech_stream.final_stats`: how long ``/end`` has been
    waiting for the accurate pass lately (``typical_ms`` the median,
    ``p95_ms``; 0 before the first), what to budget (``expect_ms``: the
    median plus max(250 ms, twice the median absolute deviation), or the
    bench's figure before enough phrases, held to [300 ms, the timeout] —
    not the p95, which sits at the timeout once one wait in twenty hits
    it) and the
    server's own cap (``timeout_ms``); ``server_ready.final_ms`` repeats
    ``expect_ms`` — the clients seed their wait for ``/end`` from it, not
    from a fixed worst case."""
    # An engine rebuild that dropped the local models' packages: put them
    # back (once per boot; nothing when they are there or never were).
    _install.start_self_heal()
    s = _settings()
    body = {
        "stt": s.voice.stt,
        "tts": s.voice.tts,
        "approve_dialogs": bool(s.voice.approve_dialogs),
        "agent_words": bool(s.notifications.ntfy_agent_words),
        "ask_popup": bool(s.notifications.ask_popup),
        "say_all": _voice.say_all(),
        "words": _voice_words.effective(s),
        "words_default": copy.deepcopy(_voice_words.DEFAULTS),
        "pronounce": _pronounce(s),
        "final": _stream.final_stats(),
        "configure_allowed": bool(allowed),
    }
    body.update(_speech.describe())
    # The phone seeds its wait for /end's accurate pass from this.
    body["server_ready"]["final_ms"] = body["final"]["expect_ms"]
    # A voice client is here: have the recognition models resident and the
    # voice warm before its first use (no-ops unless there is something to
    # load: see core.speech.warm_in_background).
    _speech.warm_in_background("stt")
    _speech.warm_in_background("tts")
    return JSONResponse(body)


@router.post("/api/voice/transcribe")
async def voice_transcribe(
    request: Request, lang: str = "", hint: str = ""
) -> JSONResponse:
    """Recognise one recorded clip. The RAW audio is the body — not multipart,
    so no form parser is needed — and ``Content-Type`` names its format
    (``audio/wav`` from every page today; ``audio/webm``, ``audio/mp4``,
    ``audio/ogg``, ``audio/mpeg`` for the other backends; a ``;codecs=``
    suffix is fine). ``?lang=en`` is a hint for the backend (the built-in
    recognizer maps it to an installed culture, ``en`` → ``en-US``);
    ``?hint=<words>`` (≤ 80 printable characters) names words the clip
    likely contains, for a backend that can bias its decoder (local
    faster-whisper's ``initial_prompt``; the others ignore it). →
    ``{"text", "backend", "ms"}``; see the module doc for the status codes."""
    got = await _clip(request)
    if not isinstance(got, tuple):
        return got
    audio, mime, backend = got
    started = time.monotonic()
    try:
        text = await _stt(
            backend.transcribe, audio, mime, _lang_hint(lang), _words_hint(hint)
        )
    except Exception as err:  # noqa: BLE001 — every failure is the backend's
        return _failed(err)
    return JSONResponse(
        {
            "text": text,
            "backend": backend.id,
            "ms": int((time.monotonic() - started) * 1000),
        }
    )


_HINT_MAX = 80


def _lang_hint(lang: str) -> str:
    hint = (lang or "").strip()
    return hint if _LANG.match(hint) else ""


def _words_hint(hint: str) -> str:
    """``?hint=``: printable, trimmed, capped — or nothing."""
    words = " ".join(str(hint or "").split())
    if not words or not words.isprintable():
        return ""
    return words[:_HINT_MAX]


async def _clip(request: Request):
    """The raw audio body + its mime + the ready recognizer, or the error
    response (415 not audio, 413 too big, 400 empty, 409/503 no backend)."""
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
    if isinstance(audio, Response):
        return audio
    if not audio:
        return JSONResponse({"error": "the clip is empty"}, status_code=400)
    # select() → ready() may touch files or PATH: never on the event loop.
    backend, refused = await asyncio.to_thread(_refusal, "stt")
    if refused is not None or backend is None:
        return refused or _failed(RuntimeError("no backend"))
    return audio, mime, backend


@router.post("/api/voice/wake")
async def voice_wake(request: Request, lang: str = "") -> JSONResponse:
    """Is this short clip the wake word on its own — ``voice.words``'s
    ``wake`` (``core.voice_words``; "MindFlock" by default)? The body
    and ``Content-Type`` are exactly ``/transcribe``'s; the clip is
    recognised with the word as the decoder's hint — on the fast streaming
    model where the backend has one (``Backend.wake_transcribe``) — and
    judged by
    ``core.wake.is_wake`` (the word alone, one or two tokens, two letters of
    slack for how whisper hears a made-up word; a sentence never wakes). →
    ``{"wake": bool, "text", "word", "backend", "ms"}``; the same status
    codes as ``/transcribe``. The clients send only a short lone utterance
    bounded by silence (their standby), so the model sees a second of audio
    at a time, never the room."""
    got = await _clip(request)
    if not isinstance(got, tuple):
        return got
    audio, mime, backend = got
    word = _wake_word()
    started = time.monotonic()
    try:
        text = await _stt(
            backend.wake_transcribe, audio, mime, _lang_hint(lang), _wake.hint(word)
        )
    except Exception as err:  # noqa: BLE001
        return _failed(err)
    return JSONResponse(
        {
            "wake": _wake.is_wake(text, word),
            "text": text,
            "word": word,
            "backend": backend.id,
            "ms": int((time.monotonic() - started) * 1000),
        }
    )


@router.post("/api/voice/stream")
def voice_stream_open(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """Open a streaming recognition (``core.speech_stream``):
    ``{"lang": "en-US", "client": optional page id}`` → ``{"id", "ready":
    true, "hints": n}`` (``n``
    characters of vocabulary hints the accurate pass is biased towards: the
    voice words, the live session titles, the MindFlock vocabulary — see
    :func:`_hints`). Only a backend that streams (``local``) can; ``GET
    /api/voice``'s ``server_ready.stt_stream`` says so beforehand. See the
    module doc for the status codes."""
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
    hints = _hints()
    sid = _stream.open_stream(
        backend.stream_transcriber(lang),
        backend.final_transcriber(lang, hints),
        client=_client_key(request, body),
    )
    if sid is None:
        return JSONResponse(
            {
                "error": "%d voice streams are already open: end one first"
                % _stream.MAX_STREAMS
            },
            status_code=409,
        )
    return JSONResponse({"id": sid, "ready": True, "hints": len(hints)})


def _no_stream() -> JSONResponse:
    return JSONResponse(
        {"error": "no such voice stream (it ended or expired): open another"},
        status_code=404,
    )


@router.post("/api/voice/stream/{sid}/chunk")
async def voice_stream_chunk(sid: str, request: Request) -> JSONResponse:
    """Feed one ``audio/wav`` chunk (16 kHz mono 16-bit PCM, the raw body)
    → ``{"committed": newly committed words, "partial": the unstable tail,
    "ms": inference ms, "window_s": seconds the pass decoded, "audio_s":
    seconds held}`` — ``ms`` and ``window_s`` are 0 when a quiet chunk ran
    no pass (``core.speech_stream``). Send the next chunk only
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
    if isinstance(audio, Response):
        return audio
    try:
        samples = _stream.decode_wav(audio)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=415)
    try:
        committed, partial = await _stt(stream.feed, samples)
    except Exception as err:  # noqa: BLE001 — every failure is the backend's
        return _failed(err)
    return JSONResponse(
        {
            "committed": committed,
            "partial": partial,
            "ms": stream.last_ms,
            "window_s": round(stream.last_window_s, 2),
            "audio_s": round(stream.held_seconds, 2),
        }
    )


@router.post("/api/voice/stream/{sid}/end")
async def voice_stream_end(sid: str) -> JSONResponse:
    """Finish a stream: one last fast pass (skipped when it could hear
    nothing new), all of it committed → ``{"committed": the words not
    committed before, "partial": ""}``; then the accurate pass over the
    whole phrase — usually started already, on the quiet chunk that ended
    the speech — adds ``"final"`` (the text to replace the typed phrase
    with), ``"final_ms"`` (the pass's time), ``"final_wait_ms"`` (how long
    this call waited for it), ``"final_early"`` and ``"same"`` (``final``
    only differs in case, spacing or punctuation) — absent when it was off,
    skipped, failed or timed out (``core.speech_stream``). The stream is
    gone afterwards (404 for a second ``end``)."""
    stream = _stream.close_stream(sid)
    if stream is None:
        return _no_stream()
    try:
        body = await _stt(stream.finish)
    except Exception as err:  # noqa: BLE001
        return _failed(err)
    return JSONResponse(body)


@router.delete("/api/voice/stream/{sid}")
def voice_stream_abandon(sid: str) -> Response:
    """Abandon a stream without a last pass → 204 (unknown ids too)."""
    _stream.close_stream(sid)
    return Response(status_code=204)


@router.post("/api/voice/speak")
async def voice_speak(payload: Optional[dict] = None) -> Response:
    """Say ``{"text", "voice"?, "shape"?}`` → the audio bytes, ``Content-Type``
    the backend's (``audio/mpeg`` for a host, ``audio/wav`` for piper and the
    built-in engine), ``X-Voice-Backend`` its id and ``X-Voice-Cache``
    ``hit`` or ``miss``. ``voice`` overrides ``voice.tts_voice`` for this one
    call. The text is shaped for the voice first
    (:func:`core.spoken_text.shape`: the person's ``voice.pronounce``, then
    markup, paths, numbers, identifiers, the tech words and the heteronyms
    by context — "goes live" is said as the adjective) — the caller's own
    text, what a caption shows, is never changed; ``shape: false`` sends it
    as written (Settings' "Try" hears a respelling itself). A repeat of the
    same shaped text in the same voice is served from :data:`SPEAK_CACHE`
    without an engine call. See the module doc for the status codes."""
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
    started = time.monotonic()
    # Settings, readiness and shaping read files and PATH: off the loop.
    plan = await asyncio.to_thread(_speak_plan, text, body.get("shape") is not False)
    if isinstance(plan, Response):
        return plan
    primary, settings, spoken, engine = plan
    key = (voice, spoken)
    hit = (
        SPEAK_CACHE.get(_engine_signature(engine, settings), key)
        if engine is not None
        else None
    )
    shared = False
    if hit is not None and engine is not None:
        audio, mime = hit
        _speech.TTS_CHAIN.note_line(spoken)
    else:
        try:
            (audio, mime, engine, _fell_back), shared = await _speak_once(
                (_engine_signature(primary, settings), voice, spoken),
                primary,
                spoken,
                voice,
            )
        except Exception as err:  # noqa: BLE001
            return _failed(err)
        if not shared:
            SPEAK_CACHE.put(_engine_signature(engine, settings), key, audio, mime)
    fell_back = engine is not primary
    headers = {
        "X-Voice-Backend": engine.id,
        "X-Voice-Engine": engine.engine_name(),
        "X-Voice-Fallback": "1" if fell_back else "0",
        "X-Voice-Cache": "hit" if hit is not None else "miss",
        "X-Voice-Ms": str(int((time.monotonic() - started) * 1000)),
        "Cache-Control": "no-store",
    }
    if shared:
        headers["X-Voice-Shared"] = "1"
    return Response(
        content=audio,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


def _speak_plan(text: str, shape: bool):
    """``/speak``'s blocking half: the configured voice (or the 409), the
    settings, the shaped text (or the 400) and the engine
    :data:`core.speech.TTS_CHAIN` would speak it with (None: down with no
    fallback) → a tuple, or the error :class:`Response`."""
    primary, refused = _refusal("tts")
    if refused is not None or primary is None:
        return refused or _failed(RuntimeError("no backend"))
    settings = _settings()
    if shape:
        spoken = _spoken_text.shape(text, _pronounce(settings))
    else:
        spoken = text.strip()
    if not spoken:
        return JSONResponse({"error": "nothing to say once shaped"}, status_code=400)
    engine, _fell_back, _why = _speech.TTS_CHAIN.pick(primary)
    return primary, settings, spoken, engine


#: ``/speak`` syntheses in flight: (configured engine's signature, voice,
#: shaped text) → the task. An identical request that arrives meanwhile (the
#: phone and the desktop, a prefetch and the line itself) awaits the same
#: synthesis instead of queueing a second one on a serial engine.
_INFLIGHT: "Dict[tuple, asyncio.Future]" = {}


async def _speak_once(key: tuple, primary, spoken: str, voice: str):
    """``(TTS_CHAIN.speak's result, shared)``: the synthesis for ``key``,
    started here or joined (``shared``). Shielded: a caller that hangs up
    does not cancel the synthesis the others wait for."""
    task = _INFLIGHT.get(key)
    shared = task is not None and not task.done()
    if not shared:
        task = asyncio.ensure_future(
            asyncio.to_thread(_speech.TTS_CHAIN.speak, primary, spoken, voice)
        )
        _INFLIGHT[key] = task

        def _done(t: "asyncio.Future", k: tuple = key) -> None:
            if _INFLIGHT.get(k) is t:
                del _INFLIGHT[k]
            if not t.cancelled():
                t.exception()  # retrieved: no "never retrieved" warning

        task.add_done_callback(_done)
    assert task is not None
    return await asyncio.shield(task), shared


@router.post("/api/voice/say-all")
def voice_say_all(
    payload: Optional[dict] = None, allowed: bool = Depends(_auth.configure_allowed)
) -> JSONResponse:
    """Ask every prompt in every session for its ``Say:`` line, or stop:
    ``{"on": bool}`` → ``{"say_all": bool}`` (the state read back). A client
    whose replies choice is *Everything* turns this on, so a prompt typed
    straight into a terminal — which no per-prompt ``say`` can reach — still
    ends with the spoken line. It is the standing ``<voice dir>/say-all``
    marker the Claude prompt hook reads (``core.voice.set_say_all``), plus
    the instruction typed after a ``/send`` for the other CLIs; turning it on
    re-pins every live Claude session's hooks so one launched by an older
    build learns the marker. Owner only (403); 400 without a boolean
    ``on``; 500 when the marker cannot be written."""
    if not allowed:
        return _auth.configure_refused()
    on = (payload or {}).get("on")
    if not isinstance(on, bool):
        return JSONResponse({"error": 'pass {"on": true|false}'}, status_code=400)
    try:
        state = _voice.set_say_all(on)
    except OSError as err:
        return JSONResponse({"error": str(err)}, status_code=500)
    if state:
        try:
            from backend.web import server as _server

            _server.repin_voice_hooks()
        except Exception:  # noqa: BLE001 — the re-pin is enrichment
            pass
    return JSONResponse({"say_all": state})


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
