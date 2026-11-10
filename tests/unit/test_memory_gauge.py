"""The memory gauge reads DISTRESS (``backend.memory_pressure``), 2026-11-10.

The owner: "It has 9.7/24 GB and thinks we are critical; it is far too
trigger happy." Pins the distress signals beyond the available floor and
PSI (test_memory_pressure.py has those): the swap-in RATE (swap merely in
use is nothing), OOM kills (low alone, critical with company), the
``/proc/vmstat`` parser; the sustain (a step up waits
:data:`~backend.memory_pressure.SUSTAIN_SECONDS`, a step down does not) and
``pending``; ``why`` and the plain-words ``describe``; the shield acting only
on sustained critical; the launch gate's swap penalty only while swapping
in; and :func:`~backend.memory_pressure.replay`, the back-test over
``memory.jsonl``.
"""

from __future__ import annotations

import pytest

from backend import host_memory as hm
from backend import memory_pressure as mp
from backend.web.core import launch_gate as gate
from backend.web.core import memory_shield as shield

GiB = 1024**3
MiB = 1024**2
TOTAL = 24 * GiB


class Box:
    """A fake guest whose readings and clock the test moves."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        self.avail = 14 * GiB
        self.psi = {"some_avg10": 0.2, "some_avg60": 0.2, "full_avg10": 0.1}
        self.psi["full_avg60"] = 0.1
        self.vm = {"pswpin": 1000, "oom_kill": 0}
        monkeypatch.setattr(mp, "_clock", lambda: self.t)
        monkeypatch.setattr(mp, "_read_meminfo", self._mem)
        monkeypatch.setattr(mp, "_read_psi", lambda: dict(self.psi))
        monkeypatch.setattr(mp, "_read_vmstat", lambda: dict(self.vm))

    def _mem(self):
        return {
            "total": TOTAL,
            "available": int(self.avail),
            "free": 2 * GiB,
            "swap_total": 8 * GiB,
            "swap_free": 4 * GiB,  # 4 GB of swap IN USE: not distress
        }

    def read(self, dt: float = 10.0, swapped_in: int = 0) -> dict:
        """Advance the clock ``dt`` s, count ``swapped_in`` more pages."""
        self.t += dt
        self.vm["pswpin"] += swapped_in
        return mp.snapshot(fresh=True)


@pytest.fixture
def box(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    mp._reset_for_tests()
    hm._reset_for_tests()
    b = Box(monkeypatch)
    yield b
    mp._reset_for_tests()
    hm._reset_for_tests()


# --------------------------------------------------------------------------- #
# the owner's machine reads ok
# --------------------------------------------------------------------------- #
def test_the_owners_machine_today_reads_ok(box, monkeypatch):
    """26 GB guest, 16.6 GB available, PSI 0.2, 0.4 GB of swap used; Windows
    0.7 GB "available" of 31.4 with commit at 75% — every old rule tripped on
    Windows' available alone. Nothing here is distress."""
    monkeypatch.setattr(
        mp,
        "_read_meminfo",
        lambda: {
            "total": 26 * GiB,
            "available": int(16.6 * GiB),
            "free": int(1.6 * GiB),
            "swap_total": 8 * GiB,
            "swap_free": int(7.6 * GiB),
        },
    )
    for i in range(8):
        hm._set_reading(
            {
                "available": int(0.7 * GiB),
                "total": int(31.4 * GiB),
                "committed": int(0.75 * 60 * GiB),
                "commit_limit": 60 * GiB,
                "pages_in": 20000 if i == 3 else 40,  # one spike
            }
        )
        snap = box.read()
        assert snap["level"] == "ok" and snap["pending"] is None, snap["why"]
    assert snap["host"]["commit_pct"] == 75.0
    assert mp.describe(snap) == ""


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def test_vmstat_parser_reads_the_two_counters(tmp_path):
    f = tmp_path / "vmstat"
    f.write_text("nr_free_pages 1\npswpin 217717\npswpout 634330\noom_kill 3\n")
    assert mp._read_vmstat(str(f)) == {"pswpin": 217717, "oom_kill": 3}
    (tmp_path / "old").write_text("nr_free_pages 1\npswpin 5\n")
    assert mp._read_vmstat(str(tmp_path / "old")) == {"pswpin": 5}
    assert mp._read_vmstat(str(tmp_path / "missing")) is None


