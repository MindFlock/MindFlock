"""One-click local models: what ``POST /api/voice/install-local`` runs.

The built-in Windows recognizer (``core.speech_system``) is SAPI 5's
desktop dictation — the only Windows engine that reads a recorded file
offline, and a poor listener. The accurate offline path is the ``local``
backend (faster-whisper + piper, ``core.speech``), which used to need
``uv sync --group voice`` in a terminal. This module does the same from
the Settings screen, in a background thread, three steps:

1. **installing packages** — ``uv pip install --python <sys.executable>
   faster-whisper piper-tts`` into the interpreter the server runs (the
   live server runs from a uv tool venv). ``uv`` is found on PATH, else next
   to ``sys.executable``; without it, ``<sys.executable> -m pip install``.
2. **downloading recognition model (<name>)** — the faster-whisper model of
   ``voice.stt_model`` (``small`` unless it names another size) into
   ``speech.voice_dir()/whisper``, the cache ``LocalSTT`` loads from, so the
   first phrase is not a download.
3. **downloading voice (<name>)** — the piper voice of ``voice.tts_voice``
   (``en_US-lessac-medium`` unless it names a piper voice), fetched the way
   ``LocalTTS`` fetches one (``speech.fetch_piper_voice``).

Steps 2 and 3 run in a helper process (:func:`_download_main`, the same
interpreter): the packages were installed after this server imported
anything, and a model download must never cost the server its memory.
Every step's output (stdout + stderr) feeds :func:`status`'s ``log`` (the
last :data:`LOG_LINES` lines); a step that exits non-zero, cannot start or
outlives :data:`STEP_TIMEOUT` fails the whole install with its reason.

On success ``voice.stt_backend`` becomes ``"local"`` — the person asked
for accuracy; the voice backend stays whatever it was — and
``speech.invalidate()`` drops the import caches so ``GET /api/voice``
reports ``local`` ready without a restart.

Nothing here runs for real under pytest: :func:`_spawn` (every subprocess)
refuses there, and tests replace it.
"""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Deque, List, Optional, Sequence

from backend.web.core import speech as _speech

__all__ = ["PACKAGES", "install_argv", "start", "status"]

PACKAGES = ("faster-whisper", "piper-tts")
#: How many output lines ``status()`` keeps.
LOG_LINES = 20
#: One step's limit (a slow network pulling ~500 MB of model).
STEP_TIMEOUT = 30 * 60.0

STEP_PACKAGES = "installing packages"


# --------------------------------------------------------------------------- #
# Seams
# --------------------------------------------------------------------------- #
def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _isfile(path: str) -> bool:
    return os.path.isfile(path)


def _spawn(argv: Sequence[str]):
    """One step's process: stdout and stderr merged, text lines."""
    from backend.config.home_guard import under_pytest

    if under_pytest():
        raise OSError("the local-models install does not run under pytest")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        **kwargs,
    )


def _settings():
    from backend.config import settings as settings_store

    return settings_store


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #
def _uv() -> Optional[str]:
    """``uv`` on PATH, else beside the interpreter (a uv tool venv's
    ``bin/`` has none, but a standalone install's does)."""
    found = _which("uv")
    if found:
        return found
    here = Path(sys.executable).parent
    for name in ("uv", "uv.exe"):
        cand = str(here / name)
        if _isfile(cand):
            return cand
    return None


def install_argv() -> List[str]:
    """Step 1's command line."""
    uv = _uv()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *PACKAGES]
    return [sys.executable, "-m", "pip", "install", *PACKAGES]


def _download_argv(kind: str, name: str) -> List[str]:
    return [
        sys.executable,
        "-c",
        "from backend.web.core.speech_install import _download_main; _download_main()",
        kind,
        name,
    ]


