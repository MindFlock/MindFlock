"""Windows host memory inside WSL (``backend.host_memory``) and how the
memory gates act on it.

Pins the PowerShell line parser, staleness (a dead loop is no reading, never
a gate), the reader thread (one per process, its restart backoff, stop), that
the level is the WORSE of the guest's and the host's with the host's own
signals (headroom floor, raw 256 MB floor, commit charge, sustained hard
paging — never raw "available" alone) and hysteresis, the Windows refusal
line, the shield sizing a shed by the host's
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
    """``box(guest_avail_gib, host_avail_gib | None, *, free_gib, commit,
    pages_in)`` → level. The guest is a 20 GB WSL VM with no PSI and no
    vmstat (``free_gib`` None: no MemFree, so it can reuse nothing); the host
    a 32 GB Windows (``commit``: the commit charge as a share of a 40 GB
    limit; ``pages_in``: this reading's hard-paging rate). No sustain here:
    every reading moves the level (test_memory_gauge pins the sustain)."""
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    monkeypatch.setattr(mp, "_read_vmstat", lambda: None)
    mp._reset_for_tests()
    hm._reset_for_tests()

    def set_(
        guest_gib,
        host_gib,
        host_total=HOST_TOTAL,
        *,
        free_gib=None,
        commit=None,
        pages_in=None,
    ):
        mem = {
            "total": 20 * GiB,
            "available": int(guest_gib * GiB),
            "swap_total": 0,
            "swap_free": 0,
        }
        if free_gib is not None:
            mem["free"] = int(free_gib * GiB)
        monkeypatch.setattr(mp, "_read_meminfo", lambda: dict(mem))
        monkeypatch.setattr(mp, "_read_psi", lambda: None)
        if host_gib is None:
            hm._STATE["reading"] = None
        else:
            r = {"available": int(host_gib * GiB), "total": host_total}
            if commit is not None or pages_in is not None:
                r.update(
                    committed=int((commit or 0.5) * 40 * GiB),
                    commit_limit=40 * GiB,
                    pages_in=int(pages_in or 0),
                )
            hm._set_reading(r)
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
        "mem 1 2 3 4",
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


def test_a_windows_with_no_headroom_raises_the_level_while_the_vm_has_room(box):
    """Headroom is a floor now: low at 2 GB, critical at 1 GB (the guest here
    can reuse nothing, so headroom is Windows' own available)."""
    assert box(10, 16) == "ok"
    assert box(10, 2.4) == "ok"  # was low under the old 2.5 GB share line
    assert box(10, 1.9) == "low"
    snap = mp.snapshot()
    assert snap["guest_level"] == "ok" and snap["host"]["level"] == "low"
    assert box(10, 0.9) == "critical"


def test_the_worse_of_the_two_wins(box):
    assert box(0.8, 16) == "critical"  # the VM itself is out (under 1 GB)
    assert box(1.4, 0.9) == "critical"  # guest low, host critical


def test_the_host_level_steps_down_only_past_its_recovery_lines(box):
    assert box(10, 1.9) == "low"
    assert box(10, 2.5) == "low"  # over the entry floor, under recovery (3)
    assert box(10, 3.1) == "ok"
    assert box(10, 1.0) == "critical"
    assert box(10, 1.4) == "critical"  # recovery from critical needs 1.5 GB
    assert box(10, 1.6) == "low"


def test_the_refusal_line_names_the_windows_signal(box):
    box(10, 1.9)
    assert mp.describe().startswith("memory is low: Windows has only 1.9 GB to give")
    box(0.8, 16)
    assert mp.describe() == "memory is critical: only 0.8 GB of 20.0 GB available (4%)"


def test_the_shield_frees_the_hosts_shortfall_when_it_is_bigger(box):
    box(10, 0.9)
    snap = mp.snapshot()
    # guest needs nothing; the host needs its 2 GB headroom floor minus 0.9
    want = int(round((2 * GiB - int(0.9 * GiB)) / MiB))
    assert shield.shed_need_mb(snap, 3 * GiB) == want
    box(10, 16)
    assert shield.shed_need_mb(mp.snapshot(), 3 * GiB) == 0


def test_the_launch_budget_is_capped_by_the_hosts_headroom(box):
    box(10, 6)  # host ok, VM 10 GiB available
    snap = mp.snapshot()
    cost = {"session_mb": 1024, "peak_mb": 1024, "source": "measured", "samples": 3}
    b = gate.budget(snap, cost, reserve=2 * GiB)
    # VM alone: (10 - 2) GiB → 8; host: 6 - 2 (its headroom floor) → 4
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


def test_parse_line_reads_the_long_line_with_commit_and_paging():
    assert hm.parse_line("mem 1739264 32903628 46353840 60166604 149\r\n") == {
        "available": 1739264 * 1024,
        "total": 32903628 * 1024,
        "committed": 46353840 * 1024,
        "commit_limit": 60166604 * 1024,
        "pages_in": 149,
    }
    # impossible extras keep the short reading
    assert hm.parse_line("mem 1 2 3 0 5") == {"available": 1024, "total": 2048}
    assert hm.parse_line("mem 1 2 3 4 -5") == {"available": 1024, "total": 2048}
    assert hm.parse_line("mem 1 2 x 4 5") == {"available": 1024, "total": 2048}


def test_parse_line_long_form_edges():
    # PagesInputPersec can arrive with a fraction: truncated, not refused.
    assert hm.parse_line("mem 1 2 3 4 123.7")["pages_in"] == 123
    # A negative commit charge or limit is impossible: the short reading.
    assert hm.parse_line("mem 1 2 -3 4 5") == {"available": 1024, "total": 2048}
    assert hm.parse_line("mem 1 2 3 -4 5") == {"available": 1024, "total": 2048}
    assert hm.parse_line("mem 1 2 3 4 n/a") == {"available": 1024, "total": 2048}
    # A bad short part still refuses the whole long line.
    assert hm.parse_line("mem 5 4 3 4 5") is None
    # 4 or 5 fields are neither form.
    assert hm.parse_line("mem 1 2 3") is None
    assert hm.parse_line("mem 1 2 3 4") is None
    assert hm.parse_line("mem 1 2 3 4 5 6") is None


def test_the_script_asks_for_the_commit_charge_and_paging_but_survives_without():
    assert "Win32_PerfFormattedData_PerfOS_Memory" in hm._SCRIPT
    assert "CommitLimit" in hm._SCRIPT and "PagesInputPersec" in hm._SCRIPT
    assert "catch" in hm._SCRIPT  # no perf counters: the short line still goes
    # The long line's three extras, in kB like the short ones, appended to
    # the short ``mem <avail> <total>`` the parser reads.
    assert "[math]::Floor($p.CommittedBytes / 1024)" in hm._SCRIPT
    assert "[math]::Floor($p.CommitLimit / 1024)" in hm._SCRIPT
    assert "(' {0} {1} {2}' -f " in hm._SCRIPT
    assert (
        "('mem {0} {1}' -f $o.FreePhysicalMemory, $o.TotalVisibleMemorySize) + $x"
        in hm._SCRIPT
    )


def test_one_spike_among_quiet_readings_leaves_the_median_alone():
    hm._reset_for_tests()
    base = {"available": GiB, "total": HOST_TOTAL, "committed": 1, "commit_limit": 2}
    for i, rate in enumerate([100, 100, 20000, 100, 100, 100, 100]):
        hm._set_reading(dict(base, pages_in=rate), now=1000.0 + 10 * i)
        r = hm.reading(now=1000.0 + 10 * i)
        assert r["pages_in_samples"] == min(i + 1, hm.PAGING_WINDOW)
    assert r["pages_in_median"] == 100
    assert len(hm._STATE["paging"]) == hm.PAGING_WINDOW
    # A short line in between (perf counters gone) empties the window: the
    # next long line starts a fresh minute, the old spike never comes back.
    hm._set_reading({"available": GiB, "total": HOST_TOTAL}, now=1070.0)
    r = hm.reading(now=1070.0)
    assert "pages_in_median" not in r and "pages_in_samples" not in r
    hm._set_reading(dict(base, pages_in=40), now=1080.0)
    r = hm.reading(now=1080.0)
    assert r["pages_in_median"] == 40 and r["pages_in_samples"] == 1
    # A stale long reading is no reading, extras and all; a reset empties
    # the window.
    assert hm.reading(now=1080.0 + hm.STALE_SECONDS + 1) is None
    hm._reset_for_tests()
    assert hm._STATE["paging"] == [] and hm.reading() is None


def test_the_paging_rate_is_the_median_of_the_last_minute():
    hm._reset_for_tests()
    base = {"available": GiB, "total": HOST_TOTAL, "committed": 1, "commit_limit": 2}
    for i, rate in enumerate([600, 0, 0, 0, 0, 0, 60]):
        hm._set_reading(dict(base, pages_in=rate), now=1000.0 + 10 * i)
    r = hm.reading(now=1060.0)
    assert r["pages_in"] == 60 and r["pages_in_samples"] == hm.PAGING_WINDOW
    assert r["pages_in_median"] == 0  # one 60 among zeros moves nothing
    hm._set_reading(dict(base, pages_in=600), now=1065.0)
    hm._set_reading(dict(base, pages_in=900), now=1066.0)
    hm._set_reading(dict(base, pages_in=700), now=1067.0)
    assert hm.reading(now=1067.0)["pages_in_median"] == 330.0  # [0,0,60,600,700,900]
    hm._set_reading({"available": GiB, "total": HOST_TOTAL}, now=1070.0)
    assert "pages_in_median" not in hm.reading(now=1070.0)  # short line: no rate
    # a gap (the loop restarted) starts the window over
    hm._set_reading(dict(base, pages_in=50), now=1080.0)
    hm._set_reading(dict(base, pages_in=70), now=1200.0)
    assert hm.reading(now=1200.0)["pages_in_samples"] == 1
    hm._reset_for_tests()


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
    assert hm._STATE == {
        "reading": None,
        "thread": None,
        "proc": None,
        "stop": False,
        "paging": [],
    }


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
def test_host_low_line_is_the_headroom_floor_whatever_the_size():
    assert mp.host_low_line(16 * GiB) == 2 * GiB
    assert mp.host_low_line(64 * GiB) == 2 * GiB
    assert mp.host_thresholds()["low"] == (2 * GiB, 85.0, 3000.0)
    assert mp.host_thresholds()["critical"] == (GiB, 90.0, 8000.0)


def test_only_a_short_windows_still_reads_low_to_every_caller(box):
    box(10, 1.9)
    assert mp.level() == "low" and mp.is_low() is True
    box(10, 16)
    assert mp.is_low(fresh=True) is False


def test_from_critical_a_big_recovery_goes_straight_to_ok(box):
    assert box(10, 0.9) == "critical"
    assert box(10, 16) == "ok"


def test_raw_windows_available_alone_is_not_distress(box):
    """2026-11-10: FreePhysicalMemory swung 0.7–3.4 GB in minutes while
    nothing was short. With the guest's cache as headroom, 0.7 GB reads ok."""
    assert box(12, 0.7, free_gib=1.0) == "ok"
    assert box(12, 0.3, free_gib=1.0) == "ok"  # still over the 256 MB floor
    assert box(12, 0.2, free_gib=1.0) == "critical"  # under it: Windows pages
    assert box(12, 0.4, free_gib=1.0) == "critical"  # held to 512 MB
    assert box(12, 0.6, free_gib=1.0) == "ok"


def test_the_commit_charge_is_the_hosts_real_danger(box):
    """At the commit limit an allocation fails, whatever "available" says."""
    assert box(10, 16, commit=0.75) == "ok"  # what this machine reads
    assert box(10, 16, commit=0.86) == "low"
    assert box(10, 16, commit=0.91) == "critical"
    assert "Windows commit charge is at 91% of its limit" in mp.describe()
    assert box(10, 16, commit=0.88) == "critical"  # recovery needs ≤ 87
    assert box(10, 16, commit=0.86) == "low"
    assert box(10, 16, commit=0.82) == "low"  # recovery needs ≤ 80
    assert box(10, 16, commit=0.79) == "ok"


def test_hard_paging_alone_is_reported_not_a_level(box):
    """A minute of page-ins with Windows short is reported in the snapshot,
    but only stalls, swap-in, OOM kills, the commit charge or the floors set
    the level (owner, 2026-11-10: "pretty arbitrary")."""
    kw = dict(free_gib=1.0, commit=0.5)
    for _ in range(4):
        level = box(10, 1.5, pages_in=20000, **kw)
    assert level == "ok"
    assert mp.snapshot()["host"]["pages_in_median"] == 20000
    assert "Windows is paging" not in mp.describe()


def test_one_paging_spike_in_a_quiet_minute_is_not_distress(box):
    """The owner saw PagesInputPersec ~20000 at one sample while nothing was
    short: the median of the minute ignores it."""
    kw = dict(free_gib=1.0, commit=0.6)
    levels = [box(10, 1.2, pages_in=p, **kw) for p in (0, 0, 20000, 0, 0, 0)]
    assert levels == ["ok"] * 6
    assert mp.snapshot()["host"]["pages_in_median"] == 0


def test_a_lost_reading_forgets_the_hosts_level(box):
    assert box(10, 0.9) == "critical"
    assert box(10, None) == "ok"
    assert mp._STATE["host_level"] == "ok"
    # 1.4 GB would hold critical by hysteresis; fresh from ok it is only low.
    assert box(10, 1.4) == "low"


def test_a_stale_reading_drops_out_of_the_snapshot(box):
    assert box(10, 0.9) == "critical"
    hm._STATE["reading"]["at"] = time.time() - hm.STALE_SECONDS - 1
    assert mp.level(fresh=True) == "ok"
    assert mp.snapshot()["host"] is None


def test_the_snapshot_carries_the_host_reading(box):
    box(10, 1.9)
    host = mp.snapshot()["host"]
    assert set(host) == {
        "available",
        "total",
        "available_pct",
        "guest_reusable",
        "headroom",
        "headroom_pct",
        "commit_pct",
        "pages_in_median",
        "level",
        "pending",
        "why",
        "at",
    }
    assert host["available"] == int(1.9 * GiB) and host["total"] == HOST_TOTAL
    assert host["available_pct"] == 5.9 and host["level"] == "low"
    assert host["commit_pct"] is None and host["pages_in_median"] is None
    box(10, 16, commit=0.5, pages_in=10)
    host = mp.snapshot()["host"]
    assert host["commit_pct"] == 50.0 and host["commit_limit"] == 40 * GiB
    assert host["pages_in_median"] == 10


def test_the_refusal_line_names_both_sides_when_both_are_short(box):
    # Guest 1.4 of 20 GB is under its 1.5 GB floor, and Windows at 1.9 GB.
    assert box(1.4, 1.9) == "low"
    assert mp.snapshot()["guest_level"] == "low"
    assert mp.describe() == (
        "memory is low: only 1.4 GB of 20.0 GB available (7%); Windows has "
        "only 1.9 GB to give (1.9 GB available, 0.0 GB WSL can reuse)"
    )
    # Windows worse than the guest: only Windows is named.
    assert box(1.4, 0.9) == "critical"
    assert mp.describe() == (
        "memory is critical: Windows has only 0.9 GB to give (0.9 GB "
        "available, 0.0 GB WSL can reuse)"
    )


# --------------------------------------------------------------------------- #
# headroom: guest memory the VM already holds counts for Windows
# --------------------------------------------------------------------------- #
def _wsl(monkeypatch, *, guest_avail, guest_free, host_avail, host_total=HOST_TOTAL):
    """A 25 GB WSL guest whose MemAvailable is mostly page cache."""
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    monkeypatch.setattr(mp, "_read_vmstat", lambda: None)
    mp._reset_for_tests()
    hm._reset_for_tests()
    monkeypatch.setattr(
        mp,
        "_read_meminfo",
        lambda: {
            "total": 25 * GiB,
            "available": int(guest_avail * GiB),
            "free": int(guest_free * GiB),
            "swap_total": 0,
            "swap_free": 0,
        },
    )
    monkeypatch.setattr(mp, "_read_psi", lambda: None)
    hm._set_reading({"available": int(host_avail * GiB), "total": host_total})
    return mp.snapshot(fresh=True)


def test_guest_cache_the_vm_already_holds_is_windows_headroom(monkeypatch):
    """2026-11-10: Windows read 0.8 GB free of 31.4 GB while WSL held ~11 GB
    of page cache; every start queued though a new agent would only have
    reused that cache. The cache counts: the level reads ok."""
    snap = _wsl(monkeypatch, guest_avail=11.7, guest_free=0.7, host_avail=0.8)
    host = snap["host"]
    assert host["available"] == int(0.8 * GiB)
    assert host["guest_reusable"] == int(11.7 * GiB) - int(0.7 * GiB)
    assert host["headroom"] == host["available"] + host["guest_reusable"]
    assert host["level"] == "ok" and snap["level"] == "ok"
    assert mp.describe(snap) == ""
    hm._reset_for_tests()
    mp._reset_for_tests()


def test_free_guest_memory_is_not_headroom(monkeypatch):
    """MemFree may already be handed back to Windows: reusing it grows the
    VM, so only available-minus-free counts."""
    snap = _wsl(monkeypatch, guest_avail=12, guest_free=11.5, host_avail=0.8)
    assert snap["host"]["guest_reusable"] == int(0.5 * GiB)
    # 0.8 + 0.5 = 1.3 GB: under the 2 GB headroom floor, over critical's 1 GB.
    assert snap["host"]["level"] == "low" and snap["level"] == "low"
    assert "0.5 GB WSL can reuse" in mp.describe(snap)
    hm._reset_for_tests()
    mp._reset_for_tests()


def test_a_windows_paging_its_apps_reads_critical_whatever_the_guest_holds(
    monkeypatch,
):
    snap = _wsl(monkeypatch, guest_avail=12, guest_free=0.5, host_avail=0.2)
    assert snap["host"]["level"] == "critical" and snap["level"] == "critical"
    assert snap["why"][0].startswith("Windows is down to 204 MB available")
    hm._reset_for_tests()
    mp._reset_for_tests()


def test_no_memfree_reading_counts_nothing_extra():
    assert mp.guest_reusable({"available": 10 * GiB}) == 0
    assert mp.guest_reusable(None) == 0
    assert mp.guest_reusable({"available": GiB, "free": 2 * GiB}) == 0


def test_host_headroom_falls_back_to_available_for_an_older_snapshot():
    assert mp.host_headroom({"available": 3 * GiB}) == 3 * GiB
    assert mp.host_headroom({"available": GiB, "headroom": 9 * GiB}) == 9 * GiB
    assert mp.host_headroom(None) == 0


def test_the_gate_and_the_shield_read_the_headroom():
    from backend.web.core import launch_gate as gate
    from backend.web.core import memory_shield as shield

    host = {
        "available": int(0.8 * GiB),
        "headroom": 12 * GiB,
        "total": HOST_TOTAL,
        "level": "ok",
    }
    snap = {
        "level": "ok",
        "available": 12 * GiB,
        "total": 25 * GiB,
        "swap_total": 0,
        "swap_free": 0,
        "host": host,
    }
    assert shield.shed_need_mb(snap, low_line_bytes=2 * GiB) == 0
    cost = {"session_mb": 1024, "peak_mb": 1024, "source": "measured", "samples": 3}
    b = gate.budget(snap, cost, reserve=2 * GiB)
    # guest: (12 - 2) GiB → 10; Windows headroom: 12 - 2 (its floor) → 10
    assert b["room"] == 10
    host["headroom"] = 7 * GiB
    assert gate.budget(snap, cost, reserve=2 * GiB)["room"] == 5
