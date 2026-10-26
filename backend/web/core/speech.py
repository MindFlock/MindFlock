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
  server tick to call too.

Files the local backend keeps, under ``~/.mindflock/voice/`` (or
``$MINDFLOCK_VOICE_DIR``):

* ``whisper/`` — faster-whisper's ``download_root``: the CTranslate2 model,
  ``small`` run as int8 on the CPU unless ``voice.stt_model`` names another
  size (``base``, ``medium`` …). Downloaded from Hugging Face on first use.
* ``piper/<voice>.onnx`` + ``<voice>.onnx.json`` — piper voices, downloaded
  from Hugging Face on first use (:data:`PIPER_VOICE` unless
  ``voice.tts_voice`` names another; ~60 MB each).

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

import importlib
import importlib.util
import io
import json
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
    "IDLE_SECONDS",
    "backends",
    "choose",
    "describe",
    "get",
    "ids",
    "loaded",
    "mime_of",
    "select",
    "sweep_idle",
    "unload_all",
    "voice_dir",
]

#: A cached local model is dropped after this long without a call.
IDLE_SECONDS = 600.0
#: Where ``openai`` goes when ``voice.stt_url`` / ``voice.tts_url`` are blank.
OPENAI_URL = "https://api.openai.com/v1"
OPENAI_STT_MODEL = "gpt-transcribe"
#: ``/audio/speech`` still serves the undated ``gpt-4o-mini-tts`` alias; the
#: dated snapshots and tts-1 retire on 2027-01-06 and their named successor
#: (``gpt-realtime-2.1-mini``) is a realtime model that endpoint does not serve.
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "alloy"
WHISPER_MODEL = "small"
PIPER_VOICE = "en_US-lessac-medium"
STT_TIMEOUT = 30.0
TTS_TIMEOUT = 20.0
NOT_INSTALLED = "local models not installed: uv sync --group voice"

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
    """A backend could not do the job; ``str(err)`` is a sentence for the UI."""


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


def sweep_idle(now: Optional[float] = None) -> List[str]:
    """Drop every cached model idle for :data:`IDLE_SECONDS` or more; returns
    the keys dropped. Cheap when nothing is loaded — a server tick may call
    it freely; the timer armed on load calls it anyway."""
    stamp = _now() if now is None else now
    dropped: List[str] = []
    with _LOCK:
        for key in list(_CACHE):
            if stamp - _USED.get(key, stamp) >= IDLE_SECONDS:
                _CACHE.pop(key, None)
                _USED.pop(key, None)
                dropped.append(key)
    return dropped


def loaded() -> List[str]:
    """The cache keys currently held (``whisper:small``, ``piper:<voice>``)."""
    with _LOCK:
        return sorted(_CACHE)


