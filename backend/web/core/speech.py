"""Server-side speech for the voice features (docs/web-ui.md, "Voice"): the
phone page's optional server engines and the desktop app's mic, which
always uses the server (Electron's Chromium has no working Web Speech
recognition). ``routes/speech.py`` turns it into
``POST /api/voice/transcribe`` and ``POST /api/voice/speak``.

Three backend ids, each in a recognition (``stt``) and a voice (``tts``)
flavour, in preference order (:data:`BACKENDS`):

* ``system`` — the operating system's own engines
  (:mod:`backend.web.core.speech_system`): Windows ``System.Speech`` through
  PowerShell (natively or from WSL) and macOS ``say`` + Apple's Speech
  framework. Nothing to install, so it is first: on a fresh Windows or
  macOS install, "the first ready one" is this. Plain Linux has none.
* ``openai`` — any OpenAI-compatible host with a key: OpenAI itself, Groq's
  whisper, a self-hosted compatible server. ``{url}/audio/transcriptions``
  (multipart ``file`` + ``model``) for recognition, ``{url}/audio/speech``
  (JSON ``model``/``input``/``voice``, mp3 back) for the voice. ``ready()``
  touches no network: it only checks that a key is set. The URL defaults to
  :data:`OPENAI_URL`; the model fields are free text because every host has
  its own names (Groq wants ``whisper-large-v3-turbo``).
* ``local`` — faster-whisper (recognition) and piper (voice) on this machine,
  the optional upgrade installed with ``uv sync --group voice`` (the
  ``voice`` dependency group in pyproject.toml). Both are imported lazily
  inside the call, never at module import, so the server starts without
  them; both are refused while :func:`backend.memory_pressure.is_low` (this
  box has been OOM-killed before); a loaded model stays in a module cache
  and is unloaded after :data:`IDLE_SECONDS` without use — a daemon timer
  armed on load sweeps it, and :func:`sweep_idle` is cheap enough for a
  server tick to call too. The exception: while ``voice.stt_backend`` is
  ``local`` its whisper models (one-shot and streaming) stay resident — a
  reload is the slowest thing a press can hit; piper voices still unload.

Files the local backend keeps, under ``~/.mindflock/voice/`` (or
``$MINDFLOCK_VOICE_DIR``):

* ``whisper/`` — faster-whisper's ``download_root``: the CTranslate2 models,
  run as int8 on the CPU. A phrase sent whole uses ``small`` unless
  ``voice.stt_model`` names another size (``base``, ``medium`` …; a name
  that is not a faster-whisper size, like the ``whisper-1`` a host wants,
  reads as ``small``); the live transcript uses the much faster
  ``voice.stt_stream_model`` (:func:`stream_whisper_model`: ``base.en`` for
  English, ``base`` otherwise), and at the phrase's end the accurate pass
  re-decodes the whole phrase with ``stt_model`` again unless
  ``voice.stt_final`` says ``off`` or names another size
  (:func:`final_whisper_model`). Downloaded from Hugging Face on first use,
  or ahead of time by the one-click install (``core.speech_install``, which
  puts the packages in too).
* ``piper/<voice>.onnx`` + ``<voice>.onnx.json`` — piper voices, downloaded
  from Hugging Face on first use (:data:`PIPER_VOICE` unless
  ``voice.tts_voice`` names a piper voice; ~60 MB each).

``local`` recognition is also the one that streams (:attr:`Backend.streams`,
:meth:`LocalSTT.stream_transcriber`): ``core.speech_stream`` re-transcribes a
growing window and commits the words two passes agree on, and
:meth:`LocalSTT.final_transcriber` is the accurate pass over the whole phrase
at its end.

faster-whisper decodes webm/mp4 clips through its bundled PyAV, so a system
``ffmpeg`` is not required; ``ready()`` still says when it is missing because
that is the first thing to try when a clip is refused.

Settings come from ``VoiceSettings`` (``backend.config.settings``), read on
every call so a save in Settings → Voice takes effect at once.
Tests replace :func:`_http` (the one HTTP exchange), :func:`_installed` /
:func:`_import` (the lazy runtimes) and :func:`_now` (the idle clock);
nothing here is reached by the test suite over the network.
``speech_system`` has its own seams (its subprocess runner) and spawns
nothing under pytest.
"""

from __future__ import annotations

import http.client
import importlib
import importlib.util
import inspect
import io
import json
import logging
import re
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from backend import memory_pressure

__all__ = [
    "Backend",
    "SpeechError",
    "BACKENDS",
    "HINT_CHARS",
    "IDLE_SECONDS",
    "backends",
    "choose",
    "describe",
    "get",
    "ids",
    "listen",
    "listening",
    "loaded",
    "mime_of",
    "recognition_hints",
    "select",
    "session_titles",
    "shed_for_memory",
    "speech_form",
    "stt_loaded",
    "sweep_idle",
    "unload_all",
    "voice_dir",
    "warm",
    "warm_in_background",
]

_logger = logging.getLogger(__name__)

#: A cached local model is dropped after this long without a call (never
#: local recognition's while a client listens: :func:`_resident`). Half an
#: hour: a model unloaded between two sentences costs the next press ~7 s
#: (faster-whisper ``small`` loading), which read as "the mic is slow to
#: start"; :func:`warm` loads it ahead of use when a listening mode starts
#: (the standby's :func:`listen`, a phrase's stream opening), and
#: :func:`_memory_block` still refuses a load on a starved box.
IDLE_SECONDS = 1800.0
#: Where ``openai`` goes when ``voice.stt_url`` / ``voice.tts_url`` are blank.
OPENAI_URL = "https://api.openai.com/v1"
OPENAI_STT_MODEL = "gpt-transcribe"
#: ``/audio/speech`` still serves the undated ``gpt-4o-mini-tts`` alias; the
#: dated snapshots and tts-1 retire on 2027-01-06 and their named successor
#: (``gpt-realtime-2.1-mini``) is a realtime model that endpoint does not serve.
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "alloy"
WHISPER_MODEL = "small"
#: The live transcript's model (``voice.stt_stream_model`` unset): ``base``
#: decodes ~5x faster than ``small`` on a CPU at a small accuracy cost, and
#: the English-only build is the better ``base`` for English. Measured
#: against the bigger ones as the fast tier (cycle 6 bench, 10 s pad, 2
#: threads, greedy): small.en 12.3% WER and 38/48 control words at a
#: 387/439 ms pass, distil-small.en 15.9% and 40/48 at 333/368 ms, base.en
#: 14.5–17.8% and 38–39/48 at 176–185/300–324 ms. The bigger two pass slower
#: than the 300 ms chunk cadence (the first word +140–170 ms, and it queues
#: under load), so ``base.en`` stays; ``voice.stt_stream_model`` picks one.
STREAM_MODEL = "base.en"
STREAM_MODEL_MULTI = "base"
PIPER_VOICE = "en_US-lessac-medium"
STT_TIMEOUT = 30.0
TTS_TIMEOUT = 20.0
#: The warm-up line a local OpenAI-compatible voice host is asked to say (its
#: audio is discarded), and how long it may take: a cloned-voice model
#: loading on a CPU takes ~40 s on its first call.
WARM_TEXT = "Ready."
WARM_TIMEOUT = 180.0
#: A host or OS engine (no model cache to look in) is warmed again at most
#: this often by :func:`warm_in_background`, unless its setup changed.
REWARM_SECONDS = 600.0
NOT_INSTALLED = "local models not installed: Settings → Voice installs them"

KIND_WORD = {"stt": "recognition", "tts": "voice"}

_EXT = {
    "audio/webm": ".webm",
    "audio/mp4": ".mp4",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
}


class SpeechError(Exception):
    """A backend could not do the job; ``str(err)`` is a sentence for the UI.

    ``down`` marks a failure of the ENGINE rather than of the request — a
    host that refused the connection or reset it, timed out (hung: see
    :func:`host_hung_after`), answered 5xx or sent no audio — the one kind
    that marks it down in :class:`TtsChain` (a 4xx, a missing key or a bad
    voice name is the request's fault and comes back as it is). A host
    that is only SLOW is never ``down``: :class:`TtsChain` tells the two
    apart."""

    def __init__(self, *args: object, down: bool = False) -> None:
        super().__init__(*args)
        self.down = down


# --------------------------------------------------------------------------- #
# Seams: settings, the clock, HTTP, the lazy runtimes
# --------------------------------------------------------------------------- #
def _voice():
    from backend.config import settings as settings_store

    return settings_store.load_settings().voice


def voice_dir() -> Path:
    """Where the local backend keeps its model files (see the module doc)."""
    from backend.config.home_guard import guard

    raw = os.environ.get("MINDFLOCK_VOICE_DIR") or "~/.mindflock/voice"
    return Path(guard(os.path.expanduser(raw), "voice model dir"))


def _now() -> float:
    return time.monotonic()


def _http(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Tuple[int, str, bytes]:
    """One HTTP exchange → ``(status, content-type, body)``. An HTTP error
    status comes back as a value (its body carries the host's reason); a
    network failure raises ``OSError`` (``URLError``, timeout)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                int(resp.status),
                str(resp.headers.get("Content-Type") or ""),
                resp.read(),
            )
    except urllib.error.HTTPError as err:
        try:
            data = err.read()
        except Exception:  # noqa: BLE001
            data = b""
        ctype = str(err.headers.get("Content-Type") or "") if err.headers else ""
        return int(err.code), ctype, data


def _installed(module: str) -> bool:
    """Whether an optional runtime is importable, without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 — a broken install reads as absent
        return False


def _import(module: str):
    return importlib.import_module(module)


def _memory_block() -> str:
    """Why a local model may not load right now ("" when it may)."""
    try:
        if memory_pressure.is_low():
            return memory_pressure.describe() or "memory is low"
    except Exception:  # noqa: BLE001 — the guard never breaks a request
        return ""
    return ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _base(url: str) -> str:
    return (url or "").strip().rstrip("/") or OPENAI_URL


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def _local_host(url: str) -> bool:
    """Whether ``url`` points at this machine or a private network — a local
    model server (Voice Lab, Kokoro-FastAPI, openedai-speech, a whisper
    server) that speaks the OpenAI shape and wants no key. Loopback,
    ``.local``/``.ts.net`` names, RFC 1918 / link-local / ULA addresses, and
    any plain ``http://`` host (a key over clear text would be the worse
    choice anyway)."""
    try:
        from urllib.parse import urlsplit
        import ipaddress

        parts = urlsplit(url if "://" in url else "http://" + url)
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host in ("localhost", "0.0.0.0") or host.endswith(
            (".local", ".localhost", ".ts.net")
        ):
            return True
        try:
            return (
                ipaddress.ip_address(host).is_private
                or ipaddress.ip_address(host).is_loopback
            )
        except ValueError:
            pass
        return parts.scheme == "http"
    except Exception:  # noqa: BLE001 — an unreadable URL wants a key
        return False


def mime_of(raw: str) -> str:
    """The bare media type of a Content-Type (``audio/webm;codecs=opus`` → ``audio/webm``)."""
    return (raw or "").split(";", 1)[0].strip().lower()


def _ext(mime: str) -> str:
    return _EXT.get(mime_of(mime), ".bin")


def _multipart(
    fields: Dict[str, str], file_field: str, filename: str, mime: str, data: bytes
) -> Tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type (stdlib only)."""
    boundary = "----MindFlock" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(
            (
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, value)
            ).encode("utf-8")
        )
    out.write(
        (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, mime)
        ).encode("utf-8")
    )
    out.write(data)
    out.write(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


def _error_detail(data: bytes) -> str:
    """The host's own reason from an error body (OpenAI's
    ``{"error": {"message"}}``, or the first bytes of whatever it sent)."""
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if isinstance(err, str) and err:
            return err[:200]
        if isinstance(obj, dict) and obj.get("message"):
            return str(obj["message"])[:200]
    except Exception:  # noqa: BLE001
        pass
    return data[:200].decode("utf-8", "replace").strip()


def _temp_clip(audio: bytes, mime: str) -> str:
    fd, path = tempfile.mkstemp(prefix="mindflock-clip-", suffix=_ext(mime))
    with os.fdopen(fd, "wb") as fh:
        fh.write(audio)
    return path


# --------------------------------------------------------------------------- #
# The model cache (local backend)
# --------------------------------------------------------------------------- #
_CACHE: Dict[str, object] = {}
_USED: Dict[str, float] = {}
_LOCK = threading.RLock()
_TIMER: Optional[threading.Timer] = None


def _cached(key: str, loader: Callable[[], object]) -> object:
    """The loaded model under ``key`` (loading it once), stamped as used now."""
    with _LOCK:
        obj = _CACHE.get(key)
        if obj is None:
            obj = loader()
            _CACHE[key] = obj
        _USED[key] = _now()
        _arm_timer()
        return obj


#: How long one "I am listening" from a client (:func:`listen`: the wake-word
#: standby, renewed while it is on) keeps recognition resident. The clients
#: renew well inside it; a page that goes away simply lets it lapse.
LISTEN_LEASE_SECONDS = 180.0
#: client key → ``_now()`` its listening lease runs out (:func:`listen`).
_LISTENING: Dict[str, float] = {}


def listen(client: str, on: bool = True) -> bool:
    """A client's listening mode — the wake-word standby — is on (renewed
    for :data:`LISTEN_LEASE_SECONDS`) or off; returns whether ANY client is
    listening now (:func:`listening`). Only while one is are the ``local``
    recognition models resident (:func:`_resident`); otherwise they load on
    the first phrase and are idle-unloaded like anything else — ~1.1 GB
    that nobody pays for when nobody listens."""
    key = str(client or "")[:64] or "-"
    with _LOCK:
        if on:
            _LISTENING[key] = _now() + LISTEN_LEASE_SECONDS
        else:
            _LISTENING.pop(key, None)
    return listening()


def listening() -> bool:
    """Whether some client's listening lease (:func:`listen`) is live."""
    now = _now()
    with _LOCK:
        for key, until in list(_LISTENING.items()):
            if until <= now:
                _LISTENING.pop(key, None)
        return bool(_LISTENING)


