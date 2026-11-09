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
    # VM: 10 - 2 reserve = 8 GB; Windows: 6 - 2.56 (8% of 32) = 3.44 GB.
    snap = {"level": "ok", "available": 10 * GiB, "total": 20 * GiB}
    snap["host"] = _host(6 * GiB)
    b = gate.budget(snap, _COST, reserve=2 * GiB)
    assert b["room"] == 3 and b["free_mb"] == 3523  # 6144 - 2621.4
    assert b["available_mb"] == 10240  # the guest's own number still reported
    assert b["why"] == (
        "room for 3 more sessions: Windows has 6.0 GB available over its own "
        "reserve of 2.6 GB, each ~1.0 GB (measured over 3 sessions)"
    )


def test_budget_keeps_the_guest_sum_when_the_guest_binds():
    snap = {"level": "ok", "available": 4 * GiB, "total": 20 * GiB}
    snap["host"] = _host(16 * GiB)
    b = gate.budget(snap, _COST, reserve=2 * GiB)
    assert b["room"] == 2 and b["free_mb"] == 2048
    assert "Windows" not in b["why"]
    assert "4.0 GB available − 2.0 GB reserve" in b["why"]


def test_windows_with_no_headroom_leaves_no_room_while_still_ok():
    # Windows at 3 GB is ok (it came down from above), the low line 2.56 GB;
    # an agent warming with 1 GB still to come eats past it.
    snap = {"level": "ok", "available": 10 * GiB, "total": 20 * GiB}
    snap["host"] = _host(3 * GiB)
    b = gate.budget(snap, _COST, [None], reserve=2 * GiB)
    assert b["room"] == 0 and b["to_come_mb"] == 1024
    host_free = 3 * GiB - 0.08 * 32 * GiB - 1 * GiB
    assert b["free_mb"] == round(host_free / 1024**2)
    assert b["short_mb"] == 1024  # all of one session: nothing is free
    assert b["why"].startswith("one more session needs ~1.0 GB; only 0.0 GB")
    assert "Windows has 3.0 GB available over its own reserve" in b["why"]


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
    import json

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
