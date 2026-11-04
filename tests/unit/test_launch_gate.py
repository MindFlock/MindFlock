"""The launch admission gate (backend/web/core/launch_gate.py): go / wait /
confirm / refuse from memory, agents still warming up and the live cap, plus
the FIFO queue automatic starts wait in."""

from __future__ import annotations

import time

import pytest

from backend import memory_pressure
from backend.web.core import launch_gate as gate

GiB = 1024**3


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gate._reset_for_tests()
    monkeypatch.setattr(gate, "AUTO_DRAIN", False)
    for k in (
        "MINDFLOCK_AGENT_WARMUP_MB",
        "MINDFLOCK_MAX_LIVE_AGENTS",
        "MINDFLOCK_AUTO_LAUNCH_SPACING",
        "MINDFLOCK_LAUNCH_QUEUE_MAX",
    ):
        monkeypatch.delenv(k, raising=False)
    gate.set_probe(lambda: [])
    yield
    gate.set_probe(None)
    gate._reset_for_tests()


def _memory(monkeypatch, level="ok", available=10 * GiB, total=20 * GiB):
    snap = {
        "level": level,
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "psi": None,
    }
    monkeypatch.setattr(memory_pressure, "snapshot", lambda fresh=False: dict(snap))
    monkeypatch.setattr(
        memory_pressure,
        "thresholds",
        lambda: {
            "low": (0.15, 2 * GiB, 20.0, 5.0),
            "critical": (0.07, 1 * GiB, 40.0, 15.0),
        },
    )
    return snap


def _agents(n, created=0.0):
    gate.set_probe(lambda: [("mindflock_a%d" % i, created) for i in range(n)])


def test_ok_memory_goes(monkeypatch):
    _memory(monkeypatch)
    assert gate.decide("auto")["decision"] == "go"
    assert gate.decide("person")["decision"] == "go"


def test_low_memory_waits_automatic_and_asks_a_person(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    assert gate.decide("auto")["decision"] == "wait"
    v = gate.decide("person")
    assert v["decision"] == "confirm"
    body = gate.person_refusal(v)
    assert body["memory_confirm"] is True and body["level"] == "low"
    assert "3.0 GB free" in body["message"]
    assert "may freeze the machine" in body["message"]


def test_critical_memory_refuses_a_person(monkeypatch):
    _memory(monkeypatch, level="critical", available=1 * GiB)
    v = gate.decide("person")
    assert v["decision"] == "refuse"
    assert gate.person_refusal(v)["level"] == "critical"


def test_warming_agents_count_against_headroom(monkeypatch):
    # 4 GB free of 20 reads ok (20%), but 4 agents started a moment ago will
    # take ~2.7 GB more: 1.3 GB left is past the critical floor.
    _memory(monkeypatch, available=4 * GiB)
    _agents(4, created=time.time() - 10)
    v = gate.decide("auto")
    assert v["decision"] == "wait"
    assert "4 agents still starting" in v["reason"]
    assert gate.decide("person")["decision"] == "refuse"
    # Long-running agents are not warming.
    _agents(4, created=time.time() - 600)
    assert gate.decide("auto", fresh=True)["decision"] == "go"


def test_note_start_counts_before_tmux_exists(monkeypatch):
    _memory(monkeypatch, available=4 * GiB)
    for i in range(4):
        gate.note_start("w%d" % i)
    a = gate.assess()
    assert a["warming"] == 4 and a["live"] == 4
    assert gate.decide("auto")["decision"] == "wait"


def test_live_cap(monkeypatch):
    _memory(monkeypatch, available=15 * GiB)
    assert gate.assess()["cap"] == 16  # 20 GB / 1.2
    _agents(16)
    v = gate.decide("auto", fresh=True)
    assert v["decision"] == "wait" and "16 agents are running" in v["reason"]
    # A person is never capped.
    assert gate.decide("person")["decision"] == "go"
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "0")
    assert gate.decide("auto")["decision"] == "go"


def test_unknown_memory_never_holds(monkeypatch):
    monkeypatch.setattr(
        memory_pressure, "snapshot", lambda fresh=False: {"level": "unknown"}
    )
    assert gate.decide("auto")["decision"] == "go"
    assert gate.decide("person")["decision"] == "go"


