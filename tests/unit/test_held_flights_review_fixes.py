"""Regressions from the 0.14.7 review of held workers and the flights store.

1. A PAUSED held worker's turn never launches its agent (nor does a crash
   hold on one that ran): it stays held and goes once it can start — the
   same before and after a restart. A shared-folder one launches only
   through the relaunch path (sandboxed or refused).
2. Deleting a held worker in a chain (X → A → B) releases B after X — it
   never waits for ever on a session that is gone.
3. Opening a waiting worker's pane does not start its agent; a person's
   Start still does.
4. A full launch queue keeps the worker held (never "running" with no
   agent, which wait_for_session reads as finished).
5. Two processes rewriting flights.json lose no update (the lock file).

The order tests run against ``test_worker_order``'s fake server and
``test_held_workers_lazy_start``'s lazy-start stand-ins.
"""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from backend import session
from backend.web import server
from backend.web.core import agent_sessions, held, launch_gate, prompt_queue
from backend.web.core import terminal
from backend.web.core import worker_order as wo
from backend.web.core import worker_order_driver as drv
from tests.unit.test_held_workers_lazy_start import _chain, lazy  # noqa: F401
from tests.unit.test_worker_order import _Inst, _repo, _spawn
from tests.unit.test_worker_order import fake  # noqa: F401 — the fixture


def _done(srv, title):
    srv.reports[title] = {"status": "done", "ts": 9e9}


# --------------------------------------------------------------------------- #
# 1. A paused (or crash-held, or shared-folder) worker's turn
# --------------------------------------------------------------------------- #
def test_a_paused_held_worker_is_not_launched_when_its_turn_comes(lazy):  # noqa: F811
    repo, w1, w2 = _chain(lazy)
    drv.tick()  # w1's turn
    w2.Status = session.Paused
    _done(lazy, "w1")
    drv.tick()
    drv.tick()
    # Its turn came, but it is paused: no agent, no gate, still held.
    assert (drv._tmux_of("w2"), repo) not in lazy.launched
    assert [c["title"] for c in lazy.gate["calls"]] == ["w1"]
    row = drv.row_order("w2", "orch")
    assert row["state"] == "held" and row["word"] == "waiting"
    assert "paused" in row["detail"]
    st = prompt_queue.get_state("w2")
    assert st["held"] is True and st["enabled"] is False
    assert drv.agent_deferred(w2)
    # A direct start (the gate's drainer, a restored turn) is refused too —
    # as the relaunch path refuses it after a restart.
    assert drv.start_agent("w2", w2) == agent_sessions.PAUSED_REFUSED
    assert (drv._tmux_of("w2"), repo) not in lazy.launched
    # Resumed: the next pass gives it its turn.
    w2.Status = session.Running
    drv.tick()
    assert lazy.launched[-1] == (drv._tmux_of("w2"), repo)
    assert drv.row_order("w2", "orch")["state"] == "running"
    assert prompt_queue.get_state("w2")["enabled"] is True


def test_a_paused_worker_takes_no_slot(lazy):  # noqa: F811
    repo = _repo(str(lazy.tmp / "r"))
    lazy.ENGINE.instances["orch"] = _Inst("orch", repo)
    from tests.unit.test_held_workers_lazy_start import _spawn as _lazy_spawn

    status, _body = drv.set_order("orch", {"max_parallel": 1})
    assert status == 200
    a = _lazy_spawn(lazy, "a", "orch", repo, {})
    _lazy_spawn(lazy, "b", "orch", repo, {})
    a.Status = session.Paused
    drv.tick()
    # a can't start: b, behind it in the queue for the one slot, goes.
    assert drv.row_order("a", "orch")["state"] == "held"
    assert drv.row_order("b", "orch")["state"] == "running"


def test_a_crash_hold_is_dropped_only_for_an_agent_that_never_ran(lazy):  # noqa: F811
    repo, w1, w2 = _chain(lazy)
    name = drv._tmux_of("w2")
    # Never ran (the marker mark_never_ran wrote; launch still deferred): a
    # boot hold on it is no crash's — its turn drops it and launches.
    held.hold("w2", held.REASON_RESTART)
    assert drv.start_agent("w2", w2) is None
    assert not held.is_held("w2")
    assert lazy.launched[-1] == (name, repo)
    # w1 ran, crashed and is held offline: only a person's Start brings it
    # back, never its turn.
    w1._tmux_session.__dict__.pop(drv._DEFERRED_DIR, None)
    terminal._clear_exit_marker(drv._tmux_of("w1"))
    held.hold("w1", held.REASON_CRASH)
    assert drv.start_agent("w1", w1) == agent_sessions.HELD_REFUSED
    assert held.is_held("w1")
    assert lazy.ensured == []
    held.release("w1")


