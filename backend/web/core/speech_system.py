"""The operating system's own speech engines as a ``core.speech`` backend
(id ``system``, label "This computer's speech engine").

A fresh install has no model files and no API key, and the desktop app's
Chromium cannot do Web Speech recognition at all — so the engines Windows
and macOS already ship are the default: :class:`SystemSTT` and
:class:`SystemTTS` register themselves FIRST in ``speech.BACKENDS``, and
``voice.stt_backend == ""`` ("the first ready one") lands on them wherever
they exist. ``local`` and ``openai`` stay the opt-in upgrades.

Per platform (:func:`platform_kind`):

* ``windows`` / ``wsl`` — .NET ``System.Speech`` through Windows PowerShell
  (``powershell.exe -NoProfile -NonInteractive -EncodedCommand``; WSL reaches
  it through interop). Voice: ``SpeechSynthesizer`` → a WAV file. Recognition:
  ``SpeechRecognitionEngine`` with a ``DictationGrammar`` over a WAV file,
  ``Recognize()`` until the clip runs out (one phrase per call, joined with
  spaces). Scripts are built from constants; every value goes in as a
  single-quoted PowerShell literal (:func:`ps_quote`) and the script travels
  base64-encoded, so no text is ever parsed as code or mangled by
  command-line quoting. On WSL the files live in the WINDOWS temp dir
  (resolved once by the probe, mapped with ``wslpath -u``) so PowerShell can
  read them; they are deleted after each call.
* ``mac`` — voice: ``say -o <wav> --data-format=LEI16@22050 [-v voice] -f
  <text file>`` (the text never rides argv). Recognition: Apple's Speech
  framework through PyObjC (``pyobjc-framework-Speech``, a main dependency on
  macOS), on-device when the recognizer supports it. It runs in a short-lived
  helper process (``python -c … _mac_main``): the framework delivers results
  on the main run loop, which a server worker thread never pumps, and macOS
  may kill a process that asks for Speech Recognition permission without the
  right Info.plist — that must cost a helper, never the server. The one
  precondition is **Dictation**: the probe reads it with ``defaults`` (no
  recognition run) and a kLSRErrorDomain 201 at transcribe time says the
  same (:data:`DICTATION_OFF`, remembered for :data:`RETRY_SECONDS`); a
  plain python is authorized without a dialog once it is on, so only a
  denied / restricted status (:data:`MAC_PERMISSION`) blocks besides.
* ``none`` (plain Linux) — not ready: :data:`LINUX_NONE`.

``ready()`` stays cheap: :func:`probe` asks the platform once per process
(voices, recognizer cultures, the Windows temp dir) and caches the verdict;
a failed probe is retried after :data:`RETRY_SECONDS`, and a failed call
drops the cache so the next poll asks again. Under pytest nothing is spawned
unless ``MINDFLOCK_SPEECH_PROBE=1`` (the suite reads ``GET /api/voice`` and
the doctor all over the place). Tests replace :func:`_run` (every
subprocess), :func:`_sys_platform`, :func:`_read_proc_version`,
:func:`_which` and :func:`_mac_helper`.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from backend.web.core import speech as _speech

__all__ = [
    "LABEL",
    "Probe",
    "SystemSTT",
    "SystemTTS",
    "describe",
    "mac_probe",
    "mac_transcribe",
    "platform_kind",
    "probe",
    "ps_quote",
    "reset",
    "stt_script",
    "tts_script",
]

LABEL = "This computer's speech engine"
#: One PowerShell / ``say`` call.
TIMEOUT = 20.0
#: The macOS recognizer's own wait for a final result; the helper process
#: gets :data:`MAC_HELPER_SLACK` on top for its start-up.
MAC_STT_TIMEOUT = 30.0
MAC_HELPER_SLACK = 15.0
#: How long a failed probe is believed before the next poll asks again.
RETRY_SECONDS = 60.0
WIN_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
NEEDS_WAV = "the built-in recognizer needs a WAV clip (audio/wav): reload the page"
LINUX_NONE = (
    "no built-in speech engine on Linux: local models (uv sync --group voice)"
    " or an OpenAI-compatible host"
)
MAC_PERMISSION = (
    "needs permission: System Settings → Privacy & Security → Speech Recognition"
)
#: Dictation off (the ``defaults`` keys below, or kLSRErrorDomain 201
#: "Siri and Dictation are disabled" from a recognition run).
DICTATION_OFF = (
    "Turn on Dictation: System Settings → Keyboard → Dictation"
    " — macOS recognition uses it"
)
#: ``defaults read <domain> <key>`` == 1 means Dictation is on; the first key
#: is absent until Dictation was ever turned on, the second is the other sign.
_DICTATION_KEYS = (
    ("com.apple.assistant.support", "Dictation Enabled"),
    ("com.apple.HIToolbox", "AppleDictationAutoEnable"),
)
#: How long the helper pumps the run loop for an authorization answer.
MAC_AUTH_WAIT = 3.0
NO_PYOBJC = (
    "Apple's Speech framework is not importable (pyobjc-framework-Speech):"
    " reinstall MindFlock"
)
NOT_PROBED = (
    "the built-in speech engine is not probed under pytest"
    " (MINDFLOCK_SPEECH_PROBE=1 allows it)"
)

_WAV_MIMES = ("audio/wav", "audio/x-wav", "audio/wave")
#: What Apple's recognizer decodes (AVFoundation): WAV and MP4/M4A.
_MAC_MIMES = _WAV_MIMES + ("audio/mp4", "audio/x-m4a")
#: SFSpeechRecognizerAuthorizationStatus.
_MAC_AUTH = {0: "notDetermined", 1: "denied", 2: "restricted", 3: "authorized"}
_PLATFORM_WORD = {"windows": "Windows", "wsl": "Windows (from WSL)", "mac": "macOS"}


# --------------------------------------------------------------------------- #
# Seams
# --------------------------------------------------------------------------- #
def _sys_platform() -> str:
    return sys.platform


def _read_proc_version() -> str:
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _isfile(path: str) -> bool:
    return os.path.isfile(path)


def _now() -> float:
    return time.monotonic()


def _run(
    argv: Sequence[str],
    timeout: float,
    cwd: Optional[str] = None,
) -> Tuple[int, bytes, bytes]:
    """One subprocess → ``(returncode, stdout, stderr)``. Raises
    ``subprocess.TimeoutExpired`` / ``OSError`` like ``subprocess.run``."""
    kwargs: dict = {}
    if os.name == "nt":  # no console window flashing up from a GUI server
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        cwd=cwd,
        check=False,
        **kwargs,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _probe_allowed() -> bool:
    from backend.config.home_guard import under_pytest

    return os.environ.get("MINDFLOCK_SPEECH_PROBE") == "1" or not under_pytest()


# --------------------------------------------------------------------------- #
# The platform
# --------------------------------------------------------------------------- #
def powershell_path(kind: str) -> str:
    """The Windows PowerShell to run on ``kind`` ("" when there is none)."""
    if kind == "windows":
        return _which("powershell") or _which("powershell.exe") or "powershell.exe"
    if kind == "wsl":
        return _which("powershell.exe") or (
            WIN_POWERSHELL if _isfile(WIN_POWERSHELL) else ""
        )
    return ""


def platform_kind() -> str:
    """``"windows"``, ``"wsl"`` (Linux under WSL with a reachable
    ``powershell.exe``), ``"mac"`` or ``"none"``. Cheap: no subprocess."""
    plat = _sys_platform()
    if plat == "win32":
        return "windows"
    if plat == "darwin":
        return "mac"
    if plat.startswith("linux"):
        ver = _read_proc_version().lower()
        if ("microsoft" in ver or "wsl" in ver) and powershell_path("wsl"):
            return "wsl"
    return "none"


# --------------------------------------------------------------------------- #
# PowerShell scripts
# --------------------------------------------------------------------------- #
#: PowerShell ends a single-quoted literal at any of these, not just ``'``.
_PS_QUOTES = {0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0: None}

_PS_HEAD = (
    "$ErrorActionPreference = 'Stop'\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
    "try {\n"
    "Add-Type -AssemblyName System.Speech\n"
)
#: Every failure is one ``E<tab>message`` line on stdout and exit 1, so the
#: reason never has to be dug out of PowerShell's CLIXML stderr.
_PS_TAIL = "} catch {\n'E' + [char]9 + $_.Exception.Message\nexit 1\n}\n"


def ps_quote(value: str) -> str:
    """``value`` as a single-quoted PowerShell literal: the typographic
    single quotes PowerShell also treats as delimiters become ``'``, every
    ``'`` is doubled, NULs are dropped. Nothing inside is ever expanded."""
    text = str(value).translate(_PS_QUOTES)
    return "'" + text.replace("'", "''") + "'"


