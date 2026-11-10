"""``backend.memory_pressure`` — the one reading every memory gate trusts.

Pins the available floor (share setting + absolute bytes) and PSI avg60
lines, the hysteresis that keeps a box hovering at the line from flapping the
gates, the parsers on real-shaped ``/proc`` text, ``unknown`` where nothing
can be read (never a gate), the off switch, and the one-line reason the
refusals carry. The swap-in rate, OOM kills, the sustain and the back-test
live in test_memory_gauge.py; here every reading moves the level at once
(``SUSTAIN_SECONDS`` is 0).
"""

from __future__ import annotations

import pytest

from backend import memory_pressure as mp

GiB = 1024**3
MiB = 1024**2
TOTAL = 32 * GiB


@pytest.fixture
def machine(monkeypatch):
    """Feed the probe readings: ``machine(avail_gib, some60=0, full60=0)``."""
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    monkeypatch.setattr(mp, "_read_vmstat", lambda: None)
    mp._reset_for_tests()

    def set_(avail_gib, some60=0.0, full60=0.0, psi=True):
        monkeypatch.setattr(
            mp,
            "_read_meminfo",
            lambda: {
                "total": TOTAL,
                "available": int(avail_gib * GiB),
                "swap_total": 8 * GiB,
                "swap_free": 2 * GiB,
            },
        )
        monkeypatch.setattr(
            mp,
            "_read_psi",
            lambda: (
                {
                    "some_avg10": some60,
                    "some_avg60": some60,
                    "full_avg10": full60,
                    "full_avg60": full60,
                }
                if psi
                else None
            ),
        )
        return mp.level(fresh=True)

    return set_


def test_plenty_of_memory_reads_ok(machine):
    assert machine(16) == "ok"


def test_available_floor_raises_the_level_only_when_genuinely_small(machine):
    """32 GB: low at 8% (2.56 GB), critical at 4% (1.28 GB). 4.5 GB (14%)
    read low under the old 15% line; it is room for four more agents."""
    assert machine(4.5) == "ok"
    assert machine(2.5) == "low"  # 7.8%
    assert machine(1.2) == "critical"  # 3.75%


def test_psi_raises_the_level_even_with_memory_free(machine):
    """Free memory can look fine while the box thrashes through swap; PSI is
    the direct reading of that. ``full`` is the strong signal."""
    assert machine(16, some60=25) == "low"
    assert machine(16, some60=45) == "low"  # was critical at 40
    mp._reset_for_tests()
    assert machine(16, some60=55) == "critical"
    mp._reset_for_tests()
    assert machine(16, full60=6) == "low"
    assert machine(16, full60=20) == "critical"


def test_hysteresis_steps_down_only_past_the_recovery_line(machine):
    assert machine(2.5) == "low"
    assert machine(3.0) == "low"  # 9.4%: past the entry line, short of 10%
    assert machine(3.3) == "ok"  # 10.3%
    assert machine(1.2) == "critical"
    assert machine(1.8) == "critical"  # 5.6%: not yet 6%
    assert machine(2.0) == "low"  # past critical recovery, still under 10%


def test_without_a_host_reading_the_level_is_the_guests(machine):
    """Off WSL (or before the reader has a line) the snapshot still carries
    the new keys: ``host`` None and ``guest_level`` the level itself."""
    from backend import host_memory

    host_memory._reset_for_tests()
    machine(2.5)
    snap = mp.snapshot()
    assert snap["host"] is None
    assert snap["guest_level"] == snap["level"] == "low"
    machine(16)
    snap = mp.snapshot()
    assert snap["host"] is None and snap["guest_level"] == snap["level"] == "ok"


def test_no_psi_file_is_just_one_signal_fewer(machine):
    assert machine(16, psi=False) == "ok"
    mp._reset_for_tests()
    assert machine(1.2, psi=False) == "critical"


