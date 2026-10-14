"""``backend.memory_pressure`` — the one reading every memory gate trusts.

Pins the two signals (MemAvailable share/floor, PSI avg60), the hysteresis
that keeps a box hovering at the line from flapping the gates, the parsers
on real-shaped ``/proc`` text, ``unknown`` where nothing can be read (never a
gate), the off switch, and the one-line reason the refusals carry.
"""

from __future__ import annotations

import pytest

from backend import memory_pressure as mp

GiB = 1024**3
TOTAL = 32 * GiB


@pytest.fixture
def machine(monkeypatch):
    """Feed the probe readings: ``machine(avail_gib, some60=0, full60=0)``."""
    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
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


def test_available_share_and_floor_raise_the_level(machine):
    assert machine(4.5) == "low"  # 14% of 32 GB
    assert machine(2.0) == "critical"  # 6%


def test_psi_raises_the_level_even_with_memory_free(machine):
    """Free memory can look fine while the box thrashes through swap; PSI is
    the direct reading of that."""
    assert machine(16, some60=25) == "low"
    assert machine(16, full60=20) == "critical"


def test_hysteresis_steps_down_only_past_the_recovery_line(machine):
    assert machine(4.5) == "low"
    assert machine(5.5) == "low"  # 17%: past the entry line, short of recovery
    assert machine(7.0) == "ok"  # 22%
    assert machine(2.0) == "critical"
    assert machine(2.8) == "critical"  # 8.75%: not yet 10%
    assert machine(3.5) == "low"  # past critical recovery, still under 15%


def test_no_psi_file_is_just_one_signal_fewer(machine):
    assert machine(16, psi=False) == "ok"
    mp._reset_for_tests()
    assert machine(2.0, psi=False) == "critical"


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


def test_describe_names_the_numbers(machine):
    machine(2.0, some60=17.4)
    assert mp.describe() == (
        "memory is critically low: 2.0 GB free of 32.0 GB (6%), "
        "processes stalled on memory 17% of the last minute"
    )
    machine(30)  # 94% free clears even the critical recovery line at once
    assert mp.describe() == ""


def test_the_route_serves_the_reading_with_its_message(machine):
    from fastapi.testclient import TestClient

    from backend.web.server import app

    machine(4.5)
    body = TestClient(app).get("/api/system/memory").json()
    assert body["level"] == "low"
    assert body["message"].startswith("memory is low: 4.5 GB free of 32.0 GB")


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