def probe_script() -> str:
    """Voices (``V``), recognizer cultures (``R``) and the temp dir (``T``),
    one tab-separated line each."""
    return (
        _PS_HEAD + "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.GetInstalledVoices() | ForEach-Object { 'V' + [char]9 + $_.VoiceInfo.Name }\n"
        "$s.Dispose()\n"
        "[System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()"
        " | ForEach-Object { 'R' + [char]9 + $_.Culture.Name }\n"
        "'T' + [char]9 + (Get-Item -LiteralPath ([System.IO.Path]::GetTempPath())).FullName\n"
        + _PS_TAIL
    )


def tts_script(path: str, text: str, voice: str = "") -> str:
    """Say ``text`` into the WAV file at the Windows ``path``."""
    lines = ["$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"]
    if voice:
        lines.append("$s.SelectVoice(%s)\n" % ps_quote(voice))
    lines.append("$s.SetOutputToWaveFile(%s)\n" % ps_quote(path))
    lines.append("$s.Speak(%s)\n" % ps_quote(text))
    lines.append("$s.Dispose()\n'OK'\n")
    return _PS_HEAD + "".join(lines) + _PS_TAIL


def stt_script(path: str, culture: str = "") -> str:
    """Recognise the WAV file at the Windows ``path``: one ``P`` line per
    phrase. ``Recognize()`` throws ``InvalidOperationException`` once the
    file is used up (or returns ``$null``); either ends the loop."""
    engine = "New-Object System.Speech.Recognition.SpeechRecognitionEngine"
    if culture:
        engine += "([System.Globalization.CultureInfo]%s)" % ps_quote(culture)
    return (
        _PS_HEAD + "$e = " + engine + "\n"
        "$e.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))\n"
        "$e.SetInputToWaveFile(%s)\n" % ps_quote(path)
        + "for ($n = 0; $n -lt 200; $n++) {\n"
        "try { $r = $e.Recognize() } catch [System.InvalidOperationException] { break }\n"
        "if ($r -eq $null) { break }\n"
        "'P' + [char]9 + $r.Text\n"
        "}\n"
        "$e.Dispose()\n" + _PS_TAIL
    )