def test_parsers_read_real_proc_text(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       24611032 kB\nMemFree:  100 kB\n"
        "MemAvailable:    3111648 kB\nSwapTotal:      16777216 kB\n"
        "SwapFree:        2532324 kB\n"
    )
    m = mp._read_meminfo(str(meminfo))
    assert m == {
        "total": 24611032 * 1024,
        "available": 3111648 * 1024,
        "free": 100 * 1024,
        "swap_total": 16777216 * 1024,
        "swap_free": 2532324 * 1024,
    }
    psi = tmp_path / "psi"
    psi.write_text(
        "some avg10=30.63 avg60=28.40 avg300=32.13 total=635285533\n"
        "full avg10=22.37 avg60=17.56 avg300=19.60 total=408455656\n"
    )
    assert mp._read_psi(str(psi)) == {
        "some_avg10": 30.63,
        "some_avg60": 28.40,
        "full_avg10": 22.37,
        "full_avg60": 17.56,
    }
    assert mp._read_meminfo(str(tmp_path / "missing")) is None
    assert mp._read_psi(str(tmp_path / "missing")) is None


def test_nothing_to_read_is_unknown_and_never_low(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    mp._reset_for_tests()
    monkeypatch.setattr(mp, "_read_meminfo", lambda: None)
    assert mp.level(fresh=True) == "unknown"
    assert mp.is_low(fresh=True) is False


def test_the_off_switch(machine, monkeypatch):
    machine(1.0)
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "0")
    assert mp.level(fresh=True) == "unknown"


def test_describe_names_the_signals(machine):
    machine(16, some60=34.2)
    assert mp.describe() == (
        "memory is low: processes stalled on memory 34% of the last minute"
    )
    assert mp.snapshot()["why"] == [
        "processes stalled on memory 34% of the last minute"
    ]
    machine(1.2, full60=17.6)
    assert mp.describe() == (
        "memory is critical: only 1.2 GB of 32.0 GB available (4%); every "
        "running process stalled on memory 18% of the last minute"
    )
    machine(30)  # 94% free, no stall: clears even the critical recovery lines
    assert mp.describe() == "" and mp.snapshot()["why"] == []


def test_describe_reads_an_older_snapshot_by_its_numbers():
    """A snapshot without ``why`` (a test's, or an older shape) keeps the
    numbers line."""
    snap = {"level": "low", "available": 2 * GiB, "total": 20 * GiB}
    snap["available_pct"] = 10.0
    assert mp.describe(snap) == "memory is low: 2.0 GB free of 20.0 GB (10%)"


def test_the_route_serves_the_reading_with_its_message(machine):
    from fastapi.testclient import TestClient

    from backend.web.server import app

    machine(2.5)
    body = TestClient(app).get("/api/system/memory").json()
    assert body["level"] == "low"
    assert body["message"] == "memory is low: only 2.5 GB of 32.0 GB available (8%)"
    assert body["why"] == ["only 2.5 GB of 32.0 GB available (8%)"]


def test_the_pipeline_holds_a_launch_until_memory_recovers(monkeypatch):
    import asyncio

    from backend.ticket_ingestion import session_runner as sr

    readings = iter([True, True, False])
    seen = []

    def is_low(**kw):
        v = next(readings, False)
        seen.append(v)
        return v

    monkeypatch.setattr(mp, "is_low", is_low)
    monkeypatch.setattr(sr, "_MEMORY_POLL_SECONDS", 0)
    asyncio.run(sr._wait_for_memory("ticket sc-1"))
    assert seen == [True, True, False]


def test_the_pipeline_hold_is_capped(monkeypatch):
    """A misreading must never stall ticket intake for good."""
    import asyncio

    from backend.ticket_ingestion import session_runner as sr

    monkeypatch.setattr(mp, "is_low", lambda **kw: True)
    monkeypatch.setattr(sr, "_MEMORY_POLL_SECONDS", 0)
    monkeypatch.setattr(sr, "_MEMORY_MAX_WAIT_SECONDS", 0)
    asyncio.run(asyncio.wait_for(sr._wait_for_memory("ticket sc-2"), 5))


def test_the_pipeline_also_waits_on_the_launch_gate(monkeypatch):
    """Memory reads ok, but the machine already runs as many agents as the
    launch gate's live cap allows: the pipeline's launch waits for that too,
    and goes once an agent stops."""
    import asyncio

    from backend.ticket_ingestion import session_runner as sr
    from backend.web.core import launch_gate

    monkeypatch.setattr(mp, "is_low", lambda **kw: False)
    monkeypatch.setattr(sr, "_MEMORY_POLL_SECONDS", 0)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "2")
    agents = [[("mindflock_a", 0.0), ("mindflock_b", 0.0)]]
    polls = []

    def probe():
        polls.append(1)
        if len(polls) >= 3:
            agents[0] = agents[0][:1]  # one of them stopped
        return list(agents[0])

    launch_gate.set_probe(probe)
    monkeypatch.setattr(launch_gate, "_PROBE_SECONDS", 0.0)
    assert "automatic starts stop at 2" in sr._held_reason()
    asyncio.run(asyncio.wait_for(sr._wait_for_memory("ticket sc-3"), 5))
    assert sr._held_reason() == ""