def test_vmstat_parser_never_raises(tmp_path):
    f = tmp_path / "vmstat"
    f.write_text("nr_free_pages 1\npgfault 9\n")
    assert mp._read_vmstat(str(f)) is None  # neither counter
    f.write_text("pswpin 12x\noom_kill 1\n")
    assert mp._read_vmstat(str(f)) is None  # a garbled counter: no reading
    f.write_text("pswpin 5 extra\noom_kill 2\n")
    assert mp._read_vmstat(str(f)) == {"oom_kill": 2}  # odd lines skipped
    assert mp._read_vmstat(str(tmp_path)) is None  # unreadable (a directory)


def test_the_swap_in_rate_needs_ten_seconds_and_keeps_a_minute():
    mp._reset_for_tests()
    assert mp._swapin_rate(None, 100.0) is None
    assert mp._swapin_rate({"oom_kill": 1}, 100.0) is None
    assert mp._swapin_rate({"pswpin": 0}, 100.0) is None
    assert mp._swapin_rate({"pswpin": 900}, 109.0) is None  # 9 s: too short
    assert mp._swapin_rate({"pswpin": 1000}, 110.0) == 100.0
    # Quiet from then on: once the burst is over a minute old it is gone.
    for t in range(120, 180, 10):
        mp._swapin_rate({"pswpin": 1000}, float(t))
    assert mp._swapin_rate({"pswpin": 1000}, 180.0) == 0.0
    assert all(180.0 - t <= mp.RATE_WINDOW_SECONDS + 10 for t, _ in mp._STATE["vm"])
    # The counter going back (another kernel, a reset) is never a negative
    # rate: the history starts over.
    assert mp._swapin_rate({"pswpin": 5}, 190.0) is None
    assert mp._swapin_rate({"pswpin": 5}, 200.0) == 0.0
    mp._reset_for_tests()


def test_oom_kills_count_for_five_minutes_and_never_from_boot():
    mp._reset_for_tests()
    assert mp._oom_recent({"oom_kill": 57}, 0.0) == 0  # since boot: baseline
    assert mp._oom_recent({"oom_kill": 57}, 10.0) == 0
    assert mp._oom_recent({"oom_kill": 59}, 20.0) == 2
    assert mp._oom_recent({"oom_kill": 60}, 100.0) == 3  # within: they add up
    assert mp._oom_recent({"oom_kill": 60}, 399.0) == 3
    assert mp._oom_recent({"oom_kill": 60}, 400.0) == 0  # 300 s since the last
    assert mp._oom_recent({"oom_kill": 61}, 500.0) == 1  # the old ones stay gone
    assert mp._oom_recent({"oom_kill": 3}, 510.0) == 0  # counter went back
    assert mp._oom_recent({"pswpin": 1}, 520.0) == 0
    assert mp._oom_recent(None, 530.0) == 0
    mp._reset_for_tests()


def test_swap_in_use_is_nothing_swapping_in_is_distress(box, monkeypatch):
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    assert box.read(swapped_in=0)["swap_in_mb_s"] is None  # one reading: no rate
    snap = box.read(swapped_in=10)
    assert snap["level"] == "ok" and snap["swap_in_mb_s"] == 0.0
    # 400 pages/s (1.6 MB/s) over the window: low.
    for _ in range(7):
        snap = box.read(swapped_in=4000)
    assert snap["level"] == "low"
    assert snap["why"] == ["swapping 1.6 MB/s back in from disk"]
    # 3000 pages/s (12 MB/s): critical, once the window holds it.
    for _ in range(7):
        snap = box.read(swapped_in=30000)
    assert snap["level"] == "critical"
    assert mp.describe(snap) == (
        "memory is critical: swapping 12.3 MB/s back in from disk"
    )
    # It stops: the window drains, the level comes down.
    for _ in range(8):
        snap = box.read(swapped_in=0)
    assert snap["level"] == "ok" and snap["swap_in_mb_s"] == 0.0


def test_an_oom_kill_alone_is_low_with_company_critical(box, monkeypatch):
    """The counter also counts a kill inside a memory-capped cgroup (a test
    suite in its own scope): alone it holds launches, no more."""
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    assert box.read()["level"] == "ok"  # the first reading is the baseline
    box.vm["oom_kill"] = 1
    snap = box.read()
    assert snap["level"] == "low" and snap["oom_kills"] == 1
    assert snap["why"] == ["the kernel's OOM killer ended 1 process in the last 5 min"]
    box.psi["full_avg60"] = 6.0  # and the box is stalling: the machine ran out
    snap = box.read()
    assert snap["level"] == "critical"
    assert snap["why"][1].startswith("every running process stalled on memory 6%")
    box.psi["full_avg60"] = 0.1
    snap = box.read(dt=mp.OOM_WINDOW_SECONDS)  # five quiet minutes later
    assert snap["level"] == "ok" and snap["oom_kills"] == 0


