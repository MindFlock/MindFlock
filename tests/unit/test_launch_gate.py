"""The launch admission gate (backend/web/core/launch_gate.py): go / wait /
confirm / refuse from memory, agents still warming up and the live cap, plus
the FIFO queue automatic starts wait in."""

from __future__ import annotations

import json
import os
import threading
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
        "MINDFLOCK_AUTO_SPACING_FAST",
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
    # A person is asked only on the level NOW (ok here): the projection
    # paces automatic starts, never a person's own (owner, 2026-11-10).
    assert gate.decide("person")["decision"] == "go"
    # Long-running agents are not warming.
    _agents(4, created=time.time() - 600)
    assert gate.decide("auto", fresh=True)["decision"] == "go"


def test_a_person_is_never_asked_over_the_room_estimate(monkeypatch):
    """13 GB free and the level ok, but the room estimate full (a big
    learned cost): automatic starts wait, a person's start just goes."""
    _memory(monkeypatch, available=13 * GiB)
    monkeypatch.setattr(gate, "_budget_full", lambda a: True)
    assert gate.decide("auto")["decision"] == "wait"
    v = gate.decide("person")
    assert v["decision"] == "go" and gate.person_refusal(v) is None


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


def test_room_counts_memory_warming_and_the_cap(monkeypatch):
    # 10 GB free of 20, the low line at 3 GB (15%): 7 GB of room = 10 agents
    # of 700 MB; 2 warming take ~1.4 GB of it.
    _memory(monkeypatch, available=10 * GiB)
    assert gate.room() == 10
    _agents(2, created=time.time() - 5)
    assert gate.room() == 8
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "4")
    assert gate.room() == 2
    _memory(monkeypatch, level="low", available=3 * GiB)
    assert gate.room() == 0
    monkeypatch.setattr(
        memory_pressure, "snapshot", lambda fresh=False: {"level": "unknown"}
    )
    monkeypatch.delenv("MINDFLOCK_MAX_LIVE_AGENTS")
    assert gate.room() is None


def _host(available, total=32 * GiB, level="ok"):
    return {
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "level": level,
        "at": 0.0,
    }


_COST = {"session_mb": 1024, "peak_mb": 1024, "source": "measured", "samples": 3}


def test_budget_is_capped_by_windows_headroom_and_says_so():
    # VM: 10 - 2 reserve = 8 GB; Windows: 6 - 2 (its headroom floor) = 4 GB.
    snap = {"level": "ok", "available": 10 * GiB, "total": 20 * GiB}
    snap["host"] = _host(5.5 * GiB)
    b = gate.budget(snap, _COST, reserve=2 * GiB)
    assert b["room"] == 3 and b["free_mb"] == 3584  # 5632 - 2048
    assert b["available_mb"] == 10240  # the guest's own number still reported
    assert b["why"] == (
        "room for 3 more sessions: Windows has 5.5 GB available over its own "
        "reserve of 2.0 GB, each ~1.0 GB (measured over 3 sessions)"
    )


def test_budget_keeps_the_guest_sum_when_the_guest_binds():
    snap = {"level": "ok", "available": 4 * GiB, "total": 20 * GiB}
    snap["host"] = _host(16 * GiB)
    b = gate.budget(snap, _COST, reserve=2 * GiB)
    assert b["room"] == 2 and b["free_mb"] == 2048
    assert "Windows" not in b["why"]
    assert "4.0 GB available − 2.0 GB reserve" in b["why"]


def test_windows_with_no_headroom_leaves_no_room_while_still_ok():
    # Windows at 2.5 GB is ok (it came down from above), its floor 2 GB;
    # an agent warming with 1 GB still to come eats past it.
    snap = {"level": "ok", "available": 10 * GiB, "total": 20 * GiB}
    snap["host"] = _host(int(2.5 * GiB))
    b = gate.budget(snap, _COST, [None], reserve=2 * GiB)
    assert b["room"] == 0 and b["to_come_mb"] == 1024
    host_free = 2.5 * GiB - 2 * GiB - 1 * GiB
    assert b["free_mb"] == round(host_free / 1024**2)
    assert b["short_mb"] == 1024  # all of one session: nothing is free
    assert b["why"].startswith("one more session needs ~1.0 GB; only 0.0 GB")
    assert "Windows has 2.5 GB available over its own reserve" in b["why"]


@pytest.mark.parametrize("host", [None, {}, {"available": GiB, "total": 0}])
def test_no_host_reading_leaves_the_budget_as_it_was(host):
    base = {"level": "ok", "available": 10 * GiB, "total": 20 * GiB}
    want = gate.budget(dict(base), _COST, reserve=2 * GiB)
    got = gate.budget(dict(base, host=host), _COST, reserve=2 * GiB)
    assert got == want and got["room"] == 8


def test_the_cap_and_warmup_are_settings_the_env_beats(monkeypatch):
    _memory(monkeypatch)
    vals = {"max_live_agents": 5, "agent_warmup_mb": 500}
    monkeypatch.setattr(gate, "_setting", lambda name: vals.get(name))
    assert gate.assess()["cap"] == 5
    assert gate.warmup_bytes() == 500 * 1024**2
    vals["max_live_agents"] = 0
    assert gate.assess()["cap"] == 0
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "9")
    monkeypatch.setenv("MINDFLOCK_AGENT_WARMUP_MB", "800")
    assert gate.assess()["cap"] == 9 and gate.warmup_bytes() == 800 * 1024**2


def test_the_settings_fields_round_trip(tmp_path, monkeypatch):
    from backend.config import settings as S

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "s.json"))
    S.update_settings(general={"max_live_agents": 12, "agent_warmup_mb": 650})
    g = S.load_settings().general
    assert (g.max_live_agents, g.agent_warmup_mb) == (12, 650)
    gate._SETTINGS_CACHE.update({"at": -1e18})
    assert gate._setting("max_live_agents") == 12
    S.update_settings(general={"max_live_agents": -3})
    assert S.load_settings().general.max_live_agents is None
    from backend.web.core import settings_sync

    # Sized to THIS machine's RAM: never synced to the person's other devices.
    assert {"max_live_agents", "agent_warmup_mb"} <= set(settings_sync.LOCAL["general"])
    assert "max_live_agents" not in settings_sync.SYNCED.get("general", ())