def _download_main(argv: Optional[List[str]] = None) -> None:
    """The helper process: ``whisper <name>`` or ``piper <name>`` → the
    download, a last line saying where it went, exit 0; any failure a last
    line with the reason and exit 1."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 2 or args[0] not in ("whisper", "piper"):
            raise ValueError("usage: whisper <model> | piper <voice>")
        kind, name = args
        if kind == "whisper":
            where = _speech.fetch_whisper_model(name)
        else:
            where = str(_speech.fetch_piper_voice(name))
    except Exception as err:  # noqa: BLE001 — the reason is the last line
        print("error: %s" % (str(err) or type(err).__name__), flush=True)
        sys.exit(1)
    print("saved to %s" % where, flush=True)


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_STATE: dict = {
    "state": "idle",
    "step": "",
    "started": None,
    "finished": None,
    "error": "",
}
_LOG: Deque[str] = collections.deque(maxlen=LOG_LINES)
_THREAD: Optional[threading.Thread] = None


def status() -> dict:
    """``{"state": "idle"|"running"|"done"|"failed", "step", "log": [the
    last LOG_LINES lines], "started", "finished", "error"}`` (epoch seconds
    or None)."""
    with _LOCK:
        return {**_STATE, "log": list(_LOG)}


def _set(**fields) -> None:
    with _LOCK:
        _STATE.update(fields)


def _log(line: str) -> None:
    line = line.rstrip()
    if line:
        with _LOCK:
            _LOG.append(line[:400])


def start() -> bool:
    """Start the install in a background thread → False when one is
    already running (the route's 409)."""
    global _THREAD
    with _LOCK:
        if _STATE["state"] == "running":
            return False
        _STATE.update(
            state="running",
            step=STEP_PACKAGES,
            started=time.time(),
            finished=None,
            error="",
        )
        _LOG.clear()
        _THREAD = threading.Thread(
            target=_install, daemon=True, name="voice-install-local"
        )
        _THREAD.start()
    return True


def wait(timeout: Optional[float] = None) -> None:
    """Join the running install (tests)."""
    thread = _THREAD
    if thread is not None:
        thread.join(timeout)


def reset() -> None:
    """Back to idle (tests). Never while running."""
    with _LOCK:
        if _STATE["state"] == "running":
            return
        _STATE.update(state="idle", step="", started=None, finished=None, error="")
        _LOG.clear()


class _StepFailed(Exception):
    pass


def _shown(argv: Sequence[str]) -> str:
    """``argv`` for the log, the helper's ``-c`` program elided."""
    parts = list(argv)
    if len(parts) > 2 and parts[1] == "-c":
        parts[2] = "…"
    return "$ " + " ".join(parts)


def _run_step(step: str, argv: Sequence[str]) -> None:
    _set(step=step)
    _log(_shown(argv))
    try:
        proc = _spawn(argv)
    except OSError as err:
        raise _StepFailed("%s: could not start %s: %s" % (step, argv[0], err))
    timed_out = threading.Event()

    def _expire():
        timed_out.set()
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    timer = threading.Timer(STEP_TIMEOUT, _expire)
    timer.daemon = True
    timer.start()
    last = ""
    try:
        for line in iter(proc.stdout.readline, ""):
            for part in line.replace("\r", "\n").split("\n"):
                if part.strip():
                    last = part.strip()
                    _log(part)
        code = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise _StepFailed("%s took longer than %d minutes" % (step, STEP_TIMEOUT // 60))
    if code != 0:
        raise _StepFailed("%s failed: %s" % (step, last or "exit %d" % code))


def _install() -> None:
    try:
        voice = _settings().load_settings().voice
        model = _speech.whisper_model(voice.stt_model)
        piper = _speech.piper_voice(voice.tts_voice)
        _run_step(STEP_PACKAGES, install_argv())
        _speech.invalidate()
        _run_step(
            "downloading recognition model (%s)" % model,
            _download_argv("whisper", model),
        )
        _run_step("downloading voice (%s)" % piper, _download_argv("piper", piper))
        _settings().update_settings(voice={"stt_backend": "local"})
        _speech.invalidate()
    except _StepFailed as err:
        _set(state="failed", error=str(err), finished=time.time())
        return
    except Exception as err:  # noqa: BLE001 — never leave it "running"
        _set(
            state="failed",
            error="%s: %s" % (type(err).__name__, err),
            finished=time.time(),
        )
        return
    _log("local models are ready: recognition now uses faster-whisper %s" % model)
    _set(state="done", step="done", finished=time.time())