def test_some_stall_alone_needs_more_than_full(box, monkeypatch):
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    box.psi.update(some_avg60=34.0)
    snap = box.read()
    assert snap["level"] == "low"
    assert mp.describe(snap) == (
        "memory is low: processes stalled on memory 34% of the last minute"
    )


# --------------------------------------------------------------------------- #
# sustain
# --------------------------------------------------------------------------- #
def test_a_step_up_waits_for_the_sustain_and_says_pending(box):
    box.psi.update(full_avg60=20.0)
    snap = box.read()
    assert snap["level"] == "ok" and snap["pending"] == "critical"
    assert snap["why"] == [
        "every running process stalled on memory 20% of the last minute"
    ]
    assert mp.describe(snap) == ""  # ok is ok: nothing is held yet
    assert box.read(dt=20)["level"] == "ok"
    snap = box.read(dt=10)  # 30 s past the first reading
    assert snap["level"] == "critical" and snap["pending"] is None


def test_one_bad_reading_moves_nothing(box):
    box.avail = 0.5 * GiB
    assert box.read()["pending"] == "critical"
    box.avail = 14 * GiB
    snap = box.read(dt=25)
    assert snap["level"] == "ok" and snap["pending"] is None
    box.avail = 0.5 * GiB
    assert box.read(dt=10)["level"] == "ok"  # the clock started over


def test_low_can_land_while_critical_still_waits(box):
    box.psi.update(some_avg60=25.0)  # low
    box.read()
    box.psi.update(full_avg60=20.0)  # critical, 20 s later
    snap = box.read(dt=20)
    assert snap["level"] == "ok" and snap["pending"] == "critical"
    snap = box.read(dt=10)
    assert snap["level"] == "low" and snap["pending"] == "critical"
    snap = box.read(dt=20)
    assert snap["level"] == "critical"


def test_a_step_down_is_immediate_past_the_recovery_lines(box):
    box.psi.update(full_avg60=20.0)
    box.read()
    assert box.read(dt=30)["level"] == "critical"
    box.psi.update(full_avg60=9.0)  # under 15, not under the 8 recovery
    assert box.read()["level"] == "critical"
    box.psi.update(full_avg60=6.0)
    assert box.read()["level"] == "low"
    box.psi.update(full_avg60=0.5)
    assert box.read()["level"] == "ok"


def test_the_sustain_follows_the_level_not_the_signal(box):
    """A stall points at low for 20 s, then eases while available falls
    under 8%: the readings pointed at low the whole time, so the clock
    carries over and low lands 30 s after the first."""
    box.psi.update(some_avg60=25.0)
    assert box.read()["pending"] == "low"
    box.psi.update(some_avg60=0.2)
    box.avail = 1.8 * GiB  # 7.5% of 24 GB
    snap = box.read(dt=20)
    assert snap["level"] == "ok" and snap["pending"] == "low"
    snap = box.read(dt=10)
    assert snap["level"] == "low" and snap["pending"] is None
    assert snap["why"] == ["only 1.8 GB of 24.0 GB available (7%)"]


def test_a_stale_host_reading_starts_the_hosts_sustain_over(box):
    import time

    def host():
        hm._set_reading(
            {
                "available": 4 * GiB,
                "total": 32 * GiB,
                "committed": int(0.93 * 60 * GiB),
                "commit_limit": 60 * GiB,
                "pages_in": 0,
            }
        )

    host()
    assert box.read()["host"]["pending"] == "critical"
    host()
    assert box.read(dt=20)["pending"] == "critical"
    hm._STATE["reading"]["at"] = time.time() - hm.STALE_SECONDS - 1
    snap = box.read(dt=5)
    assert snap["host"] is None and snap["pending"] is None
    assert mp._STATE["since"]["host"] == {}
    host()  # 25 s after the first: would have been 5 s short of the sustain
    snap = box.read()
    assert snap["level"] == "ok" and snap["pending"] == "critical"
    host()
    assert box.read(dt=20)["level"] == "ok"  # 30 s since the FIRST, 20 since this
    host()
    assert box.read(dt=10)["level"] == "critical"


