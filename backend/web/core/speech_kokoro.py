"""The built-in voice: Kokoro-82M on this computer (backend id ``kokoro``).

The voice assistant speaks with it (``POST /api/voice/speak`` with
``"as": "assistant"``, the voice ``voice.assistant_voice``), and it is an
ordinary voice backend too — ``voice.tts_backend = "kokoro"`` — and the
first local fallback of :class:`core.speech.TtsChain`, ahead of piper
(``speech.FALLBACK_TTS``). Natural where piper and the OS voices are
robotic, and fast enough on a CPU that a short reply does not wait on it:
measured 2026-11-09 on the owner's laptop (8 cores under WSL, load average
~10 from other work, 2 ONNX threads) the model loads in ~1 s, a 4.1 s
sentence takes 1.1–1.5 s (real-time factor ~0.27, up to ~0.4 when busier)
and the process holds ~270 MB more once a sentence has run.

What it runs: ``kokoro-onnx`` (MIT) — Kokoro-82M (Apache-2.0) exported to
ONNX, int8-quantized (:data:`MODEL_FILE`, ~92 MB) with its voice styles
(:data:`VOICES_FILE`, ~28 MB), on onnxruntime. Text becomes phonemes
through ``phonemizer`` + ``espeakng-loader`` (espeak-ng's library and data
ship inside the wheel for Linux, macOS and Windows, x86-64 and arm64: no
system package). The package is optional — the ``voice`` dependency group,
or the one-click install (``core.speech_install``), which records it so an
engine rebuild carries it (``self_update.carried_packages``) — and is
imported lazily, never at server start.

The model files live under ``speech.voice_dir()/models/kokoro/`` (the
MindFlock config dir's ``voice/models/kokoro/``), downloaded by
:func:`fetch_model` from the kokoro-onnx GitHub release
(:data:`RELEASE_URL`), each checked against its pinned size and sha256
(:data:`FILES`) before it is moved into place.

The loaded model is one entry of ``speech``'s model cache
(:data:`CACHE_KEY`), so the idle sweep, the memory shedding and the memory
guard apply as they do to piper; :func:`warm_in_background` loads it ahead
of the first line (``GET /api/voice?assistant=1``, or the assistant itself).
Calls are serialised on the model's lock (the phonemizer's espeak backend
is not thread-safe).

Tests replace :func:`_open_url` (the one download) and ``speech._import`` /
``speech._installed``; nothing here touches the network under pytest.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from backend.web.core import speech as _speech

__all__ = [
    "CACHE_KEY",
    "DEFAULT_VOICE",
    "FILES",
    "KokoroTTS",
    "MODULE",
    "PACKAGE",
    "assistant_voice",
    "available",
    "fetch_model",
    "ids",
    "loaded",
    "model_dir",
    "unavailable_reason",
    "voice_id",
    "voices",
    "warm_in_background",
]

#: The pip package and the module it installs.
PACKAGE = "kokoro-onnx"
MODULE = "kokoro_onnx"
#: The model files of the kokoro-onnx ``model-files-v1.0`` release.
RELEASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
)
#: The release's model version (the installer's ``kokoro <version>`` step).
MODEL_VERSION = "v1.0"
MODEL_FILE = "kokoro-v1.0.int8.onnx"
VOICES_FILE = "voices-v1.0.bin"
#: file → (bytes, sha256), checked on every download (verified 2026-11-09).
FILES: Dict[str, Tuple[int, str]] = {
    MODEL_FILE: (
        92361271,
        "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",  # pragma: allowlist secret — a sha256
    ),
    VOICES_FILE: (
        28214398,
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",  # pragma: allowlist secret — a sha256
    ),
}
#: ``speech._cached``'s key for the loaded model.
CACHE_KEY = "kokoro:v1.0-int8"
#: The assistant's voice while ``voice.assistant_voice`` is blank.
DEFAULT_VOICE = "af_heart"
#: Kokoro's output rate.
SAMPLE_RATE = 24000
#: Speaking rate (Kokoro takes 0.5–2.0).
SPEED = 1.0
#: ONNX Runtime threads for one synthesis. Measured on the owner's laptop
#: (8 cores, WSL, other work running): 1 → RTF 0.59, 2 → 0.40, 4 → 0.48,
#: all → 1.14 (contention). ``MINDFLOCK_KOKORO_THREADS`` overrides it.
THREADS = 2
_THREADS_ENV = "MINDFLOCK_KOKORO_THREADS"

#: The English voices of ``voices-v1.0.bin``: (id, label, espeak language).
#: The first letter is the accent (a: US, b: UK), the second the gender.
#: Its other languages need their own phonemizer setup and are not offered.
VOICES: Tuple[Tuple[str, str, str], ...] = (
    ("af_heart", "Heart (US, female)", "en-us"),
    ("af_bella", "Bella (US, female)", "en-us"),
    ("af_nicole", "Nicole (US, female, soft)", "en-us"),
    ("af_aoede", "Aoede (US, female)", "en-us"),
    ("af_kore", "Kore (US, female)", "en-us"),
    ("af_sarah", "Sarah (US, female)", "en-us"),
    ("af_nova", "Nova (US, female)", "en-us"),
    ("af_sky", "Sky (US, female)", "en-us"),
    ("af_alloy", "Alloy (US, female)", "en-us"),
    ("af_jessica", "Jessica (US, female)", "en-us"),
    ("af_river", "River (US, female)", "en-us"),
    ("am_michael", "Michael (US, male)", "en-us"),
    ("am_fenrir", "Fenrir (US, male)", "en-us"),
    ("am_puck", "Puck (US, male)", "en-us"),
    ("am_echo", "Echo (US, male)", "en-us"),
    ("am_eric", "Eric (US, male)", "en-us"),
    ("am_liam", "Liam (US, male)", "en-us"),
    ("am_onyx", "Onyx (US, male)", "en-us"),
    ("am_adam", "Adam (US, male)", "en-us"),
    ("bf_emma", "Emma (UK, female)", "en-gb"),
    ("bf_isabella", "Isabella (UK, female)", "en-gb"),
    ("bf_alice", "Alice (UK, female)", "en-gb"),
    ("bf_lily", "Lily (UK, female)", "en-gb"),
    ("bm_george", "George (UK, male)", "en-gb"),
    ("bm_fable", "Fable (UK, male)", "en-gb"),
    ("bm_lewis", "Lewis (UK, male)", "en-gb"),
    ("bm_daniel", "Daniel (UK, male)", "en-gb"),
)
_LANG = {vid: lang for vid, _label, lang in VOICES}
#: A download's read size, and how often it prints its progress.
_CHUNK = 1 << 16
_PROGRESS_STEP = 10


# --------------------------------------------------------------------------- #
# Voices
# --------------------------------------------------------------------------- #
def voices() -> List[dict]:
    """``[{"id", "label", "lang"}]`` — what ``GET /api/voice/voices?engine=
    kokoro`` lists, in the order Settings shows them."""
    return [{"id": vid, "label": label, "lang": lang} for vid, label, lang in VOICES]


def ids() -> List[str]:
    """Every offered voice id (:data:`VOICES`)."""
    return [vid for vid, _label, _lang in VOICES]


def voice_id(raw: object = "", default: str = "") -> str:
    """``raw`` when it names a Kokoro voice, else ``default`` when that does,
    else :data:`DEFAULT_VOICE`. A host's voice name (``alloy``, a Voice Lab
    clone) reaching this engine as the fallback is not a Kokoro voice."""
    for cand in (raw, default):
        name = str(cand or "").strip()
        if name in _LANG:
            return name
    return DEFAULT_VOICE


def assistant_voice() -> str:
    """The assistant's voice: ``voice.assistant_voice`` when it names a
    Kokoro voice, else :data:`DEFAULT_VOICE`."""
    try:
        raw = getattr(_speech._voice(), "assistant_voice", "")
    except Exception:  # noqa: BLE001 — unreadable settings: the default
        raw = ""
    return voice_id(raw)


# --------------------------------------------------------------------------- #
# The model files
# --------------------------------------------------------------------------- #
def model_dir() -> Path:
    """``speech.voice_dir()/models/kokoro`` (``~/.mindflock/voice/models/
    kokoro`` unless ``$MINDFLOCK_VOICE_DIR`` moves the voice dir)."""
    return _speech.voice_dir() / "models" / "kokoro"


def _present(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def missing_files() -> List[str]:
    """The :data:`FILES` not on disk at their full size (no hashing: that is
    done once, on download)."""
    try:
        root = model_dir()
    except Exception:  # noqa: BLE001 — the home guard refused: none present
        return list(FILES)
    return [
        name for name, (size, _sha) in FILES.items() if not _present(root / name, size)
    ]


def unavailable_reason() -> str:
    """Why the built-in voice cannot speak here → ``""`` when it can (the
    package importable and both model files on disk). Memory pressure is
    :meth:`KokoroTTS.ready`'s, not this: the files stay installed."""
    if not _speech._installed(MODULE):
        return "%s is not installed: Settings → Voice installs it" % PACKAGE
    missing = missing_files()
    if missing:
        return (
            "the Kokoro model is not downloaded (%s): Settings → Voice installs it"
            % (", ".join(missing))
        )
    return ""


