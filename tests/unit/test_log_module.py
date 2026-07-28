"""Unit tests for the Go-port logger (:mod:`backend.log`).

Covers behavior that only exists in this module and has no other coverage:
the ``Every`` throttle state machine, the size-based rotation (a mindflock
addition guarding against unbounded log growth), and ``_go_sprint``'s spacing
rule. The module is normally only monkeypatched away in other tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import log


class _FakeClock:
    """Stand-in for ``time.monotonic`` with a manually-advanced value."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


class TestEveryShouldLog:
    def test_first_call_true_then_throttled_until_deadline(self, monkeypatch):
        clock = _FakeClock()
        # Replace the module's time reference so we don't touch the real clock.
        monkeypatch.setattr(log, "_time", clock)

        ev = log.Every(5.0)
        assert ev.ShouldLog() is True  # first call always fires + arms timer
        assert ev.ShouldLog() is False  # immediately after: throttled

        clock.now = 1004.9  # still before the 1005.0 deadline
        assert ev.ShouldLog() is False

        clock.now = 1005.0  # deadline reached
        assert ev.ShouldLog() is True
        assert ev.ShouldLog() is False  # re-armed for another 5s

        clock.now = 1010.0  # next deadline reached
        assert ev.ShouldLog() is True


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Point the logger at a tmp file with a small rotation cap, and restore all
    mutated module globals afterward."""
    logfile = tmp_path / "mindflock.log"
    # monkeypatch records the pre-test values and restores them on teardown,
    # even though Initialize()/_rotate() reassign these globals during the test.
    monkeypatch.setattr(log, "logFileName", str(logfile))
    monkeypatch.setattr(log, "_MAX_LOG_BYTES", 200)
    monkeypatch.setattr(log, "globalLogFile", None)
    monkeypatch.setattr(log, "_bytes_written", 0)
    monkeypatch.setattr(log, "InfoLog", None)
    monkeypatch.setattr(log, "WarningLog", None)
    monkeypatch.setattr(log, "ErrorLog", None)
    try:
        yield logfile
    finally:
        # Close the live handle before monkeypatch restores logFileName.
        try:
            log.Close()
        except Exception:
            pass


class TestRotation:
    def test_creates_single_backup_and_resets_live_file(self, isolated_log):
        logfile = isolated_log
        log.Initialize(False)

        # Emit well past the 200-byte cap so rotation happens many times.
        for i in range(60):
            log.InfoLog.Printf("padding line number %d with extra text", i)

        backup = Path(str(logfile) + ".1")
        assert backup.exists()
        assert backup.stat().st_size > 0

        # Invariant enforced by _emit: after every write the live file stays
        # under the cap (rotation happens before the write that would exceed).
        assert log._bytes_written <= log._MAX_LOG_BYTES
        assert logfile.stat().st_size <= log._MAX_LOG_BYTES
        # On-disk size of the fresh file tracks the in-process counter.
        assert logfile.stat().st_size == log._bytes_written

        # Exactly one backup is kept across repeated rotations (no .2, .3, ...).
        siblings = sorted(p.name for p in tmp_siblings(logfile))
        assert siblings == ["mindflock.log", "mindflock.log.1"]

    def test_no_rotation_when_disabled(self, isolated_log, monkeypatch):
        logfile = isolated_log
        monkeypatch.setattr(log, "_MAX_LOG_BYTES", 0)  # 0 disables rotation
        log.Initialize(False)

        for i in range(60):
            log.InfoLog.Printf("padding line number %d with extra text", i)

        assert not Path(str(logfile) + ".1").exists()
        assert logfile.stat().st_size > 200  # grew freely, no cap


def tmp_siblings(logfile: Path):
    return list(logfile.parent.glob("mindflock.log*"))


class TestGoSprint:
    def test_space_between_two_non_strings(self):
        assert log._go_sprint((1, 2)) == "1 2"

    def test_no_space_between_string_and_non_string(self):
        assert log._go_sprint(("a", 1)) == "a1"
        assert log._go_sprint((1, "a")) == "1a"

    def test_single_argument_is_exact(self):
        assert log._go_sprint((42,)) == "42"
        assert log._go_sprint(("hi",)) == "hi"


class TestMaxLogBytes:
    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_LOG_MAX_BYTES", "1234")
        assert log._max_log_bytes() == 1234

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_LOG_MAX_BYTES", "not-an-int")
        assert log._max_log_bytes() == 5 * 1024 * 1024


class TestPrintFamily:
    def test_print_and_println_write_to_file(self, isolated_log):
        logfile = isolated_log
        log.Initialize(False)
        log.InfoLog.Print("a", 1)  # fmt.Sprint: no space (str then non-str)
        log.InfoLog.Println("x", "y")  # fmt.Sprintln: space-joined
        for lg in (log.InfoLog,):
            pass
        content = logfile.read_text()
        assert "a1" in content
        assert "x y" in content

    def test_printf_bad_format_does_not_raise(self, isolated_log):
        logfile = isolated_log
        log.Initialize(False)
        # Too few args for the format -> fall back to a joined form, never raise.
        log.InfoLog.Printf("%d and %d", 7)
        assert "7" in logfile.read_text()

    def test_daemon_prefix(self, isolated_log):
        log.Initialize(True)
        assert log.InfoLog._prefix == "[DAEMON] INFO:"
        assert log.ErrorLog._prefix == "[DAEMON] ERROR:"


class TestEmitResilience:
    def test_noop_when_no_output_handle(self, monkeypatch):
        # globalLogFile None AND the logger's own out None -> silent no-op.
        monkeypatch.setattr(log, "globalLogFile", None)
        log._Logger(None, "P:")._emit("hi")  # must not raise

    def test_swallows_write_error(self, monkeypatch):
        class BadFile:
            def write(self, _s):
                raise ValueError("file is closed")

            def flush(self):
                pass

        monkeypatch.setattr(log, "globalLogFile", BadFile())
        monkeypatch.setattr(log, "_MAX_LOG_BYTES", 0)  # skip the rotation branch
        log._Logger(None, "P:")._emit("boom")  # write() raises -> swallowed


class TestInitializeAndClose:
    def test_initialize_open_failure_raises_runtime_error(self, tmp_path, monkeypatch):
        # A log path whose parent dir does not exist -> os.open ENOENT.
        monkeypatch.setattr(log, "logFileName", str(tmp_path / "missing" / "x.log"))
        for attr in ("InfoLog", "WarningLog", "ErrorLog", "globalLogFile"):
            monkeypatch.setattr(log, attr, None)
        with pytest.raises(RuntimeError):
            log.Initialize(False)

    def test_close_swallows_close_error(self, monkeypatch, capsys):
        class Boom:
            def close(self):
                raise OSError("cannot close")

        monkeypatch.setattr(log, "globalLogFile", Boom())
        log.Close()  # Go ignores the close error; we must too
        assert "wrote logs to" in capsys.readouterr().out


class TestNewEvery:
    def test_constructs_every_with_timeout(self):
        ev = log.NewEvery(2.5)
        assert isinstance(ev, log.Every)
        assert ev.timeout == 2.5
