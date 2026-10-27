"""``core.speech_system`` (the OS's own speech engines, backend id ``system``):
the platform probe, the PowerShell script builders, the Windows / WSL round
trips against a fake subprocess runner, ``ready()`` caching, the ``system``
block of ``describe()``, and the macOS branch with PyObjC absent and with
fake ``Speech`` / ``Foundation`` modules.

Nothing here spawns a process except ``test_real_*``, the one bounded smoke
test that runs only where Windows speech is reachable (Windows or WSL).
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import types
import wave

import pytest

from backend.config import settings as S
from backend.web.core import speech
from backend.web.core import speech_system as ss


def _wav(seconds: float = 0.1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


WAV = _wav()


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, request):
    """A fresh probe cache, probing allowed, and — except in the real smoke
    test — a runner that refuses to spawn anything."""
    ss.reset()
    monkeypatch.setenv("MINDFLOCK_SPEECH_PROBE", "1")
    if not request.node.name.startswith("test_real_"):

        def _no_spawn(argv, timeout, cwd=None):
            raise AssertionError("unexpected subprocess: %r" % (argv,))

        monkeypatch.setattr(ss, "_run", _no_spawn)
    yield
    ss.reset()


def _set(**voice):
    S.update_settings(voice=voice)
    S.invalidate()


def _script(argv) -> str:
    """The PowerShell script inside an ``-EncodedCommand`` argv."""
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    return base64.b64decode(argv[4]).decode("utf-16-le")


def _platform(monkeypatch, plat, proc="", which=None, isfile=False):
    monkeypatch.setattr(ss, "_sys_platform", lambda: plat)
    monkeypatch.setattr(ss, "_read_proc_version", lambda: proc)
    monkeypatch.setattr(ss, "_which", lambda name: (which or {}).get(name))
    monkeypatch.setattr(ss, "_isfile", lambda path: isfile)


WSL_PROC = "Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc) #1 SMP"
PS = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
PROBE_OUT = (
    "V\tMicrosoft David Desktop\r\nV\tMicrosoft Zira Desktop\r\n"
    "R\ten-US\r\nT\tC:\\Users\\me\\AppData\\Local\\Temp\\\r\n"
).encode()


class _PowerShell:
    """A fake ``_run`` playing PowerShell (and ``wslpath``) on WSL: the
    probe answers :data:`PROBE_OUT`, a voice script writes ``self.wav`` to
    the mapped file, a recognition script prints ``self.phrases``."""

    def __init__(self, unix_temp):
        self.unix_temp = str(unix_temp)
        self.calls: list = []
        self.wav = WAV
        self.phrases = ["Open a new session", "to fix the login bug"]
        self.fail = None
        self.seen_clip = None

    def _local(self, win_path: str) -> str:
        name = win_path.rsplit("\\", 1)[-1]
        return os.path.join(self.unix_temp, name)

    def __call__(self, argv, timeout, cwd=None):
        self.calls.append((list(argv), timeout, cwd))
        if argv[0] == "wslpath":
            assert argv[1:] == ["-u", "C:\\Users\\me\\AppData\\Local\\Temp\\"]
            return 0, (self.unix_temp + "\n").encode(), b""
        script = _script(argv)
        if self.fail:
            raise self.fail
        if "GetInstalledVoices" in script:
            return 0, PROBE_OUT, b""
        if "SetOutputToWaveFile" in script:
            path = script.split("SetOutputToWaveFile('", 1)[1].split("')", 1)[0]
            with open(self._local(path), "wb") as fh:
                fh.write(self.wav)
            return 0, b"OK\r\n", b""
        if "SetInputToWaveFile" in script:
            path = script.split("SetInputToWaveFile('", 1)[1].split("')", 1)[0]
            with open(self._local(path), "rb") as fh:
                self.seen_clip = fh.read()
            out = "".join("P\t%s\r\n" % p for p in self.phrases)
            return 0, out.encode(), b""
        raise AssertionError("unknown script")

    def scripts(self):
        return [_script(a) for a, _t, _c in self.calls if a[0] != "wslpath"]


@pytest.fixture
def wsl(monkeypatch, tmp_path):
    _platform(monkeypatch, "linux", WSL_PROC, which={"powershell.exe": PS})
    fake = _PowerShell(tmp_path)
    monkeypatch.setattr(ss, "_run", fake)
    return fake


# --------------------------------------------------------------------------- #
# The platform probe
# --------------------------------------------------------------------------- #
class TestPlatformKind:
    def test_windows_and_mac(self, monkeypatch):
        _platform(monkeypatch, "win32")
        assert ss.platform_kind() == "windows"
        _platform(monkeypatch, "darwin")
        assert ss.platform_kind() == "mac"

    def test_wsl_with_powershell_on_path(self, monkeypatch):
        _platform(monkeypatch, "linux", WSL_PROC, which={"powershell.exe": PS})
        assert ss.platform_kind() == "wsl"
        assert ss.powershell_path("wsl") == PS

    def test_wsl_without_path_falls_back_to_the_system32_copy(self, monkeypatch):
        _platform(
            monkeypatch, "linux", "Linux version 5.15 (Microsoft@WSL)", isfile=True
        )
        assert ss.platform_kind() == "wsl"
        assert ss.powershell_path("wsl") == ss.WIN_POWERSHELL

    def test_wsl_without_any_powershell_is_none(self, monkeypatch):
        _platform(monkeypatch, "linux", WSL_PROC)
        assert ss.platform_kind() == "none"

    def test_plain_linux_is_none(self, monkeypatch):
        _platform(
            monkeypatch,
            "linux",
            "Linux version 6.8.0-45-generic",
            which={"powershell.exe": PS},
        )
        assert ss.platform_kind() == "none"
        _platform(monkeypatch, "freebsd14")
        assert ss.platform_kind() == "none"

    def test_plain_linux_ready_says_what_to_use_instead(self, monkeypatch):
        _platform(monkeypatch, "linux", "Linux version 6.8.0-45-generic")
        assert ss.SystemSTT().ready() == (False, ss.LINUX_NONE)
        assert ss.SystemTTS().ready() == (False, ss.LINUX_NONE)
        assert "Settings → Voice" in ss.LINUX_NONE

    def test_nothing_is_spawned_under_pytest_by_default(self, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_SPEECH_PROBE")
        _platform(monkeypatch, "linux", WSL_PROC, which={"powershell.exe": PS})
        assert ss.SystemSTT().ready() == (False, ss.NOT_PROBED)  # _run would raise


# --------------------------------------------------------------------------- #
# Script builders
# --------------------------------------------------------------------------- #
class TestScripts:
    def test_ps_quote(self):
        assert ss.ps_quote("plain") == "'plain'"
        assert ss.ps_quote("O'Neil") == "'O''Neil'"
        # PowerShell also ends a literal at typographic single quotes.
        assert ss.ps_quote("don\u2019t \u2018x\u2019") == "'don''t ''x'''"
        assert ss.ps_quote("a\x00b $env:PATH `n") == "'ab $env:PATH `n'"

    def test_tts_script_exact_text_for_a_voice_with_a_quote(self):
        script = ss.tts_script(
            "C:\\T\\out.wav", "It's done; $(rm -r x)", "O'Neil Desktop"
        )
        assert script == (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
            "try {\n"
            "Add-Type -AssemblyName System.Speech\n"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$s.SelectVoice('O''Neil Desktop')\n"
            "$s.SetOutputToWaveFile('C:\\T\\out.wav')\n"
            "$s.Speak('It''s done; $(rm -r x)')\n"
            "$s.Dispose()\n"
            "'OK'\n"
            "} catch {\n"
            "'E' + [char]9 + $_.Exception.Message\n"
            "exit 1\n"
            "}\n"
        )

    def test_tts_script_without_a_voice_selects_none(self):
        assert "SelectVoice" not in ss.tts_script("C:\\a.wav", "hi")

    def test_stt_script(self):
        script = ss.stt_script("C:\\T\\clip.wav", "en-US")
        assert (
            "$e = New-Object System.Speech.Recognition.SpeechRecognitionEngine"
            "([System.Globalization.CultureInfo]'en-US')\n" in script
        )
        assert (
            "LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))"
            in script
        )
        assert "$e.SetInputToWaveFile('C:\\T\\clip.wav')\n" in script
        assert "catch [System.InvalidOperationException] { break }" in script
        assert "if ($r -eq $null) { break }" in script
        default = ss.stt_script("C:\\T\\clip.wav")
        assert (
            "$e = New-Object System.Speech.Recognition.SpeechRecognitionEngine\n"
            in default
        )

    def test_probe_script_lists_voices_recognizers_and_temp(self):
        script = ss.probe_script()
        assert "GetInstalledVoices()" in script
        assert "SpeechRecognitionEngine]::InstalledRecognizers()" in script
        assert "GetTempPath()" in script

    def test_argv_is_encoded_never_raw_text(self):
        argv = ss._ps_argv("powershell.exe", ss.tts_script("C:\\a.wav", 'say "hi"'))
        assert argv[:4] == [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
        ]
        assert 'say "hi"' not in " ".join(argv)
        assert "$s.Speak('say \"hi\"')" in _script(argv)

    def test_pick_voice_and_culture(self):
        voices = ["Microsoft David Desktop", "Microsoft Zira Desktop"]
        assert (
            ss._pick_voice("microsoft zira desktop", voices) == "Microsoft Zira Desktop"
        )
        assert ss._pick_voice("alloy", voices) == ""  # another backend's voice
        assert ss._pick_voice("", voices) == ""
        cultures = ["en-GB", "en-US", "de-DE"]
        assert ss._pick_culture("en-us", cultures) == "en-US"
        assert ss._pick_culture("de", cultures) == "de-DE"
        assert ss._pick_culture("fr", cultures) == ""
        assert ss._pick_culture("", cultures) == ""


# --------------------------------------------------------------------------- #
# Windows / WSL round trips
# --------------------------------------------------------------------------- #
class TestWindows:
    def test_probe_maps_the_windows_temp_dir(self, wsl, tmp_path):
        found = ss.probe()
        assert found.platform == "wsl" and found.stt_ok and found.tts_ok
        assert found.voices == ["Microsoft David Desktop", "Microsoft Zira Desktop"]
        assert found.recognizers == ["en-US"]
        assert found.win_temp == "C:\\Users\\me\\AppData\\Local\\Temp"
        assert found.unix_temp == str(tmp_path)
        # The probe ran from a Windows-side cwd (no UNC-path warning).
        assert wsl.calls[0][2] in ("/mnt/c", None)
        assert wsl.calls[0][1] == ss.TIMEOUT == 20.0

    def test_speak_round_trips_the_wav_and_cleans_up(self, wsl, tmp_path):
        audio, mime = ss.SystemTTS().speak("Hello there", "Microsoft Zira Desktop")
        assert (audio, mime) == (WAV, "audio/wav")
        script = wsl.scripts()[-1]
        assert "$s.SelectVoice('Microsoft Zira Desktop')" in script
        assert "$s.Speak('Hello there')" in script
        # PowerShell got the Windows path of a file in the Windows temp dir.
        assert (
            "SetOutputToWaveFile('C:\\Users\\me\\AppData\\Local\\Temp\\mindflock-speech-"
            in script
        )
        assert wsl.calls[-1][2] == str(tmp_path)
        assert os.listdir(tmp_path) == []

    def test_speak_uses_the_configured_voice_only_when_installed(self, wsl):
        _set(tts_voice="alloy")
        ss.SystemTTS().speak("Hi")
        assert "SelectVoice" not in wsl.scripts()[-1]
        _set(tts_voice="Microsoft David Desktop")
        ss.SystemTTS().speak("Hi")
        assert "SelectVoice('Microsoft David Desktop')" in wsl.scripts()[-1]

    def test_transcribe_joins_every_phrase(self, wsl, tmp_path):
        text = ss.SystemSTT().transcribe(WAV, "audio/wav", "en")
        assert text == "Open a new session to fix the login bug"
        assert wsl.seen_clip == WAV
        assert "CultureInfo]'en-US')" in wsl.scripts()[-1]
        assert os.listdir(tmp_path) == []

    def test_transcribe_nothing_heard(self, wsl):
        wsl.phrases = []
        assert ss.SystemSTT().transcribe(WAV, "audio/x-wav") == ""

    def test_non_wav_clip_is_refused_without_a_call(self, wsl):
        ss.probe()
        before = len(wsl.calls)
        for audio, mime in (
            (b"\x1aE\xdf\xa3webm", "audio/webm"),
            (b"not-riff", "audio/wav"),
        ):
            with pytest.raises(speech.SpeechError) as err:
                ss.SystemSTT().transcribe(audio, mime)
            assert str(err.value) == ss.NEEDS_WAV
        assert len(wsl.calls) == before
        assert ss._PROBE is not None  # a bad clip is not the engine's failure

    def test_timeout_is_a_speech_error_and_reprobes(self, wsl):
        ss.probe()
        wsl.fail = subprocess.TimeoutExpired("powershell.exe", 20)
        with pytest.raises(speech.SpeechError) as err:
            ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert str(err.value) == "the Windows speech engine took longer than 20 s"
        assert ss._PROBE is None  # the next ready() asks again
        wsl.fail = None
        assert ss.SystemSTT().ready()[0] is True

    def test_powershell_error_line_is_the_reason(self, wsl, monkeypatch):
        ss.probe()

        def _boom(argv, timeout, cwd=None):
            return (
                1,
                b"E\tCannot set voice. No matching voice is installed\r\n",
                b"#< CLIXML",
            )

        monkeypatch.setattr(ss, "_run", _boom)
        with pytest.raises(speech.SpeechError) as err:
            ss.SystemTTS().speak("Hi")
        assert str(err.value) == (
            "the Windows speech engine failed: Cannot set voice. No matching voice is installed"
        )

    def test_clixml_stderr_is_read_when_the_script_never_ran(self):
        err = (
            b'#< CLIXML\r\n<Objs><S S="Error">Add-Type : Cannot find assembly_x000D__x000A_</S>'
            b'<S S="Error">System.Speech_x000D__x000A_</S></Objs>'
        )
        assert ss._clixml_error(err) == "Add-Type : Cannot find assembly System.Speech"

    def test_windows_native_uses_the_local_temp_dir(self, monkeypatch, tmp_path):
        _platform(monkeypatch, "win32", which={"powershell": "C:\\ps.exe"})
        monkeypatch.setattr(ss.tempfile, "gettempdir", lambda: str(tmp_path))
        fake = _PowerShell(tmp_path)
        monkeypatch.setattr(ss, "_run", fake)
        found = ss.probe()
        assert found.platform == "windows" and found.win_temp == ""
        assert ss.SystemSTT().transcribe(WAV, "audio/wav") == (
            "Open a new session to fix the login bug"
        )
        assert fake.calls[-1][0][0] == "C:\\ps.exe" and fake.calls[-1][2] is None
        assert str(tmp_path) in fake.scripts()[-1]
        assert not any(a[0] == "wslpath" for a, _t, _c in fake.calls)

    def test_no_recognizer_installed(self, wsl, monkeypatch):
        monkeypatch.setattr(
            sys.modules[__name__], "PROBE_OUT", PROBE_OUT.replace(b"R\ten-US\r\n", b"")
        )
        ok, detail = ss.SystemSTT().ready()
        assert ok is False
        assert detail == (
            "Windows (from WSL) has no speech recognizer installed:"
            " Settings → Time & language → Speech"
        )
        assert ss.SystemTTS().ready()[0] is True


# --------------------------------------------------------------------------- #
# The warm PowerShell
# --------------------------------------------------------------------------- #
class _WarmProc:
    """A fake ``Popen`` playing :func:`ss.warm_script`: a ready line, then
    one JSON answer per base64 request line (``self.answer(req)``; return
    None to say nothing, ``"exit"`` to die)."""

    def __init__(self, owner, argv, cwd):
        import queue as _q

        self.owner, self.argv, self.cwd = owner, list(argv), cwd
        self.out: "_q.Queue[bytes]" = _q.Queue()
        self.code = None
        self.killed = False
        self.stdin = self
        self.stdout = self
        if owner.ready:
            self.out.put(b'{"ready":true}\r\n')
        else:
            self._exit(1)

    # stdin
    def write(self, line):
        req = json.loads(base64.b64decode(line.strip()).decode("utf-8"))
        self.owner.requests.append(req)
        answer = self.owner.answer(req)
        if answer == "exit":
            self._exit(1)
        elif answer is not None:
            self.out.put(json.dumps(answer).encode() + b"\r\n")

    def flush(self):
        pass

    def close(self):
        if self.code is None:
            self._exit(0)

    # stdout
    def readline(self):
        return self.out.get(timeout=10)

    def _exit(self, code):
        self.code = code
        self.out.put(b"")

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        if self.code is None:
            self._exit(-9)

    def wait(self, timeout=None):
        return self.code


class _Warm:
    def __init__(self, tmp_path):
        self.tmp = str(tmp_path)
        self.procs: list = []
        self.requests: list = []
        self.ready = True
        self.fail_start = None
        self.phrases = ["open a new session", "to fix the login bug"]
        self.reply = None  # override: a dict / None / "exit"

    def __call__(self, argv, cwd=None):
        if self.fail_start:
            raise self.fail_start
        proc = _WarmProc(self, argv, cwd)
        self.procs.append(proc)
        return proc

    def answer(self, req):
        if self.reply is not None:
            return self.reply if self.reply != "silent" else None
        name = req["path"].rsplit("\\", 1)[-1]
        local = os.path.join(self.tmp, name)
        if req["op"] == "tts":
            with open(local, "wb") as fh:
                fh.write(WAV)
            return {"ok": True}
        if req["op"] == "stt":
            assert os.path.exists(local)
            return {"ok": True, "phrases": self.phrases}
        return {"ok": False, "error": "unknown request"}


@pytest.fixture
def warm(wsl, monkeypatch, tmp_path):
    """WSL with the warm process allowed and played by :class:`_Warm`."""
    monkeypatch.setenv("MINDFLOCK_SPEECH_WARM", "1")
    fake = _Warm(tmp_path)
    monkeypatch.setattr(ss, "_popen", fake)
    yield fake
    ss.reset()


class TestWarm:
    def test_one_process_serves_every_call(self, warm, wsl, tmp_path):
        stt, tts = ss.SystemSTT(), ss.SystemTTS()
        for _ in range(3):
            assert stt.transcribe(WAV, "audio/wav", "en") == (
                "open a new session to fix the login bug"
            )
            assert tts.speak("Hi there", "Microsoft Zira Desktop") == (
                WAV,
                "audio/wav",
            )
        assert len(warm.procs) == 1 and ss._WARM.starts == 1
        proc = warm.procs[0]
        assert proc.argv[:4] == [PS, "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        assert _script(proc.argv) == ss.warm_script()
        assert proc.cwd == str(tmp_path)  # the Windows temp dir, no UNC warning
        assert wsl.scripts() == [ss.probe_script()]  # no one-shot after the probe
        stt_req, tts_req = warm.requests[:2]
        assert stt_req["op"] == "stt" and stt_req["culture"] == "en-US"
        assert stt_req["path"].startswith("C:\\Users\\me\\AppData\\Local\\Temp\\")
        assert tts_req == {
            "op": "tts",
            "path": tts_req["path"],
            "text": "Hi there",
            "voice": "Microsoft Zira Desktop",
        }
        assert os.listdir(tmp_path) == []  # every clip deleted

    def test_text_is_data_in_a_base64_line_never_script(self, warm):
        tts = ss.SystemTTS()
        tts.speak("It's $(rm -r x)\nline two \u201cquoted\u201d", "alloy")
        assert warm.requests[-1]["text"] == (
            "It's $(rm -r x)\nline two \u201cquoted\u201d"
        )
        assert warm.requests[-1]["voice"] == ""  # not installed: the default
        script = ss.warm_script()
        assert "ConvertFrom-Json" in script and "FromBase64String" in script
        assert "SetInputToNull()" in script and "SetOutputToNull()" in script
        assert "Reply @{ready=$true}" in script

    def test_a_failed_answer_raises_and_restarts(self, warm, wsl):
        stt = ss.SystemSTT()
        stt.transcribe(WAV, "audio/wav")
        warm.reply = {"ok": False, "error": "the grammar broke"}
        with pytest.raises(speech.SpeechError) as err:
            stt.transcribe(WAV, "audio/wav")
        assert str(err.value) == "the Windows speech engine failed: the grammar broke"
        assert warm.procs[0].killed and not ss._WARM.running
        warm.reply = None
        assert stt.transcribe(WAV, "audio/wav")  # a fresh process
        assert len(warm.procs) == 2

    def test_a_timeout_kills_it(self, warm, monkeypatch):
        stt = ss.SystemSTT()
        stt.transcribe(WAV, "audio/wav")
        monkeypatch.setattr(ss, "WARM_TIMEOUT", 0.2)
        warm.reply = "silent"
        with pytest.raises(speech.SpeechError) as err:
            stt.transcribe(WAV, "audio/wav")
        assert str(err.value) == "the Windows speech engine took longer than 0 s"
        assert warm.procs[0].killed

    def test_a_process_that_dies_mid_request_falls_back_to_one_shot(self, warm, wsl):
        warm.reply = "exit"
        assert ss.SystemSTT().transcribe(WAV, "audio/wav") == (
            "Open a new session to fix the login bug"  # the one-shot fake's
        )
        assert "SetInputToWaveFile" in wsl.scripts()[-1]
        assert not ss._WARM.running

    @pytest.mark.parametrize("how", ["oserror", "no-ready"])
    def test_cannot_start_falls_back_and_waits_before_retrying(
        self, warm, wsl, monkeypatch, how
    ):
        clock = [5000.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        if how == "oserror":
            warm.fail_start = OSError("interop is off")
        else:
            warm.ready = False
        tts = ss.SystemTTS()
        assert tts.speak("Hi")[0] == WAV  # through the one-shot script
        tries = len(warm.procs)
        tts.speak("Hi")
        assert len(warm.procs) == tries  # not retried at once
        assert sum("SetOutputToWaveFile" in s for s in wsl.scripts()) == 2
        warm.fail_start, warm.ready = None, True
        clock[0] += ss.RETRY_SECONDS
        tts.speak("Hi")
        assert ss._WARM.running
        assert sum("SetOutputToWaveFile" in s for s in wsl.scripts()) == 2

    def test_idle_exit(self, warm, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert ss._WARM.reap_idle() is False
        clock[0] += ss.WARM_IDLE - 1
        assert ss._WARM.reap_idle() is False and ss._WARM.running
        clock[0] += 2
        assert ss._WARM.reap_idle() is True and not ss._WARM.running
        assert warm.procs[0].code == 0  # stdin closed: it exited by itself
        ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert len(warm.procs) == 2

    def test_never_warm_under_pytest_without_the_opt_in(self, wsl, monkeypatch):
        spawned = []
        monkeypatch.setattr(ss, "_popen", lambda *a, **k: spawned.append(a))
        monkeypatch.delenv("MINDFLOCK_SPEECH_WARM", raising=False)
        ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert spawned == []
        assert "SetInputToWaveFile" in wsl.scripts()[-1]


# --------------------------------------------------------------------------- #
# Caching and describe()
# --------------------------------------------------------------------------- #
class TestCaching:
    def test_one_probe_per_process(self, wsl):
        for _ in range(3):
            assert ss.SystemSTT().ready()[0] is True
            assert ss.SystemTTS().ready()[0] is True
        speech.describe()
        assert sum(1 for a, _t, _c in wsl.calls if a[0] != "wslpath") == 1

    def test_failed_probe_is_retried_after_a_while(self, wsl, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        wsl.fail = OSError("interop is off")
        ok, detail = ss.SystemSTT().ready()
        assert ok is False and detail == "could not start PowerShell: interop is off"
        wsl.fail = None
        clock[0] += 10
        assert ss.SystemSTT().ready()[0] is False  # still the cached failure
        clock[0] += ss.RETRY_SECONDS
        assert ss.SystemSTT().ready()[0] is True
        assert len(wsl.scripts()) == 2

    def test_system_is_first_and_chosen_when_unset(self, wsl):
        assert speech.ids("stt")[0] == "system" and speech.ids("tts")[0] == "system"
        backend, detail = speech.select("stt")
        assert backend.id == "system"
        assert detail == (
            "Windows (from WSL) speech recognition (en-US):"
            " basic accuracy — install local models for better recognition"
        )
        assert speech.choose("tts") == "system"

    def test_describe_system_block(self, wsl):
        d = speech.describe()
        assert d["backends"]["stt"][0] == "system"
        assert d["server_ready"]["stt_backend"] == "system"
        assert d["system"] == {
            "available": True,
            "platform": "wsl",
            "detail": "Recognition: Windows (from WSL) speech recognition (en-US)."
            " Voice: Windows (from WSL) voices: Microsoft David Desktop,"
            " Microsoft Zira Desktop.",
            "voices": ["Microsoft David Desktop", "Microsoft Zira Desktop"],
            "recognizers": ["en-US"],
            "stt_quality": "basic",
        }
        # The probe's own sentence stays plain; the recommendation rides
        # only the ready() detail the Settings row prints.
        assert d["server_ready"]["stt_detail"].endswith(
            "basic accuracy — install local models for better recognition"
        )

    def test_mac_recognition_is_good_and_needs_no_recommendation(self, monkeypatch):
        found = ss.Probe(
            "mac",
            stt_ok=True,
            stt_detail="Apple speech recognition on this Mac",
            tts_ok=True,
            tts_detail="macOS voices: Samantha",
        )
        monkeypatch.setattr(ss, "probe", lambda: found)
        assert ss.describe()["stt_quality"] == "good"
        assert ss.SystemSTT().ready() == (True, "Apple speech recognition on this Mac")

    def test_describe_on_plain_linux(self, monkeypatch):
        _platform(monkeypatch, "linux", "Linux version 6.8.0")
        assert ss.describe() == {
            "available": False,
            "platform": "none",
            "detail": ss.LINUX_NONE,
            "voices": [],
            "recognizers": [],
            "stt_quality": "none",
        }

    def test_registration_is_idempotent(self):
        before = speech.ids("stt")
        ss._register()
        assert speech.ids("stt") == before


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #
def _fake_objc(
    status=3,
    text="open a new session",
    error=None,
    final=True,
    grant=True,
    error_code=None,
):
    """Fake ``Speech`` + ``Foundation`` modules; ``log`` records the calls."""
    log: dict = {"requested_auth": 0}

    class Recognizer:
        _status = status

        @classmethod
        def authorizationStatus(cls):
            return cls._status

        @classmethod
        def requestAuthorization_(cls, handler):
            log["requested_auth"] += 1
            if grant:  # a bare python: macOS never answers
                cls._status = 3
                handler(3)

        @classmethod
        def supportedLocales(cls):
            return [types.SimpleNamespace(localeIdentifier=lambda: "en_US")]

        @classmethod
        def alloc(cls):
            return cls()

        def initWithLocale_(self, locale):
            log["locale"] = locale
            return self

        def isAvailable(self):
            return True

        def supportsOnDeviceRecognition(self):
            return True

        def recognitionTaskWithRequest_resultHandler_(self, request, handler):
            log["url"] = request.url
            result = None
            if text is not None:
                best = types.SimpleNamespace(formattedString=lambda: text)
                result = types.SimpleNamespace(
                    bestTranscription=lambda: best, isFinal=lambda: final
                )
            err = (
                types.SimpleNamespace(
                    localizedDescription=lambda: error,
                    domain=lambda: (error_code or ("", 0))[0],
                    code=lambda: (error_code or ("", 0))[1],
                )
                if error
                else None
            )
            handler(result, err)
            return types.SimpleNamespace(
                cancel=lambda: log.__setitem__("cancelled", True)
            )

    class URLRequest:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithURL_(self, url):
            self.url = url
            return self

        def setRequiresOnDeviceRecognition_(self, flag):
            log["on_device"] = flag

    class Locale:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithLocaleIdentifier_(self, ident):
            return "locale:" + ident

        @classmethod
        def currentLocale(cls):
            return "locale:current"

    loop = types.SimpleNamespace(runMode_beforeDate_=lambda mode, date: None)
    speech_mod = types.ModuleType("Speech")
    speech_mod.SFSpeechRecognizer = Recognizer
    speech_mod.SFSpeechURLRecognitionRequest = URLRequest
    foundation = types.ModuleType("Foundation")
    foundation.NSLocale = Locale
    foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=lambda p: "file://" + p)
    foundation.NSRunLoop = types.SimpleNamespace(currentRunLoop=lambda: loop)
    foundation.NSDate = types.SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda s: s)
    foundation.NSDefaultRunLoopMode = "default"
    return speech_mod, foundation, log


def _inject(monkeypatch, speech_mod, foundation):
    monkeypatch.setitem(sys.modules, "Speech", speech_mod)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)


def _inprocess_helper(args, timeout):
    """``_mac_helper`` without the process: the helper's own entry point."""
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        ss._mac_main(args)
    finally:
        sys.stdout = real
    return json.loads(out.getvalue())