def test_the_share_lines_are_settings(machine, monkeypatch):
    """``general.memory_low_pct`` / ``memory_critical_pct`` move the share
    lines (absolute floors and the other signals stay); recovery keeps its
    margin."""
    monkeypatch.setattr(mp, "_share_settings", lambda: (25, 10))
    assert machine(7.5) == "low"  # 23% of 32 GB: low at a 25% line
    assert machine(3.0) == "critical"  # 9%
    assert machine(3.5) == "critical"  # 10.9%: not past the 12% recovery
    assert machine(4.5) == "low"  # 14%: out of critical, under 27%
    assert machine(9) == "ok"  # 28%
    th = mp.thresholds()
    assert th["low"][0] == 0.25 and th["recover_low"][0] == 0.27
    assert th["critical"][0] == 0.10 and th["recover_critical"][0] == 0.12


def test_a_zero_share_drops_that_signal_only(machine, monkeypatch):
    monkeypatch.setattr(mp, "_share_settings", lambda: (0, 0))
    assert machine(2.0) == "ok"  # 6% free, but the share line is off
    assert machine(1.4) == "low"  # under the 1.5 GB floor all the same
    assert machine(0.9) == "critical"  # under 1 GB
    mp._reset_for_tests()
    assert machine(16, full60=20.0) == "critical"  # PSI still counts


def test_the_settings_are_read_clamped_and_cached(monkeypatch):
    from backend.config import settings as S

    mp._reset_for_tests()
    assert mp._share_settings() == (8, 4)  # unset: the defaults
    S.update_settings(general={"memory_low_pct": 30, "memory_critical_pct": 50})
    assert mp._share_settings() == (8, 4)  # cached for _SETTINGS_SECONDS
    mp._reset_for_tests()
    assert mp._share_settings() == (30, 30)  # critical never above low
    S.update_settings(general={"memory_low_pct": 400, "memory_critical_pct": 3})
    mp._reset_for_tests()
    assert mp._share_settings() == (90, 3)
    S.update_settings(general={"memory_low_pct": "", "memory_critical_pct": ""})
    mp._reset_for_tests()


def test_an_explicit_old_setting_is_kept_and_unset_reads_the_new_defaults():
    """No migration: a settings.json that pinned the old 15 / 7 keeps them;
    unset (None) is 8 / 4; 0 drops the share and clamps critical to it."""
    from backend.config import settings as S

    mp._reset_for_tests()
    S.update_settings(general={"memory_low_pct": 15, "memory_critical_pct": 7})
    g = S.load_settings().general
    assert (g.memory_low_pct, g.memory_critical_pct) == (15, 7)
    assert mp._share_settings() == (15, 7)
    th = mp.thresholds()
    assert th["low"][0] == 0.15 and th["recover_low"][0] == 0.17
    S.update_settings(general={"memory_low_pct": 0, "memory_critical_pct": None})
    g = S.load_settings().general
    assert (g.memory_low_pct, g.memory_critical_pct) == (0, None)
    mp._reset_for_tests()
    assert mp._share_settings() == (0, 0)
    th = mp.thresholds()
    assert th["low"][:2] == (0.0, int(1.5 * GiB))  # the floor stays
    assert th["critical"][:2] == (0.0, 1 * GiB)
    S.update_settings(general={"memory_low_pct": None, "memory_critical_pct": None})
    g = S.load_settings().general
    assert (g.memory_low_pct, g.memory_critical_pct) == (None, None)
    mp._reset_for_tests()
    assert mp._share_settings() == (8, 4)
    mp._reset_for_tests()


# --------------------------------------------------------------------------- #
# the guest's and the host's lines, exactly at the line (pure classifiers)
# --------------------------------------------------------------------------- #
BIG = 100 * GiB  # 8% = 8 GB, 4% = 4 GB: the byte floors never bind


def _g(avail=50 * GiB, *, total=BIG, some=0.0, full=0.0, swapin=None, oom=0):
    return {
        "total": total,
        "available": avail,
        "psi": {"some_avg60": some, "full_avg60": full},
        "swapin": swapin,
        "oom": oom,
    }


