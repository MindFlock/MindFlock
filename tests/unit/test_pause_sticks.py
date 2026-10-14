"""Pause sticks: nothing but Resume starts a paused session's agent again.

Pause stops the agent and (since 0.7.5) keeps the worktree. The web server's
relaunch-on-touch helper relaunches any agent whose worktree exists, and the
agent terminal websocket called it with no paused check — so a pane left open
on a session (or opened on a paused one) relaunched its agent within a second
while the row still read Paused. Before 0.7.5 the missing worktree made the
socket fail with 4409 instead, which masked it.

Pinned here: the helper refuses a paused session (never reaching the launch),
the terminal socket closes with 4423 without launching, a wake refuses, the
typing routes answer 409, the queue/mailbox/limit-watch lanes don't relaunch,
the shell tab still attaches, and Resume still relaunches.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.session.storage import Status
from backend.web import server
from backend.web.core import agent_sessions
from backend.web.core import prompt_queue
from backend.web.core import queue_driver
from backend.web.core import sleep
from backend.web.core.agent_sessions import PAUSED_REFUSED
from tests.unit.test_mailbox_routes import _mk_inst
from tests.unit.test_ownership import _FakeProc


class _Launching(Exception):
    """Raised where the real relaunch would begin resolving the provider."""


@pytest.fixture
def paused(tmp_path, monkeypatch):
    """One paused session ``p`` on an empty engine, its worktree on disk and
    no agent tmux alive. Every launch is a recorded no-op."""
    wt = tmp_path / "p"
    wt.mkdir()
    monkeypatch.setattr(server.ENGINE, "instances", {})
    inst = _mk_inst("p", str(wt), status=Status.Paused)
    server.ENGINE.instances["p"] = inst
    monkeypatch.setattr(server, "_live_session_name", lambda name: None)
    monkeypatch.setattr(server, "_budget_locked", lambda title: False)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda title: None)
    launched = []

    def ensure(i, title):
        launched.append(title)
        return "mindflock_" + title, None

    inst.launched = launched
    inst.ensure = ensure
    return inst


def _no_provider(monkeypatch):
    """Make reaching the real launch path observable: provider resolution is
    the first thing ``_ensure_agent_session`` does once it decides to launch."""

    def boom(program):
        raise _Launching(program)

    monkeypatch.setattr(agent_sessions.providers, "resolve", boom)


# --------------------------------------------------------------------------- #
# the helper every lazy relaunch goes through
# --------------------------------------------------------------------------- #
def test_the_relaunch_helper_refuses_a_paused_session(paused, monkeypatch):
    _no_provider(monkeypatch)
    name, err = agent_sessions._ensure_agent_session(paused, "p")
    assert err == PAUSED_REFUSED
    assert name == "mindflock_p"


def test_a_paused_session_whose_agent_is_somehow_alive_is_only_attached(
    paused, monkeypatch
):
    _no_provider(monkeypatch)
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    assert agent_sessions._ensure_agent_session(paused, "p") == ("mindflock_p", None)


def test_after_resume_the_helper_launches_again(paused, monkeypatch):
    """The guard is about the status, not the session: once Resume has set it
    running again, a dead agent is relaunched as before."""
    _no_provider(monkeypatch)
    paused.Status = Status.Running
    with pytest.raises(_Launching):
        agent_sessions._ensure_agent_session(paused, "p")


# --------------------------------------------------------------------------- #
# the agent terminal websocket
# --------------------------------------------------------------------------- #
def test_the_terminal_socket_closes_4423_and_launches_nothing(paused, monkeypatch):
    _no_provider(monkeypatch)  # the REAL helper runs: it must not get this far
    spawned = []
    monkeypatch.setattr(
        server.ptyprocess.PtyProcess, "spawn", lambda *a, **k: spawned.append(a)
    )
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/p/terminal") as ws:
        frame = json.loads(ws.receive_text())
        assert frame == {"type": "error", "message": PAUSED_REFUSED, "paused": True}
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 4423
    assert spawned == []


def test_a_stale_sleep_record_does_not_wake_a_paused_session(paused, monkeypatch):
    """Paused outranks asleep: a leftover record is dropped, not woken."""
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    sleep.mark("p", sleep.REASON_MANUAL)
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/p/terminal") as ws:
        assert json.loads(ws.receive_text())["paused"] is True
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 4423
    assert paused.launched == []
    assert not sleep.is_sleeping("p")


def test_ensure_awake_refuses_a_paused_session(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    sleep.mark("p", sleep.REASON_MANUAL)
    assert sleep.ensure_awake(paused, "p", "test") == (False, PAUSED_REFUSED)
    assert paused.launched == []
    # Not asleep (the usual case): nothing to wake, and still nothing launched.
    assert sleep.ensure_awake(paused, "p", "test") == (False, None)
    assert paused.launched == []


def test_the_terminal_attaches_once_resumed(paused, monkeypatch):
    """Resume flips the status first-hand (the engine relaunches; this route
    never does). The next socket then attaches as usual."""
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    monkeypatch.setattr(server, "_ATTACH_PROBE_GRACE_S", 0.0)
    procs = []

    def spawn(*a, **kw):
        procs.append(_FakeProc())
        return procs[-1]

    monkeypatch.setattr(server.ptyprocess.PtyProcess, "spawn", spawn)

    def resume():
        paused.Status = Status.Running

    monkeypatch.setattr(paused, "Resume", resume)
    c = TestClient(server.app)
    r = c.post("/api/instances/p/resume")
    assert r.status_code == 200, r.text
    with c.websocket_connect("/api/instances/p/terminal") as ws:
        ws.receive_text()  # the driver frame: attached
    assert paused.launched == ["p"]
    assert len(procs) == 1


# --------------------------------------------------------------------------- #
# the shell tab keeps working
# --------------------------------------------------------------------------- #
def test_the_shell_socket_still_attaches_while_paused(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    monkeypatch.setattr(server, "_ATTACH_PROBE_GRACE_S", 0.0)
    shells = []

    def ensure_shell(title, wt):
        shells.append((title, wt))
        return "mindflock_p_sh", None

    monkeypatch.setattr(server, "_ensure_shell_session", ensure_shell)
    procs = []

    def spawn(*a, **kw):
        procs.append((a, _FakeProc()))
        return procs[-1][1]

    monkeypatch.setattr(server.ptyprocess.PtyProcess, "spawn", spawn)
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/p/shell") as ws:
        ws.receive_text()  # the driver frame: attached
    assert shells == [("p", paused.GetWorktreePath())]
    assert len(procs) == 1 and "mindflock_p_sh" in procs[0][0][0]
    assert paused.launched == []  # the agent was never touched


# --------------------------------------------------------------------------- #
# typing paths: 409, never a relaunch
# --------------------------------------------------------------------------- #
def test_send_is_409_paused_and_relaunches_nothing(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    sent = []
    monkeypatch.setattr(server, "_send_to_agent", lambda *a, **k: sent.append(a))
    c = TestClient(server.app)
    for body in ({"text": "hi"}, {"text": "hi", "dialog_safe": True}):
        r = c.post("/api/instances/p/send", json=body)
        assert r.status_code == 409
        assert r.json() == {"error": PAUSED_REFUSED, "paused": True}
    assert paused.launched == [] and sent == []


def test_a_composed_message_is_refused_too(paused, monkeypatch):
    """``boot=False`` (the Code Map's buttons): same refusal, said plainly."""
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    assert server._deliver_to_agent(paused, "p", "hi") == (False, PAUSED_REFUSED)
    assert paused.launched == []


def test_send_now_is_409_and_the_item_stays_queued(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    item = prompt_queue.enqueue("p", "later")
    c = TestClient(server.app)
    r = c.post("/api/instances/p/queue/send_now", json={"id": item["items"][0]["id"]})
    assert r.status_code == 409
    assert r.json() == {"error": PAUSED_REFUSED, "paused": True}
    assert [i["text"] for i in prompt_queue.get_state("p")["items"]] == ["later"]
    assert paused.launched == []


def test_revive_is_409(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    r = TestClient(server.app).post("/api/instances/p/revive")
    assert r.status_code == 409
    assert paused.launched == []


def test_wake_route_never_relaunches_a_paused_session(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    sleep.mark("p", sleep.REASON_MANUAL)
    r = TestClient(server.app).post("/api/instances/p/wake")
    assert r.status_code == 409
    assert r.json()["error"] == PAUSED_REFUSED
    assert paused.launched == []


# --------------------------------------------------------------------------- #
# the background lanes
# --------------------------------------------------------------------------- #
def test_the_queue_drain_and_limit_watch_hold_a_paused_session(paused, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    monkeypatch.setattr(
        server, "_agent_activity", lambda inst, title: "offline"
    )  # what a stopped agent reads: the drain's reboot branch
    prompt_queue.enqueue("p", "go on")
    queue_driver._drain_one_queue("p")
    queue_driver._watch_one_limited("p")
    assert paused.launched == []


def test_the_mailbox_lanes_hold_a_paused_session(paused, monkeypatch):
    from backend.web.core import mailbox

    monkeypatch.setattr(server, "_ensure_agent_session", paused.ensure)
    monkeypatch.setattr(server, "_agent_activity", lambda inst, title: "offline")
    msg = {"id": "m1", "from": "x", "text": "hello"}
    result, detail = server._mail_deliver_now(paused, "p", msg)
    assert result == "pending" and "paused" in detail
    monkeypatch.setattr(mailbox, "next_pending", lambda title: msg)
    queue_driver._drain_one_mailbox("p")
    assert paused.launched == []


def test_the_revive_sweep_skips_a_paused_session(paused):
    from backend.web.core import revive

    assert revive.skip_reason(server, paused, "p", 0.0) == revive.SKIP_PAUSED
    assert os.path.isdir(paused.GetWorktreePath())  # skipped despite the folder
