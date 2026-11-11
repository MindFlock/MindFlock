"""Regressions from the code review of MindFlock 0.14.7's memory gauge.

1. An OOM kill plus a recovery-band hold read ``critical``: the kill made
   the level ``low``, so on the next reading ``prev`` was low and a signal
   still in the recovery band (PSI some 11 %) counted as "another signal"
   — critical for the whole 300 s OOM window with 12 of 24 GB free, every
   person's start refused and the shield shedding.
2. A person's refusal said "Memory is critically low: 13.0 GB free. No room
   for another session…" when the cause was a PSI stall: the dialog never
   used the verdict's reason.
3. The shield kept putting an idle agent to sleep every 90 s for a critical
   that rested only on Windows (its commit charge), which sleeping a WSL
   agent does not move.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import memory_pressure as mp
from backend.web.core import launch_gate as gate
from backend.web.core import memory_shield as shield

GiB = 1024**3
MiB = 1024**2


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mp._reset_for_tests()
    gate._reset_for_tests()
    shield._reset_for_tests()
    monkeypatch.setattr(mp, "_share_settings", lambda: (8, 4))
    monkeypatch.setattr(gate, "AUTO_DRAIN", False)
    for k in (
        "MINDFLOCK_AGENT_WARMUP_MB",
        "MINDFLOCK_AUTO_LAUNCH_SPACING",
        "MINDFLOCK_SHED_IDLE",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "0")
    gate.set_probe(lambda: [])
    yield
    gate.set_probe(None)
    mp._reset_for_tests()
    gate._reset_for_tests()
    shield._reset_for_tests()


# --------------------------------------------------------------------------- #
# 1. OOM + hysteresis
# --------------------------------------------------------------------------- #
def _g(some=0.0, oom=0, avail=12 * GiB, total=24 * GiB):
    return {
        "total": total,
        "available": avail,
        "psi": {"some_avg60": some, "full_avg60": 0.0},
        "swapin": None,
        "oom": oom,
    }


def test_an_oom_kill_with_a_recovery_band_hold_stays_low():
    # PSI some 11 %: under the low entry line (20), over its recovery (10).
    lvl, why = mp.classify_guest("low", _g(some=11.0, oom=1))
    assert lvl == "low"
    assert why[0].startswith("the kernel's OOM killer ended 1 process")
    # Past an ENTRY line it is still the machine running out.
    assert mp.classify_guest("low", _g(some=20.0, oom=1))[0] == "critical"
    assert mp.classify_guest("ok", _g(some=20.0, oom=1))[0] == "critical"
    assert mp.classify_guest("ok", _g(oom=1))[0] == "low"
    # A real critical hold is not lowered by the kill.
    assert mp.classify_guest("critical", _g(some=35.0, oom=1))[0] == "critical"


def test_the_reviewers_oom_replay_never_reads_critical():
    """The sub-agent's repro: one OOM kill, PSI some settling at 11 %, 12 of
    24 GB free → low from t=40, critical t=80..290 before the fix."""
    rows = [
        {
            "at": float(t),
            "total_mb": 24 * 1024,
            "available_mb": 12 * 1024,
            "psi_some_avg60": 11.0 if t >= 40 else 0.0,
            "psi_full_avg60": 0.0,
            "oom_kills": 1,
        }
        for t in range(0, 300, 10)
    ]
    out = mp.replay(rows)
    levels = {r["at"]: r["level"] for r in out}
    assert "critical" not in levels.values(), levels
    assert levels[40.0] == "low" and levels[290.0] == "low"


# --------------------------------------------------------------------------- #
# 2. a person's refusal names its real reason
# --------------------------------------------------------------------------- #
STALL = "processes stalled on memory 34% of the last minute"


def _memory(monkeypatch, level, available, why=None, total=24 * GiB):
    snap = {
        "level": level,
        "guest_level": level,
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "psi": {"some_avg10": 34.0, "some_avg60": 34.0},
    }
    if why is not None:
        snap["why"] = why
    monkeypatch.setattr(mp, "snapshot", lambda fresh=False: dict(snap))
    return snap


def test_a_stall_refusal_leads_with_the_stall_not_the_free_amount(monkeypatch):
    _memory(monkeypatch, "critical", 13 * GiB, why=[STALL])
    v = gate.decide("person")
    assert v["decision"] == "refuse"
    body = gate.person_refusal(v)
    assert body["message"].startswith("Memory is critical: " + STALL + ".")
    assert "critically low" not in body["message"]
    assert "No room" not in body["message"] and "room for" not in body["message"]
    assert body["reason"] == "memory is critical: " + STALL
    assert body["error"].startswith("Memory is critical: " + STALL + ".")
    assert "start_anyway: true" in body["error"]
    assert body["level"] == "critical"


def test_a_low_refusal_leads_with_its_signal(monkeypatch):
    swap = "swapping 1.6 MB/s back in from disk"
    _memory(monkeypatch, "low", 9 * GiB, why=[swap])
    body = gate.person_refusal(gate.decide("person"))
    assert body["message"] == (
        "Memory is low: %s. Starting another agent may freeze the machine." % swap
    )


def test_an_older_snapshot_without_why_keeps_the_free_amount(monkeypatch):
    _memory(monkeypatch, "low", 3 * GiB)
    body = gate.person_refusal(gate.decide("person"))
    assert body["message"].startswith("Memory is low: 3.0 GB free.")
    assert "No room" not in body["message"]


# --------------------------------------------------------------------------- #
# 3. the shield leaves a Windows-only critical alone
# --------------------------------------------------------------------------- #
COMMIT = "Windows commit charge is at 98% of its limit (58.8 GB of 60.0 GB)"


@pytest.fixture
def candidates(monkeypatch):
    cands = [
        {"title": t, "row": {"activity": "idle"}, "idle_s": 600.0, "mb": mb, "inst": 0}
        for t, mb in (("a", 1200), ("b", 900))
    ]
    monkeypatch.setattr(shield, "_candidates", lambda srv, now: (list(cands), {}))
    monkeypatch.setattr(
        gate, "assess", lambda now=None, **kw: {"cost": {"session_mb": 700}}
    )
    from backend.web.core import events

    monkeypatch.setattr(events.BUS, "emit", lambda *a, **kw: {})
    slept = []
    said = []
    monkeypatch.setattr(shield, "_info", lambda fmt, *a: said.append(fmt % a))
    return SimpleNamespace(
        slept=slept, said=said, sleeper=lambda inst, title, idle: slept.append(title)
    )


def _host_snap(guest_level):
    return {
        "level": "critical",
        "guest_level": guest_level,
        "available": 12 * GiB,
        "total": 24 * GiB,
        "available_pct": 50.0,
        "psi": {"some_avg10": 1.0, "full_avg10": 0.0},
        "why": [COMMIT],
        "host": {
            "available": 6 * GiB,
            "total": 32 * GiB,
            "headroom": 10 * GiB,
            "commit_pct": 98.0,
            "level": "critical",
            "why": [COMMIT],
        },
    }


def test_a_critical_on_windows_commit_alone_sheds_nothing(candidates):
    snap = _host_snap("ok")
    srv = SimpleNamespace()
    for t in (5000.0, 5061.0, 5200.0, 5400.0):
        assert shield.tick(srv, snap, t, sleeper=candidates.sleeper)["shed"] == []
    assert candidates.slept == []
    st = shield.status()
    assert st["critical_since"] is None
    assert st["last_decision"].startswith("memory is critical on Windows' side only")
    assert COMMIT in st["last_decision"]
    # Logged once, not every 10 s.
    assert sum("Windows' side only" in s for s in candidates.said) == 1


def test_a_guest_critical_still_sheds_with_windows_short_too(candidates):
    snap = _host_snap("critical")
    srv = SimpleNamespace()
    assert shield.tick(srv, snap, 5000.0, sleeper=candidates.sleeper)["shed"] == []
    assert shield.tick(srv, snap, 5061.0, sleeper=candidates.sleeper)["shed"] == ["a"]


def test_host_only_critical_reads_the_snapshot():
    assert shield.host_only_critical(_host_snap("ok"))
    assert shield.host_only_critical(_host_snap("low"))
    assert not shield.host_only_critical(_host_snap("critical"))
    # An older snapshot without guest_level is the guest's own.
    assert not shield.host_only_critical({"level": "critical"})
    assert not shield.host_only_critical({"level": "low", "guest_level": "ok"})