def _ps_argv(ps: str, script: str) -> List[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _lines(out: bytes, tag: str) -> List[str]:
    text = out.decode("utf-8-sig", "replace")
    want = tag + "\t"
    return [ln[len(want) :].strip() for ln in text.splitlines() if ln.startswith(want)]


def _powershell(kind: str, script: str, cwd: Optional[str] = None) -> bytes:
    """Run ``script``; its stdout, or a :class:`SpeechError` with the reason."""
    ps = powershell_path(kind)
    if not ps:
        raise _speech.SpeechError("PowerShell is not reachable from WSL")
    try:
        code, out, err = _run(_ps_argv(ps, script), TIMEOUT, cwd)
    except subprocess.TimeoutExpired as exc:
        raise _speech.SpeechError(
            "the Windows speech engine took longer than %d s" % TIMEOUT
        ) from exc
    except OSError as exc:
        raise _speech.SpeechError("could not start PowerShell: %s" % exc) from exc
    if code != 0:
        reason = " ".join(_lines(out, "E")) or _clixml_error(err) or "exit %d" % code
        raise _speech.SpeechError("the Windows speech engine failed: %s" % reason)
    return out


def _clixml_error(err: bytes) -> str:
    text = err.decode("utf-8", "replace")
    parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
    joined = " ".join(p.replace("_x000D__x000A_", " ") for p in parts)
    return re.sub(r"\s+", " ", joined).strip()[:300] or text.strip()[:300]


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
@dataclass
class Probe:
    """What this machine's built-in engine can do (cached per process)."""

    platform: str
    stt_ok: bool = False
    stt_detail: str = ""
    tts_ok: bool = False
    tts_detail: str = ""
    voices: List[str] = field(default_factory=list)
    recognizers: List[str] = field(default_factory=list)
    #: WSL: the Windows temp dir as Windows (``C:\\…``) and Linux paths.
    win_temp: str = ""
    unix_temp: str = ""
    failed: bool = False
    at: float = 0.0


_PROBE: Optional[Probe] = None
_PROBE_LOCK = threading.Lock()


def reset() -> None:
    """Forget the cached probe (a failed call, tests)."""
    global _PROBE
    with _PROBE_LOCK:
        _PROBE = None


def probe() -> Probe:
    """The cached verdict for this machine, probing at most once per process
    (again :data:`RETRY_SECONDS` after a failure)."""
    global _PROBE
    kind = platform_kind()
    if kind == "none":
        return Probe("none", False, LINUX_NONE, False, LINUX_NONE)
    if not _probe_allowed():
        return Probe(kind, False, NOT_PROBED, False, NOT_PROBED)
    with _PROBE_LOCK:
        cached = _PROBE
        if cached is not None and cached.platform == kind:
            if not cached.failed or _now() - cached.at < RETRY_SECONDS:
                return cached
        fresh = _probe_mac() if kind == "mac" else _probe_windows(kind)
        fresh.at = _now()
        _PROBE = fresh
        return fresh


def _failed_probe(kind: str, why: str) -> Probe:
    return Probe(kind, False, why, False, why, failed=True)


def _probe_windows(kind: str) -> Probe:
    try:
        out = _powershell(kind, probe_script(), _ps_cwd(kind, ""))
    except _speech.SpeechError as err:
        return _failed_probe(kind, str(err))
    voices = _lines(out, "V")
    recognizers = _lines(out, "R")
    temps = _lines(out, "T")
    found = Probe(kind, voices=voices, recognizers=recognizers)
    if kind == "wsl":
        win = temps[0] if temps else ""
        unix = _wslpath_u(win) if win else ""
        if not unix:
            return _failed_probe(
                kind, "could not find the Windows temp folder from WSL"
            )
        found.win_temp, found.unix_temp = win.rstrip("\\"), unix
    where = _PLATFORM_WORD[kind]
    found.stt_ok = bool(recognizers)
    found.stt_detail = (
        "%s speech recognition (%s)" % (where, ", ".join(recognizers))
        if recognizers
        else "%s has no speech recognizer installed: Settings → Time & language → Speech"
        % where
    )
    found.tts_ok = bool(voices)
    found.tts_detail = (
        "%s voices: %s" % (where, ", ".join(voices))
        if voices
        else "%s has no voices installed" % where
    )
    return found


def _wslpath_u(win: str) -> str:
    try:
        code, out, _err = _run(["wslpath", "-u", win], 5.0)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    path = out.decode("utf-8", "replace").strip()
    return path if code == 0 and os.path.isabs(path) else ""


def _ps_cwd(kind: str, unix_temp: str) -> Optional[str]:
    """WSL: a Windows-side cwd, or powershell.exe warns about a UNC path."""
    if kind != "wsl":
        return None
    if unix_temp:
        return unix_temp
    return "/mnt/c" if os.path.isdir("/mnt/c") else None


def _probe_mac() -> Probe:
    found = Probe("mac")
    try:
        info = _mac_helper(["probe"], MAC_HELPER_SLACK)
    except _speech.SpeechError as err:
        info = {"ok": False, "detail": str(err), "failed": True}
    found.stt_ok = bool(info.get("ok"))
    found.stt_detail = str(info.get("detail") or "")
    found.recognizers = [str(x) for x in info.get("locales") or []]
    found.failed = bool(info.get("failed"))
    if found.stt_ok and not _dictation_on():
        # Retried after RETRY_SECONDS, so turning it on is picked up.
        found.stt_ok, found.stt_detail, found.failed = False, DICTATION_OFF, True
    try:
        found.voices = _say_voices()
    except _speech.SpeechError as err:
        found.tts_detail, found.failed = str(err), True
    else:
        found.tts_ok = bool(found.voices)
        found.tts_detail = (
            "macOS voices: %s" % ", ".join(found.voices[:12])
            + (
                " (+%d more)" % (len(found.voices) - 12)
                if len(found.voices) > 12
                else ""
            )
            if found.voices
            else "macOS has no voices for say"
        )
    return found


def _dictation_on() -> bool:
    """Whether macOS Dictation is on, read from its preferences (cheap: no
    recognition run). Unknown (``defaults`` missing or failing) reads as on —
    a recognition run then says for sure."""
    tool = _which("defaults") or "/usr/bin/defaults"
    unknown = True
    for domain, key in _DICTATION_KEYS:
        try:
            code, out, _err = _run([tool, "read", domain, key], 5.0)
        except (OSError, subprocess.TimeoutExpired):
            continue
        unknown = False
        if code == 0 and out.decode("utf-8", "replace").strip() == "1":
            return True
    return unknown


def _work_failed() -> None:
    """A call failed: the next ``ready()`` asks the platform again."""
    reset()


def _stt_off(detail: str) -> None:
    """Remember that recognition is off for a reason only a real run shows
    (Dictation disabled): ``ready()`` says so until the probe is retried
    :data:`RETRY_SECONDS` later."""
    with _PROBE_LOCK:
        if _PROBE is not None:
            _PROBE.stt_ok, _PROBE.stt_detail = False, detail
            _PROBE.failed, _PROBE.at = True, _now()


def _pick_voice(voice: str, installed: Sequence[str]) -> str:
    """``voice`` when it names an installed voice ("" = the system default).
    ``voice.tts_voice`` is shared by every backend, so ``alloy`` or a piper
    name must not break the built-in engine."""
    want = (voice or "").strip().lower()
    for name in installed:
        if name.lower() == want:
            return name
    return ""


def _pick_culture(lang: str, recognizers: Sequence[str]) -> str:
    """The recognizer culture for a ``lang`` hint (``en`` → ``en-US``), or
    "" for the engine's default."""
    want = (lang or "").strip().lower().replace("_", "-")
    if not want:
        return ""
    for name in recognizers:
        if name.lower() == want:
            return name
    for name in recognizers:
        if name.lower().startswith(want + "-"):
            return name
    return ""


def _is_wav(audio: bytes, mime: str) -> bool:
    return (
        _speech.mime_of(mime) in _WAV_MIMES
        and audio[:4] == b"RIFF"
        and audio[8:12] == b"WAVE"
    )


# --------------------------------------------------------------------------- #
# Windows / WSL work
# --------------------------------------------------------------------------- #
def _temp_pair(found: Probe, kind: str, suffix: str) -> Tuple[str, str]:
    """A fresh temp file name as ``(path this process uses, path PowerShell
    uses)`` — the same file seen from Linux and from Windows on WSL."""
    name = "mindflock-speech-%s%s" % (uuid.uuid4().hex, suffix)
    if kind == "wsl":
        return os.path.join(found.unix_temp, name), found.win_temp + "\\" + name
    path = os.path.join(tempfile.gettempdir(), name)
    return path, path


def _unlink(*paths: str) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _windows_speak(found: Probe, text: str, voice: str) -> Tuple[bytes, str]:
    kind = found.platform
    local, remote = _temp_pair(found, kind, ".wav")
    try:
        _powershell(
            kind,
            tts_script(remote, text, _pick_voice(voice, found.voices)),
            _ps_cwd(kind, found.unix_temp),
        )
        try:
            with open(local, "rb") as fh:
                data = fh.read()
        except OSError as err:
            raise _speech.SpeechError(
                "the Windows speech engine wrote no audio (%s)" % err
            ) from err
    finally:
        _unlink(local)
    if not data:
        raise _speech.SpeechError("the Windows speech engine wrote no audio")
    return data, "audio/wav"


def _windows_transcribe(found: Probe, audio: bytes, lang: str) -> str:
    kind = found.platform
    local, remote = _temp_pair(found, kind, ".wav")
    try:
        with open(local, "wb") as fh:
            fh.write(audio)
        out = _powershell(
            kind,
            stt_script(remote, _pick_culture(lang, found.recognizers)),
            _ps_cwd(kind, found.unix_temp),
        )
    except OSError as err:
        raise _speech.SpeechError("could not write the clip: %s" % err) from err
    finally:
        _unlink(local)
    return " ".join(p for p in _lines(out, "P") if p).strip()


# --------------------------------------------------------------------------- #
# macOS work
# --------------------------------------------------------------------------- #
#: ``say -v ?``: ``Name<spaces>lang_REGION<spaces># sample sentence``. A
#: name may hold spaces and parentheses ("Eddy (English (US))"), and a long
#: one is followed by a single space, so the name is everything before the
#: locale token.
_SAY_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}[_-][A-Za-z0-9_-]+)\s+#")