def test_the_queue_survives_a_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_FILE", str(tmp_path / "q.json"))
    _memory(monkeypatch, level="low", available=3 * GiB)
    monkeypatch.setattr(gate, "PERSIST", True)
    ran = []
    monkeypatch.setitem(
        gate._FACTORIES, "test-kind", lambda rec: (lambda: ran.append(rec["args"]["n"]))
    )
    gate.enqueue("a", lambda: None, kind="test-kind", args={"n": 1}, parent="orch")
    gate.enqueue("b", lambda: None, kind="test-kind", args={"n": 2})
    gate.enqueue("c", lambda: None, kind="test-kind")  # no args: not saved
    gate.enqueue("d", lambda: None, kind="nobody-restores-me", args={})
    doc = json.loads((tmp_path / "q.json").read_text())
    assert [i["title"] for i in doc["items"]] == ["a", "b", "d"]
    # The server stops; the next one boots.
    since = gate.waiting("a")["since"]
    gate._reset_for_tests()
    assert gate.items() == []
    assert gate.restore() == 2
    assert [i["title"] for i in gate.items()] == ["a", "b"]
    assert gate.waiting("a")["since"] == since and gate.waiting("a")["parent"] == "orch"
    _memory(monkeypatch)
    assert gate.drain_once() == "a" and ran == [1]
    # Each change rewrites the file.
    doc = json.loads((tmp_path / "q.json").read_text())
    assert [i["title"] for i in doc["items"]] == ["b"]
    gate.cancel("b")
    assert json.loads((tmp_path / "q.json").read_text())["items"] == []


def test_decide_names_what_holds_it(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    assert gate.decide("auto")["why"] == "memory"
    _memory(monkeypatch, available=4 * GiB)
    _agents(4, created=time.time() - 5)
    assert gate.decide("auto", fresh=True)["why"] == "warming"
    _agents(0)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "1")
    gate.note_start("x")
    assert gate.decide("auto", fresh=True)["why"] == "cap"


@pytest.mark.parametrize(
    "swap_in, penalised",
    [
        (0.0, False),
        (0.99, False),
        (1.0, True),  # SWAP_PENALTY_FROM_MB_S: at the line it counts
        (5.0, True),
        ("n/a", True),  # unreadable: the old rule
        ({"x": 1}, True),
        (None, True),  # absent (an older snapshot, or no rate yet)
    ],
)
def test_swap_in_use_costs_room_only_while_it_is_read_back(swap_in, penalised):
    snap = {
        "level": "ok",
        "available": 10 * GiB,
        "total": 20 * GiB,
        "swap_total": 8 * GiB,
        "swap_free": 6 * GiB,  # 2 GB in swap, at the 10%-of-RAM cap
    }
    if swap_in is not None:
        snap["swap_in_mb_s"] = swap_in
    swap_pen, psi_pen = gate._penalties(snap)
    assert swap_pen == (2 * GiB if penalised else 0) and psi_pen == 0


def test_the_reserve_is_8_percent_of_ram_and_windows_keeps_2_gb():
    memory_pressure._reset_for_tests()  # the default share settings
    assert gate.reserve_bytes(64 * GiB) == int(0.08 * (64 * GiB))  # 5.12 GB
    assert gate.reserve_bytes(16 * GiB) == 2 * GiB  # the floor
    for host_total in (16 * GiB, 128 * GiB):
        snap = {"level": "ok", "available": 40 * GiB, "total": 64 * GiB}
        snap["host"] = _host(6 * GiB, total=host_total)
        b = gate.budget(snap, _COST)
        assert b["reserve_mb"] == round(0.08 * 64 * 1024)
        assert b["free_mb"] == 4096  # Windows binds: 6 - its 2 GB floor
        assert "over its own reserve of 2.0 GB" in b["why"]
    memory_pressure._reset_for_tests()


def test_a_pending_step_up_starts_but_a_sustained_low_waits(monkeypatch):
    """``pending`` is the gauge still waiting out its sustain: the level is
    ok, so an automatic start goes. Once low lands it waits, room or not."""
    snap = _memory(monkeypatch, available=16 * GiB)
    snap.update(pending="critical", why=["swapping 9.5 MB/s back in from disk"])
    assert gate.decide("auto")["decision"] == "go"
    snap.update(level="low", pending=None)
    v = gate.decide("auto")
    assert v["decision"] == "wait" and v["why"] == "memory"
    assert "swapping 9.5 MB/s back in from disk" in v["reason"]


# --------------------------------------------------------------------------- #
# Why a launch waits (the ``why`` contract) and an honest log line
# --------------------------------------------------------------------------- #
def _logs(monkeypatch) -> list:
    lines: list = []
    monkeypatch.setattr(gate, "_log", lambda fmt, *a: lines.append(fmt % a))
    return lines


def test_a_spaced_start_says_spacing_not_memory(monkeypatch):
    """The live log said ``vc-words waits for memory (spawn): the last agent
    started 4s ago; automatic starts are 20s apart``: the stagger, blamed
    on memory."""
    _memory(monkeypatch)
    lines = _logs(monkeypatch)
    assert gate.gate_auto("a", lambda: None, kind="spawn") is None
    rec = gate.gate_auto("b", lambda: None, kind="spawn")
    assert rec["why"] == "spacing" and gate.waiting("b")["why"] == "spacing"
    assert lines == ["b waits (spacing, spawn): " + rec["reason"]]
    assert gate.status()["why"] == "spacing"