SAY_VOICES = (
    b"Albert              en_US    # Hello! My name is Albert.\n"
    b"Eddy (English (US)) en_US    # Hello! My name is Eddy.\n"
    b"Samantha            en_US    # Hello! My name is Samantha.\n"
    b"Thomas              fr_FR    # Bonjour, je m'appelle Thomas.\n"
)


class _Say:
    """A fake ``_run`` playing ``say`` and ``defaults`` on macOS;
    ``dictation`` maps a ``(domain, key)`` to what ``defaults read`` prints
    (absent = the key does not exist, exit 1)."""

    def __init__(self):
        self.calls: list = []
        self.text_seen = None
        self.dictation = {("com.apple.assistant.support", "Dictation Enabled"): "1"}

    def __call__(self, argv, timeout, cwd=None):
        self.calls.append(list(argv))
        if argv[0].endswith("defaults"):
            assert argv[1] == "read"
            value = self.dictation.get((argv[2], argv[3]))
            if value is None:
                return 1, b"", b"The domain/default pair does not exist\n"
            return 0, (value + "\n").encode(), b""
        if argv[1:] == ["-v", "?"]:
            return 0, SAY_VOICES, b""
        assert argv[1] == "-o" and argv[3] == "--data-format=LEI16@22050"
        with open(argv[argv.index("-f") + 1], encoding="utf-8") as fh:
            self.text_seen = fh.read()
        with open(argv[2], "wb") as fh:
            fh.write(WAV)
        return 0, b"", b""


