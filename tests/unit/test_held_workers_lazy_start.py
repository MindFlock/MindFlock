"""A held worker starts its agent only when its turn comes.

An orchestrator's serial chain (``after=[...]``, overlapping fences, a cap)
used to start every worker's agent CLI at once, each then sitting at an empty
prompt (~230 MB) until its turn. Now a held worker's session is made at once —
worktree, branch, fence, queued task — but its agent is launched on release,
through the launch gate as an automatic start, and only then is its task typed
in. These pin: no agent at create, the gated launch before the delivery, the
row still reading waiting, revive and the crash detectors leaving a worker
that never ran alone, a person's early Start, and an unordered worker still
starting at once.

The driver runs against ``test_worker_order``'s fake server (the REAL prompt
queue and order store, pointed at tmp by ``conftest``); the engine's tmux
session is a stand-in whose ``start`` records what tmux would have run.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend import session
from backend.config import settings as _settings
from backend.providers import resolve as _resolve
from backend.session.storage import Loading
from backend.web import server
from backend.web.core import held, launch_gate, prompt_queue, revive, terminal
from backend.web.core import session_create
from backend.web.core import worker_order_driver as drv
from tests.unit.test_webui import _stub_create
from tests.unit.test_worker_order import _Inst, _repo
from tests.unit.test_worker_order import fake  # noqa: F401 — the fixture


class _Tmux:
    """The engine's tmux session object, minus tmux: ``start`` is the
    ``tmux new-session`` that launches the agent CLI."""

    def __init__(self, title, launched):
        self.sanitized_name = drv._tmux_of(title)
        self.launched = launched

    def start(self, work_dir):
        self.launched.append((self.sanitized_name, work_dir))
        return None


class _LazyInst(_Inst):
    """``_Inst`` plus the engine's Start: the worktree is there, and Start
    hands the tmux object its folder to launch the agent in."""

    def __init__(self, title, wt, launched, **kw):
        super().__init__(title, wt, **kw)
        self.Program = "claude"
        self.Status = Loading
        self._tmux_session = _Tmux(title, launched)
        self._started = False

    def Start(self, first_time_setup):  # noqa: N802
        err = self._tmux_session.start(self._wt)
        if err is None:
            self._started = True
            self.Status = session.Running
        return err

    def Started(self):  # noqa: N802
        return self._started


@pytest.fixture
def lazy(fake, monkeypatch, tmp_path):  # noqa: F811
    """``fake`` with live agents read from what was launched, the relaunch
    path, the launch gate's automatic start, and exit markers in tmp."""
    launched: list = []
    ensured: list = []
    gate: dict = {"calls": [], "queue": None, "launch": None}
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", tmp_path / "markers")

    def live(name):
        names = {n for n, _wt in launched} | {n for n, _t in ensured}
        return name if name in names else None

    def ensure(inst, title):
        name = drv._tmux_of(title)
        ensured.append((name, title))
        return name, None

    def gate_auto(title, launch, *, kind, source="", parent="", **_kw):
        gate["calls"].append(
            {
                "title": title,
                "kind": kind,
                "parent": parent,
                "launched_before": list(launched),
                "queue": dict(prompt_queue.get_state(title)),
            }
        )
        if gate["queue"] is not None:
            gate["launch"] = launch
            return dict(gate["queue"], title=title)
        return None

    fake._live_session_name = live
    fake._ensure_agent_session = ensure
    monkeypatch.setattr(drv._launch_gate, "gate_auto", gate_auto)
    fake.launched, fake.ensured, fake.gate = launched, ensured, gate
    return fake


def _spawn(srv, title, parent, wt, payload):
    """What the create route does for a worker: register it in the order,
    defer its agent when it is held, Start it, mark a deferred one never-ran."""
    inst = _LazyInst(title, wt, srv.launched, parent=parent)
    srv.ENGINE.instances[title] = inst
    order, err = drv.on_create(parent, title, inst, payload, None, "TASK " + title)
    assert err is None
    deferred = bool(order and order.get("held")) and drv.defer_agent(inst)
    inst.Start(True)
    if deferred:
        drv.mark_never_ran(title, inst)
    return inst


def _chain(srv):
    repo = _repo(str(srv.tmp / "r"))
    srv.ENGINE.instances["orch"] = _Inst("orch", repo)
    w1 = _spawn(srv, "w1", "orch", repo, {"fence": {"only": ["a/**"]}})
    w2 = _spawn(srv, "w2", "orch", repo, {"after": ["w1"]})
    return repo, w1, w2


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def test_a_held_workers_agent_is_not_launched_at_create(lazy):
    repo, w1, w2 = _chain(lazy)
    # Both are created and started (worktree, branch, saved)…
    assert w1.Started() and w2.Started()
    # …w1 is ordered by its fence only, so it too waits a tick for it; w2
    # waits for w1. Neither has an agent: no tmux new-session.
    assert lazy.launched == []
    assert drv.agent_deferred(w2)
    assert prompt_queue.get_state("w2")["held"] is True
    assert prompt_queue.list_queue("w2")[0]["text"] == "TASK w2"