def test_memory_and_warming_waits_say_memory(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    lines = _logs(monkeypatch)
    rec = gate.gate_auto("a", lambda: None, kind="spawn")
    assert rec["why"] == "memory"
    assert lines[-1].startswith("a waits for memory (spawn): ")
    gate._reset_for_tests()
    _memory(monkeypatch, available=4 * GiB)
    _agents(4, created=time.time() - 5)
    rec = gate.gate_auto("w", lambda: None, kind="spawn")
    assert rec["why"] == "warming"
    assert lines[-1].startswith("w waits for memory (warming, spawn): ")


def test_admit_keeps_the_old_shape_and_admit_names_why(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    go, reason = gate.admit_auto("x")
    assert go is False and "low" in reason
    assert gate.admit("x")[0] is False and gate.admit("x")[2] == "memory"
    gate.enqueue("q", lambda: None, kind="spawn", reason="r")
    assert gate.admit("y") == (
        False,
        "1 launch ahead of it; the first waits: r",
        "queue",
    )


def test_drain_judges_the_head_and_marks_the_rest_queue(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    for t in ("a", "b", "c"):
        gate.gate_auto(t, lambda: None, kind="spawn")
    # Newcomers behind someone already waiting are held by the queue.
    assert [i["why"] for i in gate.items()] == ["memory", "queue", "queue"]
    _memory(monkeypatch, available=10 * GiB)
    gate.note_start("other", auto=True)  # a stagger is running
    calls = []
    monkeypatch.setattr(gate, "on_change", lambda: calls.append(1))
    lines = _logs(monkeypatch)
    assert gate.drain_once() is None
    q = gate.items()
    assert [i["why"] for i in q] == ["spacing", "queue", "queue"]
    assert q[1]["reason"].startswith("1 launch ahead of it; the first waits: the last")
    assert q[2]["reason"].startswith("2 launches ahead of it")
    st = gate.status()
    assert st["why"] == "spacing" and st["reason"] == q[0]["reason"]
    assert calls and lines == ["a waits (spacing, spawn): " + q[0]["reason"]]


# --------------------------------------------------------------------------- #
# A failed launch takes its admission back
# --------------------------------------------------------------------------- #
def test_a_failed_queued_launch_is_no_phantom_warming_agent(monkeypatch):
    _memory(monkeypatch)
    gate.note_start("earlier", auto=True, now=time.time() - 100)
    before = gate._LAST_AUTO["at"]
    gate.enqueue("a", lambda: "no worktree", kind="spawn")
    gate.enqueue("b", lambda: None, kind="spawn")
    assert gate.drain_once() == "a"
    assert gate.last_outcome("a")["error"] == "no worktree"
    assert gate.assess()["warming"] == 0 and gate._LAST_AUTO["at"] == before
    # No stagger after a launch that never happened: the next goes now.
    assert gate.drain_once() == "b"


def test_a_raising_launch_is_taken_back_too(monkeypatch):
    _memory(monkeypatch)

    def boom():
        raise RuntimeError("no worktree")

    gate.enqueue("a", boom, kind="spawn")
    assert gate.drain_once() == "a"
    assert gate.assess()["warming"] == 0 and gate._LAST_AUTO["at"] == 0.0


def test_a_failed_launch_whose_session_exists_still_counts(monkeypatch):
    _memory(monkeypatch)
    name = gate._tmux_name("a")
    gate.enqueue("a", lambda: "said it failed", kind="spawn")
    gate.set_probe(lambda: [(name, time.time())])
    assert gate.drain_once() == "a"
    assert gate._LAST_AUTO["at"] > 0
    assert gate.unnote_start("a") is False
    assert gate.assess(fresh=True)["warming"] == 1


def test_unnote_leaves_a_later_start_alone(monkeypatch):
    _memory(monkeypatch)
    gate.note_start("a", auto=True, now=time.time() - 3)
    gate.note_start("b", auto=True)
    later = gate._LAST_AUTO["at"]
    assert gate.unnote_start("a") is True
    assert gate._LAST_AUTO["at"] == later  # b's stagger stands
    assert gate.unnote_start("a") is False  # once only
    assert gate.assess()["warming"] == 1


# --------------------------------------------------------------------------- #
# In flight: saved until the launch returns, and never blocking the rest
# --------------------------------------------------------------------------- #
def _persist(monkeypatch, tmp_path):
    path = tmp_path / "q.json"
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_FILE", str(path))
    monkeypatch.setattr(gate, "PERSIST", True)
    return path


def _join_starts():
    """Wait out every background launch thread, so none settles (records,
    saves, un-notes) inside the next test."""
    for th in threading.enumerate():
        if th.name == "launch-gate-start":
            th.join(5)


def _saved_titles(path):
    return [
        (i["title"], i.get("inflight")) for i in json.loads(path.read_text())["items"]
    ]


def test_a_launch_stays_on_disk_until_it_returns(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch)
    seen = []
    gate.enqueue(
        "a", lambda: seen.append(_saved_titles(path)), kind="spawn", args={"n": 1}
    )
    gate.enqueue("b", lambda: None, kind="spawn", args={"n": 2})
    assert gate.drain_once() == "a"
    # While a's launch ran it was on disk, flagged, ahead of b.
    assert seen == [[("a", True), ("b", False)]]
    assert _saved_titles(path) == [("b", False)]


def test_a_restart_mid_launch_brings_it_back_first(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch)
    release = threading.Event()
    gate.enqueue("a", lambda: release.wait(5) and None, kind="k", args={"n": 1})
    gate.enqueue("b", lambda: None, kind="k", args={"n": 2})
    try:
        assert gate.drain_once(wait=False) == "a"
        assert _saved_titles(path) == [("a", True), ("b", False)]
        # The server dies here (a never returned); the next boot restores.
        gate._reset_for_tests()
        monkeypatch.setitem(gate._FACTORIES, "k", lambda rec: (lambda: None))
        assert gate.restore() == 2
        q = gate.items()
        assert [i["title"] for i in q] == ["a", "b"]
        assert {i["why"] for i in q} == {"restart"}
        assert "was starting when the server stopped" in q[0]["reason"]
    finally:
        release.set()
        _join_starts()


class _Waited(threading.Event):
    """An in-flight item's ``done`` that runs ``on_wait`` the moment someone
    starts waiting on it: a test lets the launch go exactly then, with no
    sleep and no timer."""

    def __init__(self, on_wait):
        super().__init__()
        self._on_wait = on_wait

    def wait(self, timeout=None):
        self._on_wait()
        return super().wait(timeout)


def _release_on_wait(title, release):
    """Let ``title``'s in-flight launch go once Start anyway waits on it."""
    with gate._LOCK:
        gate._in_flight(title)["done"] = _Waited(release.set)


def _blocked(release, entered=None, result=None, runs=None):
    """A launch that holds until ``release`` (``entered`` is set once it
    runs; it fails the test's outcome rather than hang if never let go)."""

    def launch():
        if runs is not None:
            runs.append(threading.current_thread())
        if entered is not None:
            entered.set()
        return result if release.wait(30) else "never released"

    return launch


def test_a_slow_launch_never_stalls_the_ones_behind_it(monkeypatch):
    _memory(monkeypatch)
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "0")
    release = threading.Event()
    ran = threading.Event()
    gate.enqueue("slow", _blocked(release), kind="spawn")
    gate.enqueue("next", ran.set, kind="spawn")
    try:
        assert gate.drain_once(wait=False) == "slow"
        assert gate.waiting("slow") is None and gate.status()["starting"] == ["slow"]
        # Still launching, it counts as warming; the next one goes anyway.
        assert gate.assess()["warming"] >= 1
        assert gate.drain_once(wait=False) == "next"
        assert ran.wait(30)  # it ran while slow was still held
        assert not release.is_set()
        # Cancel can't call back a launch already starting.
        assert gate.cancel("slow") is False
        # Start anyway on it waits for its outcome instead of launching twice.
        _release_on_wait("slow", release)
        assert gate.start_now("slow") == (True, None)
    finally:
        release.set()
        _join_starts()
    assert gate.status()["starting"] == []
    assert gate.last_outcome("slow")["started"] is True


def test_cancel_cannot_call_back_a_launch_already_starting(monkeypatch):
    _memory(monkeypatch)
    lines = _logs(monkeypatch)
    release = threading.Event()
    gate.enqueue("a", _blocked(release, result="boom"), kind="spawn")
    try:
        assert gate.drain_once(wait=False) == "a"
        assert gate.cancel("a") is False
        assert "a: not cancelled, it is already starting" in lines
        # Not recorded as cancelled, and still starting.
        assert gate.last_outcome("a") is None
        assert gate.status()["starting"] == ["a"]
    finally:
        release.set()
        _join_starts()
    # It ran to its own outcome.
    out = gate.last_outcome("a")
    assert out["started"] is False and out["error"] == "boom"
    # Something never queued says nothing.
    n = len(lines)
    assert gate.cancel("nope") is False and len(lines) == n


def test_start_anyway_on_a_starting_launch_never_launches_twice(monkeypatch):
    _memory(monkeypatch)
    release = threading.Event()
    runs: list = []
    gate.enqueue("a", _blocked(release, result="boom", runs=runs), kind="spawn")
    try:
        assert gate.drain_once(wait=False) == "a"
        _release_on_wait("a", release)
        # Its outcome, once it finished: the error of the one launch.
        assert gate.start_now("a") == (True, "boom")
    finally:
        release.set()
        _join_starts()
    assert len(runs) == 1 and runs[0].name == "launch-gate-start"
    # By its id too, and gone once it has settled.
    assert gate.start_now("a") == (False, None)


def test_start_anyway_gives_up_waiting_on_a_stuck_launch(monkeypatch):
    _memory(monkeypatch)
    monkeypatch.setattr(gate, "START_NOW_WAIT_SECONDS", 0.01)
    release = threading.Event()
    runs: list = []
    gate.enqueue("a", _blocked(release, runs=runs), kind="spawn")
    try:
        assert gate.drain_once(wait=False) == "a"
        assert gate.start_now("a") == (True, "still starting")
        assert gate.status()["starting"] == ["a"]
    finally:
        release.set()
        _join_starts()
    assert len(runs) == 1 and gate.last_outcome("a")["started"] is True


def test_a_persons_start_anyway_does_not_space_the_next_automatic_start(monkeypatch):
    _memory(monkeypatch, level="critical", available=1 * GiB)
    ran = []
    gate.enqueue("a", lambda: ran.append("a"), kind="spawn")
    gate.enqueue("b", lambda: ran.append("b"), kind="spawn")
    assert gate.start_now("a") == (True, None)
    # Noted as warming, but not as an automatic start.
    assert gate._LAST_AUTO["at"] == 0.0
    assert gate._tmux_name("a") in gate._ADMITTED
    _memory(monkeypatch)
    assert gate.decide("auto", fresh=True)["decision"] == "go"
    assert gate.drain_once() == "b" and ran == ["a", "b"]
    assert gate._LAST_AUTO["at"] > 0  # the drainer's own start is automatic


def test_a_stale_verdict_admits_nothing(monkeypatch):
    """Another automatic start was admitted while this step read memory: the
    go it got is for a machine that has changed, so it is judged again."""
    _memory(monkeypatch)
    ran = []
    gate.enqueue("a", lambda: ran.append("a"), kind="spawn", reason="r")
    real = gate.decide

    def racing(kind="auto", **kw):
        verdict = real(kind, **kw)
        gate.note_start("racer", auto=True)  # between the read and the verdict
        return verdict

    monkeypatch.setattr(gate, "decide", racing)
    calls = []
    monkeypatch.setattr(gate, "on_change", lambda: calls.append(1))
    assert gate.drain_once() is None
    assert ran == [] and gate.status()["starting"] == [] and calls == []
    rec = gate.waiting("a")  # untouched: neither admitted nor re-judged
    assert (rec["position"], rec["why"], rec["reason"]) == (1, "memory", "r")
    monkeypatch.setattr(gate, "decide", real)
    # Judged again, it meets the racer's stagger; past it, it goes.
    assert gate.drain_once() is None and gate.waiting("a")["why"] == "spacing"
    assert gate.drain_once(now=time.time() + 60) == "a" and ran == ["a"]


def test_the_background_start_runs_in_its_own_daemon_thread(monkeypatch):
    _memory(monkeypatch)
    release, entered = threading.Event(), threading.Event()
    runs: list = []
    gate.enqueue("a", _blocked(release, entered, runs=runs), kind="spawn")
    calls = []
    monkeypatch.setattr(gate, "on_change", lambda: calls.append(1))
    try:
        assert gate.drain_once(wait=False) == "a"
        assert entered.wait(30)
        th = runs[0]
        assert th is not threading.current_thread()
        assert th.name == "launch-gate-start" and th.daemon is True
        assert calls == [1]  # on admission: the row stops saying it waits
        assert gate.items() == [] and gate.status()["starting"] == ["a"]
    finally:
        release.set()
        _join_starts()
    assert calls == [1, 1]  # and again once the launch has returned
    assert gate.status()["starting"] == []


def test_the_inline_start_runs_in_the_callers_thread(monkeypatch):
    _memory(monkeypatch)
    runs = []
    gate.enqueue("a", lambda: runs.append(threading.current_thread()), kind="spawn")
    assert gate.drain_once() == "a"
    assert runs == [threading.current_thread()]


def test_a_reset_leaves_nothing_in_flight_or_to_undo(monkeypatch):
    _memory(monkeypatch)
    release = threading.Event()
    gate.enqueue("a", _blocked(release), kind="spawn")
    try:
        assert gate.drain_once(wait=False) == "a"
        gate.note_start("b", auto=True)
        assert gate._INFLIGHT and gate._UNDO and gate._ADMITTED
        gate._reset_for_tests()
        assert gate._INFLIGHT == [] and gate._UNDO == {} and gate._ADMITTED == {}
        assert gate._QUEUE == [] and gate._LAST_AUTO["at"] == 0.0
        _memory(monkeypatch)
        assert gate.status()["starting"] == [] and gate.assess()["warming"] == 0
    finally:
        release.set()
        _join_starts()
    # The launch that outlived the reset settles without coming back.
    assert gate._INFLIGHT == [] and gate.items() == []


# --------------------------------------------------------------------------- #
# Restore never drops; a re-queue updates its whole record
# --------------------------------------------------------------------------- #
def _write(path, recs):
    path.write_text(json.dumps({"v": 1, "items": recs}))


def test_restore_ignores_the_queue_cap_and_deletes_nothing(monkeypatch, tmp_path):
    path = tmp_path / "q.json"
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_FILE", str(path))
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_MAX", "1")
    _memory(monkeypatch, level="low", available=3 * GiB)
    _write(path, [{"title": t, "kind": "k", "args": {}, "since": 1.0} for t in "abc"])
    monkeypatch.setitem(gate._FACTORIES, "k", lambda rec: (lambda: None))
    assert gate.restore() == 3
    assert [i["title"] for i in gate.items()] == ["a", "b", "c"]
    assert [t for t, _f in _saved_titles(path)] == ["a", "b", "c"]
    # New launches still meet the cap.
    with pytest.raises(gate.QueueFull):
        gate.enqueue("d", lambda: None, kind="k")


def test_a_requeue_replaces_what_describes_the_launch(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    gate.enqueue(
        "a", lambda: None, kind="queued prompt", source="prompt queue", args={}
    )
    gate.enqueue("z", lambda: None, kind="spawn")
    rec = gate.enqueue(
        "a",
        lambda: None,
        kind="spawn",
        source="agent",
        creates=True,
        args={"payload": {"title": "a"}},
    )
    assert rec["position"] == 1 and rec["kind"] == "spawn"
    assert rec["source"] == "agent" and rec["creates"] is True
    saved = json.loads(path.read_text())["items"][0]
    assert (saved["kind"], saved["source"], saved["creates"]) == (
        "spawn",
        "agent",
        True,
    )
    assert saved["args"] == {"payload": {"title": "a"}}


# --------------------------------------------------------------------------- #
# Adaptive spacing
# --------------------------------------------------------------------------- #
def test_plenty_of_room_uses_the_short_stagger(monkeypatch):
    _memory(monkeypatch, available=10 * GiB)
    t0 = time.time()
    gate.note_start("x", auto=True, now=t0)
    a = gate.assess(now=t0 + 1)
    assert a["budget"]["room"] >= gate.FAST_SPACING_ROOM
    assert gate.spacing_for(a) == gate.AUTO_SPACING_FAST_SECONDS
    v = gate.decide("auto", now=t0 + 2)
    assert v["why"] == "spacing" and "5s apart while there is room for" in v["reason"]
    assert gate.decide("auto", now=t0 + 6)["decision"] == "go"
    monkeypatch.setenv("MINDFLOCK_AUTO_SPACING_FAST", "8")
    assert gate.decide("auto", now=t0 + 6)["why"] == "spacing"
    # Never over the ceiling.
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "3")
    assert gate.spacing_for(a) == 3.0


def test_little_room_keeps_the_full_stagger(monkeypatch):
    # 4.5 GB free, 3 GB reserve, one 0.7 GB agent warming: room for 1.
    _memory(monkeypatch, available=int(4.5 * GiB))
    t0 = time.time()
    gate.note_start("x", auto=True, now=t0)
    a = gate.assess(now=t0 + 1)
    assert 1 <= a["budget"]["room"] < gate.FAST_SPACING_ROOM
    assert gate.spacing_for(a) == gate.AUTO_SPACING_SECONDS
    v = gate.decide("auto", now=t0 + 6)
    assert v["why"] == "spacing" and v["reason"].endswith("20s apart")


def test_spacing_for_needs_every_reading_ok():
    ok = {"level": "ok", "projected_level": "ok", "budget": {"room": 9}}
    assert gate.spacing_for(ok) == gate.AUTO_SPACING_FAST_SECONDS
    for bad in (
        dict(ok, projected_level="low"),
        dict(ok, level="unknown"),
        dict(ok, budget={"room": None}),
        dict(ok, budget={"room": 2}),
    ):
        assert gate.spacing_for(bad) == gate.AUTO_SPACING_SECONDS


# --------------------------------------------------------------------------- #
# The person's dialog never contradicts itself
# --------------------------------------------------------------------------- #
def test_low_memory_gives_its_reason_not_the_arithmetic(monkeypatch):
    snap = _memory(monkeypatch, level="low", available=10 * GiB)
    snap["why"] = ["swapping 9.5 MB/s back in from disk"]
    body = gate.person_refusal(gate.decide("person"))
    assert "is free above" not in body["message"]
    assert "No room for another session until memory recovers" in body["message"]
    assert "swapping 9.5 MB/s back in from disk" in body["message"]


def test_the_room_sentence_by_cause():
    base = {"room": 0, "session_mb": 700, "reserve_mb": 3072, "free_mb": 300}
    s = gate._room_sentence(dict(base, level="ok", short_mb=400))
    assert s.startswith("No room for another session: one needs ~0.7 GB, 0.3 GB")
    cap = "16 agents are running; automatic starts stop at 16"
    s = gate._room_sentence(dict(base, level="ok", short_mb=0, why=cap))
    assert s == "No room for another automatic start: %s." % cap
    s = gate._room_sentence(dict(base, level="critical", why="memory is critical."))
    assert s == "No room for another session until memory recovers: memory is critical."


# --------------------------------------------------------------------------- #
# Two servers on one queue file
# --------------------------------------------------------------------------- #
_GONE_PID = 2**22 + 7  # past Linux's pid_max: no such process


def test_a_save_keeps_another_live_servers_items(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    other = {"pid": os.getppid(), "boot": "theirs", "start": None}
    _write(
        path, [{"title": "x", "kind": "k", "args": {}, "since": 1.0, "owner": other}]
    )
    gate.enqueue("a", lambda: None, kind="k", args={})
    assert [t for t, _f in _saved_titles(path)] == ["a", "x"]
    gate.cancel("a")
    assert [t for t, _f in _saved_titles(path)] == ["x"]


def test_restore_adopts_only_items_whose_owner_is_gone(monkeypatch, tmp_path):
    path = tmp_path / "q.json"
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_FILE", str(path))
    _memory(monkeypatch, level="low", available=3 * GiB)
    live = {"pid": os.getppid(), "boot": "theirs", "start": None}
    gone = {"pid": _GONE_PID, "boot": "old", "start": None}
    # The parent's pid, but another start time: a process that reused it.
    reused = {"pid": os.getppid(), "boot": "old", "start": -5}

    def rec(t, owner):
        r = {"title": t, "kind": "k", "args": {}, "since": 1.0}
        if owner:
            r["owner"] = owner
        return r

    _write(
        path,
        [
            rec("live", live),
            rec("gone", gone),
            rec("legacy", None),
            rec("reused", reused),
        ],
    )
    monkeypatch.setitem(gate._FACTORIES, "k", lambda r: (lambda: None))
    assert gate.restore() == 3
    assert [i["title"] for i in gate.items()] == ["gone", "legacy", "reused"]
    # The file now: the three, owned by this process, and the live server's.
    doc = json.loads(path.read_text())["items"]
    assert [i["title"] for i in doc] == ["gone", "legacy", "reused", "live"]
    assert all(i["owner"]["pid"] == os.getpid() for i in doc[:3])
    assert doc[3]["owner"] == live


def test_a_stopped_servers_items_wait_for_the_next_boot(monkeypatch, tmp_path):
    """A running server's save keeps a stopped one's items (only a boot
    adopts them), so neither loses the other's launches."""
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    gone = {"pid": _GONE_PID, "boot": "old", "start": None}
    _write(path, [{"title": "g", "kind": "k", "args": {}, "since": 1.0, "owner": gone}])
    gate.enqueue("a", lambda: None, kind="k", args={})
    assert [t for t, _f in _saved_titles(path)] == ["a", "g"]


# --------------------------------------------------------------------------- #
# The short stagger's bounds
# --------------------------------------------------------------------------- #
def test_the_short_stagger_is_clamped_to_the_full_one(monkeypatch):
    assert gate.fast_spacing() == gate.AUTO_SPACING_FAST_SECONDS
    monkeypatch.setenv("MINDFLOCK_AUTO_SPACING_FAST", "60")
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "20")
    assert gate.fast_spacing() == 20.0
    roomy = {"level": "ok", "projected_level": "ok", "budget": {"room": 9}}
    assert gate.spacing_for(roomy) == 20.0
    monkeypatch.setenv("MINDFLOCK_AUTO_SPACING_FAST", "not a number")
    assert gate.fast_spacing() == gate.AUTO_SPACING_FAST_SECONDS


@pytest.mark.parametrize("room", [0, 1, 2, 3, 50, None])
def test_no_stagger_at_all_when_the_spacing_is_zero(monkeypatch, room):
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "0")
    a = {"level": "ok", "projected_level": "ok", "budget": {"room": room}}
    assert gate.fast_spacing() == 0.0 and gate.spacing_for(a) == 0.0
    # And nothing is held for it.
    _memory(monkeypatch)
    gate.note_start("x", auto=True)
    assert gate.decide("auto")["decision"] == "go"


@pytest.mark.parametrize(
    "room, level, projected, fast",
    [
        (gate.FAST_SPACING_ROOM, "ok", "ok", True),  # exactly at the line
        (gate.FAST_SPACING_ROOM + 1, "ok", "ok", True),
        (gate.FAST_SPACING_ROOM - 1, "ok", "ok", False),
        (gate.FAST_SPACING_ROOM, "ok", "low", False),
        (gate.FAST_SPACING_ROOM, "low", "ok", False),
        (gate.FAST_SPACING_ROOM, "ok", "unknown", False),
        (float(gate.FAST_SPACING_ROOM), "ok", "ok", False),  # not a count
    ],
)
def test_the_short_stagger_starts_at_room_for_three(room, level, projected, fast):
    a = {"level": level, "projected_level": projected, "budget": {"room": room}}
    want = gate.AUTO_SPACING_FAST_SECONDS if fast else gate.AUTO_SPACING_SECONDS
    assert gate.spacing_for(a) == want
    assert gate.spacing_for({}) == gate.AUTO_SPACING_SECONDS  # nothing measured


# --------------------------------------------------------------------------- #
# The wait line: once per cause, and never blaming memory for a stagger
# --------------------------------------------------------------------------- #
def test_the_wait_line_is_logged_only_when_the_cause_changes(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    lines = _logs(monkeypatch)
    gate.gate_auto("a", lambda: None, kind="spawn")
    gate.gate_auto("b", lambda: None, kind="spawn")
    assert len(lines) == 2 and lines[0].startswith("a waits for memory (spawn): ")
    assert lines[1].startswith("b waits (queue, spawn): 1 launch ahead of it")
    # Still low, step after step: nothing new to say.
    assert gate.drain_once() is None and gate.drain_once() is None
    assert len(lines) == 2
    # Memory recovers into a running stagger: one line for the new cause.
    _memory(monkeypatch)
    gate.note_start("other", auto=True)
    assert gate.drain_once() is None and gate.drain_once() is None
    assert len(lines) == 3 and lines[2].startswith("a waits (spacing, spawn): ")
    # And back: memory again is a change too.
    _memory(monkeypatch, level="low", available=3 * GiB)
    assert gate.drain_once() is None and gate.drain_once() is None
    assert len(lines) == 4 and lines[3].startswith("a waits for memory (spawn): ")
    # Only ever the head's line: the ones behind it are not logged per step.
    assert [ln.split(" ", 1)[0] for ln in lines] == ["a", "b", "a", "a"]


def test_a_spacing_held_record_never_logs_for_memory(monkeypatch):
    """Regression: the stagger, the cap and the queue are never blamed on
    memory, at enqueue or on any later drain step."""
    _memory(monkeypatch)
    lines = _logs(monkeypatch)
    assert gate.gate_auto("first", lambda: None, kind="spawn") is None
    for t in ("a", "b"):
        assert gate.gate_auto(t, lambda: None, kind="spawn") is not None
    for _ in range(3):
        assert gate.drain_once() is None
    assert gate.waiting("a")["why"] == "spacing"
    assert lines and not any("for memory" in ln for ln in lines)
    assert gate._wait_line("t", "spawn", "cap", "r") == "t waits (cap, spawn): r"
    assert gate._wait_line("t", "spawn", "restart", "") == (
        "t waits (restart, spawn): queued"
    )
    assert gate._wait_line("t", "spawn", "", "r") == "t waits (queue, spawn): r"
    for why in gate.WHYS:
        says_memory = "for memory" in gate._wait_line("t", "spawn", why, "r")
        assert says_memory == (why in ("memory", "warming"))


def test_a_cap_held_start_says_cap(monkeypatch):
    _memory(monkeypatch, available=15 * GiB)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "2")
    _agents(2)
    lines = _logs(monkeypatch)
    rec = gate.gate_auto("a", lambda: None, kind="relaunch")
    assert rec["why"] == "cap" and gate.status()["why"] == "cap"
    assert lines == ["a waits (cap, relaunch): " + rec["reason"]]


# --------------------------------------------------------------------------- #
# What a failed launch may still take back
# --------------------------------------------------------------------------- #
def test_an_undo_record_expires_with_the_warm_up(monkeypatch):
    _memory(monkeypatch)
    now = time.time()
    gate.note_start("old", auto=True, now=now - gate.WARMUP_SECONDS - 1)
    gate.note_start("recent", auto=True, now=now - gate.WARMUP_SECONDS + 30)
    assert set(gate._UNDO) == {gate._tmux_name("old"), gate._tmux_name("recent")}
    # The next start noted sweeps what is past the warm-up.
    gate.note_start("new", auto=True, now=now)
    assert set(gate._UNDO) == {gate._tmux_name("recent"), gate._tmux_name("new")}
    assert gate.unnote_start("old") is False
    assert gate._LAST_AUTO["at"] == now  # nothing was put back
    assert gate.unnote_start("recent") is True


def test_unnote_restores_the_admission_it_replaced(monkeypatch):
    _memory(monkeypatch)
    key = gate._tmux_name("a")
    now = time.time()
    # A relaunch of a session noted 10 s ago fails: the first note stands.
    gate.note_start("a", now=now - 10)
    gate.note_start("a", auto=True, now=now)
    assert gate._ADMITTED[key] == now and gate._LAST_AUTO["at"] == now
    assert gate.unnote_start("a") is True
    assert gate._ADMITTED[key] == now - 10 and gate._LAST_AUTO["at"] == 0.0
    assert gate.assess()["warming"] == 1
    # One it replaced that is itself past the warm-up is not put back.
    gate._reset_for_tests()
    gate.set_probe(lambda: [])
    gate.note_start("a", now=now - gate.WARMUP_SECONDS - 5)
    gate.note_start("a", now=now)
    assert gate.unnote_start("a") is True
    assert key not in gate._ADMITTED and gate.assess()["warming"] == 0


def test_unnote_of_a_person_start_leaves_the_stagger_alone(monkeypatch):
    _memory(monkeypatch)
    now = time.time()
    gate.note_start("auto", auto=True, now=now - 3)
    gate.note_start("mine", now=now)  # a person's: no stagger of its own
    assert gate.unnote_start("mine") is True
    assert gate._LAST_AUTO["at"] == now - 3
    assert gate._tmux_name("mine") not in gate._ADMITTED


# --------------------------------------------------------------------------- #
# Whose item is it: the owner tag
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("owner", [None, "pid 4", 7, [], {}, {"pid": None}])
def test_an_untagged_or_unreadable_owner_is_gone(owner):
    assert gate._owner_alive(owner) is False


@pytest.mark.parametrize("pid", ["x", 0, -1])
def test_an_owner_with_no_real_pid_is_gone(monkeypatch, pid):
    monkeypatch.setattr(gate, "_pid_alive", lambda p: True)
    assert gate._owner_alive({"pid": pid, "boot": gate._BOOT["id"]}) is False


def test_this_pid_is_alive_only_under_this_boot():
    me = gate._owner()
    assert me["pid"] == os.getpid() and me["boot"] == gate._BOOT["id"]
    assert gate._owner_alive(me) is True and gate._is_mine({"owner": me}) is True
    # The same pid before an execv restart: another boot id.
    before = dict(me, boot="the boot before")
    assert gate._owner_alive(before) is False
    assert gate._is_mine({"owner": before}) is False
    # A reset is a new boot: what was saved before reads as an earlier server's.
    gate._reset_for_tests()
    assert gate._owner_alive(me) is False and gate._owner()["boot"] != me["boot"]


@pytest.mark.parametrize(
    "exists, saved_start, start_now, alive",
    [
        (True, 4242, 4242, True),  # the same process
        (True, 4242, 9999, False),  # the pid was reused
        (True, 4242, None, True),  # its start can't be read now: believed
        (True, None, 9999, True),  # saved without one (no /proc): believed
        (True, True, 9999, True),  # a bool is not a start time
        (False, 4242, 4242, False),  # no such process
        (False, None, None, False),
    ],
)
def test_another_servers_owner(monkeypatch, exists, saved_start, start_now, alive):
    other = os.getpid() + 1
    asked = []
    monkeypatch.setattr(gate, "_pid_alive", lambda p: asked.append(p) or exists)
    monkeypatch.setattr(gate, "_proc_start", lambda p: start_now)
    owner = {"pid": other, "boot": "theirs", "start": saved_start}
    assert gate._owner_alive(owner) is alive
    assert asked == [other]
    # A pid saved as text (hand-edited, another writer) reads the same.
    assert gate._owner_alive(dict(owner, pid=str(other))) is alive


@pytest.mark.skipif(os.name == "nt", reason="os.kill(pid, 0) is the POSIX probe")
@pytest.mark.parametrize(
    "raised, alive",
    [
        (None, True),
        (PermissionError(), True),  # it exists, another user's
        (ProcessLookupError(), False),
        (OSError(), False),
        (OverflowError(), False),
    ],
)
def test_pid_alive_reads_the_signal_zero_probe(monkeypatch, raised, alive):
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        if raised is not None:
            raise raised

    monkeypatch.setattr(gate.os, "kill", kill)
    assert gate._pid_alive(4321) is alive
    assert sent == [(4321, 0)]  # never a real signal
    monkeypatch.setattr(gate, "_proc_start", lambda p: None)
    assert gate._owner_alive({"pid": 4321, "boot": "theirs"}) is alive


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="Linux /proc only")
def test_proc_start_tells_a_process_from_its_pid(monkeypatch):
    mine = gate._proc_start(os.getpid())
    assert isinstance(mine, int) and mine > 0
    assert gate._owner()["start"] == mine
    assert gate._proc_start(_GONE_PID) is None
    # The parent (pytest's launcher) is alive: its own start time matches,
    # another does not.
    parent = os.getppid()
    start = gate._proc_start(parent)
    assert gate._owner_alive({"pid": parent, "boot": "x", "start": start}) is True
    assert gate._owner_alive({"pid": parent, "boot": "x", "start": start + 1}) is False
    assert gate._owner_alive({"pid": _GONE_PID, "boot": "x", "start": start}) is False


