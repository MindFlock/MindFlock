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
   ``voice.stt_model`` for English (``speech.whisper_model``: ``small.en``,
   the English-only twin of ``small``, unless it names a size) into
   ``speech.voice_dir()/whisper``, the cache ``LocalSTT`` loads from, so the
   first phrase is not a download — the one-shot clip and the accurate pass
   both run it. Another language's ``small`` is not fetched here (as its
   ``base`` stream model is not): its first phrase downloads it. An install
   from before the twin has ``small``; ``LocalSTT`` keeps using it and
   fetches ``small.en`` in the background on the first warm or phrase
   (``speech.english_default_model``); then **downloading streaming model
   (<name>)**, the live transcript's (``speech.stream_whisper_model``:
   ``base.en``, ~75 MB) — skipped when it is the same model.
3. **downloading voice (<name>)** — the piper voice of ``voice.tts_voice``
   (``en_US-lessac-medium`` unless it names a piper voice), fetched the way
   ``LocalTTS`` fetches one (``speech.fetch_piper_voice``); then
   **downloading the built-in voice (Kokoro)** — ``kokoro-onnx``'s model
   files (~120 MB, sha256-pinned: ``speech_kokoro.fetch_model``) into
   ``speech.voice_dir()/models/kokoro``, the voice assistant's and the
   chain's first local fallback. ``kokoro-onnx`` is one of step 1's
   packages.

The download steps run in a helper process (:func:`_download_main`, the same
interpreter): the packages were installed after this server imported
anything, and a model download must never cost the server its memory.
Every step's output (stdout + stderr) feeds :func:`status`'s ``log`` (the
last :data:`LOG_LINES` lines); a step that exits non-zero, cannot start or
outlives :data:`STEP_TIMEOUT` fails the whole install with its reason.

On success ``voice.stt_backend`` becomes ``"local"`` — the person asked
for accuracy; the voice backend stays whatever it was — and
``speech.invalidate()`` drops the import caches so ``GET /api/voice``
reports ``local`` ready without a restart.

**Surviving a release.** The packages go into the server's venv, and every
engine rebuild (``uv tool install --force``, install.sh, the desktop app's
one-click update) rebuilt that venv from ``mindflock[web]`` alone — each
release cost the owner this install again. Two layers, the ones the Database
explorer's drivers use (``self_update``): step 1's success is recorded
(``self_update.record_voice_packages`` → ``~/.mindflock/voice/packages.json``)
and the server's updater / ``mindflock update`` carry each recorded package
as ``--with`` (``pipx inject`` after pipx); and :func:`start_self_heal` — at
boot and on ``GET /api/voice`` — reinstalls the recorded packages when the
venv lost them (step 1 only, as a ``running`` install the Settings screen
follows; the models on disk are untouched). A heal whose package no longer
exists on the index drops the record, so the next boot does not trip again.

**Moonshine** (``start(what="moonshine")``, ``core.speech_moonshine``): the
fast streaming recognizer, a separate install — ``moonshine-voice`` into the
same venv (:data:`MOONSHINE_PACKAGES`, with its version floor), then its ``tiny-streaming`` model
into ``speech.voice_dir()/moonshine`` by the same helper process
(``moonshine tiny-streaming``). Recorded beside faster-whisper's packages
(the record is merged, never replaced), so it survives a release and the
self-heal puts it back too. It changes no setting: whether streams use it is
the caller's call (``speech_moonshine.available``).

