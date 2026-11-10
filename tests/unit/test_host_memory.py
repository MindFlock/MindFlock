"""Windows host memory inside WSL (``backend.host_memory``) and how the
memory gates act on it.

Pins the PowerShell line parser, staleness (a dead loop is no reading, never
a gate), the reader thread (one per process, its restart backoff, stop), that
the level is the WORSE of the guest's and the host's with the host's own
hysteresis, the Windows refusal line, the shield sizing a shed by the host's
shortfall, the launch budget capped by the host's headroom, and that the
server under pytest never starts a real PowerShell. Nothing here spawns
``powershell.exe``: the loop runs on a fake ``Popen`` and a fake clock.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from backend import host_memory as hm
from backend import memory_pressure as mp
from backend.web.core import launch_gate as gate
from backend.web.core import memory_shield as shield

GiB = 1024**3
MiB = 1024**2
HOST_TOTAL = 32 * GiB


@pytest.fixture
def box(monkeypatch):
    """``box(guest_avail_gib, host_avail_gib | None)`` → level. The guest is
    a 20 GB WSL VM with no PSI; the host a 32 GB Windows. Windows reads
    ``host_gib`` for ``held`` seconds (two readings that far apart; a rise
    needs mp._HOST_RISE_SECONDS of them) — ``held=0`` is a one-reading dip."""
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    mp._reset_for_tests()
    hm._reset_for_tests()
    clock = {"t": time.time()}

    def set_(guest_gib, host_gib, host_total=HOST_TOTAL, held=60.0):
        monkeypatch.setattr(
            mp,
            "_read_meminfo",
            lambda: {
                "total": 20 * GiB,
                "available": int(guest_gib * GiB),
                "swap_total": 0,
                "swap_free": 0,
            },
        )
        monkeypatch.setattr(mp, "_read_psi", lambda: None)
        if host_gib is None:
            hm._STATE["reading"] = None
            return mp.level(fresh=True)
        reading = {"available": int(host_gib * GiB), "total": host_total}
        clock["t"] += 10.0
        hm._set_reading(reading, now=clock["t"])
        if held:
            mp.level(fresh=True)
            clock["t"] += held
            hm._set_reading(reading, now=clock["t"])
        return mp.level(fresh=True)

    yield set_
    hm._reset_for_tests()
    mp._reset_for_tests()


def test_parse_line_reads_the_loop_and_ignores_everything_else():
    assert hm.parse_line("mem 5099000 32900000\r\n") == {
        "available": 5099000 * 1024,
        "total": 32900000 * 1024,
    }
    for junk in (
        "",
        "Windows PowerShell",
        "mem x 1",
        "mem 5 4",
        "mem 1 0",
        "mem 1 2 3",
    ):
        assert hm.parse_line(junk) is None


def test_a_stale_reading_is_no_reading():
    hm._reset_for_tests()
    hm._set_reading({"available": GiB, "total": HOST_TOTAL}, now=1000.0)
    assert hm.reading(now=1000.0 + hm.STALE_SECONDS - 1) is not None
    assert hm.reading(now=1000.0 + hm.STALE_SECONDS + 1) is None
    hm._reset_for_tests()


def test_start_is_a_no_op_outside_wsl_or_when_switched_off(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "0")
    assert hm.start() is False
    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "1")
    monkeypatch.setattr("backend.osenv.is_wsl", lambda: False)
    assert hm.start() is False


def test_no_host_reading_leaves_the_guest_level_alone(box):
    assert box(10, None) == "ok"
    snap = mp.snapshot()
    assert snap["host"] is None and snap["guest_level"] == "ok"


def test_a_short_windows_raises_the_level_while_the_vm_has_room(box):
    assert box(10, 16) == "ok"
    assert box(10, 2.4) == "ok"  # 7.5%, over 2 GiB: Windows idles here
    assert box(10, 1.9) == "low"  # under 2 GiB
    snap = mp.snapshot()
    assert snap["guest_level"] == "ok" and snap["host"]["level"] == "low"
    assert box(10, 1.2) == "critical"


def test_the_worse_of_the_two_wins(box):
    assert box(0.8, 16) == "critical"  # the VM itself is out
    assert box(2.5, 1.0) == "critical"  # guest low, host critical


def test_the_host_level_steps_down_only_past_its_recovery_lines(box):
    assert box(10, 2.0) == "low"
    assert box(10, 2.4) == "low"  # over the entry line, under recovery (8%)
    assert box(10, 2.6) == "ok"  # 8.1% and over 2.5 GiB
    assert box(10, 1.0) == "critical"
    assert box(10, 1.9) == "critical"  # recovery from critical needs 2 GiB + 6%
    assert box(10, 2.2) == "low"


def test_the_refusal_line_names_windows_when_windows_is_the_cause(box):
    box(10, 2.0)
    line = mp.describe()
    assert line.startswith("Windows memory is low: 2.0 GB free of 32.0 GB")
    box(0.8, 16)
    assert mp.describe().startswith("memory is critically low")


def test_the_shield_frees_the_hosts_shortfall_when_it_is_bigger(box):
    box(10, 1.0)
    snap = mp.snapshot()
    # guest needs nothing; the host needs its low line (max(6% of 32, 2) =
    # 2 GiB) minus 1 GiB
    want = int(round((2 * GiB - 1 * GiB) / MiB))
    assert shield.shed_need_mb(snap, 3 * GiB) == want
    box(10, 16)
    assert shield.shed_need_mb(mp.snapshot(), 3 * GiB) == 0


def test_the_launch_budget_is_capped_by_the_hosts_headroom(box):
    box(10, 6)  # host ok (6 GiB > 2.5), VM 10 GiB available
    snap = mp.snapshot()
    cost = {"session_mb": 1024, "peak_mb": 1024, "source": "measured", "samples": 3}
    b = gate.budget(snap, cost, reserve=2 * GiB)
    # VM alone: (10 - 2) GiB → 8; host: 6 - 2 → 4
    assert b["room"] == 4
    assert "Windows has 6.0 GB available" in b["why"]
    box(10, None)
    assert gate.budget(mp.snapshot(), cost, reserve=2 * GiB)["room"] == 8


# --------------------------------------------------------------------------- #
# the parser and the switch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line",
    [
        "mem 5 4",  # more available than total
        "mem -1 4",  # negative available
        "mem 0 0",  # no total
        "mem 1.5 4",  # not an integer
        "mem 1 two",
        "MEM 1 2",
        "mem 1",
    ],
)
def test_parse_line_refuses_impossible_readings(line):
    assert hm.parse_line(line) is None


def test_parse_line_takes_windows_line_endings_and_padding():
    assert hm.parse_line("  mem 1 2 \r\n") == {"available": 1024, "total": 2048}
    assert hm.parse_line("mem 0 2\r\n") == {"available": 0, "total": 2048}
    assert hm.parse_line("mem 2 2") == {"available": 2048, "total": 2048}


def test_switched_off_beats_wsl(monkeypatch):
    monkeypatch.setattr("backend.osenv.is_wsl", lambda: True)
    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "0")
    assert hm._enabled() is False
    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "1")
    assert hm._enabled() is True


def test_a_broken_osenv_reads_as_not_wsl(monkeypatch):
    def boom():
        raise RuntimeError("no /proc/version")

    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "1")
    monkeypatch.setattr("backend.osenv.is_wsl", boom)
    assert hm._enabled() is False
    import backend

    monkeypatch.delattr(backend, "osenv")
    monkeypatch.setitem(sys.modules, "backend.osenv", None)  # import fails
    assert hm._enabled() is False


# --------------------------------------------------------------------------- #
# the reader thread
# --------------------------------------------------------------------------- #
@pytest.fixture
def loop(monkeypatch):
    """``_run`` on fakes: ``loop.runs`` is a list of ``(lines, seconds)`` —
    what each PowerShell start prints and how long it lives on the fake
    monotonic clock; an empty list means every start prints nothing and dies.
    ``loop.stop_after`` sleeps end the loop. Records the sleeps and procs."""
    hm._reset_for_tests()
    st = SimpleNamespace(runs=[], stop_after=1, sleeps=[], procs=[], t=0.0)

    class FakeProc:
        def __init__(self, argv, **kw):
            lines, lasted = st.runs.pop(0) if st.runs else ([], 0.0)
            self.argv, self.kw, self.killed = argv, kw, False
            self.stdout = lines if not callable(lines) else lines(self)
            st.t += lasted
            st.procs.append(self)

        def kill(self):
            self.killed = True

        def wait(self):
            return 0

    def sleep(seconds):
        st.sleeps.append(seconds)
        if len(st.sleeps) >= st.stop_after:
            hm._STATE["stop"] = True

    monkeypatch.setattr(
        hm,
        "subprocess",
        SimpleNamespace(
            Popen=FakeProc, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE
        ),
    )
    monkeypatch.setattr(
        hm,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: st.t, sleep=sleep),
    )
    monkeypatch.setattr(hm, "_powershell", lambda: "/fake/powershell.exe")
    st.Proc = FakeProc
    yield st
    hm._reset_for_tests()


def test_no_powershell_means_no_loop_and_no_retry(loop, monkeypatch):
    monkeypatch.setattr(hm, "_powershell", lambda: None)
    hm._run()
    assert loop.procs == [] and loop.sleeps == []
    assert hm.reading() is None


def test_only_mem_lines_become_the_reading(loop):
    loop.runs = [(["Windows PowerShell\r\n", "garbage\r\n", "mem 4000 32000\r\n"], 1)]
    before = time.time()
    hm._run()
    r = hm.reading()
    assert r["available"] == 4000 * 1024 and r["total"] == 32000 * 1024
    assert before <= r["at"] <= time.time()
    (proc,) = loop.procs
    assert proc.argv[0] == "/fake/powershell.exe" and "-NoProfile" in proc.argv
    assert proc.kw["stdin"] == subprocess.DEVNULL
    assert proc.killed and hm._STATE["proc"] is None


def test_garbage_alone_sets_no_reading(loop):
    loop.runs = [(["Windows PowerShell\r\n", "mem x y\r\n"], 1)]
    hm._run()
    assert hm.reading() is None


def test_a_dying_loop_restarts_with_a_doubling_capped_wait(loop):
    loop.stop_after = 7
    hm._run()
    assert loop.sleeps == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert len(loop.procs) == 7


def test_a_loop_that_lived_a_while_starts_over_at_the_short_wait(loop):
    good = ["mem 4000 32000\n"]
    loop.runs = [
        ([], 0.0),  # dies at once → 15
        (good, 10.0),  # readings, but short-lived → 30, no reset
        (good, hm._BACKOFF_CAP + 1),  # readings for over the cap → back to 15
        ([], hm._BACKOFF_CAP + 1),  # long-lived, no readings → no reset: 30
    ]
    loop.stop_after = 4
    hm._run()
    assert loop.sleeps == [15.0, 30.0, 15.0, 30.0]


def test_stop_kills_the_loop_and_returns_without_a_wait(loop):
    def lines(proc):
        yield "mem 1 2\n"
        hm.stop()  # the server goes away mid-loop
        yield "mem 1 2\n"
        raise AssertionError("read past the stop")

    loop.runs = [(lines, 1)]
    hm._run()
    (proc,) = loop.procs
    assert hm._STATE["stop"] is True and proc.killed
    assert loop.sleeps == [] and hm._STATE["proc"] is None


def test_stop_kills_the_current_proc_and_reset_clears_everything(loop):
    proc = loop.Proc(["x"])
    hm._STATE["proc"] = proc
    hm._set_reading({"available": 1, "total": 2})
    hm.stop()
    assert hm._STATE["stop"] is True and proc.killed
    hm._reset_for_tests()
    assert hm._STATE == {"reading": None, "thread": None, "proc": None, "stop": False}


def test_start_runs_one_reader_per_process(monkeypatch):
    hm._reset_for_tests()
    release = threading.Event()
    calls = []

    def run():
        calls.append(1)
        release.wait(5)

    monkeypatch.setattr(hm, "_enabled", lambda: True)
    monkeypatch.setattr(hm, "_run", run)
    try:
        assert hm.start() is True
        t = hm._STATE["thread"]
        assert hm.start() is True
        assert hm._STATE["thread"] is t and t.daemon and t.name == "host-memory"
    finally:
        release.set()
        hm._STATE["thread"].join(5)
    assert calls == [1]
    # Once the reader has ended, a new start brings up a fresh one.
    release.clear()
    try:
        assert hm.start() is True
        assert hm._STATE["thread"] is not t
    finally:
        release.set()
        hm._STATE["thread"].join(5)
    assert calls == [1, 1]
    hm._reset_for_tests()


def test_the_server_under_pytest_never_starts_the_reader(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    hm._reset_for_tests()
    # Even "inside WSL" with a PowerShell to run, the lifespan holds off.
    monkeypatch.setattr("backend.osenv.is_wsl", lambda: True)
    monkeypatch.setenv("MINDFLOCK_HOST_MEMORY", "1")
    monkeypatch.setattr(hm, "_powershell", lambda: None)
    started = []
    monkeypatch.setattr(hm, "start", lambda: started.append(1) or True)
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    with TestClient(server.app):
        pass
    assert started == [] and hm._STATE["thread"] is None


# --------------------------------------------------------------------------- #
# the host's level
# --------------------------------------------------------------------------- #
def test_host_low_line_is_the_bigger_of_the_share_and_the_floor():
    assert mp.host_low_line(16 * GiB) == 2 * GiB  # 6% would be 0.96
    assert mp.host_low_line(64 * GiB) == pytest.approx(0.06 * 64 * GiB)  # 3.84
    assert mp.host_thresholds()["low"] == (0.06, 2 * GiB)


def test_only_a_short_windows_still_reads_low_to_every_caller(box):
    box(10, 2.0)
    assert mp.level() == "low" and mp.is_low() is True
    box(10, 16)
    assert mp.is_low(fresh=True) is False


def test_from_critical_a_big_recovery_goes_straight_to_ok(box):
    assert box(10, 1.0) == "critical"
    assert box(10, 16) == "ok"


def test_leaving_low_needs_both_the_share_and_the_bytes(box):
    # 16 GB Windows: 2.3 GB is 14.4% (past 8%) but under 2.5 GB → stays low.
    assert box(10, 2.0, host_total=16 * GiB) == "low"
    assert box(10, 2.3, host_total=16 * GiB) == "low"
    assert box(10, 2.6, host_total=16 * GiB) == "ok"
    mp._reset_for_tests()
    # 64 GB Windows: 4.5 GB is past 2.5 GB but 7.0% (under 8%) → stays low.
    assert box(10, 3.5, host_total=64 * GiB) == "low"  # under 6% (3.84)
    assert box(10, 4.5, host_total=64 * GiB) == "low"
    assert box(10, 5.2, host_total=64 * GiB) == "ok"  # 8.1%


def test_leaving_critical_needs_both_the_share_and_the_bytes(box):
    # 64 GB Windows: critical at ≤4% (2.56 GB); 2.2 GB clears 2 GB but is
    # 3.4% → still critical; 4 GB (6.25%) → low, not ok.
    assert box(10, 2.2, host_total=64 * GiB) == "critical"
    assert box(10, 3.0, host_total=64 * GiB) == "critical"  # 4.7%: under 6%
    assert box(10, 4.0, host_total=64 * GiB) == "low"


def test_a_lost_reading_forgets_the_hosts_level(box):
    assert box(10, 1.0) == "critical"
    assert box(10, None) == "ok"
    assert mp._STATE["host_level"] == "ok"
    # 1.9 GB would hold critical by hysteresis; fresh from ok it is only low.
    assert box(10, 1.9) == "low"


def test_a_dip_shorter_than_the_rise_window_never_moves_the_level(box):
    assert box(10, 16) == "ok"
    assert box(10, 1.9, held=0) == "ok"  # one reading under the low line
    assert box(10, 1.9, held=10) == "ok"  # 20 s past it: short of 30
    assert box(10, 3.0) == "ok"  # back over it: the count starts again
    assert box(10, 1.9, held=10) == "ok"  # 10 s
    assert box(10, 1.9, held=10) == "low"  # 30 s past the line, all told


def test_critical_rises_after_two_readings(box):
    assert box(10, 16) == "ok"
    assert box(10, 1.0, held=0) == "ok"  # one reading: not yet
    assert box(10, 1.0, held=0) == "critical"  # 10 s later: held
    assert box(10, 16) == "ok"
    # Past critical but not yet held: low still needs its own 30 s.
    assert box(10, 1.0, held=0) == "ok"


def test_a_low_level_steps_down_at_once(box):
    assert box(10, 1.9) == "low"
    assert box(10, 2.6, held=0) == "ok"


def test_the_2026_11_09_plateau_reads_ok(box):
    # Windows idled at ~2.8 GB of 31.4 for an hour after one dip to 2 GB:
    # the old 3.5 GB recovery line held it low (and the wake word off).
    total = int(31.4 * GiB)
    assert box(10, 2.0, host_total=total, held=0) == "ok"
    for _ in range(6):
        assert box(10, 2.8, host_total=total) == "ok"


def test_a_stale_reading_drops_out_of_the_snapshot(box):
    assert box(10, 1.0) == "critical"
    hm._STATE["reading"]["at"] = time.time() - hm.STALE_SECONDS - 1
    assert mp.level(fresh=True) == "ok"
    assert mp.snapshot()["host"] is None


def test_the_snapshot_carries_the_host_reading(box):
    box(10, 2.0)
    host = mp.snapshot()["host"]
    assert set(host) == {"available", "total", "available_pct", "level", "at"}
    assert host["available"] == 2 * GiB and host["total"] == HOST_TOTAL
    assert host["available_pct"] == 6.2 and host["level"] == "low"


def test_the_refusal_line_keeps_the_guest_wording_when_both_are_short(box):
    # Guest 2.5 of 20 GB (12.5%) is low, and so is Windows at 2 GB.
    assert box(2.5, 2.0) == "low"
    assert mp.snapshot()["guest_level"] == "low"
    assert mp.describe().startswith("memory is low: 2.5 GB free of 20.0 GB")
    # Windows worse than the guest: Windows is named.
    assert box(2.5, 1.0) == "critical"
    assert (
        mp.describe() == "Windows memory is critically low: 1.0 GB free of 32.0 GB (3%)"
    )


# --------------------------------------------------------------------------- #
# the host's rise delay (_classify_host with the reading's time)
# --------------------------------------------------------------------------- #
@pytest.fixture
def host():
    """``host(prev, gib, at, total=32 GiB)`` → ``_classify_host``'s level for
    one Windows reading of ``gib`` GiB available at ``at`` (seconds)."""
    mp._reset_for_tests()

    def classify(prev, gib, at=None, total=HOST_TOTAL):
        avail = int(gib * GiB)
        return mp._classify_host(prev, avail / total, avail, at)

    yield classify
    mp._reset_for_tests()


def test_low_rises_at_exactly_the_rise_window(host):
    assert mp._HOST_RISE_SECONDS == {"low": 30.0, "critical": 10.0}
    assert host("ok", 1.9, at=100.0) == "ok"  # the first reading starts the count
    assert host("ok", 1.9, at=129.9) == "ok"
    assert host("ok", 1.9, at=130.0) == "low"


def test_a_reading_back_over_the_line_restarts_the_count(host):
    assert host("ok", 1.9, at=100.0) == "ok"
    assert host("ok", 1.9, at=125.0) == "ok"
    assert host("ok", 2.1, at=128.0) == "ok"  # over 2 GiB: the count clears
    assert "low" not in mp._STATE["host_since"]
    assert host("ok", 1.9, at=131.0) == "ok"  # 31 s after the FIRST dip: no
    assert host("ok", 1.9, at=160.9) == "ok"
    assert host("ok", 1.9, at=161.0) == "low"  # 30 s from the second dip


def test_critical_needs_ten_seconds_and_one_reading_alone_never_does(host):
    assert host("ok", 1.0, at=100.0) == "ok"
    assert host("ok", 1.0, at=109.9) == "ok"
    assert host("ok", 1.0, at=110.0) == "critical"


def test_a_raw_critical_not_yet_held_falls_back_to_low_when_low_is_held(host):
    # Past low since 100; past critical only since 128.
    assert host("ok", 1.9, at=100.0) == "ok"
    assert host("ok", 1.0, at=128.0) == "ok"  # low 28 s, critical 0 s
    assert host("ok", 1.0, at=130.0) == "low"  # low 30 s, critical 2 s
    # With less than 30 s past low and prev ok, it stays ok.
    mp._reset_for_tests()
    assert host("ok", 1.0, at=200.0) == "ok"
    assert host("ok", 1.0, at=205.0) == "ok"


def test_a_low_level_is_kept_while_a_raw_critical_is_not_yet_held(host):
    assert host("low", 1.0, at=100.0) == "low"  # never back to ok
    assert host("low", 1.0, at=109.0) == "low"
    assert host("low", 1.0, at=110.0) == "critical"


def test_stepping_down_is_never_delayed(host):
    # From low: one reading at 8% and 2.5 GiB or more is ok at once.
    assert host("low", 2.6, at=100.0) == "ok"
    # Under the recovery line (7.5%) a low level holds.
    assert host("low", 2.4, at=100.0) == "low"
    # From critical: one reading past 6% and 2 GiB is low (or ok) at once.
    assert host("critical", 2.1, at=100.0) == "low"
    assert host("critical", 16, at=100.0) == "ok"
    assert host("critical", 1.9, at=100.0) == "critical"  # under 2 GiB


def test_without_a_reading_time_the_level_rises_at_once(host):
    assert host("ok", 1.9) == "low"
    assert host("ok", 1.0) == "critical"
    assert host("low", 1.0) == "critical"
    assert mp._STATE["host_since"] == {}  # no time, no count


def test_a_corrupt_since_record_is_rebuilt(host):
    mp._STATE["host_since"] = "junk"
    assert host("ok", 1.9, at=100.0) == "ok"
    assert mp._STATE["host_since"] == {"low": 100.0}
    mp._STATE["host_since"] = None
    assert host("ok", 16, at=100.0) == "ok"
    assert mp._STATE["host_since"] == {}


def test_reset_clears_the_since_record(host):
    host("ok", 1.0, at=100.0)
    assert mp._STATE["host_since"] == {"low": 100.0, "critical": 100.0}
    mp._reset_for_tests()
    assert mp._STATE["host_since"] == {}


def test_a_stale_reading_forgets_when_windows_went_past_the_line(box):
    assert box(10, 1.9, held=20) == "ok"  # past low for 20 s
    assert mp._STATE["host_since"].get("low") is not None
    hm._STATE["reading"]["at"] = time.time() - hm.STALE_SECONDS - 1
    assert mp.level(fresh=True) == "ok"
    assert mp._STATE["host_since"] == {}
    # The next dip counts from itself, not from the one before the gap.
    assert box(10, 1.9, held=0) == "ok"
    assert box(10, 1.9, held=10) == "ok"  # 20 s since the new dip
    assert box(10, 1.9, held=0) == "low"  # 30 s


def test_a_host_reading_switched_off_forgets_the_since_record(box, monkeypatch):
    assert box(10, 1.9, held=20) == "ok"
    assert mp._STATE["host_since"]
    monkeypatch.setattr(hm, "reading", lambda now=None: None)
    assert mp.level(fresh=True) == "ok"
    assert mp._STATE["host_since"] == {} and mp._STATE["host_level"] == "ok"


def test_the_narrow_low_band_on_a_32_and_a_16_gb_windows(host):
    # 32 GB: 2.4 GiB is 7.5% — under the 8% recovery share, stays low.
    assert host("low", 2.4, at=100.0) == "low"
    assert host("low", 2.6, at=100.0) == "ok"  # 8.1%, over 2.5 GiB
    # 16 GB: the floor binds both ways (6% = 0.96 GiB, 8% = 1.28 GiB).
    small = 16 * GiB
    assert host("ok", 2.0, at=None, total=small) == "low"  # at the 2 GiB floor
    assert host("ok", 2.1, at=None, total=small) == "ok"
    assert host("low", 2.4, at=None, total=small) == "low"  # 15%, under 2.5 GiB
    assert host("low", 2.5, at=None, total=small) == "ok"
    # 64 GB: the share binds (6% = 3.84 GiB, 8% = 5.12 GiB).
    big = 64 * GiB
    assert host("ok", 3.8, at=None, total=big) == "low"
    assert host("ok", 3.9, at=None, total=big) == "ok"
    assert host("low", 5.1, at=None, total=big) == "low"
    assert host("low", 5.2, at=None, total=big) == "ok"