def test_the_create_route_defers_the_agent_and_says_so(monkeypatch, tmp_path):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda: None)
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", tmp_path / "markers")
    monkeypatch.setattr(_settings, "update_settings", lambda **kw: None)
    _stub_create(monkeypatch, git_enabled=True)
    launched: list = []
    made: dict = {}

    def new_instance(opts):
        inst = _LazyInst(opts.title, str(tmp_path), launched)
        inst.Path, inst.Prompt, inst.ExtraEnv = "/tmp/x", opts.prompt or "", {}
        inst.Parent = getattr(opts, "parent", "") or ""
        inst.SetStatus = lambda s: setattr(inst, "Status", s)
        made[opts.title] = inst
        return inst

    monkeypatch.setattr(session, "NewInstance", new_instance)
    tasks: list = []
    monkeypatch.setattr(server, "_register_task", tasks.append)
    noted: list = []
    monkeypatch.setattr(
        session_create._launch_gate, "note_start", lambda t, **k: noted.append(t)
    )
    instances["orch"] = _Inst("orch", str(tmp_path))
    instances["w1"] = _Inst("w1", str(tmp_path), parent="orch")

    def create(**payload):
        base = {"program": "claude", "repo_path": "/tmp/x", "parent": "orch"}
        status, body = asyncio.run(
            session_create.create_result(dict(base, **payload), auto="")
        )
        assert status == 202, body
        for coro in tasks:
            asyncio.run(coro)
        tasks.clear()
        return body

    held_body = create(title="w2", prompt="do the api", after=["w1"])
    assert held_body["prompt_delivery"] == "held"
    assert held_body["order"]["agent_deferred"] is True
    assert made["w2"].Started() and launched == []  # made, agent not launched
    assert "w2" not in noted  # not counted as a warming agent
    name = drv._tmux_of("w2")
    assert terminal._read_exit_marker(name) == 0

    # An unordered worker still starts its agent at once.
    free = create(title="w3", prompt="do the docs")
    assert "order" not in free and free["prompt_delivery"] != "held"
    assert launched == [(drv._tmux_of("w3"), str(tmp_path))]
    assert "w3" in noted
    assert terminal._read_exit_marker(drv._tmux_of("w3")) is None


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
def test_release_launches_through_the_gate_then_delivers(lazy):
    repo, w1, w2 = _chain(lazy)
    drv.tick()
    # w1's fence landed: its turn came — its agent launched through the gate
    # BEFORE its queue went on; w2 still has no agent.
    (call,) = lazy.gate["calls"]
    assert call["title"] == "w1" and call["kind"] == drv.TURN_KIND
    assert call["parent"] == "orch"
    assert call["queue"]["enabled"] is False  # the task still parked
    assert lazy.launched == [(drv._tmux_of("w1"), repo)]
    assert prompt_queue.get_state("w1")["enabled"] is True
    assert not drv.agent_deferred(w1)
    assert drv.agent_deferred(w2)
    drv.tick()
    assert len(lazy.gate["calls"]) == 1  # w2 is held: no launch, no gate
    lazy.reports["w1"] = {"status": "done", "ts": 9e9}
    drv.tick()
    call = lazy.gate["calls"][-1]
    assert call["title"] == "w2" and call["kind"] == drv.TURN_KIND
    assert call["queue"]["held"] is True  # launched first…
    assert lazy.launched[-1] == (drv._tmux_of("w2"), repo)
    st = prompt_queue.get_state("w2")  # …then the task is handed over
    assert st["enabled"] is True and "held" not in st
    text = prompt_queue.list_queue("w2")[0]["text"]
    assert "held this task until w1 finished" in text and text.endswith("TASK w2")
    # The queue drain gives the fresh CLI its boot grace before typing.
    from backend.web.core import queue_driver

    assert time.time() - queue_driver._QUEUE_STATE["w2"]["rebooted_at"] < 60


def test_a_release_waits_in_the_gate_when_memory_is_short(lazy):
    repo, w1, w2 = _chain(lazy)
    lazy.gate["queue"] = {"position": 1, "reason": "memory is low"}
    drv.tick()
    # Queued, not launched; its queue is on (the drain sees it waiting for
    # memory and leaves the launch to the gate).
    assert lazy.launched == []
    assert prompt_queue.get_state("w1")["enabled"] is True
    assert "waits for memory" in drv.row_order("w1", "orch")["detail"]
    # The gate's drainer runs the launch later: the deferred first launch.
    assert lazy.gate["launch"]() is None
    assert lazy.launched == [(drv._tmux_of("w1"), repo)]