# --------------------------------------------------------------------------- #
# Saves are ordered: the newer picture stays on disk
# --------------------------------------------------------------------------- #
def test_an_older_snapshot_never_overwrites_a_newer_one(monkeypatch, tmp_path):
    from backend.web.core import title_store

    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    gate.enqueue("a", lambda: None, kind="k", args={})
    writes = []
    real_persist = title_store.persist

    def persist(path_fn, doc, what):
        writes.append([i["title"] for i in doc["items"]])
        return real_persist(path_fn, doc, what)

    monkeypatch.setattr(title_store, "persist", persist)
    # The slow saver stops between taking its snapshot and writing it.
    slow = threading.Event()
    holding, go_on = threading.Event(), threading.Event()
    real_owner = gate._owner

    def owner():
        if threading.current_thread().name == "slow-saver" and not slow.is_set():
            slow.set()
            holding.set()
            assert go_on.wait(30)
        return real_owner()

    monkeypatch.setattr(gate, "_owner", owner)
    th = threading.Thread(target=gate._save, name="slow-saver")
    th.start()
    try:
        assert holding.wait(30)  # it holds the picture with only "a"
        gate.enqueue("b", lambda: None, kind="k", args={})  # a newer save
        assert writes == [["a", "b"]]
    finally:
        go_on.set()
        th.join(30)
    assert not th.is_alive()
    assert writes == [["a", "b"]]  # the older one was skipped, not written
    assert [t for t, _f in _saved_titles(path)] == ["a", "b"]
    # A later save still writes.
    gate.cancel("a")
    assert writes[-1] == ["b"] and [t for t, _f in _saved_titles(path)] == ["b"]