**Kokoro** (``start(what="kokoro")``, ``core.speech_kokoro``): the built-in
voice alone — ``kokoro-onnx`` (:data:`KOKORO_PACKAGES`) and its model files,
for someone who installed the local models before it was part of them (the
assistant's "install the voice" prompt). Recorded and healed like
Moonshine; it changes no setting (the assistant uses it as soon as it is
ready; ``voice.tts_backend`` is the person's).

Nothing here runs for real under pytest: :func:`_spawn` (every subprocess)
refuses there, and tests replace it.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Deque, List, Optional, Sequence

from backend.web.core import speech as _speech

__all__ = [
    "KOKORO",
    "KOKORO_PACKAGES",
    "MOONSHINE",
    "MOONSHINE_PACKAGES",
    "PACKAGES",
    "install_argv",
    "missing_recorded_packages",
    "start",
    "start_self_heal",
    "status",
]

PACKAGES = ("faster-whisper", "piper-tts", "kokoro-onnx")
#: ``start(what=...)``: the local models (default), Moonshine streaming, or
#: the built-in Kokoro voice alone.
LOCAL, MOONSHINE, KOKORO = "local", "moonshine", "kokoro"
WHATS = (LOCAL, MOONSHINE, KOKORO)
#: Moonshine's packages, as recorded (plain names: the record refuses specs).
MOONSHINE_PACKAGES = ("moonshine-voice",)
#: The built-in voice's package (``core.speech_kokoro``).
KOKORO_PACKAGES = ("kokoro-onnx",)
#: What gets installed for a recorded name: the floor is the API
#: ``core.speech_moonshine`` / ``core.speech_kokoro`` was written and
#: measured against (Kokoro: ``Kokoro.from_session``, the espeak loader).
_INSTALL_SPEC = {
    "moonshine-voice": "moonshine-voice>=0.1.6",
    "kokoro-onnx": "kokoro-onnx>=0.6.1",
}
#: The module each recorded package provides (the self-heal's probe).
_MODULES = {
    "faster-whisper": "faster_whisper",
    "piper-tts": "piper",
    "moonshine-voice": "moonshine_voice",
    "kokoro-onnx": "kokoro_onnx",
}
#: The Kokoro download step's name.
STEP_KOKORO = "downloading the built-in voice (Kokoro, ~120 MB)"
#: How many output lines ``status()`` keeps.
LOG_LINES = 20
#: One step's limit (a slow network pulling ~500 MB of model).
STEP_TIMEOUT = 30 * 60.0

STEP_PACKAGES = "installing packages"
STEP_HEAL = "reinstalling the local models' packages after the update"
#: The installer's "this package is gone from the index" (uv and pip).
_NO_SUCH_PACKAGE = re.compile(
    r"No matching distribution|not found in the package registry|No solution found",
    re.I,
)


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


def _self_update():
    from backend.web.core import self_update

    return self_update


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


def install_argv(packages: Sequence[str] = PACKAGES) -> List[str]:
    """Step 1's command line for ``packages`` (recorded names; one with a
    version floor goes in as its spec, :data:`_INSTALL_SPEC`)."""
    specs = [_INSTALL_SPEC.get(p, p) for p in packages]
    uv = _uv()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *specs]
    return [sys.executable, "-m", "pip", "install", *specs]


def _download_argv(kind: str, name: str) -> List[str]:
    return [
        sys.executable,
        "-c",
        "from backend.web.core.speech_install import _download_main; _download_main()",
        kind,
        name,
    ]


def _download_main(argv: Optional[List[str]] = None) -> None:
    """The helper process: ``whisper <name>``, ``piper <name>``,
    ``moonshine <arch>`` or ``kokoro v1.0`` → the download (Kokoro prints
    its progress), a last line saying where it went, exit 0; any failure a
    last line with the reason and exit 1."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 2 or args[0] not in ("whisper", "piper", "moonshine", "kokoro"):
            raise ValueError(
                "usage: whisper <model> | piper <voice> | moonshine <arch> | kokoro v1.0"
            )
        kind, name = args
        if kind == "kokoro":
            from backend.web.core import speech_kokoro as _kokoro

            if name != _kokoro.MODEL_VERSION:
                raise ValueError(
                    "only the Kokoro %s model is supported" % _kokoro.MODEL_VERSION
                )
            where = _kokoro.fetch_model()
        elif kind == "whisper":
            where = _speech.fetch_whisper_model(name)
        elif kind == "moonshine":
            from backend.web.core import speech_moonshine as _moonshine

            if name != _moonshine.ARCH:
                raise ValueError("only the %s model is supported" % _moonshine.ARCH)
            where = _moonshine.fetch_model()
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
    "what": "",
    "step": "",
    "started": None,
    "finished": None,
    "error": "",
}
_LOG: Deque[str] = collections.deque(maxlen=LOG_LINES)
_THREAD: Optional[threading.Thread] = None
#: The self-heal ran (or found nothing to do) this boot.
_HEAL_TRIED = False


def status() -> dict:
    """``{"state": "idle"|"running"|"done"|"failed", "what": "local"|
    "moonshine"|"kokoro"|"" (the last install's :func:`start` ``what``), "step",
    "log": [the last LOG_LINES lines], "started", "finished", "error"}``
    (epoch seconds or None)."""
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


def start(heal: bool = False, what: str = LOCAL) -> bool:
    """Start the install in a background thread → False when one is
    already running (the route's 409). ``heal``: the packages step only
    (:func:`start_self_heal`). ``what``: :data:`LOCAL` (faster-whisper,
    piper and Kokoro and their models), :data:`MOONSHINE` (``moonshine-voice``
    and its streaming model) or :data:`KOKORO` (``kokoro-onnx`` and its
    model); one install at a time either way."""
    global _THREAD
    if what not in WHATS:
        raise ValueError("unknown install: %r" % (what,))
    with _LOCK:
        if _STATE["state"] == "running":
            return False
        _STATE.update(
            state="running",
            what=what,
            step=STEP_HEAL if heal else STEP_PACKAGES,
            started=time.time(),
            finished=None,
            error="",
        )
        _LOG.clear()
        target = _install
        if not heal and what == MOONSHINE:
            target = _install_moonshine
        elif not heal and what == KOKORO:
            target = _install_kokoro
        _THREAD = threading.Thread(
            target=target,
            args=(heal,),
            daemon=True,
            name="voice-install-local",
        )
        _THREAD.start()
    return True


def missing_recorded_packages() -> List[str]:
    """The packages the record says this venv had, when the venv has lost
    any of them (an engine rebuild): ``[]`` with no record, or while every
    recorded runtime still imports (a name with no known module counts as
    present)."""
    packages = _self_update().recorded_voice_packages()
    if not packages:
        return []
    modules = [_MODULES[p] for p in packages if p in _MODULES]
    if all(_speech._installed(m) for m in modules):
        return []
    return packages


def _record(packages: Sequence[str]) -> None:
    """Add ``packages`` to the record (keeping what it had: the local models'
    and Moonshine's installs are separate)."""
    su = _self_update()
    su.record_voice_packages(
        list(dict.fromkeys([*su.recorded_voice_packages(), *packages]))
    )


def start_self_heal() -> bool:
    """Put the recorded packages back after a rebuild that dropped them —
    once per boot (the lifespan's warm-ups and ``GET /api/voice`` both call
    it; cheap when idle) → whether a heal started."""
    global _HEAL_TRIED
    with _LOCK:
        if _HEAL_TRIED:
            return False
        _HEAL_TRIED = True
    try:
        if not missing_recorded_packages():
            return False
    except Exception:  # noqa: BLE001 — a probe must never fail a poll
        return False
    return start(heal=True)


def wait(timeout: Optional[float] = None) -> None:
    """Join the running install (tests)."""
    thread = _THREAD
    if thread is not None:
        thread.join(timeout)


def reset() -> None:
    """Back to idle (tests). Never while running."""
    global _HEAL_TRIED
    with _LOCK:
        if _STATE["state"] == "running":
            return
        _STATE.update(
            state="idle", what="", step="", started=None, finished=None, error=""
        )
        _LOG.clear()
        _HEAL_TRIED = False


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
        raise _StepFailed("%s failed: %s" % (step, _reason(last) or "exit %d" % code))


def _reason(last: str) -> str:
    """A failed step's last output line as its reason, without uv's
    ``Caused by:`` lead-in (its error chain ends on the root cause, which is
    the part a person can act on: "dns error: …", "No space left …")."""
    return re.sub(r"^\s*Caused by:\s*", "", last or "").strip()


def _install(heal: bool = False) -> None:
    try:
        if heal:
            try:
                _run_step(
                    STEP_HEAL, install_argv(missing_recorded_packages() or PACKAGES)
                )
            except _StepFailed as err:
                # Gone from the index: stop carrying it, or every boot and
                # every update would trip over it again.
                if _NO_SUCH_PACKAGE.search(str(err)):
                    _self_update().forget_voice_packages()
                raise
            _speech.invalidate()
            _log("local models are back after the update")
            _set(state="done", step="done", finished=time.time())
            return
        voice = _settings().load_settings().voice
        model = _speech.whisper_model(voice.stt_model)
        stream = _speech.stream_whisper_model(voice.stt_stream_model)
        piper = _speech.piper_voice(voice.tts_voice)
        _run_step(STEP_PACKAGES, install_argv())
        # Remembered, so an engine rebuild carries them (or puts them back).
        _record(PACKAGES)
        _speech.invalidate()
        _run_step(
            "downloading recognition model (%s)" % model,
            _download_argv("whisper", model),
        )
        if stream != model:
            _run_step(
                "downloading streaming model (%s)" % stream,
                _download_argv("whisper", stream),
            )
        _run_step("downloading voice (%s)" % piper, _download_argv("piper", piper))
        _run_step(STEP_KOKORO, _download_argv("kokoro", _kokoro_version()))
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


def _install_moonshine(heal: bool = False) -> None:
    """``start(what="moonshine")``: the package, recorded, then the model
    (``heal`` is never set: a heal is :func:`_install`'s, for every
    recorded package)."""
    try:
        from backend.web.core import speech_moonshine as _moonshine

        _run_step(STEP_PACKAGES, install_argv(MOONSHINE_PACKAGES))
        _record(MOONSHINE_PACKAGES)
        _speech.invalidate()
        _run_step(
            "downloading streaming model (moonshine %s)" % _moonshine.ARCH,
            _download_argv("moonshine", _moonshine.ARCH),
        )
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
    _log("Moonshine streaming recognition is ready (%s)" % _moonshine.ARCH)
    _set(state="done", step="done", finished=time.time())


def _kokoro_version() -> str:
    from backend.web.core import speech_kokoro as _kokoro

    return _kokoro.MODEL_VERSION


def _install_kokoro(heal: bool = False) -> None:  # noqa: ARG001 — as Moonshine's
    """``start(what="kokoro")``: the package, recorded, then the model
    files; no setting changes."""
    try:
        _run_step(STEP_PACKAGES, install_argv(KOKORO_PACKAGES))
        _record(KOKORO_PACKAGES)
        _speech.invalidate()
        _run_step(STEP_KOKORO, _download_argv("kokoro", _kokoro_version()))
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
    _log("the built-in Kokoro voice is ready")
    _set(state="done", step="done", finished=time.time())