@pytest.fixture
def mac(monkeypatch):
    _platform(monkeypatch, "darwin", which={"say": "/usr/bin/say"})
    say = _Say()
    monkeypatch.setattr(ss, "_run", say)
    monkeypatch.setattr(ss, "_mac_helper", _inprocess_helper)
    return say


class TestMac:
    def test_pyobjc_absent_says_why(self, mac, monkeypatch):
        monkeypatch.setitem(sys.modules, "Speech", None)  # import → ImportError
        ok, detail = ss.SystemSTT().ready()
        assert ok is False and detail == ss.NO_PYOBJC
        with pytest.raises(speech.SpeechError) as err:
            ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert str(err.value) == ss.NO_PYOBJC
        # The voice still works: say is not PyObjC.
        assert ss.SystemTTS().ready()[0] is True

    def test_one_result_is_the_text(self, mac, monkeypatch):
        speech_mod, foundation, log = _fake_objc(text="Open a new session")
        _inject(monkeypatch, speech_mod, foundation)
        stt = ss.SystemSTT()
        assert stt.ready() == (True, "Apple speech recognition on this Mac")
        assert ss.probe().recognizers == ["en-US"]
        assert stt.transcribe(WAV, "audio/wav", "en-US") == "Open a new session"
        assert log["locale"] == "locale:en_US"
        assert log["on_device"] is True
        assert log["url"].startswith("file://") and log["url"].endswith(".wav")
        assert not os.path.exists(log["url"][len("file://") :])  # clip deleted
        assert log["requested_auth"] == 0

    def test_mp4_is_accepted_webm_is_not(self, mac, monkeypatch):
        speech_mod, foundation, log = _fake_objc()
        _inject(monkeypatch, speech_mod, foundation)
        assert (
            ss.SystemSTT().transcribe(b"....ftypM4A ", "audio/mp4")
            == "open a new session"
        )
        assert log["url"].endswith(".mp4")
        with pytest.raises(speech.SpeechError) as err:
            ss.SystemSTT().transcribe(b"\x1aE", "audio/webm")
        assert str(err.value) == ss.NEEDS_WAV

    def test_denied_authorization_is_the_permission_sentence(self, mac, monkeypatch):
        speech_mod, foundation, _log = _fake_objc(status=1)
        _inject(monkeypatch, speech_mod, foundation)
        ok, detail = ss.SystemSTT().ready()
        assert ok is False
        assert detail == (
            "Apple speech recognition needs permission: System Settings →"
            " Privacy & Security → Speech Recognition"
        )
        with pytest.raises(speech.SpeechError):
            ss.SystemSTT().transcribe(WAV, "audio/wav")
        with pytest.raises(speech.SpeechError) as err:
            ss.mac_transcribe("/tmp/x.wav")
        assert str(err.value) == detail

    def test_not_determined_is_asked_once_by_the_probe(self, mac, monkeypatch):
        # The MindFlock app (usage description in its Info.plist): the
        # prompt is answered, and recognition is ready.
        speech_mod, foundation, log = _fake_objc(status=0)
        _inject(monkeypatch, speech_mod, foundation)
        assert ss.SystemSTT().ready() == (True, "Apple speech recognition on this Mac")
        assert log["requested_auth"] == 1
        assert ss.SystemSTT().transcribe(WAV, "audio/wav") == "open a new session"
        assert log["requested_auth"] == 1
        assert log["locale"] == "locale:current"

    def test_unanswered_authorization_is_not_waited_on(self, mac, monkeypatch):
        # With Dictation on, macOS answers at once; if it ever does not, the
        # probe waits at most a few seconds and recognition is still tried
        # (only denied / restricted blocks).
        speech_mod, foundation, log = _fake_objc(status=0, grant=False)
        _inject(monkeypatch, speech_mod, foundation)
        waited = []
        monkeypatch.setattr(
            ss, "_pump", lambda f, done, seconds: waited.append(seconds)
        )
        assert ss.SystemSTT().ready() == (True, "Apple speech recognition on this Mac")
        assert waited == [ss.MAC_AUTH_WAIT] and ss.MAC_AUTH_WAIT <= 3.0
        assert log["requested_auth"] == 1

    def test_dictation_off_in_preferences_is_not_ready(self, mac, monkeypatch):
        speech_mod, foundation, log = _fake_objc()
        _inject(monkeypatch, speech_mod, foundation)
        clock = [10.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        mac.dictation = {}  # never turned on: neither key exists
        assert ss.SystemSTT().ready() == (False, ss.DICTATION_OFF)
        d = ss.describe()
        assert d["available"] is True  # say still speaks
        assert d["detail"] == (
            "Recognition: Turn on Dictation: System Settings → Keyboard →"
            " Dictation — macOS recognition uses it."
            " Voice: macOS voices: Albert, Eddy (English (US)), Samantha, Thomas."
        )
        with pytest.raises(speech.SpeechError) as err:
            ss.SystemSTT().transcribe(WAV, "audio/wav")
        assert str(err.value) == ss.DICTATION_OFF
        assert "url" not in log  # no recognition was attempted
        # Turned on (the HIToolbox sign alone is enough): picked up on the retry.
        mac.dictation = {("com.apple.HIToolbox", "AppleDictationAutoEnable"): "1"}
        assert ss.SystemSTT().ready()[0] is False
        clock[0] += ss.RETRY_SECONDS
        assert ss.SystemSTT().ready()[0] is True

    def test_dictation_zero_is_off_unknown_is_on(self, mac, monkeypatch):
        mac.dictation = {("com.apple.assistant.support", "Dictation Enabled"): "0"}
        assert ss._dictation_on() is False
        reads = [c for c in mac.calls if c[0].endswith("defaults")]
        assert [c[2:] for c in reads] == [
            ["com.apple.assistant.support", "Dictation Enabled"],
            ["com.apple.HIToolbox", "AppleDictationAutoEnable"],
        ]

        def _no_defaults(argv, timeout, cwd=None):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(ss, "_run", _no_defaults)
        assert ss._dictation_on() is True  # a recognition run will say for sure

    def test_dictation_off_is_named_and_remembered(self, mac, monkeypatch):
        speech_mod, foundation, _log = _fake_objc(
            text=None,
            error="Siri and Dictation are disabled",
            error_code=("kLSRErrorDomain", 201),
        )
        _inject(monkeypatch, speech_mod, foundation)
        clock = [100.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        stt = ss.SystemSTT()
        assert stt.ready()[0] is True
        with pytest.raises(speech.SpeechError) as err:
            stt.transcribe(WAV, "audio/wav")
        assert (
            str(err.value)
            == ss.DICTATION_OFF
            == (
                "Turn on Dictation: System Settings → Keyboard → Dictation"
                " — macOS recognition uses it"
            )
        )
        assert stt.ready() == (False, ss.DICTATION_OFF)
        d = ss.describe()
        assert d["available"] is True  # say still speaks
        assert d["detail"].startswith("Recognition: Turn on Dictation")
        clock[0] += ss.RETRY_SECONDS
        assert stt.ready()[0] is True  # re-probed: worth another try

    def test_dictation_off_by_code_alone(self, monkeypatch):
        speech_mod, foundation, _log = _fake_objc(
            text=None, error="localized elsewhere", error_code=("kLSRErrorDomain", 201)
        )
        _inject(monkeypatch, speech_mod, foundation)
        with pytest.raises(speech.SpeechError) as err:
            ss.mac_transcribe("/tmp/x.wav")
        assert str(err.value) == ss.DICTATION_OFF

    def test_no_speech_is_empty_other_errors_raise(self, monkeypatch):
        speech_mod, foundation, _log = _fake_objc(text=None, error="No speech detected")
        _inject(monkeypatch, speech_mod, foundation)
        assert ss.mac_transcribe("/tmp/x.wav") == ""
        speech_mod, foundation, _log = _fake_objc(
            text=None, error="Audio file is corrupt"
        )
        _inject(monkeypatch, speech_mod, foundation)
        with pytest.raises(speech.SpeechError) as err:
            ss.mac_transcribe("/tmp/x.wav")
        assert (
            str(err.value) == "Apple speech recognition failed: Audio file is corrupt"
        )

    def test_no_final_result_times_out_keeping_partial_text(self, monkeypatch):
        speech_mod, foundation, log = _fake_objc(text="partial words", final=False)
        _inject(monkeypatch, speech_mod, foundation)
        assert ss.mac_transcribe("/tmp/x.wav", timeout=0.05) == "partial words"
        assert log["cancelled"] is True

    def test_say_voices_and_speak(self, mac, monkeypatch):
        speech_mod, foundation, _log = _fake_objc()
        _inject(monkeypatch, speech_mod, foundation)
        found = ss.probe()
        assert found.voices == ["Albert", "Eddy (English (US))", "Samantha", "Thomas"]
        assert found.tts_ok
        _set(tts_voice="samantha")
        text = "It's done; $(rm -rf ~) `x`"
        audio, mime = ss.SystemTTS().speak(text)
        assert (audio, mime) == (WAV, "audio/wav")
        argv = mac.calls[-1]
        assert argv[:4] == ["/usr/bin/say", "-o", argv[2], "--data-format=LEI16@22050"]
        assert argv[4:6] == ["-v", "Samantha"] and argv[6] == "-f"
        assert text not in argv  # the text rides a file, never argv
        assert mac.text_seen == text
        assert not os.path.exists(argv[2])  # temp dir removed
        ss.SystemTTS().speak("Hi", "alloy")  # not a macOS voice: the default
        assert "-v" not in mac.calls[-1]

    def test_helper_killed_by_a_signal_names_the_permission(self, monkeypatch):
        monkeypatch.setattr(
            ss, "_run", lambda argv, timeout, cwd=None: (-6, b"", b"abort")
        )
        with pytest.raises(speech.SpeechError) as err:
            ss._mac_helper(["probe"], 5)
        assert "signal 6" in str(err.value) and ss.MAC_PERMISSION in str(err.value)

    def test_helper_argv_and_json(self, monkeypatch):
        seen = {}

        def _fake(argv, timeout, cwd=None):
            seen["argv"], seen["timeout"] = argv, timeout
            return 0, b'noise\n{"text": "hi"}\n', b""

        monkeypatch.setattr(ss, "_run", _fake)
        assert ss._mac_helper(["transcribe", "/tmp/a.wav", "en"], 45) == {"text": "hi"}
        assert seen["argv"][0] == sys.executable and seen["argv"][1] == "-c"
        assert "_mac_main" in seen["argv"][2]
        assert seen["argv"][3:] == ["transcribe", "/tmp/a.wav", "en"]
        monkeypatch.setattr(
            ss, "_run", lambda argv, timeout, cwd=None: (1, b"", b"Traceback\nBoom")
        )
        with pytest.raises(speech.SpeechError) as err:
            ss._mac_helper(["probe"], 5)
        assert str(err.value) == "the recognizer helper failed: Boom"


# --------------------------------------------------------------------------- #
# The one real smoke test (Windows / WSL only)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    ss.platform_kind() not in ("windows", "wsl"),
    reason="Windows speech is reachable only on Windows or WSL",
)
@pytest.mark.parametrize("warm", [False, True], ids=["one-shot", "warm"])
def test_real_windows_speech_round_trip(monkeypatch, warm):
    if warm:
        monkeypatch.setenv("MINDFLOCK_SPEECH_WARM", "1")
    starts = ss._WARM.starts
    started = __import__("time").monotonic()
    tts, stt = ss.SystemTTS(), ss.SystemSTT()
    ok, detail = tts.ready()
    if not ok:
        pytest.skip("no Windows voice: " + detail)
    audio, mime = tts.speak("mindflock speech check")
    assert mime == "audio/wav" and audio[:4] == b"RIFF"
    ok, detail = stt.ready()
    if not ok:
        pytest.skip("no Windows recognizer: " + detail)
    text = stt.transcribe(audio, "audio/wav", "en")
    words = text.lower().replace(",", " ").split()
    assert "speech" in words and "check" in words, text
    if warm:
        # The same process again, and (once warm) well under a second.
        t0 = __import__("time").monotonic()
        again = stt.transcribe(audio, "audio/wav", "en").lower()
        assert "speech" in again and "check" in again, again
        assert ss._WARM.starts == starts + 1 and ss._WARM.running
        assert __import__("time").monotonic() - t0 < 5
    assert __import__("time").monotonic() - started < 60
