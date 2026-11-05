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
  flight was joined (:func:`_speak_once`, or :class:`Prewarm`'s synthesis
  of that line). The session names a switch says and a few fixed lines are
  pre-synthesised while the voice is idle (:class:`Prewarm`, kicked by
  ``GET /api/voice`` and ``speak``), so they are hits. A configured host that refuses,
  times out (``core.speech.host_budget``) or answers 5xx falls back to
  piper for that line and the next :data:`core.speech.HOST_DOWN_SECONDS`;
  one that answers, but slower than its budget (its learned pace), is
  "too slow for now": the lines go to piper at once, without waiting, until
  a probe shows it keeps up again (``core.speech.TtsChain``). Each page
  (:func:`_client_key`) has its own reply there. The 502 comes only when
  no engine could say it. The text is
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
  ``{"committed", "partial", "ms", "window_s", "audio_s", "turn"}`` —
  ``turn`` is ``{"p", "hold_ms", "reason"}``, the end-of-turn verdict for
  the words so far (``core.speech_turn``).
* ``stream/{id}/end`` — 404 as above; 502 the last fast pass failed; 200
  ``{"committed", "partial": ""}`` plus ``{"final", "final_ms",
  "final_wait_ms", "final_early", "same"}`` when the accurate pass ran
  (``core.speech_stream``'s two tiers), and the stream is gone.
  ``DELETE stream/{id}`` — 204, always.
* ``wake`` — ``transcribe``'s codes; 200 ``{"wake", "reason", "text",
  "plain", "word", "backend", "ms"}``. ``GET wake/recent`` — 200, the last
  :data:`WAKE_RECENT_MAX` checks (docs/voice-wake.md).
