"""``core.speech_install`` (the one-click local models behind
``POST /api/voice/install-local``): the command lines, the state machine,
log capture, failure reporting, the settings switch, and the helper
process's downloads — with :func:`speech_install._spawn` (every subprocess)
and the downloaders replaced. Nothing here installs or downloads anything.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import settings as S
from backend.web.core import speech
from backend.web.core import speech_install as si


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    si.wait(5)
    si.reset()
    yield
    si.wait(5)
    si.reset()


class _Proc:
    """A fake step process: ``lines`` on stdout, then ``code``."""

    def __init__(self, argv, lines, code, gate=None):
        self.argv = list(argv)
        self._lines = list(lines)
        self.code = code
        self.gate = gate
        self.stdout = self
        self.killed = False

    def readline(self):
        if self.gate is not None:
            self.gate.wait(5)
            self.gate = None
        return self._lines.pop(0) + "\n" if self._lines else ""

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True


class _Spawner:
    def __init__(self):
        self.calls: list = []
        self.script: dict = {}  # step word -> (lines, code) or an exception
        self.gate = None
        self.invalidated = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        key = (
            "packages"
            if "install" in argv
            else ("whisper" if "whisper" in argv else "piper")
        )
        got = self.script.get(key, (["ok " + key], 0))
        if isinstance(got, BaseException):
            raise got
        lines, code = got
        return _Proc(argv, lines, code, self.gate)


@pytest.fixture
def spawn(monkeypatch):
    fake = _Spawner()
    monkeypatch.setattr(si, "_spawn", fake)
    monkeypatch.setattr(si, "_which", lambda name: "/usr/bin/uv")

    def _inv():
        fake.invalidated += 1

    monkeypatch.setattr(speech, "invalidate", _inv)
    return fake


def _run():
    assert si.start() is True
    si.wait(5)
    return si.status()


# --------------------------------------------------------------------------- #
# Command lines
# --------------------------------------------------------------------------- #
class TestArgv:
    def test_uv_on_path(self, monkeypatch):
        monkeypatch.setattr(si, "_which", lambda name: "/opt/bin/uv")
        assert si.install_argv() == [
            "/opt/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "faster-whisper",
            "piper-tts",
        ]

    def test_uv_beside_the_interpreter(self, monkeypatch):
        monkeypatch.setattr(si, "_which", lambda name: None)
        here = str(Path(sys.executable).parent / "uv")
        monkeypatch.setattr(si, "_isfile", lambda path: path == here)
        assert si.install_argv()[:2] == [here, "pip"]

    def test_pip_without_uv(self, monkeypatch):
        monkeypatch.setattr(si, "_which", lambda name: None)
        monkeypatch.setattr(si, "_isfile", lambda path: False)
        assert si.install_argv() == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "faster-whisper",
            "piper-tts",
        ]

    def test_never_spawns_under_pytest(self):
        with pytest.raises(OSError):
            si._spawn(["true"])


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
class TestInstall:
    def test_idle_shape(self):
        assert si.status() == {
            "state": "idle",
            "step": "",
            "log": [],
            "started": None,
            "finished": None,
            "error": "",
        }

    def test_success_runs_three_steps_and_switches_recognition(self, spawn):
        S.update_settings(voice={"tts_backend": "system"})
        st = _run()
        assert st["state"] == "done" and st["step"] == "done" and st["error"] == ""
        assert st["started"] and st["finished"] >= st["started"]
        install, whisper, stream, piper = spawn.calls
        assert install[:5] == [
            "/usr/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
        ]
        assert whisper[0] == sys.executable and whisper[1] == "-c"
        assert "_download_main" in whisper[2]
        assert whisper[3:] == ["whisper", "small"]
        assert stream[3:] == ["whisper", "base.en"]  # the live transcript's
        assert piper[3:] == ["piper", "en_US-lessac-medium"]
        S.invalidate()
        voice = S.load_settings().voice
        assert voice.stt_backend == "local"
        assert voice.tts_backend == "system"  # the voice stays what it was
        assert spawn.invalidated >= 1
        assert "ok packages" in st["log"] and "ok whisper" in st["log"]
        assert st["log"][-1].startswith("local models are ready")
        # The helper's -c program is not dumped into the log.
        assert any(
            l.startswith("$ " + sys.executable + " -c … whisper") for l in st["log"]
        )

    def test_the_configured_model_and_voice_are_fetched(self, spawn):
        S.update_settings(
            voice={
                "stt_model": "medium.en",
                "stt_stream_model": "tiny.en",
                "tts_voice": "en_GB-alan-low",
            }
        )
        _run()
        assert spawn.calls[1][3:] == ["whisper", "medium.en"]
        assert spawn.calls[2][3:] == ["whisper", "tiny.en"]
        assert spawn.calls[3][3:] == ["piper", "en_GB-alan-low"]

    def test_one_model_for_both_is_fetched_once(self, spawn):
        S.update_settings(voice={"stt_model": "base.en"})
        _run()
        assert [c[3:] for c in spawn.calls[1:]] == [
            ["whisper", "base.en"],
            ["piper", "en_US-lessac-medium"],
        ]

    def test_another_backends_names_fall_back_to_the_defaults(self, spawn):
        S.update_settings(
            voice={"stt_model": "whisper-1", "tts_voice": "Microsoft Zira Desktop"}
        )
        _run()
        assert spawn.calls[1][3:] == ["whisper", "small"]
        assert spawn.calls[2][3:] == ["whisper", "base.en"]
        assert spawn.calls[3][3:] == ["piper", "en_US-lessac-medium"]

    def test_steps_are_reported_while_running(self, spawn):
        gate = threading.Event()
        spawn.gate = gate
        assert si.start() is True
        st = si.status()
        assert st["state"] == "running" and st["step"] == "installing packages"
        assert si.start() is False  # one at a time
        gate.set()
        si.wait(5)
        assert si.status()["state"] == "done"

    def test_a_failed_step_stops_with_its_last_line(self, spawn):
        spawn.script["whisper"] = (
            ["Fetching 4 files", "error: Connection refused"],
            1,
        )
        st = _run()
        assert st["state"] == "failed"
        assert st["step"] == "downloading recognition model (small)"
        assert st["error"] == (
            "downloading recognition model (small) failed: error: Connection refused"
        )
        assert len(spawn.calls) == 2  # the voice was never tried
        S.invalidate()
        assert S.load_settings().voice.stt_backend == ""  # not switched

    def test_install_failure_without_output(self, spawn):
        spawn.script["packages"] = ([], 2)
        st = _run()
        assert st["error"] == "installing packages failed: exit 2"

    def test_a_step_that_cannot_start(self, spawn):
        spawn.script["packages"] = OSError("No such file or directory: 'uv'")
        st = _run()
        assert st["state"] == "failed"
        assert st["error"].startswith(
            "installing packages: could not start /usr/bin/uv"
        )

    def test_the_log_keeps_the_last_lines_and_splits_progress(self, spawn):
        spawn.script["packages"] = (
            ["line %d" % n for n in range(40)] + ["10%\r55%\r100%"],
            0,
        )
        _run()
        log = si.status()["log"]
        assert len(log) == si.LOG_LINES == 20
        assert "line 39" in log and "line 5" not in log

    def test_a_timeout_kills_the_step(self, spawn, monkeypatch):
        monkeypatch.setattr(si, "STEP_TIMEOUT", 0.05)
        gate = threading.Event()
        spawn.gate = gate
        procs = []
        real = spawn.__call__

        def _keep(argv):
            proc = real(argv)
            procs.append(proc)
            return proc

        monkeypatch.setattr(si, "_spawn", _keep)
        assert si.start() is True
        threading.Timer(0.3, gate.set).start()
        si.wait(5)
        st = si.status()
        assert st["state"] == "failed" and "took longer than" in st["error"]
        assert procs[0].killed

    def test_an_unexpected_error_never_leaves_it_running(self, spawn, monkeypatch):
        def boom(**kw):
            raise RuntimeError("settings file is read-only")

        monkeypatch.setattr(S, "update_settings", boom)
        st = _run()
        assert st["state"] == "failed"
        assert st["error"] == "RuntimeError: settings file is read-only"

    def test_it_can_run_again_after_a_failure(self, spawn):
        spawn.script["packages"] = ([], 1)
        assert _run()["state"] == "failed"
        spawn.script.clear()
        assert _run()["state"] == "done"


# --------------------------------------------------------------------------- #
# The helper process
# --------------------------------------------------------------------------- #
class TestDownloadMain:
    def test_whisper_lands_in_the_local_backends_cache(
        self, monkeypatch, tmp_path, capsys
    ):
        seen = {}

        def download_model(name, cache_dir=None, **kw):
            seen.update(name=name, cache_dir=cache_dir, **kw)
            return cache_dir + "/models--Systran--faster-whisper-" + name

        monkeypatch.setattr(
            speech,
            "_import",
            lambda mod: SimpleNamespace(download_model=download_model),
        )
        si._download_main(["whisper", "small"])
        root = str(tmp_path / "voice" / "whisper")
        assert seen == {"name": "small", "cache_dir": root}
        assert capsys.readouterr().out.strip().endswith("faster-whisper-small")

    def test_piper_uses_the_local_backends_fetch(self, monkeypatch, tmp_path, capsys):
        got = []

        def download_voice(name, folder):
            got.append((name, Path(folder)))
            (Path(folder) / (name + ".onnx")).write_bytes(b"onnx")

        monkeypatch.setattr(
            speech,
            "_import",
            lambda mod: SimpleNamespace(download_voice=download_voice),
        )
        si._download_main(["piper", "en_US-lessac-medium"])
        folder = tmp_path / "voice" / "piper"
        assert got == [("en_US-lessac-medium", folder)]
        assert str(folder / "en_US-lessac-medium.onnx") in capsys.readouterr().out
        si._download_main(["piper", "en_US-lessac-medium"])
        assert len(got) == 1  # already there: not fetched again

    def test_a_failure_is_the_last_line_and_exit_1(self, monkeypatch, capsys):
        def boom(mod):
            raise ImportError("No module named 'faster_whisper'")

        monkeypatch.setattr(speech, "_import", boom)
        with pytest.raises(SystemExit) as ex:
            si._download_main(["whisper", "small"])
        assert ex.value.code == 1
        assert capsys.readouterr().out.strip() == (
            "error: No module named 'faster_whisper'"
        )

    def test_bad_usage(self, capsys):
        with pytest.raises(SystemExit):
            si._download_main(["nope"])
        assert "usage" in capsys.readouterr().out