@pytest.fixture
def shares(monkeypatch):
    """Pin the share settings: ``shares(low, critical)`` (default 8 / 4)."""

    def set_(low=8, crit=4):
        monkeypatch.setattr(mp, "_share_settings", lambda: (low, crit))

    set_()
    return set_


@pytest.mark.parametrize(
    "reading, want",
    [
        (_g(some=19.9), "ok"),
        (_g(some=20.0), "low"),
        (_g(some=49.9), "low"),
        (_g(some=50.0), "critical"),
        (_g(full=4.9), "ok"),
        (_g(full=5.0), "low"),
        (_g(full=14.9), "low"),
        (_g(full=15.0), "critical"),
        (_g(swapin=255.9), "ok"),
        (_g(swapin=256.0), "low"),
        (_g(swapin=2047.9), "low"),
        (_g(swapin=2048.0), "critical"),
        (_g(8 * GiB + 1), "ok"),
        (_g(8 * GiB), "low"),  # exactly 8%
        (_g(4 * GiB + 1), "low"),
        (_g(4 * GiB), "critical"),  # exactly 4%
        (_g(oom=1), "low"),  # a kill alone
        (_g(oom=1, some=20.0), "critical"),  # with company
    ],
)
def test_guest_entry_lines_are_at_or_past(shares, reading, want):
    assert mp.classify_guest("ok", reading)[0] == want


@pytest.mark.parametrize(
    "avail, want",
    [
        (int(1.5 * GiB) + 1, "ok"),
        (int(1.5 * GiB), "low"),
        (1 * GiB + 1, "low"),
        (1 * GiB, "critical"),
    ],
)
def test_guest_byte_floors_with_the_share_off(shares, avail, want):
    shares(0, 0)
    assert mp.classify_guest("ok", _g(avail, total=20 * GiB))[0] == want


@pytest.mark.parametrize(
    "prev, reading, want",
    [
        # from low: every reading must clear 10% / 2 GB / some 10 / full 2 /
        # swap-in 64 pages/s
        ("low", _g(some=10.1), "low"),
        ("low", _g(some=10.0), "ok"),
        ("low", _g(full=2.1), "low"),
        ("low", _g(full=2.0), "ok"),
        ("low", _g(swapin=64.1), "low"),
        ("low", _g(swapin=64.0), "ok"),
        ("low", _g(10 * GiB - 1), "low"),
        ("low", _g(10 * GiB), "ok"),
        # from critical: 6% / 1.5 GB / some 30 / full 8 / swap-in 512 — and
        # then the low entry lines decide
        ("critical", _g(some=30.1), "critical"),
        ("critical", _g(some=30.0), "low"),
        ("critical", _g(full=8.1), "critical"),
        ("critical", _g(full=8.0), "low"),
        ("critical", _g(swapin=512.1), "critical"),
        ("critical", _g(swapin=512.0), "low"),
        ("critical", _g(6 * GiB - 1), "critical"),
        ("critical", _g(6 * GiB), "low"),
        ("critical", _g(some=1.0), "ok"),  # clear of every line at once
    ],
)
def test_guest_recovery_lines_are_strictly_past(shares, prev, reading, want):
    assert mp.classify_guest(prev, reading)[0] == want


def test_guest_byte_recovery_with_the_share_off(shares):
    shares(0, 0)
    small = 20 * GiB
    assert mp.classify_guest("low", _g(2 * GiB - 1, total=small))[0] == "low"
    assert mp.classify_guest("low", _g(2 * GiB, total=small))[0] == "ok"
    crit = int(1.5 * GiB)
    assert mp.classify_guest("critical", _g(crit - 1, total=small))[0] == "critical"
    # out of critical at 1.5 GB, but that is still the low floor
    assert mp.classify_guest("critical", _g(crit, total=small))[0] == "low"


def _h(avail=16 * GiB, *, headroom=None, commit=None, paging=None, samples=6):
    return {
        "available": avail,
        "headroom": avail if headroom is None else headroom,
        "guest_reusable": 0,
        "commit_pct": commit,
        "committed": 0,
        "commit_limit": 0,
        "pages_in_median": paging,
        "pages_in_samples": samples,
    }