def stt_loaded() -> bool:
    """Whether ``local`` recognition's models are in memory now (``False``
    for any other backend): a first phrase does not wait for a load."""
    try:
        if _configured("stt") != "local":
            return False
        backend = get("stt", "local")
        keys = getattr(backend, "warm_keys", None)
        if keys is None:
            return False
        want = list(keys())
    except Exception:  # noqa: BLE001 — unreadable settings: not loaded
        return False
    with _LOCK:
        return bool(want) and all(k in _CACHE for k in want)


def _resident() -> frozenset:
    """The cache keys :func:`sweep_idle` never drops: the whisper models of
    ``local`` recognition while it is the configured backend AND a client is
    listening (:func:`listening`: the wake-word standby is on somewhere).
    With nobody listening they unload after :data:`IDLE_SECONDS` unused."""
    try:
        if _configured("stt") != "local" or not listening():
            return frozenset()
        backend = get("stt", "local")
        keys = getattr(backend, "resident_keys", None)
        return frozenset(keys()) if keys else frozenset()
    except Exception:  # noqa: BLE001 — an unreadable setting pins nothing
        return frozenset()


def sweep_idle(now: Optional[float] = None) -> List[str]:
    """Drop every cached model idle for :data:`IDLE_SECONDS` or more, except
    the resident ones (:func:`_resident`); returns the keys dropped. Cheap
    when nothing is loaded — a server tick may call it freely; the timer
    armed on load calls it anyway."""
    stamp = _now() if now is None else now
    dropped: List[str] = []
    with _LOCK:
        keep = _resident() if _CACHE else frozenset()
        for key in list(_CACHE):
            if key in keep:
                continue
            if stamp - _USED.get(key, stamp) >= IDLE_SECONDS:
                _CACHE.pop(key, None)
                _USED.pop(key, None)
                dropped.append(key)
    return dropped


def shed_for_memory(min_idle_s: float = 120.0) -> List[str]:
    """Memory reads low (the server's memory watch calls this): drop every
    cached model unused for ``min_idle_s`` — the RESIDENT whisper models too,
    which :func:`sweep_idle` keeps (``small`` + the stream model are ~1 GB of
    the server's memory). One in use right now stays. The warm-up record of
    the dropped kind is forgotten, so the next warm loads again — once
    :func:`_memory_block` lets it. → the keys dropped. Never raises."""
    stamp = _now()
    dropped: List[str] = []
    # A live listening lease (wake-word standby) keeps its models: dropping
    # them would turn the next "MindFlock" into a multi-second reload, and low
    # memory already puts idle agents to sleep first (core.sleep).
    keep = _resident()
    try:
        with _LOCK:
            for key in list(_CACHE):
                if key in keep:
                    continue
                if stamp - _USED.get(key, stamp) >= min_idle_s:
                    _CACHE.pop(key, None)
                    _USED.pop(key, None)
                    dropped.append(key)
            if any(k.startswith("whisper") for k in dropped):
                _WARMED.pop("stt", None)
            if any(not k.startswith("whisper") for k in dropped):
                _WARMED.pop("tts", None)
    except Exception:  # noqa: BLE001 — shedding is best effort
        return dropped
    if dropped:
        import gc

        gc.collect()
        from backend.web.core import malloc_arenas

        malloc_arenas.trim()
    return dropped


def loaded() -> List[str]:
    """The cache keys currently held (``whisper:small``, ``piper:<voice>``)."""
    with _LOCK:
        return sorted(_CACHE)


def unload_all() -> None:
    """Drop every cached model, the warm-up record and the sweep timer
    (tests, shutdown)."""
    global _TIMER
    with _LOCK:
        _CACHE.clear()
        _USED.clear()
        _WARMED.clear()
        _LISTENING.clear()
        if _TIMER is not None:
            _TIMER.cancel()
            _TIMER = None


def _arm_timer() -> None:
    """Make sure a sweep is scheduled while something is loaded: one daemon
    timer at a time, re-armed by its own firing while the cache is non-empty,
    so an idle model is gone within 10–20 minutes of its last call."""
    global _TIMER
    with _LOCK:
        if _TIMER is not None and _TIMER.is_alive():
            return
        timer = threading.Timer(IDLE_SECONDS + 1.0, _timer_fire)
        timer.daemon = True
        timer.start()
        _TIMER = timer


def _timer_fire() -> None:
    global _TIMER
    with _LOCK:
        _TIMER = None
    try:
        sweep_idle()
    finally:
        with _LOCK:
            if _CACHE:
                _arm_timer()


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
def whisper_language(lang: str) -> Optional[str]:
    """The ISO-639 code a Whisper-style recognizer accepts, from whatever the
    page sent: ``navigator.language`` is a BCP-47 tag (``en-US``, ``pt-BR``),
    faster-whisper and the OpenAI-compatible hosts want ``en`` / ``pt`` (and
    reject ``en-US`` outright). The primary subtag, lower-cased, when it is two
    or three letters; ``None`` otherwise (let the model detect)."""
    code = str(lang or "").strip().replace("_", "-").split("-", 1)[0].lower()
    return code if 2 <= len(code) <= 3 and code.isalpha() else None


# --------------------------------------------------------------------------- #
# The recognition hints: the vocabulary both whisper tiers are biased towards
# --------------------------------------------------------------------------- #
#: The hint string's cap, in characters. Whisper's prompt slot takes 224
#: tokens and faster-whisper truncates ``hotwords`` there; 400 characters is
#: ~100 tokens. Only the ACCURATE pass gets hints: its cost is the encoder's,
#: the same with or without (bench: 1.27 s vs 1.28 s a pass on ``small``).
#: The FAST pass cannot afford any — on ``base.en`` 400 characters of
#: ``hotwords`` take a 2 s window from 0.43 s to 1.24 s a pass, and even 120
#: characters of voice words at the front of its prompt trip whisper's
#: temperature fallback often enough to triple the bench's median pass
#: (1386 ms against 467 ms); a ``hotwords`` list on a lone word can loop
#: ("mindflock, mindflock, …"). So the fast tier hears bare, and the
#: accurate pass — which the clients read the radio words off — is where the
#: vocabulary lands (``tests/bench/voice_bench.py``: 46/48 control words
#: and 4.4 % WER against 41/48 and 10.3 % without).
HINT_CHARS = 400
#: How many live session titles the hints carry at most (the most recently
#: active first: those are the ones a person switches to by voice).
HINT_TITLES = 12
#: The fixed MindFlock vocabulary: words whisper's priors spell wrong or
#: hear as something else, after the voice words and the titles.
HINT_VOCABULARY = (
    "MindFlock",
    "session",
    "window",
    "switch",
    "autopilot",
    "commit",
    "push",
    "pull request",
    "ticket",
    "worktree",
    "branch",
    "merge",
    "rebase",
    "diff",
    "Claude",
    "Codex",
    "pytest",
    "fence",
    "orchestrator",
    "inbox",
    "terminal",
    "submit",
    "disregard",
)
_SPLIT_TITLE = re.compile(
    r"[-_/.:\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])"
)


def speech_form(title: str) -> str:
    """A session title as it is said: ``sitecheck-bot7`` → ``sitecheck bot
    7``, ``mapUx`` → ``map Ux``, ``feature/mcp-fences-order`` → ``feature
    mcp fences order`` (split on ``-``, ``_``, ``/``, ``.``, ``:``, spaces,
    camel-case and letter/digit boundaries; ``""`` for an empty title)."""
    parts = [p for p in _SPLIT_TITLE.split(str(title or "")) if p]
    return " ".join(parts)


def session_titles() -> List[str]:
    """The live sessions' titles, the most recently active first — read from
    the server's engine (imported lazily: the server imports this module at
    startup), ``[]`` without one (tests, the bench, a bare import)."""
    try:
        from backend.web import server as _server

        instances = list(getattr(_server, "ENGINE").instances.values())
    except Exception:  # noqa: BLE001 — no engine here
        return []

    def stamp(inst) -> float:
        at = getattr(inst, "UpdatedAt", None) or getattr(inst, "CreatedAt", None)
        try:
            return at.timestamp() if at is not None else 0.0
        except Exception:  # noqa: BLE001 — not a datetime
            return 0.0

    instances.sort(key=stamp, reverse=True)
    out: List[str] = []
    for inst in instances:
        title = str(getattr(inst, "Title", "") or "").strip()
        if title and title not in out:
            out.append(title)
    return out


def recognition_hints(
    words, titles=(), limit: int = HINT_CHARS, vocabulary: bool = True
) -> str:
    """The vocabulary a whisper pass is biased towards, one comma-separated
    string of at most ``limit`` characters: the wake word and every voice
    phrase of every kind (``words``: :func:`voice_words.effective`'s dict —
    these must be heard right, so they come first), then up to
    :data:`HINT_TITLES` session titles in :func:`speech_form` (``titles``,
    newest first, as :func:`session_titles` orders them), then
    :data:`HINT_VOCABULARY` (unless ``vocabulary`` is off). A phrase that
    does not fit is skipped and the shorter ones after it still go in;
    nothing repeats (compared lower-cased); a phrase's own comma is dropped.
    Pure. ``LocalSTT.final_transcriber`` feeds it to faster-whisper as
    ``hotwords``, built fresh on every stream open (``routes/speech.py``)
    since the titles change; the fast tier gets none (:data:`HINT_CHARS`)."""
    wanted: List[str] = []
    words = words if isinstance(words, dict) else {}
    wake = words.get("wake")
    if isinstance(wake, str) and wake.strip():
        wanted.append(wake.strip())
    for kind in ("send", "end", "clear", "stop", "hush"):
        got = words.get(kind)
        for phrase in got if isinstance(got, (list, tuple)) else ():
            if isinstance(phrase, str) and phrase.strip():
                wanted.append(phrase.strip())
    count = 0
    for title in titles or ():
        spoken = speech_form(title)
        if spoken and count < HINT_TITLES:
            wanted.append(spoken)
            count += 1
    if vocabulary:
        wanted.extend(HINT_VOCABULARY)
    out: List[str] = []
    seen: set = set()
    size = 0
    for phrase in wanted:
        # The list is comma-separated, so a phrase's own comma ("over, out",
        # an optional pause) goes: "over out".
        phrase = " ".join(phrase.replace(",", " ").split())
        key = phrase.lower()
        if not phrase or key in seen:
            continue
        add = len(phrase) + (2 if out else 0)
        if size + add > limit:
            continue
        seen.add(key)
        out.append(phrase)
        size += add
    return ", ".join(out)


def _hotwords_supported(model) -> bool:
    """Whether this faster-whisper's ``transcribe`` takes ``hotwords=``
    (1.1+). Looked up once per model class; an unreadable signature reads as
    no, and the hints ride the ``initial_prompt`` instead."""
    cls = type(model)
    cached = _HOTWORDS.get(cls)
    if cached is None:
        try:
            params = inspect.signature(cls.transcribe).parameters
            cached = "hotwords" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            cached = False
        _HOTWORDS[cls] = cached
    return cached


_HOTWORDS: Dict[type, bool] = {}
_TAKES: Dict[Tuple[type, str], bool] = {}


def _takes(model, name: str) -> bool:
    """Whether this faster-whisper's ``transcribe`` takes ``name=`` (looked
    up once per model class; an unreadable signature reads as no)."""
    key = (type(model), name)
    cached = _TAKES.get(key)
    if cached is None:
        try:
            cached = name in inspect.signature(type(model).transcribe).parameters
        except (TypeError, ValueError):
            cached = False
        _TAKES[key] = cached
    return cached


def _bias(model, hints: str, prompt: str, hotwords: bool = True) -> dict:
    """The decoder-biasing arguments of one ``transcribe`` call: ``hotwords``
    for ``hints`` and ``initial_prompt`` for ``prompt`` when ``hotwords`` is
    wanted and the installed faster-whisper has it (they share the prompt
    slot, hints first); otherwise the hints open the prompt (``"<hints>.
    <prompt>"``) — the fast tier's way (see :data:`HINT_CHARS`) and the old
    versions'. Nothing for nothing: an absent key, not ``None``, keeps the
    fakes' and the old versions' calls exact."""
    kw: dict = {}
    if hints and hotwords and _hotwords_supported(model):
        kw["hotwords"] = hints
        if prompt:
            kw["initial_prompt"] = prompt
    elif hints:
        kw["initial_prompt"] = hints + ". " + prompt if prompt else hints
    elif prompt:
        kw["initial_prompt"] = prompt
    return kw