def test_the_host_sustains_on_its_own(box):
    """Windows' commit at 93% for one reading is pending; held 30 s it is
    critical, and the snapshot names Windows."""

    def host(commit):
        hm._set_reading(
            {
                "available": 4 * GiB,
                "total": 32 * GiB,
                "committed": int(commit * 60 * GiB),
                "commit_limit": 60 * GiB,
                "pages_in": 0,
            }
        )

    host(0.93)
    snap = box.read()
    assert snap["level"] == "ok" and snap["pending"] == "critical"
    assert snap["host"]["pending"] == "critical"
    host(0.93)
    snap = box.read(dt=30)
    assert snap["level"] == "critical" and snap["guest_level"] == "ok"
    assert mp.describe(snap) == (
        "memory is critical: Windows commit charge is at 93% of its limit "
        "(55.8 GB of 60.0 GB)"
    )


# --------------------------------------------------------------------------- #
# the shield and the gate
# --------------------------------------------------------------------------- #
def test_the_shield_acts_only_on_sustained_critical(box):
    """A burst (PSI full avg10 at 40%) with the level not yet critical sheds
    nothing; critical held past the gauge's sustain and the shield's minute
    sheds, naming the signal."""
    st = {"critical_since": None, "last_shed_at": 0.0}
    box.psi.update(full_avg10=40.0, full_avg60=20.0)
    snap = box.read()
    assert shield.shed_due(st, snap, box.t) == (False, "")
    snap = box.read(dt=30)
    assert snap["level"] == "critical"
    due, why = shield.shed_due(st, snap, box.t)
    assert due and why.startswith("every task stalled on memory 40% of the last 10 s")
    st = {"critical_since": None, "last_shed_at": 0.0}
    box.psi.update(full_avg10=10.0)
    assert shield.shed_due(st, box.read(), box.t)[0] is False
    due, why = shield.shed_due(st, box.read(dt=60), box.t)
    assert due and why == (
        "memory has been critical for 60s (every running process stalled on "
        "memory 20% of the last minute)"
    )


def test_a_stall_with_memory_available_still_sheds_one(box):
    """Critical from a stall has no byte target: one session at a time."""
    box.psi.update(full_avg60=20.0)
    box.read()
    snap = box.read(dt=30)
    assert shield.shed_need_mb(snap) == 0
    picks = shield.choose_shed(
        [
            {"title": "a", "row": {"activity": "idle"}, "idle_s": 600, "mb": 800},
            {"title": "b", "row": {"activity": "idle"}, "idle_s": 900, "mb": 900},
        ],
        max(shield.shed_need_mb(snap), 1),
        700,
    )
    assert [p["title"] for p in picks] == ["b"]


def test_the_gate_waits_on_low_and_counts_swap_only_while_swapping_in(box):
    cost = {"session_mb": 1024, "peak_mb": 1024, "source": "measured", "samples": 3}
    box.read()
    snap = box.read(swapped_in=0)
    assert snap["swap_in_mb_s"] == 0.0
    b = gate.budget(snap, cost, reserve=2 * GiB)
    assert b["penalty_mb"] == 0  # 4 GB parked in swap costs nothing
    assert b["room"] == 12  # (14 - 2) GB of 1 GB sessions
    snap["swap_in_mb_s"] = 5.0  # being read back: it counts (10% of RAM cap)
    assert gate.budget(snap, cost, reserve=2 * GiB)["penalty_mb"] == round(
        0.1 * TOTAL / MiB
    )
    del snap["swap_in_mb_s"]  # an older snapshot: the old rule
    assert gate.budget(snap, cost, reserve=2 * GiB)["penalty_mb"] > 0
    snap["level"], snap["why"] = "low", ["swapping 1.6 MB/s back in from disk"]
    b = gate.budget(snap, cost, reserve=2 * GiB)
    assert b["room"] == 0
    assert b["why"] == "memory is low: swapping 1.6 MB/s back in from disk"


def test_the_low_floor_reserves_less_than_the_old_15_percent():
    th = mp.thresholds()
    assert th["low"][:2] == (0.08, int(1.5 * GiB))
    assert th["critical"][:2] == (0.04, 1 * GiB)
    # 26 GB: the gate's reserve is its 2 GB floor, not 15% (3.9 GB).
    assert gate.reserve_bytes(26 * GiB) == 2 * GiB + int(0.08 * 26 * GiB - 2 * GiB)


# --------------------------------------------------------------------------- #
# the back-test
# --------------------------------------------------------------------------- #
def _row(at, avail_mb, *, total_mb=26050, host_mb=None, some10=0.0, full10=0.0):
    row = {
        "at": at,
        "total_mb": total_mb,
        "available_mb": avail_mb,
        "swap_total_mb": 8192,
        "swap_used_mb": 400,
        "psi_some_avg10": some10,
        "psi_full_avg10": full10,
        "level": "x",
    }
    if host_mb is not None:
        row.update(host_total_mb=32132, host_available_mb=host_mb)
    return row