* ``install-local`` — ``POST`` 403 not the owner; 409 already running; 200
  the status (``core.speech_install``). ``GET`` the status, for anyone.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

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
_logger = logging.getLogger(__name__)

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
    changed in Settings) drops everything.

    Beside the LRU, a small PINNED set (:class:`Prewarm`'s lines: the
    session names a switch speaks, a few fixed lines) for one signature of
    its own, :attr:`max_pinned` / :attr:`max_pinned_bytes`: it never evicts
    a recently spoken line, and the LRU's traffic (a reply on the fallback
    voice included) never drops a pin. A pin is served only for its own
    signature."""

    max_entries = 64
    max_bytes = 8 * 1024 * 1024
    max_entry_bytes = 2 * 1024 * 1024
    max_pinned = 48
    max_pinned_bytes = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._items: "OrderedDict[tuple, Tuple[bytes, str]]" = OrderedDict()
        self._sig: Optional[tuple] = None
        self.bytes = 0
        self._pins: "Dict[tuple, Tuple[bytes, str]]" = {}
        self._psig: Optional[tuple] = None
        self.pinned_bytes = 0
        self._plock = threading.Lock()  # pins are written from the prewarm thread

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._sig = None
        self.bytes = 0
        with self._plock:
            self._pins.clear()
            self._psig = None
            self.pinned_bytes = 0

    def pinned(self, sig: tuple, key: tuple) -> bool:
        """``key`` is pinned for ``sig``."""
        with self._plock:
            return sig == self._psig and key in self._pins

    def pin(self, sig: tuple, key: tuple, audio: bytes, mime: str) -> bool:
        """Keep ``key``'s clip pinned for ``sig`` (a new ``sig`` drops the
        old pins first) → False when it does not fit (too big, or the pinned
        set is full)."""
        with self._plock:
            if sig != self._psig:
                self._pins.clear()
                self._psig = sig
                self.pinned_bytes = 0
            old = self._pins.get(key)
            grow = len(audio) - (len(old[0]) if old else 0)
            if (
                len(audio) > self.max_entry_bytes
                or (old is None and len(self._pins) >= self.max_pinned)
                or self.pinned_bytes + grow > self.max_pinned_bytes
            ):
                return False
            self._pins[key] = (audio, mime)
            self.pinned_bytes += grow
            return True

    def keep_pins(self, sig: tuple, keys) -> None:
        """Drop every pin that is not for ``sig`` or not in ``keys`` (a
        session gone, a line no longer wanted)."""
        with self._plock:
            if sig != self._psig:
                self._pins.clear()
                self._psig = sig
                self.pinned_bytes = 0
                return
            for k in [k for k in self._pins if k not in keys]:
                self.pinned_bytes -= len(self._pins.pop(k)[0])

    def peek(self, sig: tuple, key: tuple) -> Optional[Tuple[bytes, str]]:
        """``key``'s clip in the LRU for ``sig``, without touching its order
        or the signature (the prewarm pins a line spoken already)."""
        return self._items.get(key) if sig == self._sig else None

    def pin_count(self) -> int:
        with self._plock:
            return len(self._pins)

    def _sync(self, sig: tuple) -> None:
        if sig != self._sig:  # the LRU only: pins keep their own signature
            self._items.clear()
            self.bytes = 0
            self._sig = sig

    def get(self, sig: tuple, key: tuple) -> Optional[Tuple[bytes, str]]:
        self._sync(sig)
        got = self._items.get(key)
        if got is not None:
            self._items.move_to_end(key)
            return got
        with self._plock:
            return self._pins.get(key) if sig == self._psig else None

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


# --------------------------------------------------------------------------- #
# Prewarm: the lines we know we will say, synthesised before they are asked
# --------------------------------------------------------------------------- #
#: At most this many session names are pre-synthesised (most recently
#: active first).
PREWARM_MAX_NAMES = 40
#: Fixed short lines the clients speak as they are (mobile.js speakLine /
#: voiceSayThenIdle, voiceDesktop.ts CONFIRM_LINES and announce, the
#: router's fixed ``say``), after the names. "Ready." is the warm's own.
PREWARM_FIXED: Tuple[str, ...] = (
    "No session is open.",
    "There are no sessions.",
    "Answer it on the screen.",
    "Nothing is being asked.",
    "Answered.",
    "Sent.",
    "Over and out.",
    "Cleared.",
)
#: A prewarm line is asked only once the voice has been idle this long
#: (``core.speech.TtsChain.idle_for``: nothing in flight, in a lane or
#: still playing for any listener).
PREWARM_QUIET_S = 3.0
#: Between two prewarm lines: a real line arriving meanwhile finds the lane
#: empty.
PREWARM_GAP_S = 0.25
#: How often a kick (``GET /api/voice``, ``/speak``) may start a run; the
#: run compares what it wants with what is pinned, so an unchanged session
#: list and voice cost no synthesis.
PREWARM_RECHECK_S = 15.0
#: A run waits this long in all for quiet, then stops until the next kick.
PREWARM_WAIT_S = 120.0
#: Poll step while waiting for quiet.
_PREWARM_POLL_S = 0.2


def _session_names() -> List[str]:
    """The names a switch speaks (``core.voice_router.switch_line``'s
    ``_name``: the row's ``name``, else its title; the phone's ``nameOf``
    prefers a peer row's ``display_title``, so that is added too), this
    device's sessions most recently active first, then other devices' and
    peers' rows — no network: the tick's snapshot and the cached rows."""
    rows: list = []
    try:
        from backend.web.core import aliases as _aliases
        from backend.web.core import events as _events

        rows = _events.sessions_snapshot()
        _aliases.overlay(rows)
        rank = {t: i for i, t in enumerate(_speech.session_titles())}
        rows.sort(key=lambda r: rank.get(r.get("title"), len(rank)))
    except Exception:  # noqa: BLE001 — no server here (tests, a bare import)
        rows = []
    try:
        from backend.web.core import remote as _remote

        rows += _remote.merged_instances()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.routes import peer_sessions as _peers

        rows += _peers.peer_rows()
    except Exception:  # noqa: BLE001
        pass
    out: List[str] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("title") or r.get("hidden_by"):
            continue
        for n in (
            r.get("name") or r.get("title"),
            r.get("name") or r.get("display_title") or r.get("title"),
        ):
            n = str(n or "").strip()
            if n and n not in out:
                out.append(n)
    return out


def _end(text: str) -> str:
    """``core.voice_router._end`` (mobile.js ``endSentence``): a full stop
    unless it ends in one — a switch says ``"<name>."``."""
    s = str(text or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


def _prewarmable(engine: _speech.Backend) -> bool:
    """Only an engine whose lines cost nothing but its own time: piper, the
    built-in voice, an OpenAI-compatible host on this machine or network
    (Voice Lab). Never a paid remote API, and nothing else (a test's fake)."""
    if isinstance(engine, _speech.OpenAITTS):
        try:
            return _speech._local_host(engine._cfg()[0])
        except Exception:  # noqa: BLE001
            return False
    if isinstance(engine, _speech.LocalTTS):
        return True
    try:
        from backend.web.core import speech_system as _system

        return isinstance(engine, _system.SystemTTS)
    except Exception:  # noqa: BLE001
        return False


def _hearing() -> bool:
    """A phrase is being heard right now: a recognition stream had a chunk
    in the last :data:`PREWARM_QUIET_S` (its words are about to become a
    switch or a reply — no prewarm line in the host's way then)."""
    try:
        now = _stream._now()
        streams = list(_stream._STREAMS.values())
        return any(now - st.touched < PREWARM_QUIET_S for st in streams)
    except Exception:  # noqa: BLE001 — a gate, never a failure
        return False


def _prewarm_thread(target, name: str) -> None:
    """Run a prewarm on a daemon thread (a seam: tests run it inline)."""
    threading.Thread(target=target, name=name, daemon=True).start()


class Prewarm:
    """Pre-synthesise the lines we know we will say — each live session's
    name as a switch says it (``"hackathon-2."``) and :data:`PREWARM_FIXED`
    — into :data:`SPEAK_CACHE`'s pinned set, so a switch is a cache hit
    instead of a synthesis (Voice Lab F5's ≥1.4 s floor) at the moment it
    is asked for.

    * Low priority: a line is asked only once the voice has been idle for
      :data:`PREWARM_QUIET_S` (``TtsChain.idle_for``) and no phrase is
      being heard (:func:`_hearing`), one line at a time,
      :data:`PREWARM_GAP_S` apart, and in the host's lane as a ``low`` line
      a real line queues ahead of (``TtsChain.send``). A real ``/speak`` for
      a line the prewarm has at the host joins it (:meth:`joining`) instead
      of queueing a second synthesis.
    * Only for the engine that would speak now (the configured voice, or
      the fallback while it is down or too slow), only when that engine is
      free to use (:func:`_prewarmable`), keyed exactly as ``/speak`` keys
      its cache (the engine signature; the shaped text, no per-call voice).
    * Bounded: :data:`PREWARM_MAX_NAMES` names, the pinned set's own size
      (``SpeakCache.max_pinned``), and runs at most every
      :data:`PREWARM_RECHECK_S` from a kick; a run that finds every line
      pinned for the current signature says nothing. A new signature (the
      voice or engine changed, the host fell back) re-pins from scratch; a
      session gone is unpinned.
    * Stops on a failure (a dead host is marked down, as on a line), when
      the chain is reset or the engine is no longer the registry's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._kicked_at = float("-inf")
        self._joins: "Dict[tuple, concurrent.futures.Future]" = {}
        self.said = 0  # lines synthesised, ever (tests, the report)
        self.last: dict = {}  # the last run: {"lines", "said", "done", "why"}

    def reset(self) -> None:
        with self._lock:
            self._running = False
            self._kicked_at = float("-inf")
            self._joins.clear()
            self.said = 0
            self.last = {}

    def kick(self, force: bool = False) -> bool:
        """Start a run in the background unless one is running or the last
        kick was under :data:`PREWARM_RECHECK_S` ago → started. Cheap: the
        plan and every wait are on the run's own thread."""
        now = time.monotonic()
        with self._lock:
            if self._running or (
                not force and now - self._kicked_at < PREWARM_RECHECK_S
            ):
                return False
            self._running, self._kicked_at = True, now
        try:
            _prewarm_thread(self._run, "mindflock-voice-prewarm")
        except Exception:  # noqa: BLE001 — no thread: the next kick
            with self._lock:
                self._running = False
            return False
        return True

    def _run(self) -> None:
        try:
            self.run_once()
        except Exception:  # noqa: BLE001 — a prewarm never raises
            _logger.debug("voice: prewarm failed", exc_info=True)
        finally:
            with self._lock:
                self._running = False

    def plan(self):
        """``(engine, sig, [(key, spoken)…])`` — what to have pinned now — or
        ``(None, None, [])`` when there is nothing to prewarm for."""
        primary, _detail = _speech.select("tts")
        if primary is None:
            return None, None, []
        chain = _speech.TTS_CHAIN
        engine: Optional[_speech.Backend] = primary
        if chain.down_reason(primary) or chain.too_slow(primary):
            engine = chain.fallback_for(primary)
        if engine is None or not _prewarmable(engine):
            return None, None, []
        settings = _settings()
        pronounce = _pronounce(settings)
        wanted: List[Tuple[tuple, str]] = []
        seen = set()
        lines = [_end(n) for n in _session_names()[:PREWARM_MAX_NAMES]]
        for text in lines + list(PREWARM_FIXED):
            spoken = _spoken_text.shape(text, pronounce)
            key = ("", spoken)
            if spoken and key not in seen:
                seen.add(key)
                wanted.append((key, spoken))
        wanted = wanted[: SPEAK_CACHE.max_pinned]
        return engine, _engine_signature(engine, settings), wanted

    def run_once(self, wait: float = PREWARM_WAIT_S) -> int:
        """One pass: pin what is missing, waiting up to ``wait`` s in all
        for quiet → the lines synthesised."""
        engine, sig, wanted = self.plan()
        self.last = {"lines": len(wanted), "said": 0, "done": False, "why": ""}
        if engine is None or sig is None:
            self.last["why"] = "nothing to prewarm for"
            return 0
        SPEAK_CACHE.keep_pins(sig, {k for k, _s in wanted})
        chain = _speech.TTS_CHAIN
        gen = chain.generation
        give_up = time.monotonic() + wait
        said = 0
        for key, spoken in wanted:
            if SPEAK_CACHE.pinned(sig, key):
                continue
            spoken_already = SPEAK_CACHE.peek(sig, key)
            if spoken_already is not None:  # no synthesis: pin that clip
                SPEAK_CACHE.pin(sig, key, *spoken_already)
                continue
            while chain.idle_for() < PREWARM_QUIET_S or _hearing():
                if time.monotonic() >= give_up:
                    self.last["why"] = "never quiet"
                    return said
                time.sleep(_PREWARM_POLL_S)
            if chain.generation != gen or _speech.get("tts", engine.id) is not engine:
                self.last["why"] = "the voice changed"
                return said
            fut: "concurrent.futures.Future" = concurrent.futures.Future()
            jkey = (sig,) + key
            with self._lock:
                self._joins[jkey] = fut
            try:
                audio, mime = chain.prewarm_line(engine, spoken)
            except Exception as err:  # noqa: BLE001
                fut.set_exception(err)
                if isinstance(err, _speech.SpeechError) and err.down:
                    chain.mark_down(engine, str(err))
                self.last["why"] = "%s: %s" % (type(err).__name__, err)
                return said
            finally:
                with self._lock:
                    if self._joins.get(jkey) is fut:
                        del self._joins[jkey]
            if not fut.done():
                fut.set_result((audio, mime, engine))
            SPEAK_CACHE.pin(sig, key, audio, mime)
            said += 1
            self.said += 1
            self.last["said"] = said
            time.sleep(PREWARM_GAP_S)
        self.last["done"] = True
        return said

    def joining(self, sig: tuple, key: tuple) -> "Optional[concurrent.futures.Future]":
        """The prewarm's synthesis of ``key`` for ``sig`` in flight, if any."""
        with self._lock:
            return self._joins.get((sig,) + key)


#: The one prewarm (kicked by ``GET /api/voice`` and ``/speak``).
PREWARM = Prewarm()


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
def voice_get(
    warm: str = "", allowed: bool = Depends(_auth.configure_allowed)
) -> JSONResponse:
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
    from a fixed worst case.

    Fetching it readies the speaking voice (warmed, the session names
    pre-synthesised) — unless ``?warm=0``: a local voice host (Voice Lab:
    gigabytes, stops after 10 idle minutes) must not be woken by someone who
    isn't using voice, and the clients send ``warm=0`` unless something will
    be said soon. It never loads RECOGNITION (local whisper, ~1.1 GB): that
    follows a listening mode — :func:`voice_listen` (the wake-word standby)
    and a phrase's stream opening (:func:`voice_stream_open`);
    ``server_ready.stt_loaded`` says whether it is in memory now."""
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
    # Ready the voice — or, with ?warm=0, only READ: the status never sends
    # the host anything then (no down/too-slow re-check: core.speech
    # TtsChain.down_reason), so a page load never wakes Voice Lab.
    warm_on = str(warm).strip().lower() not in ("0", "false", "no", "off")
    body.update(_speech.describe(probe=warm_on))
    # The phone seeds its wait for /end's accurate pass from this.
    body["server_ready"]["final_ms"] = body["final"]["expect_ms"]
    # Voice in use here: have the voice warm before its first line (a no-op
    # unless there is something to load: core.speech.warm_in_background).
    # Recognition is not warmed by a status read (voice_listen is how).
    if warm_on:
        _speech.warm_in_background("tts")
        # And the lines it will hear first — the session names a switch
        # says — pre-synthesised while the voice is idle (Prewarm; a thread
        # of its own).
        PREWARM.kick()
    return JSONResponse(body)


@router.post("/api/voice/listen")
def voice_listen(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """A page's listening mode — the wake-word standby — is on or off:
    ``{"on": bool, "client": optional page id}`` → ``{"listening": bool,
    "lease_s": n, "stt_loaded": bool}``. ``on`` renews this page's lease for
    ``lease_s`` seconds (``core.speech.LISTEN_LEASE_SECONDS``; the page
    renews it while standby lasts) and starts loading local recognition in
    the background (a no-op when it is loaded); while any page's lease is
    live the models stay resident (``core.speech._resident``), and with none
    they are idle-unloaded like any other model. ``off`` ends this page's
    lease at once. ``listening``: whether any page listens now."""
    body = payload if isinstance(payload, dict) else {}
    on = body.get("on") is not False
    client = _client_key(request, body)
    now = _speech.listen(client, on)
    if on:
        _speech.warm_in_background("stt")
    return JSONResponse(
        {
            "listening": now,
            "lease_s": int(_speech.LISTEN_LEASE_SECONDS),
            "stt_loaded": _speech.stt_loaded(),
        }
    )


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


#: How many wake checks ``GET /api/voice/wake/recent`` keeps (in memory
#: only: what the standby heard, so the owner's own voice can tune
#: ``core.wake`` — docs/voice-wake.md).
WAKE_RECENT_MAX = 50
_WAKE_RECENT: "deque[dict]" = deque(maxlen=WAKE_RECENT_MAX)
_WAKE_RECENT_LOCK = threading.Lock()


def _record_wake(entry: dict) -> None:
    """Keep one wake check for ``/wake/recent`` and log it (one debug line)."""
    with _WAKE_RECENT_LOCK:
        _WAKE_RECENT.append(entry)
    _logger.debug(
        "voice wake: %s (%s) prompted=%r plain=%r %d ms",
        "yes" if entry["wake"] else "no",
        entry["reason"],
        entry["text_prompted"],
        entry["text_plain"],
        entry["ms"],
    )


@router.post("/api/voice/wake")
async def voice_wake(request: Request, lang: str = "") -> JSONResponse:
    """Is this short clip the wake word on its own — ``voice.words``'s
    ``wake`` (``core.voice_words``; "MindFlock" by default)? The body
    and ``Content-Type`` are exactly ``/transcribe``'s; the clip is
    recognised with the word as the decoder's hint — on the fast streaming
    model where the backend has one (``Backend.wake_transcribe``) — and
    judged by ``core.wake.judge`` (the word alone after a greeting and a
    repeat are dropped; ours strictly, by its known forms and sound shape,
    a custom word with letter slack; a sentence never wakes). A yes for
    ours is checked by a second decode of the same clip WITHOUT the hint
    (``Backend.wake_verify_transcribe``; local faster-whisper only, the
    others skip it) and turned down when that plainly heard another word
    (``core.wake.veto``: the hint makes whisper write "MindFlock" for
    "Mind." and "Mine."); a no never pays for it. → ``{"wake": bool,
    "reason", "text", "plain", "word", "backend", "ms"}`` — ``plain`` the
    un-hinted text, ``null`` when that pass did not run; the same status
    codes as ``/transcribe``. Every check is kept for ``/wake/recent``.
    The clients send only a short lone utterance bounded by silence (their
    standby), so the model sees a second of audio at a time, never the
    room."""
    got = await _clip(request)
    if not isinstance(got, tuple):
        return got
    audio, mime, backend = got
    word = _wake_word()
    language = _lang_hint(lang)
    started = time.monotonic()
    entry = {"ts": time.time(), "text_prompted": "", "text_plain": None}
    try:
        text = await _stt(
            backend.wake_transcribe, audio, mime, language, _wake.hint(word)
        )
    except Exception as err:  # noqa: BLE001
        entry.update(wake=False, reason="error: %s" % type(err).__name__)
        entry["ms"] = int((time.monotonic() - started) * 1000)
        _record_wake(entry)
        return _failed(err)
    wake, reason = _wake.judge(text, word)
    plain: Optional[str] = None
    if wake and _wake.normalize(word) == _wake.WAKE_WORD:
        try:
            plain = await _stt(backend.wake_verify_transcribe, audio, mime, language)
        except Exception as err:  # noqa: BLE001 — the check failed: the yes stands
            _logger.debug("voice wake: the plain pass failed: %s", err)
            plain = None
        if plain is not None:
            vetoed = _wake.veto(plain)
            if vetoed:
                wake, reason = False, vetoed
    ms = int((time.monotonic() - started) * 1000)
    entry.update(text_prompted=text, text_plain=plain, wake=wake, reason=reason, ms=ms)
    _record_wake(entry)
    return JSONResponse(
        {
            "wake": wake,
            "reason": reason,
            "text": text,
            "plain": plain,
            "word": word,
            "backend": backend.id,
            "ms": ms,
        }
    )


@router.get("/api/voice/wake/recent")
def voice_wake_recent() -> JSONResponse:
    """The last :data:`WAKE_RECENT_MAX` wake checks, newest first →
    ``{"attempts": [{"ts", "text_prompted", "text_plain", "wake", "reason",
    "ms"}]}`` (``text_plain`` ``null`` when the un-hinted pass did not
    run). In memory: a restart empties it."""
    with _WAKE_RECENT_LOCK:
        rows = [dict(e) for e in reversed(_WAKE_RECENT)]
    return JSONResponse({"attempts": rows})


@router.post("/api/voice/stream")
def voice_stream_open(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """Open a streaming recognition (``core.speech_stream``):
    ``{"lang": "en-US", "client": optional page id}`` → ``{"id", "ready":
    true, "hints": n, "loading": bool}`` (``loading``: local recognition was
    not in memory and is loading now — the first words wait for it; ``n``
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
    # A phrase is starting: local recognition loads now if it is not in
    # memory (nobody was listening — it is not kept loaded then), in the
    # background while the person speaks; ``loading`` tells the page its
    # first words may take a few seconds ("Starting the mic…").
    loading = not _speech.stt_loaded()
    if loading:
        _speech.warm_in_background("stt")
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
    return JSONResponse(
        {"id": sid, "ready": True, "hints": len(hints), "loading": loading}
    )


def _no_stream() -> JSONResponse:
    return JSONResponse(
        {"error": "no such voice stream (it ended or expired): open another"},
        status_code=404,
    )


def _feed(stream: _stream.Stream, samples) -> tuple:
    """One chunk's pass and its end-of-turn verdict (``Stream.turn`` with
    the person's words; None when that fails — a hint, never a failure),
    together on the recognition pool: the verdict waits on the stream's
    lock, which a pass holds."""
    try:
        words: Optional[dict] = _words()
        stream.words = words
    except Exception:  # noqa: BLE001
        words = None
    got = stream.feed(samples)
    try:
        turn: Optional[dict] = stream.turn(words)
    except Exception:  # noqa: BLE001
        turn = None
    return got, turn


@router.post("/api/voice/stream/{sid}/chunk")
async def voice_stream_chunk(sid: str, request: Request) -> JSONResponse:
    """Feed one ``audio/wav`` chunk (16 kHz mono 16-bit PCM, the raw body)
    → ``{"committed": newly committed words, "partial": the unstable tail,
    "ms": inference ms, "window_s": seconds the pass decoded, "audio_s":
    seconds held, "turn": {"p", "hold_ms", "reason"}}`` — ``ms`` and
    ``window_s`` are 0 when a quiet chunk ran no pass
    (``core.speech_stream``); ``turn`` is the end-of-turn verdict for the
    words so far (``core.speech_turn``: end the phrase once the quiet after
    the last loud frame reaches ``hold_ms``), absent if it could not be
    worked out. Send the next chunk only
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
        (committed, partial), turn = await _stt(_feed, stream, samples)
    except Exception as err:  # noqa: BLE001 — every failure is the backend's
        return _failed(err)
    body = {
        "committed": committed,
        "partial": partial,
        "ms": stream.last_ms,
        "window_s": round(stream.last_window_s, 2),
        "audio_s": round(stream.held_seconds, 2),
    }
    if turn is not None:
        body["turn"] = turn
    return JSONResponse(body)


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
async def voice_speak(request: Request, payload: Optional[dict] = None) -> Response:
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
    without an engine call. The listener (:func:`_client_key`: the page's
    ``client`` id, else its address and user agent) has its own reply in
    :data:`core.speech.TTS_CHAIN` — one page falling back to piper leaves
    another's reply in the configured voice. See the module doc for the
    status codes."""
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
    listener = _client_key(request, body)
    # Settings, readiness and shaping read files and PATH: off the loop.
    plan = await asyncio.to_thread(
        _speak_plan, text, body.get("shape") is not False, listener
    )
    if isinstance(plan, Response):
        return plan
    primary, settings, spoken, engine = plan
    key = (voice, spoken)
    hit = (
        SPEAK_CACHE.get(_engine_signature(engine, settings), key)
        if engine is not None
        else None
    )
    shared = joined = False
    if hit is None and engine is not None:
        # The prewarm has this very line at the host: join it (bounded by
        # the line's budget), never queue a second synthesis behind it.
        sig = _engine_signature(engine, settings)
        pending = PREWARM.joining(sig, key)
        if pending is not None:
            try:
                audio, mime, _by = await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(pending)),
                    _speech.host_budget(spoken),
                )
                shared = joined = True
            except Exception:  # noqa: BLE001 — slow or failed: say it as usual
                pass
    if joined:
        _speech.TTS_CHAIN.note_line(spoken, listener)
    elif hit is not None and engine is not None:
        audio, mime = hit
        _speech.TTS_CHAIN.note_line(spoken, listener)
    else:
        try:
            # Keyed on the engine this listener gets: a page on the fallback
            # never joins another page's synthesis in the configured voice.
            (audio, mime, engine, _fell_back), shared = await _speak_once(
                (_engine_signature(engine or primary, settings), voice, spoken),
                primary,
                spoken,
                voice,
                listener,
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
    PREWARM.kick()  # a new session's name, a changed voice: re-pinned when idle
    return Response(
        content=audio,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


def _speak_plan(text: str, shape: bool, listener: str = ""):
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
    engine, _fell_back, _why = _speech.TTS_CHAIN.pick(primary, listener)
    return primary, settings, spoken, engine


#: ``/speak`` syntheses in flight: (configured engine's signature, voice,
#: shaped text) → the task. An identical request that arrives meanwhile (the
#: phone and the desktop, a prefetch and the line itself) awaits the same
#: synthesis instead of queueing a second one on a serial engine.
_INFLIGHT: "Dict[tuple, asyncio.Future]" = {}


async def _speak_once(key: tuple, primary, spoken: str, voice: str, listener: str = ""):
    """``(TTS_CHAIN.speak's result, shared)``: the synthesis for ``key``,
    started here or joined (``shared``). Shielded: a caller that hangs up
    does not cancel the synthesis the others wait for."""
    task = _INFLIGHT.get(key)
    shared = task is not None and not task.done()
    if not shared:
        task = asyncio.ensure_future(
            asyncio.to_thread(_speech.TTS_CHAIN.speak, primary, spoken, voice, listener)
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