def available() -> bool:
    return not unavailable_reason()


def _open_url(url: str, timeout: float = 60.0):
    """The one download seam: a readable response with ``headers``."""
    req = urllib.request.Request(url, headers={"User-Agent": "mindflock-voice"})
    return urllib.request.urlopen(req, timeout=timeout)


def _download(
    name: str, size: int, sha: str, folder: Path, say: Callable[[str], None]
) -> None:
    """``RELEASE_URL + name`` → ``folder/name``, through ``name.part``,
    checked against ``size`` and ``sha`` before the rename. Progress every
    :data:`_PROGRESS_STEP` percent through ``say``."""
    part = folder / (name + ".part")
    digest = hashlib.sha256()
    got = 0
    shown = -1
    say("downloading %s (%d MB)" % (name, size // 1_000_000))
    try:
        with _open_url(RELEASE_URL + name) as resp, open(part, "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                pct = min(100, got * 100 // max(1, size))
                if pct // _PROGRESS_STEP > shown:
                    shown = pct // _PROGRESS_STEP
                    say(
                        "%s: %d%% (%d of %d MB)"
                        % (name, pct, got // 1_000_000, size // 1_000_000)
                    )
        if got != size:
            raise _speech.SpeechError(
                "%s: got %d bytes, expected %d (the release changed?)"
                % (name, got, size)
            )
        if digest.hexdigest() != sha:
            raise _speech.SpeechError(
                "%s: checksum mismatch (download corrupted?)" % name
            )
        os.replace(part, folder / name)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def fetch_model(say: Optional[Callable[[str], None]] = None) -> str:
    """Download whichever :data:`FILES` are missing into :func:`model_dir`
    (the installer's helper process runs this; ``say`` gets the progress
    lines, default ``print``) → the folder. Raises :class:`speech.
    SpeechError` on a failed or corrupted download (nothing half-written is
    left in place)."""
    say = say or (lambda line: print(line, flush=True))
    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, (size, sha) in FILES.items():
        if _present(folder / name, size):
            say("%s already downloaded" % name)
            continue
        try:
            _download(name, size, sha, folder, say)
        except _speech.SpeechError:
            raise
        except Exception as err:  # noqa: BLE001 — offline, a 404, a full disk
            raise _speech.SpeechError(
                "could not download %s: %s (by hand: put %s%s in %s)"
                % (name, err, RELEASE_URL, name, folder)
            ) from err
    return str(folder)


# --------------------------------------------------------------------------- #
# The engine: one loaded model, shared
# --------------------------------------------------------------------------- #
class KokoroEngine:
    """A loaded ``kokoro_onnx.Kokoro`` (or a fake in tests), the lock every
    synthesis takes, and how long the load took."""

    def __init__(self, model, load_s: float = 0.0, threads: int = 0) -> None:
        self.model = model
        self.load_s = load_s
        self.threads = threads
        self.lock = threading.Lock()

    def synthesize(self, text: str, voice: str, speed: float = SPEED) -> bytes:
        """``text`` in ``voice`` → a mono 16-bit WAV at the model's rate."""
        vid = voice_id(voice)
        with self.lock:
            samples, rate = self.model.create(
                text, voice=vid, speed=speed, lang=_LANG.get(vid, "en-us")
            )
        return _wav(samples, int(rate or SAMPLE_RATE))


def _wav(samples, rate: int) -> bytes:
    """Float samples in [-1, 1] → WAV bytes (int16 PCM, mono)."""
    np = _speech._import("numpy")
    pcm = (
        np.clip(np.asarray(samples, dtype=np.float32).ravel(), -1.0, 1.0) * 32767.0
    ).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def threads() -> int:
    raw = os.environ.get(_THREADS_ENV, "").strip()
    try:
        n = int(raw) if raw else THREADS
    except ValueError:
        n = THREADS
    return max(1, min(n, os.cpu_count() or 1))


def _load() -> KokoroEngine:
    reason = unavailable_reason()
    if reason:
        raise _speech.SpeechError(reason)
    folder = model_dir()
    n = threads()
    started = time.monotonic()
    try:
        rt = _speech._import("onnxruntime")
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = n
        opts.inter_op_num_threads = 1
        session = rt.InferenceSession(
            str(folder / MODEL_FILE),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        kokoro = _speech._import(MODULE).Kokoro.from_session(
            session, str(folder / VOICES_FILE)
        )
    except Exception as err:  # noqa: BLE001
        raise _speech.SpeechError("Kokoro failed to load: %s" % err) from err
    return KokoroEngine(kokoro, time.monotonic() - started, n)


def engine() -> KokoroEngine:
    """The loaded model (loading it once, through ``speech._cached``: the
    idle sweep and memory shedding apply). Raises ``SpeechError`` when it
    cannot load (not installed, files missing, memory low)."""
    low = _speech._memory_block()
    if low:
        raise _speech.SpeechError("Kokoro held back: " + low)
    return _speech._cached(CACHE_KEY, _load)  # type: ignore[return-value]


def loaded() -> bool:
    return CACHE_KEY in _speech.loaded()


def synthesize(text: str, voice: str = "", speed: float = SPEED) -> bytes:
    """``text`` in ``voice`` (:func:`voice_id`) → WAV bytes. Raises
    ``SpeechError``."""
    eng = engine()
    try:
        return eng.synthesize(text, voice, speed)
    except _speech.SpeechError:
        raise
    except Exception as err:  # noqa: BLE001
        raise _speech.SpeechError("Kokoro failed: %s" % err) from err


_WARM_LOCK = threading.Lock()
_WARMING: Optional[threading.Thread] = None


def warm(prime: bool = True) -> str:
    """Load the model and, with ``prime``, say ``speech.WARM_TEXT`` into
    nothing once (onnxruntime's first run is the slow one) → ``"loaded"`` /
    ``"already"``. Raises ``SpeechError``."""
    was = loaded()
    eng = engine()
    if prime and not was:
        try:
            eng.synthesize(_speech.WARM_TEXT, assistant_voice())
        except Exception:  # noqa: BLE001 — a warm-up may fail quietly
            pass
    return "already" if was else "loaded"


def warm_in_background() -> Optional[threading.Thread]:
    """:func:`warm` on a daemon thread, when Kokoro can run and is not loaded
    yet and no warm is running → the thread, or None. Never raises."""
    global _WARMING
    try:
        if loaded() or not available() or _speech._memory_block():
            return None
    except Exception:  # noqa: BLE001
        return None
    with _WARM_LOCK:
        if _WARMING is not None and _WARMING.is_alive():
            return None

        def go() -> None:
            try:
                warm()
            except Exception:  # noqa: BLE001 — warming is best effort
                pass

        t = threading.Thread(target=go, name="mindflock-voice-warm-kokoro", daemon=True)
        _WARMING = t
        t.start()
        return t


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #
class KokoroTTS(_speech.Backend):
    """``voice.tts_backend = "kokoro"``, and the chain's first local
    fallback. The voice: the per-call one when it names a Kokoro voice, else
    ``voice.tts_voice`` when that does, else the assistant's
    (:func:`assistant_voice`)."""

    id = "kokoro"
    kind = "tts"
    label = "Kokoro on this computer"

    def engine_name(self) -> str:
        return "kokoro"

    def _voice_name(self, voice: str = "") -> str:
        try:
            configured = getattr(_speech._voice(), "tts_voice", "")
        except Exception:  # noqa: BLE001
            configured = ""
        for cand in (voice, configured):
            name = str(cand or "").strip()
            if name in _LANG:
                return name
        return assistant_voice()

    def ready(self) -> Tuple[bool, str]:
        try:
            reason = unavailable_reason()
        except Exception as err:  # noqa: BLE001 — never fails a status read
            reason = str(err) or type(err).__name__
        if reason:
            return False, reason
        low = _speech._memory_block()
        if low:
            return False, "Kokoro held back: " + low
        return True, "Kokoro voice %s on this computer" % self._voice_name()

    def warm_keys(self) -> List[str]:
        return [CACHE_KEY]

    def warm(self, prime: bool = True) -> str:
        return warm(prime)

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        return synthesize(text, self._voice_name(voice)), "audio/wav"


def _register() -> None:
    """Add :class:`KokoroTTS` to the voices once, just ahead of piper
    (``local``): with ``voice.tts_backend`` unset it is picked over piper,
    never over the OS voice or a configured host. Here rather than in
    ``speech`` so either module may be imported first."""
    group = _speech.BACKENDS.setdefault("tts", [])
    if any(isinstance(b, KokoroTTS) for b in group):
        return
    at = next((i for i, b in enumerate(group) if b.id == "local"), len(group))
    group.insert(at, KokoroTTS())


_register()