def test_replay_shape_and_the_old_rows_approximation():
    rows = [_row(1000.0 + 30 * i, 16000, host_mb=700) for i in range(4)]
    rows.append({"at": 2000.0, "level": "unknown"})
    out = mp.replay(rows, guest_reusable_share=0.5)
    assert [set(r) for r in out] == [
        {"at", "level", "guest_level", "host_level", "why"}
    ] * 5
    assert [r["level"] for r in out] == ["ok"] * 4 + ["unknown"]
    # The worst case (the guest can reuse nothing): Windows' 0.7 GB is under
    # the 1 GB headroom floor, and held 30 s it reads critical.
    worst = mp.replay(rows)
    assert [r["level"] for r in worst[:4]] == ["ok", "critical", "critical", "critical"]
    assert worst[1]["why"][0].startswith("Windows has only 0.7 GB to give")


def test_replay_catches_a_real_stall_and_sustains_it():
    """Nov 8: full avg10 50%+ for minutes — critical, after the sustain."""
    rows = [_row(1000.0 + 30 * i, 2000, total_mb=20002, full10=55.0) for i in range(4)]
    out = mp.replay(rows)
    assert [r["level"] for r in out] == ["ok", "critical", "critical", "critical"]
    assert "stalled on memory 55%" in out[1]["why"][-1]
    # One 10 s burst in an otherwise quiet minute: the avg60 estimate (the
    # mean of the logged avg10s, which overstates a burst) stays under 5%.
    rows = [
        _row(1000.0 + 30 * i, 16000, full10=8.0 if i == 2 else 0.0) for i in range(5)
    ]
    assert [r["level"] for r in mp.replay(rows)] == ["ok"] * 5


def test_replay_reads_the_new_fields_when_the_history_has_them():
    row = _row(1000.0, 16000, host_mb=700)
    row.update(
        psi_full_avg60=0.1,
        swap_in_mb_s=0.0,
        oom_kills=0,
        host_headroom_mb=14000,
        host_commit_pct=93.0,
        host_pages_in_median=10.0,
    )
    out = mp.replay([row, dict(row, at=1030.0)])
    assert [r["host_level"] for r in out] == ["ok", "critical"]
    assert out[1]["why"] == [
        "Windows commit charge is at 93% of its limit (0.0 GB of 0.0 GB)"
    ]


def test_replay_leaves_the_live_level_alone(box, monkeypatch):
    import copy

    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    box.psi.update(full_avg60=20.0)
    assert box.read()["level"] == "critical"
    before = copy.deepcopy(mp._STATE)
    out = mp.replay([_row(1000.0 + 30 * i, 16000) for i in range(3)])
    assert [r["level"] for r in out] == ["ok"] * 3
    assert mp._STATE == before
    assert mp.snapshot()["level"] == "critical"  # the cache, untouched too


def test_replay_survives_rows_without_or_out_of_order_at():
    stall = dict(total_mb=20002, full10=55.0)
    no_at = _row(0.0, 2000, **stall)
    del no_at["at"]
    rows = [_row(1000.0, 2000, **stall), _row(1030.0, 2000, **stall), no_at]
    rows.append(_row(1010.0, 2000, **stall))
    out = mp.replay(rows)
    assert len(out) == 4 and out[2]["at"] == 0.0
    assert all(r["level"] in mp.LEVELS for r in out)
    assert [r["level"] for r in out] == ["ok", "critical", "critical", "critical"]


def test_replay_applies_the_reusable_share_only_without_a_logged_headroom():
    """A row that logged its headroom is read as logged: the estimate from
    the guest's available is for rows written before it existed."""
    rows = [_row(1000.0, 16000, host_mb=700), _row(1030.0, 16000, host_mb=700)]
    assert [r["host_level"] for r in mp.replay(rows, guest_reusable_share=1.0)] == [
        "ok",
        "ok",
    ]
    logged = [dict(r, host_headroom_mb=900) for r in rows]
    out = mp.replay(logged, guest_reusable_share=1.0)
    assert [r["host_level"] for r in out] == ["ok", "critical"]
    assert out[1]["why"] == [
        "Windows has only 0.9 GB to give (0.7 GB available, 0.2 GB WSL can reuse)"
    ]