def test_save_generations_only_go_up(monkeypatch, tmp_path):
    _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    seen = []
    for t in ("a", "b", "c"):
        gate.enqueue(t, lambda: None, kind="k", args={})
        seen.append(gate._SAVE_STATE["written"])
    assert seen == sorted(seen) and len(set(seen)) == 3
    gate._reset_for_tests()
    assert gate._SAVE_STATE["written"] == 0


# --------------------------------------------------------------------------- #
# A file an older version wrote
# --------------------------------------------------------------------------- #
def test_restore_adopts_a_legacy_file(monkeypatch, tmp_path):
    """No owner, no ``why``, no ``inflight``: everything in it is adopted."""
    path = tmp_path / "q.json"
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_FILE", str(path))
    _memory(monkeypatch, level="low", available=3 * GiB)
    legacy = [
        {
            "title": "a",
            "kind": "k",
            "source": "agent",
            "parent": "orch",
            "since": 11.0,
            "reason": "memory is low",
            "creates": True,
            "args": {"n": 1},
        },
        {"title": "b", "kind": "k", "args": {"n": 2}},  # the bare minimum
        {"title": "no-args", "kind": "k"},  # never restorable: skipped
        {"title": "", "kind": "k", "args": {}},
        "junk",
    ]
    path.write_text(json.dumps({"items": legacy}))  # no "v" either
    built = []
    monkeypatch.setitem(
        gate._FACTORIES, "k", lambda rec: built.append(rec) or (lambda: None)
    )
    t0 = time.time()
    assert gate.restore() == 2 and gate.PERSIST is True
    a, b = gate.items()
    assert (a["title"], a["position"], a["why"]) == ("a", 1, "restart")
    assert a["reason"] == "back after a server restart; before it: memory is low"
    assert (a["source"], a["parent"], a["since"], a["creates"]) == (
        "agent",
        "orch",
        11.0,
        True,
    )
    assert (b["title"], b["position"], b["why"]) == ("b", 2, "restart")
    assert b["reason"] == "back after a server restart"
    assert b["since"] >= t0 and b["creates"] is False and b["parent"] == ""
    # The factory saw the saved record as it was.
    assert [r["args"] for r in built] == [{"n": 1}, {"n": 2}]
    assert "owner" not in built[0]
    # Rewritten in the new format, owned by this server.
    doc = json.loads(path.read_text())
    assert doc["v"] == 1 and [i["title"] for i in doc["items"]] == ["a", "b"]
    for i in doc["items"]:
        assert i["owner"]["pid"] == os.getpid() and i["owner"]["boot"]
        assert i["why"] == "restart" and i["inflight"] is False
    # They are automatic starts again: the gate judges them like any other.
    assert gate.drain_once() is None
    assert [i["why"] for i in gate.items()] == ["memory", "queue"]