def test_automatic_starts_are_spaced(monkeypatch):
    _memory(monkeypatch)
    assert gate.gate_auto("a", lambda: None, kind="spawn") is None
    rec = gate.gate_auto("b", lambda: None, kind="spawn")
    assert rec is not None and rec["position"] == 1
    assert "apart" in rec["reason"]


def test_queue_is_fifo_and_drains_one_at_a_time(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    started = []
    for t in ("a", "b", "c"):
        assert (
            gate.gate_auto(t, lambda t=t: started.append(t), kind="spawn") is not None
        )
    assert [i["title"] for i in gate.items()] == ["a", "b", "c"]
    assert gate.drain_once() is None  # still low
    _memory(monkeypatch)
    assert gate.drain_once() == "a"
    # The spacing holds the next one back.
    assert gate.drain_once() is None
    assert gate.drain_once(now=time.time() + 60) == "b"
    assert started == ["a", "b"]
    assert gate.last_outcome("a")["started"] is True


def test_a_newcomer_never_jumps_the_queue(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    gate.gate_auto("a", lambda: None, kind="spawn")
    _memory(monkeypatch)
    assert gate.gate_auto("b", lambda: None, kind="spawn") is not None
    assert [i["title"] for i in gate.items()] == ["a", "b"]


def test_requeue_keeps_place_and_queue_cap(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_MAX", "2")
    gate.gate_auto("a", lambda: None, kind="relaunch")
    gate.gate_auto("b", lambda: None, kind="relaunch")
    assert gate.gate_auto("a", lambda: None, kind="relaunch")["position"] == 1
    with pytest.raises(gate.QueueFull):
        gate.gate_auto("c", lambda: None, kind="spawn")


def test_start_anyway_and_cancel(monkeypatch):
    _memory(monkeypatch, level="critical", available=1 * GiB)
    ran = []
    gate.enqueue("a", lambda: ran.append("a"), kind="spawn")
    gate.enqueue("b", lambda: "boom", kind="spawn")
    gate.enqueue("c", lambda: ran.append("c"), kind="spawn")
    assert gate.start_now("a") == (True, None)
    assert ran == ["a"]
    assert gate.start_now("b") == (True, "boom")
    assert gate.last_outcome("b") == {
        "title": "b",
        "at": gate.last_outcome("b")["at"],
        "started": False,
        "error": "boom",
    }
    assert gate.cancel("c") is True
    assert gate.items() == [] and ran == ["a"]
    assert gate.start_now("nope") == (False, None)


def test_a_raising_launch_does_not_stop_the_queue(monkeypatch):
    _memory(monkeypatch)
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "0")

    def boom():
        raise RuntimeError("no worktree")

    gate.enqueue("a", boom, kind="spawn")
    gate.enqueue("b", lambda: None, kind="spawn")
    assert gate.drain_once() == "a"
    assert gate.last_outcome("a")["error"] == "no worktree"
    assert gate.drain_once() == "b"


def test_status_and_on_change(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    calls = []
    monkeypatch.setattr(gate, "on_change", lambda: calls.append(1))
    gate.gate_auto("a", lambda: None, kind="spawn", parent="orch")
    st = gate.status()
    assert st["holding"] is True
    assert st["queued"][0]["title"] == "a" and st["queued"][0]["parent"] == "orch"
    assert "memory is low" in st["reason"]
    assert calls


def test_tmux_probe_skips_shells_and_other_sessions(monkeypatch):
    class R:
        returncode = 0
        stdout = "mindflock_a 100\nmindflock_a_sh 100\nwork 5\nmindflock_b x\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: R())
    assert gate._tmux_agents() == [("mindflock_a", 100.0), ("mindflock_b", 0.0)]
    R.returncode = 1
    assert gate._tmux_agents() == []


def test_admit_auto_checks_and_notes_in_one_step(monkeypatch):
    """Two automatic starts racing: the first is noted before the lock is let
    go, so the second sees it (spacing, warming) and waits."""
    import threading

    _memory(monkeypatch)
    results = []
    barrier = threading.Barrier(2)

    def go(key):
        barrier.wait()
        results.append(gate.admit_auto(key)[0])

    threads = [threading.Thread(target=go, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert gate.assess()["warming"] == 1


def test_gate_auto_marks_a_queued_create(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    rec = gate.gate_auto("w1", lambda: None, kind="spawn", creates=True)
    assert rec["creates"] is True and gate.waiting("w1")["creates"] is True