def _say_voices() -> List[str]:
    say = _which("say")
    if not say:
        raise _speech.SpeechError("macOS `say` is not on PATH")
    try:
        code, out, err = _run([say, "-v", "?"], TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _speech.SpeechError("could not list macOS voices: %s" % exc) from exc
    if code != 0:
        raise _speech.SpeechError(
            "say -v ? failed: %s" % err.decode("utf-8", "replace").strip()[:200]
        )
    voices: List[str] = []
    for line in out.decode("utf-8", "replace").splitlines():
        m = _SAY_LINE.match(line)
        if m:
            voices.append(m.group("name").strip())
    return voices


def _mac_speak(found: Probe, text: str, voice: str) -> Tuple[bytes, str]:
    say = _which("say") or "say"
    tmpdir = tempfile.mkdtemp(prefix="mindflock-say-")
    wav = os.path.join(tmpdir, "out.wav")
    src = os.path.join(tmpdir, "text.txt")
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(text)
        argv = [say, "-o", wav, "--data-format=LEI16@22050"]
        picked = _pick_voice(voice, found.voices)
        if picked:
            argv += ["-v", picked]
        argv += ["-f", src]
        try:
            code, _out, err = _run(argv, TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise _speech.SpeechError("say took longer than %d s" % TIMEOUT) from exc
        except OSError as exc:
            raise _speech.SpeechError("could not start say: %s" % exc) from exc
        if code != 0:
            raise _speech.SpeechError(
                "say failed: %s" % err.decode("utf-8", "replace").strip()[:200]
            )
        try:
            with open(wav, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise _speech.SpeechError("say wrote no audio (%s)" % exc) from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not data:
        raise _speech.SpeechError("say wrote no audio")
    return data, "audio/wav"


def _mac_helper(args: List[str], timeout: float) -> dict:
    """Run :func:`_mac_main` in a helper process → its JSON answer. A helper
    killed by a signal is macOS refusing the permission request (the app
    that started MindFlock has no speech-recognition usage description)."""
    argv = [
        sys.executable,
        "-c",
        "from backend.web.core.speech_system import _mac_main; _mac_main()",
    ] + list(args)
    try:
        code, out, err = _run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise _speech.SpeechError(
            "Apple speech recognition took longer than %d s" % timeout
        ) from exc
    except OSError as exc:
        raise _speech.SpeechError("could not start the recognizer: %s" % exc) from exc
    if code < 0:
        raise _speech.SpeechError(
            "macOS stopped the recognizer (signal %d): the app that started"
            " MindFlock may not be allowed to use speech recognition — %s"
            % (-code, MAC_PERMISSION)
        )
    lines = out.decode("utf-8", "replace").strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        reason = err.decode("utf-8", "replace").strip().splitlines()
        raise _speech.SpeechError(
            "the recognizer helper failed: %s"
            % (reason[-1][:200] if reason else "exit %d" % code)
        )
    return answer


def _mac_transcribe_call(audio: bytes, mime: str, lang: str) -> str:
    fd, path = tempfile.mkstemp(prefix="mindflock-clip-", suffix=_speech._ext(mime))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        args = ["transcribe", path] + ([lang] if lang else [])
        answer = _mac_helper(args, MAC_STT_TIMEOUT + MAC_HELPER_SLACK)
    finally:
        _unlink(path)
    if answer.get("error"):
        raise _speech.SpeechError(str(answer["error"]))
    return str(answer.get("text") or "").strip()


# The helper's side: plain functions over PyObjC, so tests can run them in
# process against fake ``Speech`` / ``Foundation`` modules.
def _objc():
    """``(Speech, Foundation)`` or a :class:`SpeechError` saying why not."""
    try:
        return importlib.import_module("Speech"), importlib.import_module("Foundation")
    except Exception as err:  # noqa: BLE001 — absent or broken PyObjC
        raise _speech.SpeechError(NO_PYOBJC) from err


def _pump(foundation, done, seconds: float) -> None:
    """Run the current run loop until ``done()`` or ``seconds`` pass."""
    deadline = time.monotonic() + seconds
    loop = foundation.NSRunLoop.currentRunLoop()
    while not done() and time.monotonic() < deadline:
        loop.runMode_beforeDate_(
            foundation.NSDefaultRunLoopMode,
            foundation.NSDate.dateWithTimeIntervalSinceNow_(0.1),
        )


def _mac_auth(speech_mod, foundation, ask: bool) -> str:
    status = _MAC_AUTH.get(
        int(speech_mod.SFSpeechRecognizer.authorizationStatus()), "unknown"
    )
    if status != "notDetermined" or not ask:
        return status
    got: dict = {}

    def handler(value):
        got["status"] = value

    speech_mod.SFSpeechRecognizer.requestAuthorization_(handler)
    _pump(foundation, lambda: "status" in got, MAC_AUTH_WAIT)
    if "status" in got:
        return _MAC_AUTH.get(int(got["status"]), "unknown")
    return _MAC_AUTH.get(
        int(speech_mod.SFSpeechRecognizer.authorizationStatus()), "unknown"
    )


def mac_probe() -> dict:
    """``{"ok", "detail", "auth", "locales"}`` for Apple's recognizer. Only
    denied / restricted is not ready; an undetermined answer is asked for
    once, waiting at most :data:`MAC_AUTH_WAIT` (with Dictation on macOS
    answers at once, without a dialog). Dictation itself is checked by the
    caller (:func:`_dictation_on`)."""
    try:
        speech_mod, foundation = _objc()
        auth = _mac_auth(speech_mod, foundation, ask=True)
    except _speech.SpeechError as err:
        return {"ok": False, "detail": str(err), "auth": "", "locales": []}
    except Exception as err:  # noqa: BLE001
        return {
            "ok": False,
            "detail": "Apple speech recognition: %s" % err,
            "auth": "",
            "locales": [],
        }
    try:
        locales = sorted(
            str(loc.localeIdentifier()).replace("_", "-")
            for loc in (speech_mod.SFSpeechRecognizer.supportedLocales() or [])
        )
    except Exception:  # noqa: BLE001
        locales = []
    if auth in ("denied", "restricted"):
        return {
            "ok": False,
            "detail": "Apple speech recognition " + MAC_PERMISSION,
            "auth": auth,
            "locales": locales,
        }
    return {
        "ok": True,
        "detail": "Apple speech recognition on this Mac",
        "auth": auth,
        "locales": locales,
    }


def mac_transcribe(path: str, lang: str = "", timeout: float = MAC_STT_TIMEOUT) -> str:
    """Recognise the audio file at ``path`` with Apple's Speech framework —
    on-device when supported. Must run on a thread whose run loop it may
    pump (the helper's main thread)."""
    speech_mod, foundation = _objc()
    auth = _mac_auth(speech_mod, foundation, ask=True)
    if auth in ("denied", "restricted"):
        raise _speech.SpeechError("Apple speech recognition " + MAC_PERMISSION)
    tag = (lang or "").strip().replace("-", "_")
    locale = (
        foundation.NSLocale.alloc().initWithLocaleIdentifier_(tag)
        if tag
        else foundation.NSLocale.currentLocale()
    )
    recognizer = speech_mod.SFSpeechRecognizer.alloc().initWithLocale_(locale)
    if recognizer is None:  # no recognizer for that language: the default one
        recognizer = speech_mod.SFSpeechRecognizer.alloc().init()
    if recognizer is None or not recognizer.isAvailable():
        raise _speech.SpeechError("Apple speech recognition is not available right now")
    request = speech_mod.SFSpeechURLRecognitionRequest.alloc().initWithURL_(
        foundation.NSURL.fileURLWithPath_(path)
    )
    try:
        if recognizer.supportsOnDeviceRecognition():
            request.setRequiresOnDeviceRecognition_(True)
    except Exception:  # noqa: BLE001 — older macOS: server recognition
        pass
    state: dict = {}

    def handler(result, error):
        if result is not None:
            try:
                state["text"] = str(result.bestTranscription().formattedString())
                if result.isFinal():
                    state["done"] = True
            except Exception as err:  # noqa: BLE001
                state["error"], state["done"] = str(err), True
        if error is not None:
            try:
                state["error"] = str(error.localizedDescription())
            except Exception:  # noqa: BLE001
                state["error"] = str(error)
            try:
                state["code"] = (str(error.domain()), int(error.code()))
            except Exception:  # noqa: BLE001
                pass
            state["done"] = True

    task = recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)
    _pump(foundation, lambda: bool(state.get("done")), timeout)
    if not state.get("done"):
        try:
            task.cancel()
        except Exception:  # noqa: BLE001
            pass
        if state.get("text"):
            return state["text"].strip()
        raise _speech.SpeechError(
            "Apple speech recognition took longer than %d s" % timeout
        )
    if state.get("text"):
        return state["text"].strip()
    error = state.get("error") or ""
    if (
        state.get("code") == ("kLSRErrorDomain", 201)
        or "siri and dictation are disabled" in error.lower()
    ):
        raise _speech.SpeechError(DICTATION_OFF)
    if not error or "no speech" in error.lower():
        return ""
    raise _speech.SpeechError("Apple speech recognition failed: %s" % error)


def _mac_main(argv: Optional[List[str]] = None) -> None:
    """The helper process: ``probe`` or ``transcribe <path> [lang]`` → one
    JSON line on stdout."""
    args = list(sys.argv[1:] if argv is None else argv)
    op = args[0] if args else ""
    try:
        if op == "probe":
            answer: dict = mac_probe()
        elif op == "transcribe" and len(args) >= 2:
            answer = {"text": mac_transcribe(args[1], args[2] if len(args) > 2 else "")}
        else:
            answer = {"error": "usage: probe | transcribe <path> [lang]"}
    except _speech.SpeechError as err:
        answer = {"error": str(err)}
    except Exception as err:  # noqa: BLE001
        answer = {"error": "%s: %s" % (type(err).__name__, err)}
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# The backends
# --------------------------------------------------------------------------- #
class SystemSTT(_speech.Backend):
    id = "system"
    kind = "stt"
    label = LABEL

    def ready(self) -> Tuple[bool, str]:
        found = probe()
        return found.stt_ok, found.stt_detail

    def transcribe(self, audio: bytes, mime: str, lang: str = "") -> str:
        found = probe()
        if not found.stt_ok:
            raise _speech.SpeechError(found.stt_detail)
        try:
            if found.platform == "mac":
                if _speech.mime_of(mime) not in _MAC_MIMES:
                    raise _speech.SpeechError(NEEDS_WAV)
                return _mac_transcribe_call(audio, mime, lang)
            if not _is_wav(audio, mime):
                raise _speech.SpeechError(NEEDS_WAV)
            return _windows_transcribe(found, audio, lang)
        except _speech.SpeechError as err:
            if str(err) == DICTATION_OFF:
                _stt_off(DICTATION_OFF)
            elif str(err) != NEEDS_WAV:
                _work_failed()
            raise


class SystemTTS(_speech.Backend):
    id = "system"
    kind = "tts"
    label = LABEL

    def ready(self) -> Tuple[bool, str]:
        found = probe()
        return found.tts_ok, found.tts_detail

    def speak(self, text: str, voice: str = "") -> Tuple[bytes, str]:
        found = probe()
        if not found.tts_ok:
            raise _speech.SpeechError(found.tts_detail)
        wanted = voice or _speech._voice().tts_voice
        try:
            if found.platform == "mac":
                return _mac_speak(found, text, wanted)
            return _windows_speak(found, text, wanted)
        except _speech.SpeechError:
            _work_failed()
            raise


def describe() -> dict:
    """``GET /api/voice``'s ``system`` block::

    {"available": bool, "platform": "windows"|"wsl"|"mac"|"none",
     "detail": str, "voices": [names], "recognizers": [culture tags]}
    """
    found = probe()
    if found.platform == "none":
        detail = LINUX_NONE
    elif found.stt_detail == found.tts_detail:
        detail = found.stt_detail
    else:
        detail = "Recognition: %s. Voice: %s." % (found.stt_detail, found.tts_detail)
    return {
        "available": bool(found.stt_ok or found.tts_ok),
        "platform": found.platform,
        "detail": detail,
        "voices": list(found.voices),
        "recognizers": list(found.recognizers),
    }


def _register() -> None:
    """Put the built-in engine FIRST for both kinds (once): the fresh-install
    default wherever it exists."""
    for kind, cls in (("stt", SystemSTT), ("tts", SystemTTS)):
        group = _speech.BACKENDS.setdefault(kind, [])
        if not any(isinstance(b, cls) for b in group):
            group.insert(0, cls())


_register()