# --------------------------------------------------------------------------- #
# A rename keeps the record: its cause, its owner, its place on disk
# --------------------------------------------------------------------------- #
def _saved_recs(path):
    return json.loads(path.read_text())["items"]


def test_a_renamed_queued_launch_keeps_its_why_and_owner(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch, level="low", available=3 * GiB)
    ran = []
    monkeypatch.setitem(
        gate._FACTORIES, "k", lambda rec: (lambda: ran.append(rec["title"]))
    )
    gate.gate_auto("old", lambda: ran.append("stale"), kind="k", args={"n": 1})
    gate.gate_auto("z", lambda: None, kind="k", args={"n": 2}, parent="old")
    before = gate.waiting("old")
    owner = _saved_recs(path)[0]["owner"]
    assert before["why"] == "memory" and owner["pid"] == os.getpid()
    gate.rename("old", "new")
    after = gate.waiting("new")
    assert gate.waiting("old") is None
    assert {k: v for k, v in after.items() if k != "title"} == {
        k: v for k, v in before.items() if k != "title"
    }
    assert gate.waiting("z")["parent"] == "new" and gate.waiting("z")["why"] == "queue"
    saved = _saved_recs(path)
    assert [(r["title"], r["why"], r["parent"]) for r in saved] == [
        ("new", "memory", ""),
        ("z", "queue", "new"),
    ]
    assert all(r["owner"] == owner for r in saved)
    # The next boot brings it back under the new name, in its place.
    gate._reset_for_tests()
    assert gate.restore() == 2
    assert [(i["title"], i["parent"]) for i in gate.items()] == [
        ("new", ""),
        ("z", "new"),
    ]
    _memory(monkeypatch)
    assert gate.drain_once() == "new" and ran == ["new"]