def test_a_shared_folder_worker_launches_only_through_the_relaunch_path(
    lazy,
):  # noqa: F811
    repo, w1, w2 = _chain(lazy)
    w2.PeerShare = "share-1"
    assert drv.start_agent("w2", w2) is None
    # Not the deferred plain launch: the relaunch path, which sandboxes it.
    assert (drv._tmux_of("w2"), repo) not in lazy.launched
    assert lazy.ensured == [(drv._tmux_of("w2"), "w2")]
    assert not drv.agent_deferred(w2)


# --------------------------------------------------------------------------- #
# 2. A held worker deleted from a chain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("serial", [False, True])
def test_deleting_a_held_worker_releases_the_one_after_it(fake, serial):  # noqa: F811
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    if serial:
        assert drv.set_order("orch", {"mode": "serial"})[0] == 200
        _spawn(fake, "X", "orch", repo, {})
        _spawn(fake, "A", "orch", repo, {})
        _spawn(fake, "B", "orch", repo, {})
    else:
        _spawn(fake, "X", "orch", repo, {"fence": {"only": ["a/**"]}})
        _spawn(fake, "A", "orch", repo, {"after": ["X"]})
        _spawn(fake, "B", "orch", repo, {"after": ["A"]})
    drv.tick()
    rec = wo.load()["parents"]["orch"]
    assert rec["workers"]["X"]["state"] == "running"
    assert rec["workers"]["B"]["after"] == ["A"]
    # A is deleted before it ever started.
    del fake.ENGINE.instances["A"]
    fake.tombs["A"] = time.time()
    drv.tick()
    rec = wo.load()["parents"]["orch"]
    assert "A" not in rec["workers"]
    # B now runs after what A ran after — not before X is done…
    assert rec["workers"]["B"]["after"] == ["X"]
    assert rec["workers"]["B"]["state"] == "held"
    assert drv.row_order("B", "orch")["detail"] == "after X"
    # …and is released once it is (it used to wait for "A" for ever).
    _done(fake, "X")
    drv.tick()
    assert wo.load()["parents"]["orch"]["workers"]["B"]["state"] == "running"
    assert prompt_queue.get_state("B")["enabled"] is True


def test_forget_worker_rewires_successors_and_steps():
    rec = wo._blank_parent(1.0)
    rec["planned"] = {"A": ["X"], "B": ["A"], "C": ["A", "Y"]}
    wo.add_worker(rec, "X", created=1.0)
    wo.add_worker(rec, "Y", created=1.0)
    wo.add_worker(rec, "A", created=1.0, after=["Y"])  # + X from its step
    wo.add_worker(rec, "B", created=1.0, why={"A": "asked"}, after=["A"])
    wo.add_worker(rec, "C", created=1.0)  # A, Y from its step
    gone = wo.forget_worker(rec, "A")
    assert gone is not None and gone["after"] == ["Y", "X"]
    assert "A" not in rec["workers"]
    assert rec["workers"]["B"]["after"] == ["Y", "X"]
    assert rec["workers"]["B"]["why"] == {"Y": "asked", "X": "step"}
    # Y was already C's: no duplicate.
    assert rec["workers"]["C"]["after"] == ["X", "Y"]
    assert rec["planned"] == {"B": ["X"], "C": ["X", "Y"]}
    assert not wo.would_cycle({t: w["after"] for t, w in rec["workers"].items()})
    assert wo.forget_worker(rec, "nobody") is None


# --------------------------------------------------------------------------- #
# 3. Opening a waiting worker's pane
# --------------------------------------------------------------------------- #
def test_opening_a_waiting_workers_pane_does_not_start_its_agent(
    lazy, monkeypatch, tmp_path  # noqa: F811
):
    repo, w1, w2 = _chain(lazy)
    monkeypatch.setattr(server, "_live_session_name", lambda name: None)
    monkeypatch.setattr(launch_gate, "_ADMITTED", {})
    # A folder that is not there: past the gates, the relaunch path stops at
    # "workspace no longer exists" before anything could be launched.
    w2._wt = str(tmp_path / "nowhere")
    name, err = server.ensure_terminal_session(w2, "w2")
    assert err == "waiting its turn — press Start to start it early"
    assert "press Start" in err
    assert drv.agent_deferred(w2)  # still waiting, launch still to come
    # A person's Start (the /revive route notes the start first) still
    # starts it early.
    launch_gate.note_start("w2")
    _name, err = agent_sessions._ensure_agent_session(w2, "w2")
    assert err == "workspace no longer exists"
    # An unordered session, and a worker whose turn came, attach as before.
    monkeypatch.setattr(launch_gate, "_ADMITTED", {})
    drv.tick()  # w1's turn: running
    assert drv.row_order("w1", "orch")["state"] == "running"
    w1._wt = str(tmp_path / "nowhere")
    assert agent_sessions._ensure_agent_session(w1, "w1")[1] == (
        "workspace no longer exists"
    )