@pytest.mark.parametrize(
    "prev, reading, want",
    [
        ("ok", _h(commit=84.9), "ok"),
        ("ok", _h(commit=85.0), "low"),
        ("ok", _h(commit=89.9), "low"),
        ("ok", _h(commit=90.0), "critical"),
        ("critical", _h(commit=87.1), "critical"),
        ("critical", _h(commit=87.0), "low"),
        ("low", _h(commit=80.1), "low"),
        ("low", _h(commit=80.0), "ok"),
        ("ok", _h(commit=None), "ok"),  # the short line: no commit signal
        # the headroom floor: 2 GB / 1 GB, recovering past 3 GB / 1.5 GB
        ("ok", _h(headroom=2 * GiB), "low"),
        ("ok", _h(headroom=1 * GiB), "critical"),
        ("low", _h(headroom=3 * GiB - 1), "low"),
        ("low", _h(headroom=3 * GiB), "ok"),
        ("critical", _h(headroom=int(1.5 * GiB) - 1), "critical"),
        ("critical", _h(headroom=int(1.5 * GiB)), "low"),
        # Windows' raw available: critical at 256 MB whatever the guest can
        # reuse, held until past 512 MB
        ("ok", _h(256 * MiB, headroom=20 * GiB), "critical"),
        ("ok", _h(256 * MiB + 1, headroom=20 * GiB), "ok"),
        ("critical", _h(512 * MiB, headroom=20 * GiB), "critical"),
        ("critical", _h(512 * MiB + 1, headroom=20 * GiB), "ok"),
    ],
)
def test_host_lines(prev, reading, want):
    assert mp.classify_host(prev, reading)[0] == want


def test_host_paging_counts_only_sustained_and_while_windows_is_short():
    """Hard paging needs :data:`~mp.HOST_PAGING_MIN_SAMPLES` readings and
    Windows' OWN available under 2 GB (the headroom the guest could give
    back does not make page-ins cold file reads)."""
    room = 10 * GiB  # the headroom floor never trips here
    short = int(1.9 * GiB)
    assert (
        mp.classify_host("ok", _h(short, headroom=room, paging=9000, samples=2))[0]
        == "ok"
    )
    lvl, why = mp.classify_host("ok", _h(short, headroom=room, paging=9000, samples=3))
    assert lvl == "critical"
    assert why == ["Windows is paging 37 MB/s in from disk over the last minute"]
    assert mp.classify_host("ok", _h(short, headroom=room, paging=3000))[0] == "low"
    assert mp.classify_host("ok", _h(short, headroom=room, paging=2999))[0] == "ok"
    assert mp.classify_host("ok", _h(2 * GiB, headroom=room, paging=9000))[0] == "ok"
    # recovering: from critical past 4000, from low past 1500
    assert (
        mp.classify_host("critical", _h(short, headroom=room, paging=4001))[0]
        == "critical"
    )
    assert (
        mp.classify_host("critical", _h(short, headroom=room, paging=4000))[0] == "low"
    )
    assert mp.classify_host("low", _h(short, headroom=room, paging=1500))[0] == "ok"


def test_describe_falls_back_to_the_numbers_without_why():
    """Older snapshots (no ``why``) keep the old wording: ``critically low``,
    and ``Windows memory is …`` when only the host set the level."""
    snap = {
        "level": "critical",
        "available": 1 * GiB,
        "total": 20 * GiB,
        "available_pct": 5.0,
    }
    assert mp.describe(snap) == "memory is critically low: 1.0 GB free of 20.0 GB (5%)"
    snap["why"] = []  # empty is no why
    assert mp.describe(snap).startswith("memory is critically low: ")
    snap.update(
        available=10 * GiB,
        available_pct=50.0,
        guest_level="ok",
        host={
            "level": "critical",
            "available": 1 * GiB,
            "total": 32 * GiB,
            "available_pct": 3.1,
            "guest_reusable": 2 * GiB,
        },
    )
    assert mp.describe(snap) == (
        "Windows memory is critically low: 1.0 GB free of 32.0 GB (3%), and "
        "WSL can reuse 2.0 GB it already holds"
    )
    snap["level"] = snap["host"]["level"] = "low"
    snap["host"]["guest_reusable"] = 0
    assert mp.describe(snap) == "Windows memory is low: 1.0 GB free of 32.0 GB (3%)"
    # With ``why``, the signals and nothing else.
    snap["why"] = ["a", "", "b"]
    assert mp.describe(snap) == "memory is low: a; b"
    for lvl in ("ok", "unknown"):
        assert mp.describe(dict(snap, level=lvl)) == ""