def test_a_renamed_starting_launch_is_saved_under_the_new_name(monkeypatch, tmp_path):
    path = _persist(monkeypatch, tmp_path)
    _memory(monkeypatch)
    release, entered = threading.Event(), threading.Event()
    rebuilt = []
    monkeypatch.setitem(
        gate._FACTORIES, "k", lambda rec: rebuilt.append(rec["title"]) or (lambda: None)
    )
    gate.enqueue("old", _blocked(release, entered), kind="k", args={"n": 1})
    try:
        assert gate.drain_once(wait=False) == "old"
        assert entered.wait(30)
        owner = _saved_recs(path)[0]["owner"]
        why = _saved_recs(path)[0]["why"]
        gate.rename("old", "new")
        # It is running: its launch is kept, not rebuilt.
        assert rebuilt == []
        assert gate.status()["starting"] == ["new"]
        with gate._LOCK:
            assert gate._in_flight("new")["tmux"] == gate._tmux_name("new")
        saved = _saved_recs(path)
        assert [(r["title"], r["inflight"]) for r in saved] == [("new", True)]
        assert saved[0]["owner"] == owner and saved[0]["why"] == why
        # Its warming entry moved with it, so a failure can still be undone.
        assert gate._tmux_name("new") in gate._ADMITTED
        assert gate._tmux_name("old") not in gate._ADMITTED
        assert gate._tmux_name("new") in gate._UNDO
        # The server dies mid-launch: the next boot finds it under the new name.
        gate._reset_for_tests()
        assert gate.restore() == 1 and rebuilt == ["new"]
        rec = gate.waiting("new")
        assert rec["why"] == "restart" and "was starting when" in rec["reason"]
    finally:
        release.set()
        _join_starts()