# --------------------------------------------------------------------------- #
# 4. The launch queue is full
# --------------------------------------------------------------------------- #
def test_a_full_launch_queue_keeps_the_worker_held(lazy, monkeypatch):  # noqa: F811
    repo, w1, w2 = _chain(lazy)
    tried: list = []

    def full(title, launch, **kw):
        tried.append(title)
        raise launch_gate.QueueFull("32 launches are already waiting")

    real = launch_gate.gate_auto
    monkeypatch.setattr(launch_gate, "gate_auto", full)
    drv.tick()
    assert tried == ["w1"]
    # Not released: no agent, its task still parked, and its row still
    # reads waiting (never running/offline with nothing coming).
    assert lazy.launched == []
    row = drv.row_order("w1", "orch")
    assert row["state"] == "held" and row["word"] != "running"
    st = prompt_queue.get_state("w1")
    assert st["held"] is True and st["enabled"] is False
    assert wo.load()["parents"]["orch"]["workers"]["w1"]["released_at"] is None
    # Seen full before the gate is asked: no fast-forward, no gate call.
    monkeypatch.setattr(launch_gate, "queue_max", lambda: 1)
    monkeypatch.setattr(launch_gate, "items", lambda: [{"title": "other"}])
    drv.tick()
    assert tried == ["w1"]
    # Room again: the next pass releases it.
    monkeypatch.setattr(launch_gate, "items", lambda: [])
    monkeypatch.setattr(launch_gate, "gate_auto", real)
    drv.tick()
    assert lazy.launched == [(drv._tmux_of("w1"), repo)]
    assert drv.row_order("w1", "orch")["state"] == "running"
    assert prompt_queue.get_state("w1")["enabled"] is True


def test_start_now_survives_a_full_queue(lazy, monkeypatch):  # noqa: F811
    repo, w1, w2 = _chain(lazy)
    drv.tick()
    monkeypatch.setattr(launch_gate, "queue_max", lambda: 1)
    monkeypatch.setattr(launch_gate, "items", lambda: [{"title": "other"}])
    assert drv.set_order("orch", {"start_now": ["w2"]})[0] == 200
    w = wo.load()["parents"]["orch"]["workers"]["w2"]
    assert w["state"] == "held" and w.get("start_now") is True
    monkeypatch.setattr(launch_gate, "items", lambda: [])
    drv.tick()
    assert wo.load()["parents"]["orch"]["workers"]["w2"]["state"] == "running"


# --------------------------------------------------------------------------- #
# 5. flights.json across processes
# --------------------------------------------------------------------------- #
_ROUNDS = 40


def _bump_many(flight_id: str, go) -> None:
    """A child process: bump ``flight_id``'s counter ``_ROUNDS`` times, each
    read-modify-write slow enough to overlap the other process's."""
    from backend.web.core import flights as F

    go.wait(10)

    def bump(f):
        time.sleep(0.003)  # between the read and the write
        f["owner_at"] = float(f["owner_at"]) + 1.0

    for _ in range(_ROUNDS):
        F.mutate(flight_id, bump)


def test_two_processes_lose_no_flight_update(tmp_path, monkeypatch):
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("needs fork")
    from backend.web.core import flights as F

    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    plan = F.save_plan({"name": "P", "steps": [{"kind": "notify", "config": {}}]})
    fa = F.take_off(plan, session="pipeline-ticket")
    fb = F.take_off(plan, session="server-session")
    for f in (fa, fb):
        F.mutate(f["id"], lambda x: x.update(owner_at=0.0))
    go = ctx.Event()
    procs = [ctx.Process(target=_bump_many, args=(f["id"], go)) for f in (fa, fb)]
    for p in procs:
        p.start()
    go.set()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    F._CACHE["key"] = F._CACHE["digest"] = None
    got = {f["id"]: F.get(f["id"])["owner_at"] for f in (fa, fb)}
    assert got == {fa["id"]: float(_ROUNDS), fb["id"]: float(_ROUNDS)}
    assert os.path.exists(F.flights_path() + ".lock")