class Backend:
    """One speech engine of one kind. ``ready()`` is cheap and offline; the
    work methods raise :class:`SpeechError` with a sentence for the UI."""

    id: str = ""
    kind: str = ""  # "stt" | "tts"
    label: str = ""

    def ready(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def transcribe(
        self, audio: bytes, mime: str, lang: str = "", prompt: str = ""
    ) -> str:
        """``prompt``: words the clip likely contains (whisper's
        ``initial_prompt``; the wake-word check sends the word). Backends that
        cannot bias the decoder ignore it."""
        raise SpeechError("%s does not do recognition" % self.label)

    def wake_transcribe(
        self, audio: bytes, mime: str, lang: str = "", word: str = ""
    ) -> str:
        """A short clip for the wake-word check (``POST /api/voice/wake``),
        ``word`` as the hint: :meth:`transcribe` unless the backend has a
        faster path for one lone word."""
        return self.transcribe(audio, mime, lang, word)

    def wake_verify_transcribe(
        self, audio: bytes, mime: str, lang: str = ""
    ) -> Optional[str]:
        """The same clip again with NO hint, to check a wake the hinted pass
        found (``core.wake.veto``: the hint makes whisper write the word for
        "Mind." and "Mine."). ``None``: this backend has no such pass and
        the wake stands on the first."""
        return None

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        raise SpeechError("%s does not do voices" % self.label)

    def speak_within(self, text: str, voice: str, budget: float) -> Tuple[bytes, str]:
        """:meth:`speak`, giving up after ``budget`` seconds without an
        answer where the backend can (a host: its socket timeout); the
        others ignore the budget."""
        return self.speak(text, voice)

    def health_key(self) -> str:
        """What :class:`TtsChain` keeps health under: one engine (a host's
        key includes its URL, so a new URL starts healthy)."""
        return self.id

    #: A host that renders one request at a time (Voice Lab's F5):
    #: :class:`TtsChain` sends it ours one at a time, in order (its lane),
    #: so the time a line waits behind our own lines is ours to see rather
    #: than hidden in the host's queue.
    serial: bool = False

    def starts_cold(self) -> bool:
        """Whether this engine may have STOPPED while idle and starts again
        on the next request — a socket-activated host on this machine or
        network (Voice Lab: its proxy exits after 10 idle minutes, then the
        next connection starts it and the first synthesis loads its model).
        :class:`TtsChain` then waits for that first answer
        (:meth:`TtsChain.cold`) instead of giving the line to the fallback
        after its budget or marking the host down."""
        return False

    def engine_name(self) -> str:
        """The short name ``GET /api/voice`` reports for the engine that
        speaks (``server_ready.tts_engine``)."""
        return self.id

    #: Whether :meth:`stream_transcriber` works (``POST /api/voice/stream``).
    streams: bool = False

    def stream_transcriber(self, lang: str = "", hints: str = "") -> Callable:
        """The ``transcribe(audio, prompt)`` a :class:`speech_stream.Stream`
        is built on — only a backend whose :attr:`streams` is true has one.
        ``hints`` is :func:`recognition_hints`: the vocabulary to bias the
        decoder towards, for a backend that can."""
        raise SpeechError("%s does not stream" % self.label)

    def final_transcriber(self, lang: str = "", hints: str = "") -> Optional[Callable]:
        """The ``accurate(audio) -> text`` a stream runs over the whole
        phrase at its end (``core.speech_stream``), or ``None``: no accurate
        pass, the fast words stand. ``hints`` as above."""
        return None


class _OpenAI(Backend):
    id = "openai"
    label = "OpenAI-compatible host"

    def _call(
        self, url: str, headers: Dict[str, str], body: bytes, timeout: float
    ) -> Tuple[str, bytes]:
        """POST and return ``(content-type, body)``; every failure is a
        :class:`SpeechError` naming the host."""
        host = _host(url)
        try:
            status, ctype, data = _http("POST", url, headers, body, timeout)
        except (OSError, http.client.HTTPException) as err:  # refused, reset, timeout
            if isinstance(err, TimeoutError):  # connected, then nothing back
                raise SpeechError(
                    "%s sent nothing back in %.0f s" % (host, timeout), down=True
                ) from err
            reason = getattr(err, "reason", None) or err
            raise SpeechError(
                "could not reach %s: %s" % (host, reason), down=True
            ) from err
        if status >= 400:
            detail = _error_detail(data)
            raise SpeechError(
                "%s answered %d%s" % (host, status, (": " + detail) if detail else ""),
                down=status >= 500,
            )
        return mime_of(ctype), data


class OpenAISTT(_OpenAI):
    kind = "stt"

    def _cfg(self) -> Tuple[str, str, str]:
        v = _voice()
        return _base(v.stt_url), v.stt_api_key, (v.stt_model or OPENAI_STT_MODEL)

    def ready(self) -> Tuple[bool, str]:
        url, key, model = self._cfg()
        if not key and not _local_host(url):
            return False, "%s needs an API key for %s" % (self.label, _host(url))
        return True, "%s at %s" % (model, _host(url))

    def transcribe(
        self, audio: bytes, mime: str, lang: str = "", prompt: str = ""
    ) -> str:
        url, key, model = self._cfg()
        if not key and not _local_host(url):
            raise SpeechError("%s needs an API key for %s" % (self.label, _host(url)))
        fields = {"model": model, "response_format": "json"}
        code = whisper_language(lang)
        if code:
            fields["language"] = code
        body, ctype = _multipart(
            fields, "file", "clip" + _ext(mime), mime_of(mime), audio
        )
        headers = {"Content-Type": ctype}
        if key:
            headers["Authorization"] = "Bearer " + key
        _rtype, data = self._call(
            url + "/audio/transcriptions", headers, body, STT_TIMEOUT
        )
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except ValueError as err:
            raise SpeechError("%s sent something other than JSON" % _host(url)) from err
        text = obj.get("text") if isinstance(obj, dict) else None
        return str(text or "").strip()


class OpenAITTS(_OpenAI):
    kind = "tts"
    serial = True

    def _cfg(self) -> Tuple[str, str, str, str]:
        v = _voice()
        return (
            _base(v.tts_url),
            v.tts_api_key,
            (v.tts_model or OPENAI_TTS_MODEL),
            (v.tts_voice or OPENAI_TTS_VOICE),
        )

    def ready(self) -> Tuple[bool, str]:
        url, key, model, voice = self._cfg()
        if not key and not _local_host(url):
            return False, "%s needs an API key for %s" % (self.label, _host(url))
        return True, "%s (%s) at %s" % (model, voice, _host(url))

    def speak(
        self, text: str, voice: str = "", timeout: float = TTS_TIMEOUT
    ) -> Tuple[bytes, str]:
        url, key, model, default_voice = self._cfg()
        if not key and not _local_host(url):
            raise SpeechError("%s needs an API key for %s" % (self.label, _host(url)))
        payload = {
            "model": model,
            "input": text,
            "voice": voice or default_voice,
            "response_format": "mp3",
        }
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        body = json.dumps(payload).encode("utf-8")
        rtype, data = self._call(url + "/audio/speech", headers, body, timeout)
        if (
            not data
            or rtype.startswith("application/json")
            or rtype.startswith("text/")
        ):
            raise SpeechError("%s sent no audio back" % _host(url), down=True)
        return data, (rtype if rtype.startswith("audio/") else "audio/mpeg")

    def speak_within(self, text: str, voice: str, budget: float) -> Tuple[bytes, str]:
        return self.speak(text, voice, timeout=budget)

    def health_key(self) -> str:
        return "openai " + self._cfg()[0]

    def starts_cold(self) -> bool:
        """A host on this machine or network may be one that stops while
        idle (Voice Lab); a remote API never is."""
        try:
            return _local_host(self._cfg()[0])
        except Exception:  # noqa: BLE001 — unreadable settings
            return False

    def warm(self, prime: bool = True) -> str:  # noqa: ARG002 — no model here
        """Have a LOCAL host load its voice model: one short synthesis
        (:data:`WARM_TEXT`, the audio discarded). Never a remote host — that
        is a paid API, and it has no cold start to hide."""
        url = self._cfg()[0]
        if not _local_host(url):
            return "skipped: %s is not on this machine or network" % _host(url)
        TTS_CHAIN.warm_host(self)  # in the lane, and timed: see TtsChain
        return "loaded"


#: faster-whisper's own size names; anything else in the shared
#: ``voice.stt_model`` (``whisper-1``, a host's model) is not for ``local``.
WHISPER_SIZES = frozenset(
    "tiny tiny.en base base.en small small.en distil-small.en medium medium.en"
    " distil-medium.en large-v1 large-v2 large-v3 large distil-large-v2"
    " distil-large-v3 distil-large-v3.5 large-v3-turbo turbo".split()
)
#: A piper voice name (``en_US-lessac-medium``) — not a Windows / macOS /
#: OpenAI voice from the shared ``voice.tts_voice``.
_PIPER_NAME = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")


def whisper_model(raw: str = "") -> str:
    """The faster-whisper model for ``raw`` (``voice.stt_model``): a size
    name or a Hugging Face ``org/repo`` id, else :data:`WHISPER_MODEL`."""
    name = (raw or "").strip()
    return name if name in WHISPER_SIZES or "/" in name else WHISPER_MODEL


def stream_whisper_model(raw: str = "", lang: str = "") -> str:
    """The live transcript's faster-whisper model for ``raw``
    (``voice.stt_stream_model``) when it is a size or ``org/repo`` id, else
    :data:`STREAM_MODEL` for English (``lang``'s primary subtag ``en``, or
    none) and :data:`STREAM_MODEL_MULTI` for any other language."""
    name = (raw or "").strip()
    if name in WHISPER_SIZES or "/" in name:
        return name
    return (
        STREAM_MODEL if whisper_language(lang) in (None, "en") else STREAM_MODEL_MULTI
    )


#: ``voice.stt_final`` that turns the accurate pass off.
FINAL_OFF = "off"


def final_whisper_model(raw: str = "", stt_model: str = "") -> str:
    """The accurate pass's faster-whisper model for ``raw``
    (``voice.stt_final``): ``""`` for :data:`FINAL_OFF`, ``raw`` when it is
    a size or ``org/repo`` id, else the one-shot model
    (:func:`whisper_model` of ``stt_model``)."""
    name = (raw or "").strip()
    if name.lower() == FINAL_OFF:
        return ""
    if name in WHISPER_SIZES or "/" in name:
        return name
    return whisper_model(stt_model)


def piper_voice(raw: str = "") -> str:
    """The piper voice for ``raw`` (``voice.tts_voice``, or a per-call
    voice), else :data:`PIPER_VOICE`."""
    name = (raw or "").strip()
    return name if _PIPER_NAME.match(name) else PIPER_VOICE


def fetch_whisper_model(name: str) -> str:
    """Download faster-whisper ``name`` into ``voice_dir()/whisper`` — the
    same cache :class:`LocalSTT` loads from (its ``download_root``), so a
    pre-download is what the first phrase finds → the model's folder."""
    root = voice_dir() / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    fw = _import("faster_whisper")
    return str(fw.download_model(name, cache_dir=str(root)))


def fetch_piper_voice(name: str) -> Path:
    """``voice_dir()/piper/<name>.onnx``, downloaded first when missing (the
    one place :class:`LocalTTS` and the installer fetch a voice)."""
    folder = voice_dir() / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    onnx = folder / (name + ".onnx")
    if not onnx.is_file():
        try:
            _import("piper.download_voices").download_voice(name, folder)
        except Exception as err:  # noqa: BLE001 — offline, unknown voice
            raise SpeechError(
                "could not download piper voice %s: %s (by hand: python -m "
                "piper.download_voices %s --data-dir %s)" % (name, err, name, folder)
            ) from err
    return onnx


def invalidate() -> None:
    """Forget what this process believes about the local runtimes — after
    the one-click install put them into the running interpreter's
    environment, so ``ready()`` sees them without a restart."""
    importlib.invalidate_caches()
    forget_ffmpeg()


#: How long a resolved ``ffmpeg`` lookup stands before it is re-checked (in
#: the background; the stale answer is served meanwhile).
FFMPEG_RECHECK_SECONDS = 600.0
#: ``{"path": str ("" = not on PATH), "env": the PATH it was found on,
#: "at": when, "busy": a lookup thread is running}`` — empty before the first.
_FFMPEG: Dict[str, object] = {}
_FFMPEG_LOCK = threading.Lock()


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _spawn(target: Callable[[], None], name: str) -> None:
    """Run ``target`` on a daemon thread (tests run it inline)."""
    threading.Thread(target=target, name=name, daemon=True).start()


def _resolve_ffmpeg() -> None:
    env = os.environ.get("PATH", "")
    try:
        found = _which("ffmpeg") or ""
    except Exception:  # noqa: BLE001 — a hint, never a failure
        found = ""
    with _FFMPEG_LOCK:
        _FFMPEG.update(path=found, env=env, at=_now(), busy=False)


def ffmpeg_path() -> Optional[str]:
    """Where ``ffmpeg`` is: a path, ``""`` (not on PATH) or ``None`` (not
    looked up yet). Never blocks: ``shutil.which`` stats every PATH entry,
    and under WSL most of them are ``/mnt/c`` over 9P — 50–400 ms per call,
    which ``LocalSTT.ready()`` (reached from async routes through
    :func:`select`) used to pay on every wake check. The lookup runs once on
    a daemon thread and is re-run, again in the background, when PATH
    changes, :data:`FFMPEG_RECHECK_SECONDS` later, after
    :func:`forget_ffmpeg` (a refused clip, :func:`invalidate`)."""
    env = os.environ.get("PATH", "")
    with _FFMPEG_LOCK:
        have = "path" in _FFMPEG
        fresh = (
            have
            and _FFMPEG.get("env") == env
            and _now() - float(_FFMPEG.get("at") or 0.0) < FFMPEG_RECHECK_SECONDS
        )
        path = str(_FFMPEG["path"]) if have else None
        if fresh or _FFMPEG.get("busy"):
            return path
        _FFMPEG["busy"] = True
    try:
        _spawn(_resolve_ffmpeg, "mindflock-voice-ffmpeg")
    except Exception:  # noqa: BLE001 — no thread: try again next call
        with _FFMPEG_LOCK:
            _FFMPEG["busy"] = False
    with _FFMPEG_LOCK:  # an inline lookup (tests) has answered already
        return str(_FFMPEG["path"]) if "path" in _FFMPEG else path


def forget_ffmpeg() -> None:
    """Drop the ``ffmpeg`` lookup: the next :func:`ffmpeg_path` re-runs it
    (in the background)."""
    with _FFMPEG_LOCK:
        busy = _FFMPEG.get("busy")
        _FFMPEG.clear()
        if busy:
            _FFMPEG["busy"] = True


def _shim_av_open() -> None:
    """Let faster-whisper 1.2 decode with PyAV 15+.

    faster-whisper's ``decode_audio`` calls ``av.open(path, mode="r",
    metadata_errors="ignore")``; PyAV 15 dropped that argument, so the first
    clip fails with "open() got an unexpected keyword argument
    'metadata_errors'". An older PyAV has no wheel for Python 3.13 (it would
    build from source and fail), so instead ``av.open`` is wrapped ONCE to
    drop the argument. No-op on PyAV < 15, when either module is missing, or
    when already applied."""
    try:
        import av
        from faster_whisper import audio as fw_audio
    except Exception:  # noqa: BLE001 — nothing to shim
        return
    try:
        major = int(str(getattr(av, "__version__", "0")).split(".")[0] or 0)
    except ValueError:
        major = 0
    target = getattr(fw_audio, "av", av)
    orig = getattr(target, "open", None)
    if major < 15 or orig is None or getattr(orig, "_mf_shim", False):
        return

    def open_(*args, metadata_errors=None, **kwargs):  # noqa: ARG001 — dropped
        return orig(*args, **kwargs)

    open_._mf_shim = True  # type: ignore[attr-defined]
    target.open = open_


#: The encoder input a streaming pass pads its window to, in seconds (its
#: audio is never cut: a longer window pads to itself plus
#: :data:`_PAD_TAIL_FRAMES`). faster-whisper always pads to Whisper's 30 s,
#: and that encoder pass was ~60% of a fast pass (241 of 360 ms on 4
#: threads) whatever the window held. Measured on the 48 bench clips: base.en
#: 30 s → 10 s keeps the WER (15.2% → 14.8%) and the word times (within
#: 20 ms) and takes a pass from 374 to 155 ms; at 6 s it starts inventing
#: words. Whisper windows are at most :data:`speech_stream.WINDOW_SECONDS`.
STREAM_PAD_SECONDS = 10.0
#: The same for the accurate pass over a whole phrase — 0: Whisper's own
#: 30 s. Its text is the one sent, and with the vocabulary as ``hotwords``
#: a shorter input costs words: small on the 48 clips, WER 4.8% at 30 s,
#: 5.9% at 20 s (644 vs 1150 ms p50), 8.5% at 10 s — where a low-confidence
#: lone word also set off faster-whisper's temperature fallback for 28 s.
FINAL_PAD_SECONDS = 0.0
#: A fast pass decodes greedily, once: faster-whisper's temperature
#: fallback (5 samples at each of 5 temperatures) takes seconds — never
#: worth it to a pass the next chunk replaces — and its tokens are capped
#: at this many plus :data:`STREAM_TOKENS_PER_S` per second of window (a
#: fast speaker says ~6 a second), so a decoder caught in a loop stops.
STREAM_TOKENS = 20
STREAM_TOKENS_PER_S = 15
#: A fast pass's beam: 1 is greedy. The bench (cycle 6, 48 phrases): beam 2
#: took the pass from 176/300 to 207/403 ms (p50/p95, over the 300 ms chunk
#: cadence) for 16.7% WER against 14.5–17.8%, and heard fewer control words
#: (34 vs 38–39 of 48), ending phrases in junk ("over ever B").
STREAM_BEAM = 1
#: A short vocabulary leading the fast pass's prompt when the caller gives
#: no ``hints`` ("" = none). Keep it empty: even with the temperature
#: fallback off and the tokens capped, the control words alone ("Over. Over
#: and out. Disregard. Stop") as the prompt took the bench's fast tier to
#: 40% WER and 14/48 control words (from 14.5% and 39/48): whisper writes
#: a hint word or a rhyme into the quiet after the speech ("Over. Never",
#: "disregard. regard.") and loops ("YOU YOU YOU …" ×25), and faster-whisper's
#: word alignment raised IndexError on one prompted pass.
STREAM_VOCAB = ""
#: The quiet a window longer than its pad still gets after its audio.
_PAD_TAIL_FRAMES = 200
#: Whisper's own input: 3000 mel frames (30 s).
_FULL_FRAMES = 3000
_PAD = threading.local()
#: ``off``: a short encoder input failed once (an engine that wants 3000
#: frames), so every pass pads to 30 s again; ``ok``: one succeeded.
_PAD_STATE: Dict[str, bool] = {"ok": False, "off": False}


def _pad_frames(content: int) -> int:
    """The frames a pass under :func:`_padded` pads ``content`` mel frames
    to: its pad, or the content plus a little quiet when that is more, in
    whole seconds, at most Whisper's 3000."""
    want = int(getattr(_PAD, "frames", 0) or 0)
    if not want:
        return _FULL_FRAMES
    need = max(want, content + _PAD_TAIL_FRAMES)
    need = -(-need // 100) * 100
    return min(_FULL_FRAMES, need)


def _hook_pad() -> bool:
    """Wrap faster-whisper's ``pad_or_trim`` (in its ``transcribe`` module)
    ONCE so a pass run under :func:`_padded` pads to :func:`_pad_frames`
    instead of 3000; every other call (the one-shot clip, a thread with no
    pad set) is untouched. ``False`` when this faster-whisper has no such
    hook (the passes then pad to 30 s, as before)."""
    try:
        mod = _import("faster_whisper.transcribe")
    except Exception:  # noqa: BLE001 — no faster-whisper: nothing to hook
        return False
    orig = getattr(mod, "pad_or_trim", None)
    if orig is None:
        return False
    if getattr(orig, "_mf_pad", False):
        return True

    def pad_or_trim(array, length: int = _FULL_FRAMES, *, axis: int = -1):
        if length == _FULL_FRAMES and getattr(_PAD, "frames", 0):
            length = _pad_frames(int(array.shape[axis]))
        return orig(array, length, axis=axis)

    pad_or_trim._mf_pad = True  # type: ignore[attr-defined]
    mod.pad_or_trim = pad_or_trim
    return True


def _padded(seconds: float, decode: Callable[[], object]) -> object:
    """``decode()`` (a whole faster-whisper call, segments read) with the
    encoder input padded to ``seconds`` rather than 30 (see
    :data:`STREAM_PAD_SECONDS`). Should the very first such call fail — an
    engine that refuses a short input — short padding is switched off for
    good (logged) and the call runs again at 30 s; after one success an
    error is the call's own and propagates."""
    if _PAD_STATE["off"] or seconds <= 0 or not _hook_pad():
        return decode()
    _PAD.frames = int(seconds * 100)
    try:
        out = decode()
    except Exception as err:  # noqa: BLE001 — retried at full length below
        if _PAD_STATE["ok"]:
            raise
        _PAD.frames = 0
        out = decode()  # fails too: the error was the call's own, raised
        _PAD_STATE["off"] = True
        _logger.warning(
            "faster-whisper refused a %gs encoder input (%s); padding to 30 s",
            seconds,
            err,
        )
        return out
    finally:
        _PAD.frames = 0
    _PAD_STATE["ok"] = True
    return out


def whisper_threads(role: str, cores: Optional[int] = None) -> int:
    """The CPU threads a whisper model decodes with (CTranslate2's
    ``cpu_threads``, fixed when it loads), for ``role`` ``fast`` (the live
    transcript's and wake word's model) or anything else (the accurate
    pass's and the one-shot clip's), out of ``cores`` (``os.cpu_count()``).

    The two tiers can run at the same moment (one job per model at a time:
    ``speech_stream.Lane``), so their budgets add up to the cores rather
    than each taking CTranslate2's default 4 (8 on 8 cores, beside the
    server and the agent CLIs — the early accurate pass then doubled the
    fast passes and missed its own deadline). The fast tier gets a quarter
    (1..4): ``base.en``'s greedy decoder is sequential and gains little from
    more. The accurate pass gets the rest (at most 8): ``small``'s encoder
    is most of its time and scales with threads, and it is the pass a
    client waits on after "over" — and it rarely shares the cores, as no
    fast pass runs once a phrase's accurate pass has started. 8 cores → 2
    and 6; 4 → 1 and 3; 16 → 4 and 8.

    Two threads cost the fast tier ~170 ms a pass while it padded to 30 s
    (the encoder was most of it: 566 vs 422 ms at 2 vs 4 threads on a
    1.5 s window); padded to :data:`STREAM_PAD_SECONDS` the encoder is
    small and the greedy decoder is the rest, so the thread count no longer
    shows (189 vs 199 ms; 4 s windows 252 vs 243 ms, interleaved, load ~5)
    and a second, 4-thread copy of the model for when no final runs would
    buy nothing for its memory."""
    n = cores or os.cpu_count() or 4
    fast = max(1, min(4, n // 4))
    if role == "fast":
        return fast
    return max(1, min(8, n - fast))


class LocalSTT(Backend):
    id = "local"
    kind = "stt"
    label = "faster-whisper on this computer"
    streams = True

    def _model_name(self) -> str:
        return whisper_model(_voice().stt_model)

    def stream_model_name(self, lang: str = "") -> str:
        """The live transcript's model for ``lang`` (:func:`stream_whisper_model`)."""
        return stream_whisper_model(_voice().stt_stream_model, lang)

    def final_model_name(self) -> str:
        """The accurate pass's model (:func:`final_whisper_model`; ``""``:
        off). ``voice.stt_final`` is read with a default, so this works on a
        settings model without the field."""
        v = _voice()
        return final_whisper_model(getattr(v, "stt_final", ""), v.stt_model)

    def warm_keys(self) -> List[str]:
        """What :meth:`warm` loads: the one-shot model, the streaming one
        for English (a stream in another language loads its own on open)
        and the accurate pass's when it is a third."""
        names = {self._model_name(), self.stream_model_name(), self.final_model_name()}
        return sorted("whisper:" + n for n in names if n)

    def resident_keys(self) -> List[str]:
        """Never idle-unloaded while ``local`` recognition is configured: the
        :meth:`warm_keys` and the streaming model of any other language."""
        return sorted(
            set(self.warm_keys()) | {"whisper:" + self.stream_model_name("xx")}
        )

    def ready(self) -> Tuple[bool, str]:
        if not _installed("faster_whisper"):
            return False, NOT_INSTALLED
        low = _memory_block()
        if low:
            return False, "local recognition held back: " + low
        detail = "faster-whisper %s on this computer" % self._model_name()
        if ffmpeg_path() == "":
            detail += (
                " (ffmpeg is not on PATH: webm/mp4 clips decode through the bundled"
                " PyAV; install ffmpeg if a clip is refused)"
            )
        return True, detail

    def _role(self, name: str) -> str:
        """``fast`` when ``name`` is only ever a streaming model (either
        language's), else ``final`` — a model serving both tiers gets the
        accurate pass's thread budget (:func:`whisper_threads`)."""
        try:
            fast = {self.stream_model_name(), self.stream_model_name("xx")}
            slow = {self._model_name(), self.final_model_name()}
        except Exception:  # noqa: BLE001 — unreadable settings: the big budget
            return "final"
        return "fast" if name in fast and name not in slow else "final"

    def _load(self, name: str):
        fw = _import("faster_whisper")
        _shim_av_open()
        root = voice_dir() / "whisper"
        root.mkdir(parents=True, exist_ok=True)
        return fw.WhisperModel(
            name,
            device="cpu",
            compute_type="int8",
            download_root=str(root),
            cpu_threads=whisper_threads(self._role(name)),
            num_workers=1,
        )

    def _model(self, name: str = ""):
        """The cached model ``name`` (default: the one-shot one), after the
        install and memory checks."""
        if not _installed("faster_whisper"):
            raise SpeechError(NOT_INSTALLED)
        low = _memory_block()
        if low:
            raise SpeechError("local recognition held back: " + low)
        name = name or self._model_name()
        try:
            return _cached("whisper:" + name, lambda: self._load(name))
        except SpeechError:
            raise
        except Exception as err:  # noqa: BLE001 — download, CPU feature, …
            raise SpeechError(
                "could not load faster-whisper %s: %s" % (name, err)
            ) from err

    def transcribe(
        self, audio: bytes, mime: str, lang: str = "", prompt: str = ""
    ) -> str:
        kw = {"language": whisper_language(lang), "beam_size": 5}
        if prompt:
            kw["initial_prompt"] = prompt
        name = self._model_name()
        return self._clip(self._model(name), audio, mime, kw, name)

    def wake_transcribe(
        self, audio: bytes, mime: str, lang: str = "", word: str = ""
    ) -> str:
        """The wake-word clip on the FAST streaming model
        (:meth:`stream_model_name`) — the judgement is one lone word, which
        it hears about as well as the one-shot model in a third of the
        time — decoded as ``core.wake.decode_options`` says (greedy, once,
        no VAD, no no-speech filter, a token cap; ``word`` as the prompt)
        on a :data:`core.wake.PAD_SECONDS` encoder input."""
        return self._wake_clip(audio, mime, lang, word)

    def _wake_clip(self, audio: bytes, mime: str, lang: str, word: str) -> str:
        from backend.web.core import wake as _wake

        name = self.stream_model_name(lang)
        model = self._model(name)
        kw = {"language": whisper_language(lang)}
        kw.update(
            (k, v) for k, v in _wake.decode_options(word).items() if _takes(model, k)
        )
        return self._clip(model, audio, mime, kw, name, pad=_wake.PAD_SECONDS)

    def wake_verify_transcribe(
        self, audio: bytes, mime: str, lang: str = ""
    ) -> Optional[str]:
        """:meth:`wake_transcribe` without the prompt (the veto's pass: the
        same fast model, greedy, no VAD — never ``word_timestamps``, which
        sends the prompted decoder into "MindFlock MindFlock" loops)."""
        return self._wake_clip(audio, mime, lang, "")

    @staticmethod
    def _clip(
        model, audio: bytes, mime: str, kw: dict, name: str = "", pad: float = 0.0
    ) -> str:
        """``audio`` (any container PyAV decodes) through ``model`` (named
        ``name``: the call waits its turn on that model's lane, as a
        ``clip`` job — ``speech_stream.Lane``) → its text; the temp clip is
        removed after. ``pad``: the encoder input in seconds
        (:func:`_padded`; 0: Whisper's 30)."""
        from backend.web.core import speech_stream as _stream

        def decode() -> List[str]:
            segments, _info = model.transcribe(path, **kw)
            return [str(getattr(s, "text", "") or "").strip() for s in segments]

        def run() -> List[str]:
            return _padded(pad, decode) if pad else decode()  # type: ignore[return-value]

        path = _temp_clip(audio, mime)
        try:
            words = _stream.lane("whisper:" + (name or "?")).run(run, _stream.CLIP)
        except Exception as err:  # noqa: BLE001
            forget_ffmpeg()  # a refused clip: look for ffmpeg again
            raise SpeechError("faster-whisper failed: %s" % err) from err
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return " ".join(w for w in words if w).strip()

    def stream_transcriber(self, lang: str = "", hints: str = "") -> Callable:
        """One pass of a stream (``core.speech_stream``): the float32 window
        straight into the streaming model (:meth:`stream_model_name`, not
        the one-shot one) with word timestamps, greedy decoding and the
        committed text as the prompt — any ``hints`` lead it (never as
        ``hotwords``; the server sends none to this tier, see
        :data:`HINT_CHARS`; the bench's ``+fasthints`` does) →
        ``[(word, start_s, end_s)]``. The model is fetched per pass, so an
        unload never strands a stream. Its ``lane`` attribute names the
        model's job lane (``whisper:<name>``), which the stream queues each
        pass on (``speech_stream.Lane``)."""
        language = whisper_language(lang)
        name = self.stream_model_name(lang)
        hints = " ".join(str(hints or STREAM_VOCAB).split())

        def run(audio, prompt: str) -> List[Tuple[str, float, float]]:
            model = self._model(name)
            np = _import("numpy")
            # A copy: the stream trims its buffer later, which a view would block.
            samples = np.frombuffer(bytes(audio), dtype=np.float32)
            kw = _bias(model, hints, prompt, hotwords=False)
            if _takes(model, "temperature"):
                kw["temperature"] = 0.0
            if _takes(model, "max_new_tokens"):
                seconds = len(samples) / 16000.0
                kw["max_new_tokens"] = int(
                    STREAM_TOKENS + STREAM_TOKENS_PER_S * seconds
                )

            def decode() -> List[Tuple[str, float, float]]:
                segments, _info = model.transcribe(
                    samples,
                    language=language,
                    beam_size=STREAM_BEAM,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    # Silence in a window makes whisper invent "Thank you.";
                    # two passes would agree on it and commit it. The bundled
                    # VAD drops the quiet stretches before decoding.
                    vad_filter=True,
                    **kw,
                )
                return [
                    (str(w.word), float(w.start), float(w.end))
                    for seg in segments
                    for w in (getattr(seg, "words", None) or ())
                ]

            try:
                return _padded(STREAM_PAD_SECONDS, decode)  # type: ignore[return-value]
            except Exception as err:  # noqa: BLE001
                raise SpeechError("faster-whisper failed: %s" % err) from err

        run.lane = "whisper:" + name  # type: ignore[attr-defined]
        return run

    def final_transcriber(self, lang: str = "", hints: str = "") -> Optional[Callable]:
        """The accurate pass at a stream's end: the whole phrase (float32)
        into :meth:`final_model_name`, greedy, no conditioning, the VAD, and
        ``hints`` (:func:`recognition_hints`) as the ``hotwords`` (the prompt
        on a faster-whisper too old for them: :func:`_hotwords_supported`) —
        the vocabulary, never the fast words (those may be the wrong ones) →
        its text. ``None`` when ``voice.stt_final`` is ``off``. ``lane`` as
        in :meth:`stream_transcriber`."""
        name = self.final_model_name()
        if not name:
            return None
        language = whisper_language(lang)
        hints = " ".join(str(hints or "").split())

        def run(audio) -> str:
            from backend.web.core import speech_stream as _stream

            model = self._model(name)
            np = _import("numpy")
            samples = np.frombuffer(bytes(audio), dtype=np.float32)
            kw = _bias(model, hints, "")

            def decode() -> List[str]:
                segments, _info = model.transcribe(
                    samples,
                    language=language,
                    beam_size=1,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    **kw,
                )
                parts: List[str] = []
                for seg in segments:  # each 30 s window decodes as it is read
                    parts.append(str(getattr(seg, "text", "") or "").strip())
                    if _stream.abandoned():  # its finish gave up: stop here
                        break
                return parts

            try:
                parts = _padded(FINAL_PAD_SECONDS, decode)
            except Exception as err:  # noqa: BLE001
                raise SpeechError("faster-whisper failed: %s" % err) from err
            return " ".join(p for p in parts if p).strip()  # type: ignore[union-attr]

        run.lane = "whisper:" + name  # type: ignore[attr-defined]
        return run

    def warm(self, prime: bool = True) -> str:
        """Load every model (:meth:`warm_keys`) ahead of their first use and,
        when ``prime`` is set, run one short silent pass through each one
        this call loaded (CTranslate2 allocates its workspaces on the first
        call, another second or two). ``"loaded"`` when this call loaded
        any, ``"already"`` when all were resident."""
        fresh = False
        for key in self.warm_keys():
            name = key.split(":", 1)[1]
            was = key in loaded()
            model = self._model(name)
            fresh = fresh or not was
            if prime and not was:
                self._prime(model, key)
        return "loaded" if fresh else "already"

    @staticmethod
    def _prime(model, key: str = "") -> None:
        """One short silent pass, on the model's lane (``key``) as a
        ``clip`` job, so it never shares the cores with a live pass."""
        from backend.web.core import speech_stream as _stream

        np = _import("numpy")

        def run() -> None:
            segments, _info = model.transcribe(
                np.zeros(8000, dtype=np.float32),
                language="en",
                beam_size=1,
                vad_filter=False,
            )
            for _seg in segments:  # the generator does the work
                pass

        try:
            _stream.lane(key or "?").run(run, _stream.CLIP)
        except Exception:  # noqa: BLE001 — a warm-up may fail quietly
            pass


class LocalTTS(Backend):
    id = "local"
    kind = "tts"
    label = "piper on this computer"

    def engine_name(self) -> str:
        return "piper"

    def _voice_name(self, voice: str = "") -> str:
        return piper_voice(voice or _voice().tts_voice)

    def ready(self) -> Tuple[bool, str]:
        if not _installed("piper"):
            return False, NOT_INSTALLED
        low = _memory_block()
        if low:
            return False, "local voice held back: " + low
        name = self._voice_name()
        have = (voice_dir() / "piper" / (name + ".onnx")).is_file()
        return True, "piper voice %s on this computer%s" % (
            name,
            "" if have else " (downloads on first use, ~60 MB)",
        )

    def _load(self, name: str):
        onnx = fetch_piper_voice(name)
        return _import("piper.voice").PiperVoice.load(str(onnx))

    def _model(self, name: str):
        """The cached voice ``name``, after the install and memory checks."""
        if not _installed("piper"):
            raise SpeechError(NOT_INSTALLED)
        low = _memory_block()
        if low:
            raise SpeechError("local voice held back: " + low)
        try:
            return _cached("piper:" + name, lambda: self._load(name))
        except SpeechError:
            raise
        except Exception as err:  # noqa: BLE001
            raise SpeechError(
                "could not load piper voice %s: %s" % (name, err)
            ) from err

    def warm_keys(self) -> List[str]:
        return ["piper:" + self._voice_name()]

    def warm(self, prime: bool = True) -> str:
        """Load the configured voice and, when ``prime`` is set, say
        :data:`WARM_TEXT` into nothing once (onnxruntime's first run is the
        slow one). ``"loaded"`` / ``"already"`` like :meth:`LocalSTT.warm`."""
        name = self._voice_name()
        was = ("piper:" + name) in loaded()
        model = self._model(name)
        if prime and not was:
            try:
                with wave.open(io.BytesIO(), "wb") as wav:
                    model.synthesize_wav(WARM_TEXT, wav)
            except Exception:  # noqa: BLE001 — a warm-up may fail quietly
                pass
        return "already" if was else "loaded"

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        model = self._model(self._voice_name(voice))
        buf = io.BytesIO()
        try:
            with wave.open(buf, "wb") as wav:
                model.synthesize_wav(text, wav)
        except Exception as err:  # noqa: BLE001
            raise SpeechError("piper failed: %s" % err) from err
        return buf.getvalue(), "audio/wav"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
#: Every backend by kind, in preference order (the first ready one wins when
#: no backend is configured). ``speech_system`` puts the built-in ``system``
#: pair first when it is imported, just below. Tests swap in fakes.
BACKENDS: Dict[str, List[Backend]] = {
    "stt": [OpenAISTT(), LocalSTT()],
    "tts": [OpenAITTS(), LocalTTS()],
}

# Imported here, after Backend and BACKENDS exist, because it subclasses one
# and registers into the other (either module may be imported first).
from backend.web.core import speech_system as _system  # noqa: E402


def backends(kind: str) -> List[Backend]:
    return list(BACKENDS.get(kind) or [])


def ids(kind: str) -> List[str]:
    return [b.id for b in backends(kind)]


def get(kind: str, backend_id: str) -> Optional[Backend]:
    for b in backends(kind):
        if b.id == backend_id:
            return b
    return None


def _configured(kind: str) -> str:
    v = _voice()
    return str(v.stt_backend if kind == "stt" else v.tts_backend).strip().lower()


def select(kind: str) -> Tuple[Optional[Backend], str]:
    """The backend a request of ``kind`` should use, with its readiness
    detail — or ``(None, why)``. The configured backend
    (``voice.stt_backend`` / ``voice.tts_backend``) is the only candidate
    when set: its own detail says what is missing. Unset: the first ready
    one, else every backend's reason."""
    want = _configured(kind)
    if want:
        backend = get(kind, want)
        if backend is None:
            return None, "unknown %s backend %r (one of %s)" % (
                KIND_WORD.get(kind, kind),
                want,
                ", ".join(ids(kind)) or "none",
            )
        ok, detail = backend.ready()
        return (backend, detail) if ok else (None, detail)
    reasons: List[str] = []
    for backend in backends(kind):
        ok, detail = backend.ready()
        if ok:
            return backend, detail
        reasons.append(detail)
    return None, "no %s backend is ready (%s)" % (
        KIND_WORD.get(kind, kind),
        "; ".join(reasons) or "none configured",
    )


def choose(kind: str) -> str:
    """The backend id a request of ``kind`` would use: the configured one
    (whether or not it is ready), else the first ready one, else ``""``."""
    want = _configured(kind)
    if want and get(kind, want) is not None:
        return want
    backend, _detail = select(kind)
    return backend.id if backend else ""


# --------------------------------------------------------------------------- #
# The voice chain: the configured engine, then piper, with one voice per reply
# --------------------------------------------------------------------------- #
#: The configured voice's connect / first-byte budget for a one-word line;
#: :func:`host_budget` adds :data:`HOST_BUDGET_PER_CHAR` per character
#: because a neural host (Voice Lab's F5) renders the whole clip before its
#: first byte — about 0.035 s per character on a GPU, so the allowance is
#: ~1.7× that. The budget runs from when the line reaches the host, or from
#: when the live line ahead of it in our lane should be done (time spent
#: behind our own lines is not the host's fault): a line past it is said by
#: the fallback, and the host is NOT marked down for it (:class:`TtsChain`).
HOST_BUDGET = 1.5
HOST_BUDGET_PER_CHAR = 0.06
#: A failed engine is down this long; after it the next line (or
#: ``GET /api/voice``) starts a background probe — the engine is healthy
#: again only once the probe passes.
HOST_DOWN_SECONDS = 45.0
#: A host that :meth:`Backend.starts_cold` and has not answered for this
#: long — or not yet since this server started — may have stopped (Voice
#: Lab's socket proxy exits after 10 idle minutes): its next line is a COLD
#: START (:meth:`TtsChain.cold`), as is every line asked while one is.
COLD_AFTER_SECONDS = 600.0
#: How long a cold start's line may wait for the host's first byte: the
#: host starting (~4 s) and loading its model (Voice Lab's F5 on a CPU:
#: ~40 s). It waits, in the configured voice, instead of going to the
#: fallback after its budget; only past this is the host down.
COLD_START_TIMEOUT = 90.0
#: The probe: :data:`WARM_TEXT` has to come back within this. A model still
#: loading, or running on the wrong device, stays down.
PROBE_TIMEOUT = 3.0
#: The fallback voices, in order, after the configured one (never itself).
FALLBACK_TTS: Tuple[str, ...] = ("local",)
#: :class:`TtsChain`'s switch back: this long after the last fallback line
#: should have finished playing, with nothing in flight.
REPLY_GAP_SECONDS = 2.5
#: How fast a line is spoken, to estimate when it stops playing.
SPOKEN_CHARS_PER_SECOND = 14.0
#: A serial host's learned speed (:class:`TtsChain`): every answer it gives
#: (a line, an orphan, a probe) is a sample of ``took / host_budget(text)``
#: — its "budget ratio" — folded in with this weight for the newest.
SPEED_ALPHA = 0.5
#: A learned ratio over this is TOO SLOW FOR NOW: no line of it would arrive
#: within its budget, so the lines go to the fallback at once instead of
#: each waiting its budget out in silence first. Not down: the host keeps
#: its lane, and one probe answering under this brings it back.
SLOW_RATIO = 1.0
#: While too slow, a probe (:data:`WARM_TEXT`, in the lane) runs at most
#: this often, and only when a line or ``GET /api/voice`` asks.
SLOW_RECHECK_SECONDS = 10.0
#: How many listeners (pages) :class:`TtsChain` keeps a reply for.
MAX_LISTENERS = 32
#: The listener a pre-synthesised line (``routes/speech.py``'s prewarm) is
#: asked for: nobody's reply, so it never extends a real line's budget nor
#: makes a reply sticky.
PREWARM_LISTENER = "~prewarm"


def host_budget(text: str) -> float:
    """The configured voice's budget for ``text`` (:data:`HOST_BUDGET`)."""
    return min(TTS_TIMEOUT, HOST_BUDGET + HOST_BUDGET_PER_CHAR * len(text or ""))


def host_hung_after(text: str) -> float:
    """How long a host may send nothing back for ``text`` before it counts
    as HUNG — dead, like a refused connection: ``max(TTS_TIMEOUT, 2 ×
    host_budget)``. Our lane gives the host the line to itself, so only a
    host that cannot say one sentence in that long gets here (F5 on a CPU:
    50–60 s); a merely slow one answers first."""
    return max(TTS_TIMEOUT, 2.0 * host_budget(text))


class _Slow(SpeechError):
    """A line not back within its budget (:class:`TtsChain`): the host is
    alive and still working on it, so it is not ``down``. ``upfront``: the
    host's learned speed said so before the line was sent (nothing waited)."""

    def __init__(self, *args: object, upfront: bool = False) -> None:
        super().__init__(*args)
        self.upfront = upfront


class _Line:
    """One request of ours in a serial host's lane (:meth:`TtsChain.send`).
    ``deadline`` (``time.monotonic``) is when a waiting LINE gives up on the
    host — None for one that waits for the answer (the probe, a warm, a line
    with no fallback). ``live`` turns false when it gave up: an orphan if
    the host has it (``started``), dropped from the lane if not. ``learn``:
    its answer is a sample of the host's speed (not a warm: a cold load is
    not the host's pace). ``listener``: whose reply it belongs to. ``low``:
    a prewarm line — every real line queues ahead of it until it reaches
    the host (:meth:`TtsChain.send`)."""

    __slots__ = (
        "engine", "text", "voice", "timeout", "deadline", "gen",
        "live", "started", "sent_at", "done", "result", "error",
        "learn", "listener", "took", "low",
    )  # fmt: skip

    def __init__(self, engine: Backend, text: str, voice: str, timeout: float) -> None:
        self.engine, self.text, self.voice, self.timeout = engine, text, voice, timeout
        self.deadline: Optional[float] = None
        self.gen = 0
        self.live, self.started, self.done = True, False, False
        self.sent_at = 0.0
        self.took = 0.0
        self.learn = True
        self.low = False
        self.listener = ""
        self.result: Optional[Tuple[bytes, str]] = None
        self.error: Optional[BaseException] = None


class _Lane:
    __slots__ = ("queue", "busy", "key")

    def __init__(self, key: str = "") -> None:
        self.queue: List[_Line] = []  # in order; the head is at the host
        self.busy = False  # a drain thread is running
        self.key = key  # the host's health key


def _lane_thread(target: Callable[[], None], name: str) -> None:
    """Run a lane's drain on a daemon thread (a seam of its own: tests that
    hold :func:`_spawn`'s probes still get their lines said)."""
    threading.Thread(target=target, name=name, daemon=True).start()


class _Health:
    __slots__ = ("why", "retry_at", "probing")

    def __init__(self, why: str, retry_at: float) -> None:
        self.why = why
        self.retry_at = retry_at
        self.probing = False


class _Speed:
    """A serial host's learned pace (:meth:`TtsChain._learn`): ``ratio`` is
    the EWMA of ``took / host_budget(text)`` over ``samples`` answers, so a
    line is predicted to take ``ratio × host_budget(text)``."""

    __slots__ = ("ratio", "samples", "recheck_at", "probing", "last_s")

    def __init__(self) -> None:
        self.ratio = 0.0
        self.samples = 0
        self.recheck_at = 0.0  # _now(): the next too-slow probe is due
        self.probing = False
        self.last_s = 0.0  # the last answer's seconds at the host

    @property
    def slow(self) -> bool:
        return self.samples > 0 and self.ratio > SLOW_RATIO


class _Reply:
    """One listener's reply in progress (:class:`TtsChain`'s one voice per
    reply): ``sticky`` once a line of it fell back, ``inflight`` lines being
    said, ``quiet_at`` when its last line should have finished playing."""

    __slots__ = ("sticky", "inflight", "quiet_at")

    def __init__(self) -> None:
        self.sticky = False
        self.inflight = 0
        self.quiet_at = 0.0


class TtsChain:
    """Which engine says the next line, and the fallback when it fails.

    * The configured voice (:func:`select`) speaks within
      :func:`host_budget`. DEAD and SLOW are told apart:

      - Dead — a refused or reset connection, a connect timeout, a 5xx, no
        audio, or nothing back for :func:`host_hung_after` (hung): a
        ``down`` :class:`SpeechError`. The host is marked down for
        :data:`HOST_DOWN_SECONDS` and the same line goes to the first ready
        :data:`FALLBACK_TTS` engine (piper) — so a dead host costs one
        refused connect, once, and nothing for the lines after it. With no
        fallback ready the failure stands (the route's 502; the client
        then uses its own voice).
      - Slow — alive, but this line is not back within its budget: the
        line is said by the fallback and the host is NOT marked down. Its
        request stays at the host (an orphan holding the lane until the
        host answers, so the next line queues visibly behind it), and the
        next reply starts on the host again.

    * A :attr:`Backend.serial` host gets our requests one at a time, in
      order (:meth:`send`, one lane per host): F5 renders one line at a
      time anyway, and this way the time a line waits behind OUR lines is
      ours to see instead of eating its budget inside the host's queue. A
      line's budget runs from when the live line ahead of it should be done
      (or from now): ``budget = host_budget(this) + the budgets of the live
      lines ahead``. The lines ahead are being played meanwhile, so the
      owner hears no extra silence; a line behind one we gave up on (an
      orphan, a warm, the probe) gets only its own budget — that wait IS
      silence. A line with no fallback ready waits for the host.
    * Health is per :meth:`Backend.health_key` and re-checked only in the
      background (:meth:`_probe`, on a daemon thread), never on a request.
    * One voice per reply: once a line falls back, every line does until the
      configured engine has passed its probe AND the chain is quiet — no
      line in flight and :data:`REPLY_GAP_SECONDS` past the moment the last
      fallback line should have finished playing (its length at
      :data:`SPOKEN_CHARS_PER_SECOND`). A reply's lines arrive back to back
      (the clients ask for the next sentence while one plays), so the switch
      back lands in the pause between replies, never inside one. Failing
      over is immediate — a line the host cannot say is said by piper —
      and a SLOW line fails over the same way (without the down window):
      the lines of the reply still waiting in the lane leave it for piper
      too. So a reply changes voice at most once, host → piper, and only
      ever between two of its lines.
    * A reply is a LISTENER's (the page asking, ``listener``; "" when the
      caller does not say): the phone and the desktop hearing the same
      session each have theirs — one falling back does not pull the
      other's waiting lines off the host, and only a listener's OWN live
      lines ahead extend a line's budget (another page's lines are not
      being played to this one, so waiting behind them is silence).
    * Learned speed (TOO SLOW FOR NOW). Each answer of a serial host is a
      sample of ``took / host_budget(text)`` (:data:`SPEED_ALPHA`, see
      :class:`_Speed`); a line it gave up on counts as at least what it
      waited. Two uses, so that a host that cannot keep up costs no
      silence: a line predicted to arrive past its budget (the predicted
      time of every request ahead in the lane plus its own) is not sent at
      all — it is said by the fallback at once (an ``upfront``
      :class:`_Slow`); and while the learned ratio is over
      :data:`SLOW_RATIO` the host is too slow for EVERY line, so
      :meth:`pick` hands out the fallback straight away (the cache and the
      route see piper too). That window is not ``down``: no
      :data:`HOST_DOWN_SECONDS`, the boot warm and the probes still go to
      the host, and one probe (:meth:`_speed_probe`, at most every
      :data:`SLOW_RECHECK_SECONDS`, only while lines are asked) answering
      under the ratio ends it. A host with no samples yet is given its
      budget, as before; :meth:`warm_host` takes the first sample at boot.
    * Prewarm lines (:meth:`prewarm_line`, ``routes/speech.py``'s
      ``Prewarm``) are ``low``: every real line queues ahead of one still
      waiting, and they belong to :data:`PREWARM_LISTENER`, nobody's reply.
      The prewarm asks only past :meth:`idle_for`'s quiet, one at a time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._down: Dict[str, _Health] = {}
        self._replies: Dict[str, _Reply] = {}
        self._speed: Dict[str, _Speed] = {}
        self._cond = threading.Condition(self._lock)
        self._lanes: Dict[str, _Lane] = {}
        self._gen = 0  # bumped by reset(): an old orphan changes nothing
        self._real_at = 0.0  # _now(): a real line was last asked for / said
        self._answered: Dict[str, float] = {}  # key → _now() of its last answer
        self._starting: Dict[str, int] = {}  # key → cold-start lines in flight

    def reset(self) -> None:
        with self._lock:
            self._down.clear()
            self._replies.clear()
            self._speed.clear()
            self._lanes.clear()
            self._answered.clear()
            self._starting.clear()
            self._gen += 1
            self._real_at = 0.0
            self._cond.notify_all()

    @property
    def generation(self) -> int:
        """Bumped by :meth:`reset`: background work started before it stops."""
        return self._gen

    def idle_for(self) -> float:
        """Seconds the voice has been idle: no real line in flight or in a
        lane (an orphan, a probe or a warm holds its lane too), and nothing
        any listener was served still playing (:meth:`note_line`) — 0.0
        while any of that is going on. The prewarm speaks only past
        ``routes/speech.py``'s quiet window, so it is never ahead of a real
        line in the host's lane, and never between a reply's lines."""
        with self._lock:
            if any(lane.queue for lane in self._lanes.values()):
                return 0.0
            last = self._real_at
            for who, r in self._replies.items():
                if who == PREWARM_LISTENER:
                    continue
                if r.inflight:
                    return 0.0
                last = max(last, r.quiet_at)
            return max(0.0, _now() - last)

    def _reply(self, listener: str) -> _Reply:
        """``listener``'s reply (under the lock), made on first use; the
        idle ones are forgotten past :data:`MAX_LISTENERS`."""
        r = self._replies.get(listener)
        if r is None:
            if len(self._replies) >= MAX_LISTENERS:
                for k in [
                    k
                    for k, v in self._replies.items()
                    if not v.inflight and not v.sticky
                ]:
                    del self._replies[k]
            r = self._replies[listener] = _Reply()
        return r

    # -- learned speed --------------------------------------------------- #
    def _learn(self, key: str, text: str, took: float, at_least: bool = False) -> None:
        """Fold one answer of the host ``key`` into its speed (under the
        lock). ``at_least``: a line given up on after ``took`` — its real
        time is at least that, so it can only push the ratio up. A sample
        under :data:`SLOW_RATIO` while too slow ends the window at once
        (the host just kept up: believe it)."""
        r = took / max(0.05, host_budget(text))
        sp = self._speed.setdefault(key, _Speed())
        was = sp.slow
        if sp.samples == 0:
            sp.ratio = r
        elif at_least:
            sp.ratio = max(sp.ratio, SPEED_ALPHA * r + (1 - SPEED_ALPHA) * sp.ratio)
        elif was and r <= SLOW_RATIO:
            sp.ratio = r
        else:
            sp.ratio = SPEED_ALPHA * r + (1 - SPEED_ALPHA) * sp.ratio
        sp.samples += 1
        if not at_least:
            sp.last_s = took
        if sp.slow and not was:
            sp.recheck_at = _now() + SLOW_RECHECK_SECONDS
            _logger.info(
                "voice: %s is too slow for now (%.1f× its budget; not down)",
                key,
                sp.ratio,
            )
        elif was and not sp.slow:
            _logger.info("voice: %s keeps up again (%.1f× its budget)", key, sp.ratio)

    def _predict(self, key: str, text: str) -> Optional[float]:
        """Seconds the host ``key`` is expected to take for ``text`` (under
        the lock); None before its first sample."""
        sp = self._speed.get(key)
        if sp is None or sp.samples == 0:
            return None
        return sp.ratio * host_budget(text)

    def too_slow(self, backend: Backend, probe: bool = True) -> str:
        """Why ``backend`` is too slow for now ("" when it keeps up or is
        unmeasured). When the recheck is due this starts the probe (once) —
        the probe's answer, not the clock, ends the window. ``probe=False``
        (a status read: ``GET /api/voice?warm=0``) only reports: nothing is
        sent to the host, so reading the status never wakes one that went
        idle (Voice Lab)."""
        try:
            key = backend.health_key()
        except Exception:  # noqa: BLE001 — unreadable settings: no speed
            return ""
        with self._lock:
            sp = self._speed.get(key)
            if sp is None or not sp.slow:
                return ""
            due = probe and not sp.probing and _now() >= sp.recheck_at
            if due:
                sp.probing = True
        if due:
            try:
                _spawn(lambda: self._speed_probe(backend, key), "mindflock-voice-pace")
            except Exception:  # noqa: BLE001 — no thread: try again later
                with self._lock:
                    sp.probing = False
        with self._lock:
            if not sp.slow:  # the probe ran inline (tests) and it keeps up
                return ""
            return "%s takes %.1f× its budget (last line %.1f s)" % (
                key,
                sp.ratio,
                sp.last_s,
            )

    def _speed_probe(self, backend: Backend, key: str) -> None:
        """One :data:`WARM_TEXT` in the lane, waited for (up to
        :func:`host_hung_after`): its time is a sample (:meth:`_drain`); a
        dead answer marks the host down."""
        try:
            self.send(backend, WARM_TEXT, host_hung_after(WARM_TEXT))
        except SpeechError as err:
            if err.down:
                self.mark_down(backend, str(err))
        except Exception:  # noqa: BLE001 — a probe never raises
            pass
        finally:
            with self._lock:
                sp = self._speed.get(key)
                if sp is not None:
                    sp.probing = False
                    sp.recheck_at = _now() + SLOW_RECHECK_SECONDS

    def warm_host(self, engine: Backend) -> None:
        """A local host's warm (:meth:`OpenAITTS.warm`): :data:`WARM_TEXT`
        in the lane, then its first speed sample. A warm that came back
        within budget is that sample (it is no faster than the host); a
        slow one may have been a cold load, so one more word is timed —
        and a host that is merely slow is then known before the first
        reply, which goes to the fallback at once instead of waiting."""
        took: List[float] = []
        self.send(engine, WARM_TEXT, WARM_TIMEOUT, learn=False, took=took)
        try:
            key = engine.health_key()
        except Exception:  # noqa: BLE001
            return
        if not took:  # said outside the lane: nothing measured
            return
        with self._lock:
            if took[0] <= SLOW_RATIO * host_budget(WARM_TEXT):
                self._learn(key, WARM_TEXT, took[0])
                return
        try:
            self.send(engine, WARM_TEXT, host_hung_after(WARM_TEXT))
        except SpeechError as err:
            if err.down:
                self.mark_down(engine, str(err))

    def speed(self, backend: Backend) -> dict:
        """The learned pace of ``backend`` for ``GET /api/voice``:
        ``{"ratio", "samples", "slow"}`` (ratio 0 before a sample)."""
        try:
            key = backend.health_key()
        except Exception:  # noqa: BLE001
            return {"ratio": 0.0, "samples": 0, "slow": False}
        with self._lock:
            sp = self._speed.get(key) or _Speed()
            return {
                "ratio": round(sp.ratio, 2),
                "samples": sp.samples,
                "slow": sp.slow,
            }

    # -- health ---------------------------------------------------------- #
    def down_reason(self, backend: Backend, probe: bool = True) -> str:
        """Why ``backend`` is down ("" when healthy). Past its window this
        starts the background probe (once) and still answers "down" — the
        probe, not the clock, brings it back. ``probe=False`` (a status
        read) only reports, as :meth:`too_slow`."""
        try:
            key = backend.health_key()
        except Exception:  # noqa: BLE001 — unreadable settings: no health
            return ""
        with self._lock:
            h = self._down.get(key)
            if h is None:
                return ""
            why = h.why
            due = probe and not h.probing and _now() >= h.retry_at
            if due:
                h.probing = True
        if due:
            try:
                _spawn(lambda: self._probe(backend, key), "mindflock-voice-probe")
            except Exception:  # noqa: BLE001 — no thread: try again later
                with self._lock:
                    h.probing = False
        return why

    def mark_down(self, backend: Backend, why: str) -> None:
        try:
            key = backend.health_key()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            h = self._down.get(key)
            if h is None:
                self._down[key] = _Health(why, _now() + HOST_DOWN_SECONDS)
            else:
                h.why, h.retry_at = why, _now() + HOST_DOWN_SECONDS
            for r in self._replies.values():  # every listener's reply
                r.sticky = True
            self._cond.notify_all()  # their waiting lines leave the lane
        _logger.info("voice: %s is down for %ds: %s", key, HOST_DOWN_SECONDS, why)

    def mark_up(self, backend: Backend) -> None:
        try:
            key = backend.health_key()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._down.pop(key, None)

    def _probe(self, backend: Backend, key: str) -> None:
        """One :data:`WARM_TEXT` within :data:`PROBE_TIMEOUT`: healthy on
        an answer, down for another window on anything else."""
        try:
            self.send(backend, WARM_TEXT, PROBE_TIMEOUT)
        except Exception as err:  # noqa: BLE001
            with self._lock:
                h = self._down.get(key)
                if h is not None:
                    h.why = str(err) or type(err).__name__
                    h.retry_at = _now() + HOST_DOWN_SECONDS
                    h.probing = False
            return
        with self._lock:
            self._down.pop(key, None)
        _logger.info("voice: %s is back", key)
        if backend.kind == "tts":
            # It just said a word within the probe's time: that is a warm.
            try:
                what = "%s:%s" % (backend.id, backend.ready()[1])
                _WARMED["tts"] = ("loaded", what, _now())
            except Exception:  # noqa: BLE001 — a record, never a failure
                pass

    # -- the lane (a serial host's requests, one at a time) -------------- #
    def send(
        self,
        engine: Backend,
        text: str,
        timeout: float,
        voice: str = "",
        budget: Optional[float] = None,
        listener: str = "",
        learn: bool = True,
        took: Optional[List[float]] = None,
        low: bool = False,
    ) -> Tuple[bytes, str]:
        """``engine.speak_within(text, voice, timeout)`` — for a
        :attr:`Backend.serial` host in its lane, after every request of ours
        ahead of it. ``budget`` (a line of ``listener``'s reply): give up
        waiting this long after that listener's live line ahead should be
        done (or now), or as soon as the reply has gone to the fallback
        while this one has not reached the host — raising :class:`_Slow`,
        the request left to finish as an orphan; and when the host's
        learned speed says it cannot arrive by then, raise it before
        sending anything (``upfront``). ``None`` waits for the answer.
        ``learn``: the answer is a speed sample; ``took`` gets the
        seconds the host took appended. ``low`` (a prewarm line): every
        later line that is not ``low`` goes ahead of it while it waits —
        once at the host it is not interrupted (an HTTP synthesis cannot
        be), but nothing waits behind a queued one."""
        if not engine.serial:
            return engine.speak_within(text, voice, timeout)
        line = _Line(engine, text, voice, timeout)
        line.learn, line.listener, line.low = learn, listener, low
        with self._lock:
            key = engine.health_key()
            lane = self._lanes.get(key)
            if lane is None:
                lane = self._lanes[key] = _Lane(key)
            if budget is not None:
                now = time.monotonic()
                ahead = [
                    ln.deadline
                    for ln in lane.queue
                    if ln.live and ln.deadline is not None and ln.listener == listener
                ]
                line.deadline = max([now] + ahead) + budget
                eta = self._eta(key, lane, text, now, low)
                if eta is not None and eta > line.deadline:
                    raise _Slow(
                        "%s is too slow for this line: due in %.1f s, budget %.1f s"
                        " (not sent; not down)" % (key, eta - now, line.deadline - now),
                        upfront=True,
                    )
            line.gen = self._gen
            at = len(lane.queue)
            if not low:  # ahead of the prewarm lines still waiting
                at = next(
                    (i for i, ln in enumerate(lane.queue) if ln.low and not ln.started),
                    at,
                )
            lane.queue.insert(at, line)
            start, lane.busy = not lane.busy, True
        if start:
            try:
                _lane_thread(lambda: self._drain(lane), "mindflock-voice-lane")
            except Exception:  # noqa: BLE001 — no thread: say it here
                with self._lock:
                    lane.queue.remove(line)
                    lane.busy = False
                return engine.speak_within(text, voice, timeout)
        with self._lock:
            while not line.done:
                if line.deadline is None:
                    self._cond.wait(1.0)
                    continue
                left = line.deadline - time.monotonic()
                reply = self._replies.get(listener)
                if left <= 0 or (
                    reply is not None and reply.sticky and not line.started
                ):
                    break
                self._cond.wait(left)
            if not line.done:
                line.live = False
                if not line.started:
                    lane.queue.remove(line)
                elif learn and line.gen == self._gen:
                    # At the host past its budget: at least this slow.
                    self._learn(key, text, time.monotonic() - line.sent_at, True)
                waited = budget or 0.0
                where = "is still on it" if line.started else "had lines ahead of it"
                raise _Slow(
                    "%s is slow: no answer within %.1f s (%s; not down)"
                    % (engine.health_key(), waited, where)
                )
        if took is not None and line.started:
            took.append(line.took)
        if line.error is not None:
            raise line.error
        if line.result is None:  # never: the drain sets one or the other
            raise SpeechError("%s sent no audio back" % engine.health_key(), down=True)
        return line.result

    def _eta(
        self, key: str, lane: _Lane, text: str, now: float, low: bool = False
    ) -> Optional[float]:
        """When a line of ``text`` sent to ``lane`` now would come back, by
        the host's learned speed (under the lock): every request ahead of
        it (the one at the host: what is left of it — half its time again
        once it is overdue, as an orphan is; a waiting prewarm line is
        behind any line that is not ``low``), then its own. None while the
        speed is unmeasured."""
        own = self._predict(key, text)
        if own is None:
            return None
        t = now + own
        for ln in lane.queue:
            if ln.low and not ln.started and not low:
                continue
            p = self._predict(key, ln.text) or 0.0
            if ln.started:
                left = p - (now - ln.sent_at)
                t += left if left > 0 else 0.5 * p
            else:
                t += p
        return t

    def _drain(self, lane: _Lane) -> None:
        """Say ``lane``'s requests in order, one at a time, until it is
        empty. An orphan (its line gave up) still holds the lane until the
        host answers: the host is busy with it whether we listen or not."""
        while True:
            with self._lock:
                if not lane.queue:
                    lane.busy = False
                    return
                line = lane.queue[0]
                line.started, line.sent_at = True, time.monotonic()
            try:
                line.result = line.engine.speak_within(
                    line.text, line.voice, line.timeout
                )
            except Exception as err:  # noqa: BLE001 — handed to the caller
                line.error = err
            finally:
                with self._lock:
                    if lane.queue and lane.queue[0] is line:
                        lane.queue.pop(0)
                    line.took = time.monotonic() - line.sent_at
                    if line.error is None:
                        self._answered[lane.key] = _now()  # it is up (cold)
                    if line.learn and line.error is None and line.gen == self._gen:
                        self._learn(lane.key, line.text, line.took)
                    line.done = True
                    orphan = not line.live and line.gen == self._gen
                    self._cond.notify_all()
            if orphan:
                self._orphan_done(line)

    def _orphan_done(self, line: _Line) -> None:
        """A line we gave up on came back: late audio means slow (nothing
        to mark); a dead answer — hung past :func:`host_hung_after`, 5xx,
        reset — marks the host down, as on a live line."""
        took = line.took
        err = line.error
        if isinstance(err, SpeechError) and err.down:
            self.mark_down(line.engine, str(err))
        elif err is None:
            _logger.info(
                "voice: %s answered a line we gave up on after %.1f s (slow)",
                line.engine.health_key(),
                took,
            )

    # -- choosing -------------------------------------------------------- #
    @staticmethod
    def fallback_for(primary: Backend) -> Optional[Backend]:
        """The first ready :data:`FALLBACK_TTS` engine that is not
        ``primary``."""
        for backend_id in FALLBACK_TTS:
            if backend_id == primary.id:
                continue
            backend = get("tts", backend_id)
            if backend is None:
                continue
            try:
                ok, _detail = backend.ready()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                return backend
        return None

    def pick(
        self, primary: Backend, listener: str = "", probe: bool = True
    ) -> Tuple[Optional[Backend], bool, str]:
        """``(engine, fell_back, why)`` for ``listener``'s next line:
        ``primary``, or the fallback while ``primary`` is down (``why``, ""
        when it is healthy), too slow for now (:meth:`too_slow`) or a reply
        of this listener that began on the fallback is still going
        (:class:`TtsChain`). ``engine`` is None when ``primary`` is down and
        no fallback is ready. ``probe``: as :meth:`down_reason`."""
        why = self.down_reason(primary, probe)
        slow = "" if why else self.too_slow(primary, probe)
        with self._lock:
            r = self._reply(listener)
            if why or slow:
                r.sticky = True
            elif r.sticky and (
                r.inflight == 0 and _now() >= r.quiet_at + REPLY_GAP_SECONDS
            ):
                r.sticky = False
            sticky = r.sticky
        if not sticky:
            return primary, False, ""
        fallback = self.fallback_for(primary)
        if fallback is None:
            return (None, False, why) if why else (primary, False, "")
        return fallback, True, why

    # -- cold starts ----------------------------------------------------- #
    def cold(self, engine: Backend) -> bool:
        """Whether ``engine``'s next line is a COLD START: a serial engine
        that :meth:`Backend.starts_cold`, with no answer from it in the last
        :data:`COLD_AFTER_SECONDS` (or none yet), or a cold start of it
        still in flight (the lines of the same reply wait with it)."""
        try:
            if not engine.serial or not engine.starts_cold():
                return False
            key = engine.health_key()
        except Exception:  # noqa: BLE001 — unreadable settings: not cold
            return False
        with self._lock:
            if self._starting.get(key):
                return True
            last = self._answered.get(key)
        return last is None or _now() - last >= COLD_AFTER_SECONDS

    def _cold_line(
        self, engine: Backend, text: str, voice: str, listener: str
    ) -> Tuple[bytes, str]:
        """A cold start's line: in the lane, WAITED for (no budget, so no
        fallback for being slow) up to :data:`COLD_START_TIMEOUT` for the
        host's first byte, and not a speed sample (a model loading is not
        the host's pace). A refused or reset connection still fails at once
        (the host is down, not starting)."""
        key = engine.health_key()
        with self._lock:
            self._starting[key] = self._starting.get(key, 0) + 1
        _logger.info("voice: %s may be starting: waiting for it", key)
        try:
            return self.send(
                engine,
                text,
                max(COLD_START_TIMEOUT, host_hung_after(text)),
                voice,
                None,
                listener,
                learn=False,
            )
        finally:
            with self._lock:
                left = self._starting.get(key, 0) - 1
                if left > 0:
                    self._starting[key] = left
                else:
                    self._starting.pop(key, None)

    # -- speaking -------------------------------------------------------- #
    def note_line(self, text: str, listener: str = "") -> None:
        """A line was served to ``listener`` (a cache hit too): its reply
        runs until the line should have finished playing."""
        with self._lock:
            r = self._reply(listener)
            r.quiet_at = max(
                r.quiet_at, _now() + len(text or "") / SPOKEN_CHARS_PER_SECOND
            )

    def speak(
        self, primary: Backend, text: str, voice: str = "", listener: str = ""
    ) -> Tuple[bytes, str, Backend, bool]:
        """Say ``text`` for ``listener`` → ``(audio, mime, the engine that
        spoke, fell back)``; blocking (the route runs it on a worker
        thread). Raises the :class:`SpeechError` when no engine could say
        it."""
        engine, fell_back, why = self.pick(primary, listener)
        if engine is None:
            raise SpeechError(
                "%s is down (%s) and no fallback voice is ready" % (primary.label, why),
                down=True,
            )
        with self._lock:
            self._reply(listener).inflight += 1
            self._real_at = _now()
        try:
            if fell_back:
                audio, mime = engine.speak(text, voice)
                return audio, mime, engine, True
            try:
                if engine.serial and self.cold(engine):
                    # It may be starting (Voice Lab after its idle stop):
                    # wait for it rather than say the line with the fallback.
                    audio, mime = self._cold_line(engine, text, voice, listener)
                elif engine.serial:
                    # No fallback ready: waiting for the host beats a 502.
                    budget = host_budget(text) if self.fallback_for(engine) else None
                    audio, mime = self.send(
                        engine, text, host_hung_after(text), voice, budget, listener
                    )
                else:
                    audio, mime = engine.speak_within(text, voice, host_budget(text))
                return audio, mime, engine, False
            except _Slow as err:
                # Alive, only slow: this line (and the rest of the reply) on
                # the fallback, the host kept healthy for the next reply.
                with self._lock:
                    self._reply(listener).sticky = True
                    self._cond.notify_all()  # the reply's waiting lines leave
                _logger.info("voice: %s", err)
                fallback = self.fallback_for(engine)
                if fallback is None:
                    raise
                audio, mime = fallback.speak(text, voice)
                return audio, mime, fallback, True
            except SpeechError as err:
                if not err.down:
                    raise
                self.mark_down(engine, str(err))
                fallback = self.fallback_for(engine)
                if fallback is None:
                    raise
                audio, mime = fallback.speak(text, voice)
                return audio, mime, fallback, True
        finally:
            with self._lock:
                self._reply(listener).inflight -= 1
                self._real_at = _now()
            self.note_line(text, listener)

    def prewarm_line(
        self, engine: Backend, text: str, voice: str = ""
    ) -> Tuple[bytes, str]:
        """Say ``text`` with ``engine`` for nobody yet (the prewarm's
        cache): a serial host gets it as a ``low`` line of
        :data:`PREWARM_LISTENER` in its lane, waited for (its answer is a
        speed sample like any other); any other engine directly. No reply,
        no fallback: a failure is the caller's."""
        if engine.serial:
            return self.send(
                engine,
                text,
                host_hung_after(text),
                voice,
                None,
                PREWARM_LISTENER,
                low=True,
            )
        return engine.speak(text, voice)

    def status(self, primary: Backend, listener: str = "", probe: bool = True) -> dict:
        """``GET /api/voice``'s view of the chain for ``primary``:
        ``{"engine": Backend|None, "fell_back": bool, "why": str, "slow":
        str, "cold": bool}`` — ``slow`` why it is too slow for now ("" when
        it keeps up or is down), ``cold`` whether its next line would be a
        cold start (:meth:`cold`). ``probe=False``: a read only, nothing
        sent to the host (:meth:`down_reason`)."""
        engine, fell_back, why = self.pick(primary, listener, probe)
        slow = "" if why else self.too_slow(primary, probe)
        return {
            "engine": engine,
            "fell_back": fell_back,
            "why": why,
            "slow": slow,
            "cold": self.cold(primary),
        }


#: The one voice chain (``POST /api/voice/speak``, :func:`describe`).
TTS_CHAIN = TtsChain()


_WARMING: Dict[str, threading.Thread] = {}
#: The last warm per kind: ``(result, what was warmed, when)`` — ``result``
#: is "loaded", "loading" or "skipped: …" (:func:`describe`'s ``warm``).
_WARMED: Dict[str, Tuple[str, str, float]] = {}


def _warm_target(kind: str) -> Tuple[Optional[Backend], str]:
    """The backend a warm of ``kind`` would load, with its readiness detail
    — or ``(None, "skipped: <why>")``. Recognition: only an explicitly
    configured ``local`` (an unset backend might pick a host, and nobody
    asked for half a gigabyte of model in memory). Voice: whichever backend
    speaks (:func:`select`) — a local piper voice, a local OpenAI-compatible
    host (:meth:`OpenAITTS.warm` never calls a remote one) or the OS engine."""
    if kind == "stt":
        if _configured(kind) != "local":
            return None, "skipped: stt backend is not local"
        backend = get(kind, "local")
        if backend is None:
            return None, "skipped: no local stt backend"
        ok, detail = backend.ready()
    elif kind == "tts":
        backend, detail = select(kind)
        ok = backend is not None
        if backend is not None and TTS_CHAIN.down_reason(backend):
            # The configured voice is down: have the fallback that speaks
            # meanwhile loaded instead (its probe re-checks the host).
            fallback = TTS_CHAIN.fallback_for(backend)
            if fallback is None:
                return None, "skipped: %s is down" % backend.label
            backend, detail = fallback, fallback.ready()[1]
    else:
        return None, "skipped: no %s backend" % kind
    if not ok or backend is None:
        return None, "skipped: " + detail
    if not hasattr(backend, "warm"):
        return None, "skipped: %s has nothing to warm" % backend.label
    return backend, detail


def warm(kind: str = "stt", prime: bool = True) -> str:
    """Load ``kind``'s model now (:func:`_warm_target`), so the first press
    of the mic — or the first line read aloud — does not pay for it; returns
    ``"loaded"`` / ``"already"``, or ``"skipped: <why>"`` (nothing to warm,
    not installed, memory low, load or host failed). Never raises;
    blocking — see :func:`warm_in_background`."""
    what = ""
    try:
        backend, detail = _warm_target(kind)
        if backend is None:
            result = detail
        else:
            what = "%s:%s" % (backend.id, detail)
            try:
                result = str(backend.warm(prime=prime))  # type: ignore[attr-defined]
            except SpeechError as err:
                if kind == "tts" and err.down:
                    TTS_CHAIN.mark_down(backend, str(err))
                raise
    except Exception as err:  # noqa: BLE001 — warming is best effort
        result = "skipped: %s" % err
    _WARMED[kind] = ("loaded" if result == "already" else result, what, _now())
    _logger.debug("voice warm %s: %s", kind, result)
    return result


def _warm_due(kind: str) -> bool:
    """Whether :func:`warm_in_background` has anything to do for ``kind``: a
    model-cache backend with a key not resident, or a host / OS engine not
    warmed with its current setup in the last :data:`REWARM_SECONDS` — or,
    when that last warm FAILED, in the last :data:`HOST_DOWN_SECONDS` (a
    failure used to hold the next warm off for ten minutes, so a Voice Lab
    started meanwhile met its cold load on a real line)."""
    backend, detail = _warm_target(kind)
    if backend is None:
        return False
    keys = getattr(backend, "warm_keys", None)
    if keys is not None:
        with _LOCK:
            return not all(k in _CACHE for k in keys())
    last = _WARMED.get(kind)
    if last is None or last[1] != "%s:%s" % (backend.id, detail):
        return True
    window = REWARM_SECONDS if last[0] == "loaded" else HOST_DOWN_SECONDS
    return _now() - last[2] >= window


def warm_in_background(
    kind: str = "stt", delay_s: float = 0.0
) -> Optional[threading.Thread]:
    """Run :func:`warm` on a daemon thread unless there is nothing to do
    (:func:`_warm_due`) or a warm-up of ``kind`` is already running: the
    server calls this at boot for both kinds and whenever a voice client
    fetches ``GET /api/voice`` (the mic UI mounting). Returns the thread it
    started, or None. With ``delay_s`` (boot) the check runs on the thread
    after the delay — choosing the voice backend may probe the OS engine,
    which the event loop must not wait for."""
    if delay_s <= 0:
        try:
            if not _warm_due(kind):
                return None
        except Exception:  # noqa: BLE001 — never break the caller
            return None
    with _LOCK:
        running = _WARMING.get(kind)
        if running is not None and running.is_alive():
            return None

        def loading() -> None:
            prev = _WARMED.get(kind, ("", "", 0.0))
            _WARMED[kind] = ("loading", prev[1], prev[2])

        def go() -> None:
            if delay_s > 0:
                time.sleep(delay_s)
                try:
                    if not _warm_due(kind):
                        return
                except Exception:  # noqa: BLE001
                    return
                loading()
            warm(kind)

        t = threading.Thread(
            target=go, name="mindflock-voice-warm-" + kind, daemon=True
        )
        _WARMING[kind] = t
        if delay_s <= 0:
            loading()
        t.start()
        return t


def _describe_tts(
    ready: dict, backend: Optional[Backend], detail: str, probe: bool = True
) -> str:
    """Fill ``server_ready``'s voice keys from :data:`TTS_CHAIN` and return
    the voice's detail: ``tts`` true while SOME engine will speak (the
    configured one, or the fallback while it is down), ``tts_engine`` the
    one that will (``"piper (fallback)"``), ``tts_fallback`` whether that is
    the fallback, ``tts_down`` why the configured one is down ("" healthy),
    ``tts_cold`` whether its next line would be a cold start
    (:meth:`TtsChain.cold`: Voice Lab may have stopped while idle).
    ``tts_backend`` stays the configured id (Settings keys its rows on it).
    ``probe``: as :meth:`TtsChain.down_reason` (False: nothing is sent)."""
    ready["tts_engine"], ready["tts_fallback"], ready["tts_down"] = "", False, ""
    ready["tts_cold"] = False
    if backend is None:
        return detail
    try:
        st = TTS_CHAIN.status(backend, probe=probe)
    except Exception:  # noqa: BLE001 — a report, never a failure
        st = {
            "engine": backend,
            "fell_back": False,
            "why": "",
            "slow": "",
            "cold": False,
        }
    engine, why = st["engine"], st["why"]
    ready["tts_down"] = why
    ready["tts_cold"] = bool(st.get("cold"))
    if engine is None:
        ready["tts"] = False
        return "%s is down (%s) and no fallback voice is ready" % (detail, why)
    ready["tts_fallback"] = bool(st["fell_back"])
    name = engine.engine_name()
    if not st["fell_back"]:
        ready["tts_engine"] = name
        return detail
    ready["tts_engine"] = name + " (fallback)"
    try:
        fb_detail = engine.ready()[1]
    except Exception:  # noqa: BLE001
        fb_detail = engine.label
    if why:
        until = "while it is down (%s)" % why
    elif st.get("slow"):
        until = "while it is too slow (%s)" % st["slow"]
    else:
        until = "until the reply in progress ends"
    return "%s — speaking with %s %s" % (detail, fb_detail, until)


def describe(probe: bool = True) -> dict:
    """What ``GET /api/voice`` reports about the server side::

        {"backends": {"stt": [ids], "tts": [ids]},
         "server_ready": {"stt": bool, "tts": bool, "detail": str,
                          "stt_backend": id|"", "stt_detail": str,
                          "tts_backend": id|"", "tts_detail": str,
                          "stt_stream": bool, "tts_engine": str,
                          "tts_fallback": bool, "tts_down": str,
                          "tts_cold": bool, "stt_loaded": bool},
         "system": {"available": bool,
                    "platform": "windows"|"wsl"|"mac"|"none",
                    "detail": str, "voices": [names],
                    "recognizers": [culture tags],
                    "stt_quality": "basic"|"good"|"none"},
         "stream_model": str,
         "warm": {"stt": str, "tts": str}}

    ``detail`` is the one-paragraph summary; the per-kind keys are what the
    Settings screen prints under each row; ``stt_stream`` says the ready
    recognition backend streams (``POST /api/voice/stream``: only
    ``local``) and ``stream_model`` names the model its live transcript runs
    for English (``""`` when it does not stream); ``system`` is the built-in
    engine's own report (:func:`speech_system.describe`); ``warm`` is each
    kind's last warm-up — ``"loaded"``, ``"loading"``, ``"skipped: <why>"``,
    or ``""`` before the first. Offline and cheap: the built-in engine is
    probed once per process, not per call. ``probe=False``
    (``GET /api/voice?warm=0``): a voice host that is down or too slow is
    reported as it is, never re-checked — nothing is sent to it, so a page
    reading the status never wakes a host that went idle (Voice Lab).
    """
    ready: dict = {}
    lines: List[str] = []
    stream_model = ""
    for kind in ("stt", "tts"):
        backend, detail = select(kind)
        ready[kind] = backend is not None
        ready[kind + "_backend"] = backend.id if backend else ""
        if kind == "tts":
            detail = _describe_tts(ready, backend, detail, probe)
            backend = backend if ready["tts"] else None
        ready[kind + "_detail"] = detail
        if kind == "stt":
            ready["stt_stream"] = bool(backend is not None and backend.streams)
            namer = getattr(backend, "stream_model_name", None)
            if ready["stt_stream"] and callable(namer):
                try:
                    stream_model = str(namer())
                except Exception:  # noqa: BLE001 — a label, never a failure
                    stream_model = ""
        word = KIND_WORD[kind]
        lines.append(
            "Server %s ready (%s): %s" % (word, backend.id, detail)
            if backend
            else "Server %s not ready: %s" % (word, detail)
        )
    ready["detail"] = ". ".join(lines) + "."
    # Whether a first phrase waits for local recognition to load (it is
    # loaded only while someone listens, or after a recent phrase).
    ready["stt_loaded"] = stt_loaded()
    return {
        "backends": {k: ids(k) for k in ("stt", "tts")},
        "server_ready": ready,
        "system": _system.describe(),
        "stream_model": stream_model,
        "warm": {k: _WARMED.get(k, ("", "", 0.0))[0] for k in ("stt", "tts")},
    }