def test_a_queued_turn_comes_back_after_a_restart(lazy):
    repo, w1, w2 = _chain(lazy)
    assert drv._restore_turn({"title": "gone"}) is None
    launch = drv._restore_turn({"title": "w2"})
    # After a restart the deferred launch is forgotten: the relaunch path
    # starts it (fresh: the never-ran marker reads as a clean quit).
    w2._tmux_session.__dict__.pop(drv._DEFERRED_DIR, None)
    assert launch() is None
    assert lazy.ensured == [(drv._tmux_of("w2"), "w2")]
    assert launch_gate._FACTORIES[drv.TURN_KIND] is drv._restore_turn


def test_a_turn_that_comes_during_the_start_lets_the_start_launch_it(lazy):
    repo = _repo(str(lazy.tmp / "r"))
    lazy.ENGINE.instances["orch"] = _Inst("orch", repo)
    inst = _LazyInst("w9", repo, lazy.launched, parent="orch")
    lazy.ENGINE.instances["w9"] = inst
    assert drv.defer_agent(inst)
    assert drv.start_agent("w9", inst) is None  # before Start reached tmux
    inst.Start(True)
    assert lazy.launched == [(drv._tmux_of("w9"), repo)]
    drv.mark_never_ran("w9", inst)  # not deferred any more: no marker
    assert terminal._read_exit_marker(drv._tmux_of("w9")) is None


# --------------------------------------------------------------------------- #
# The row, revive and the crash detectors
# --------------------------------------------------------------------------- #
def test_the_row_reads_waiting(lazy):
    _chain(lazy)
    drv.tick()
    row = drv.row_order("w2", "orch")
    assert row["state"] == "held"
    assert row["word"] == "waiting" and row["detail"] == "after w1"


def test_revive_and_crash_detection_leave_a_worker_that_never_ran_alone(
    lazy, monkeypatch
):
    repo, w1, w2 = _chain(lazy)
    w2.PeerShare = ""
    srv = SimpleNamespace(
        ENGINE=SimpleNamespace(
            lock=__import__("threading").Lock(), instances={"w2": w2}
        ),
        _read_exit_marker=terminal._read_exit_marker,
        _is_natural_exit=terminal._is_natural_exit,
        _live_session_name=lambda name: None,
    )
    monkeypatch.setattr(revive, "enabled", lambda: False)
    now = time.time()
    # The sweep and the boot hold skip it: it never ran, so it never died.
    assert revive.skip_reason(srv, w2, "w2", now) == revive.SKIP_QUIT
    out = revive.hold_on_boot(srv, now=now)
    assert out["held"] == [] and not held.is_held("w2")
    # The tick's reading never saw it live: an "offline" is no death.
    revive.observe(srv, "w2", w2, "offline", now=now)
    assert not held.is_held("w2")
    assert revive.auto_state("w2") is None
    # Whoever starts it first starts it fresh, not --continue.
    code = terminal._read_exit_marker(drv._tmux_of("w2"))
    assert _resolve("claude").is_natural_exit(code)


def test_a_person_can_start_it_early_and_its_task_still_waits(lazy):
    repo, w1, w2 = _chain(lazy)
    drv.tick()
    # A person's Start (the row's Start: _ensure_agent_session) runs its agent.
    lazy.ensured.append((drv._tmux_of("w2"), "w2"))
    drv.tick()
    assert prompt_queue.get_state("w2")["held"] is True  # task still parked
    assert drv.row_order("w2", "orch")["state"] == "held"
    lazy.reports["w1"] = {"status": "done", "ts": 9e9}
    drv.tick()
    # Its turn: no second agent, no gate — the task is handed over.
    assert [c["title"] for c in lazy.gate["calls"]] == ["w1"]
    assert (drv._tmux_of("w2"), repo) not in lazy.launched
    assert not drv.agent_deferred(w2)
    assert prompt_queue.get_state("w2")["enabled"] is True


def test_an_unordered_worker_starts_at_once(lazy):
    repo = _repo(str(lazy.tmp / "r"))
    lazy.ENGINE.instances["orch"] = _Inst("orch", repo)
    inst = _spawn(lazy, "w1", "orch", repo, {})
    assert lazy.launched == [(drv._tmux_of("w1"), repo)]
    assert not drv.agent_deferred(inst)
    assert terminal._read_exit_marker(drv._tmux_of("w1")) is None