def unload_all() -> None:
    """Drop every cached model and the sweep timer (tests, shutdown)."""
    global _TIMER
    with _LOCK:
        _CACHE.clear()
        _USED.clear()
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
class Backend:
    """One speech engine of one kind. ``ready()`` is cheap and offline; the
    work methods raise :class:`SpeechError` with a sentence for the UI."""

    id: str = ""
    kind: str = ""  # "stt" | "tts"
    label: str = ""

    def ready(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def transcribe(self, audio: bytes, mime: str, lang: str = "") -> str:
        raise SpeechError("%s does not do recognition" % self.label)

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        raise SpeechError("%s does not do voices" % self.label)


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
        except OSError as err:  # URLError, timeout, refused
            reason = getattr(err, "reason", None) or err
            raise SpeechError("could not reach %s: %s" % (host, reason)) from err
        if status >= 400:
            detail = _error_detail(data)
            raise SpeechError(
                "%s answered %d%s" % (host, status, (": " + detail) if detail else "")
            )
        return mime_of(ctype), data


class OpenAISTT(_OpenAI):
    kind = "stt"

    def _cfg(self) -> Tuple[str, str, str]:
        v = _voice()
        return _base(v.stt_url), v.stt_api_key, (v.stt_model or OPENAI_STT_MODEL)

    def ready(self) -> Tuple[bool, str]:
        url, key, model = self._cfg()
        if not key:
            return False, "%s needs an API key for %s" % (self.label, _host(url))
        return True, "%s at %s" % (model, _host(url))

    def transcribe(self, audio: bytes, mime: str, lang: str = "") -> str:
        url, key, model = self._cfg()
        if not key:
            raise SpeechError("%s needs an API key for %s" % (self.label, _host(url)))
        fields = {"model": model, "response_format": "json"}
        if lang:
            fields["language"] = lang
        body, ctype = _multipart(
            fields, "file", "clip" + _ext(mime), mime_of(mime), audio
        )
        headers = {"Authorization": "Bearer " + key, "Content-Type": ctype}
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
        if not key:
            return False, "%s needs an API key for %s" % (self.label, _host(url))
        return True, "%s (%s) at %s" % (model, voice, _host(url))

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        url, key, model, default_voice = self._cfg()
        if not key:
            raise SpeechError("%s needs an API key for %s" % (self.label, _host(url)))
        payload = {
            "model": model,
            "input": text,
            "voice": voice or default_voice,
            "response_format": "mp3",
        }
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        body = json.dumps(payload).encode("utf-8")
        rtype, data = self._call(url + "/audio/speech", headers, body, TTS_TIMEOUT)
        if (
            not data
            or rtype.startswith("application/json")
            or rtype.startswith("text/")
        ):
            raise SpeechError("%s sent no audio back" % _host(url))
        return data, (rtype if rtype.startswith("audio/") else "audio/mpeg")


class LocalSTT(Backend):
    id = "local"
    kind = "stt"
    label = "faster-whisper on this computer"

    def _model_name(self) -> str:
        return _voice().stt_model or WHISPER_MODEL

    def ready(self) -> Tuple[bool, str]:
        if not _installed("faster_whisper"):
            return False, NOT_INSTALLED
        low = _memory_block()
        if low:
            return False, "local recognition held back: " + low
        detail = "faster-whisper %s on this computer" % self._model_name()
        if not shutil.which("ffmpeg"):
            detail += (
                " (ffmpeg is not on PATH: webm/mp4 clips decode through the bundled"
                " PyAV; install ffmpeg if a clip is refused)"
            )
        return True, detail

    def _load(self, name: str):
        fw = _import("faster_whisper")
        root = voice_dir() / "whisper"
        root.mkdir(parents=True, exist_ok=True)
        return fw.WhisperModel(
            name, device="cpu", compute_type="int8", download_root=str(root)
        )

    def transcribe(self, audio: bytes, mime: str, lang: str = "") -> str:
        if not _installed("faster_whisper"):
            raise SpeechError(NOT_INSTALLED)
        low = _memory_block()
        if low:
            raise SpeechError("local recognition held back: " + low)
        name = self._model_name()
        try:
            model = _cached("whisper:" + name, lambda: self._load(name))
        except SpeechError:
            raise
        except Exception as err:  # noqa: BLE001 — download, CPU feature, …
            raise SpeechError(
                "could not load faster-whisper %s: %s" % (name, err)
            ) from err
        path = _temp_clip(audio, mime)
        try:
            segments, _info = model.transcribe(
                path, language=(lang or None), beam_size=5
            )
            words = [str(getattr(s, "text", "") or "").strip() for s in segments]
        except Exception as err:  # noqa: BLE001
            raise SpeechError("faster-whisper failed: %s" % err) from err
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return " ".join(w for w in words if w).strip()


class LocalTTS(Backend):
    id = "local"
    kind = "tts"
    label = "piper on this computer"

    def _voice_name(self, voice: str = "") -> str:
        return voice or _voice().tts_voice or PIPER_VOICE

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
        folder = voice_dir() / "piper"
        folder.mkdir(parents=True, exist_ok=True)
        onnx = folder / (name + ".onnx")
        if not onnx.is_file():
            try:
                _import("piper.download_voices").download_voice(name, folder)
            except Exception as err:  # noqa: BLE001 — offline, unknown voice
                raise SpeechError(
                    "could not download piper voice %s: %s (by hand: python -m "
                    "piper.download_voices %s --data-dir %s)"
                    % (name, err, name, folder)
                ) from err
        return _import("piper.voice").PiperVoice.load(str(onnx))

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        if not _installed("piper"):
            raise SpeechError(NOT_INSTALLED)
        low = _memory_block()
        if low:
            raise SpeechError("local voice held back: " + low)
        name = self._voice_name(voice)
        try:
            model = _cached("piper:" + name, lambda: self._load(name))
        except SpeechError:
            raise
        except Exception as err:  # noqa: BLE001
            raise SpeechError(
                "could not load piper voice %s: %s" % (name, err)
            ) from err
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


def describe() -> dict:
    """What ``GET /api/voice`` reports about the server side::

        {"backends": {"stt": [ids], "tts": [ids]},
         "server_ready": {"stt": bool, "tts": bool, "detail": str,
                          "stt_backend": id|"", "stt_detail": str,
                          "tts_backend": id|"", "tts_detail": str},
         "system": {"available": bool,
                    "platform": "windows"|"wsl"|"mac"|"none",
                    "detail": str, "voices": [names],
                    "recognizers": [culture tags]}}

    ``detail`` is the one-paragraph summary; the per-kind keys are what the
    Settings screen prints under each row; ``system`` is the built-in
    engine's own report (:func:`speech_system.describe`). Offline and cheap:
    the built-in engine is probed once per process, not per call.
    """
    ready: dict = {}
    lines: List[str] = []
    for kind in ("stt", "tts"):
        backend, detail = select(kind)
        ready[kind] = backend is not None
        ready[kind + "_backend"] = backend.id if backend else ""
        ready[kind + "_detail"] = detail
        word = KIND_WORD[kind]
        lines.append(
            "Server %s ready (%s): %s" % (word, backend.id, detail)
            if backend
            else "Server %s not ready: %s" % (word, detail)
        )
    ready["detail"] = ". ".join(lines) + "."
    return {
        "backends": {k: ids(k) for k in ("stt", "tts")},
        "server_ready": ready,
        "system": _system.describe(),
    }
