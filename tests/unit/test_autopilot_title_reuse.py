"""A removed session takes its autopilot run (its lane) with it.

Titles are reused — ``untitled-2`` comes straight back, a ticket's slug is its
title every time — and the autopilot store is keyed by title. ``prune`` only
drops a record once it is older than the intake grace (30 minutes), so before
this a session deleted soon after being armed handed its target, attempt
counters and halt reason to the next session to take the name: a recreated
title would commit and open a PR nobody asked it to.

Every test owns ``ENGINE.instances`` (the live engine is the developer's real
state.json) and launches nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Status
from backend.web import server
from backend.web.core import autopilot as ap

client = TestClient(server.app)


class _Inst:
    def __init__(self, title):
        self.Title = title
        self.Parent = ""
        self.Spawned = False
        self.Program = "claude"
        self.Branch = "feat/" + title
        self.InPlace = False
        self.Path = ""
        self.Status = Status.Running
        self.killed = False

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""

    def Kill(self):  # noqa: N802
        self.killed = True


@pytest.fixture
def reg(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server, "_kill_shell_session", lambda t: None)
    monkeypatch.setattr(server, "_kill_agent_session", lambda t: None)
    monkeypatch.setattr(server, "_close_cursor_window", lambda wt: None)
    monkeypatch.setattr(server, "_record_closed", lambda inst: None)
    return instances


def test_delete_disarms_a_freshly_armed_run(reg):
    reg["untitled-2"] = _Inst("untitled-2")
    ap.arm("untitled-2", "pr", message="m")
    assert ap.get("untitled-2") is not None

    r = client.delete("/api/instances/untitled-2")

    assert r.status_code == 200
    assert "untitled-2" not in reg
    assert ap.get("untitled-2") is None


def test_a_recreated_title_does_not_inherit_the_old_target(reg):
    """The regression in one line: arm, delete inside the grace, recreate —
    the newcomer must read as un-armed, and prune must not be what saves it."""
    reg["sc-42"] = _Inst("sc-42")
    ap.arm("sc-42", "merge", message="Fix the login loop")
    ap.halt("sc-42", "pre-commit failed at mypy")
    client.delete("/api/instances/sc-42")

    reg["sc-42"] = _Inst("sc-42")  # the same title, a new session
    ap.prune(list(reg))  # within the grace: prune alone would keep it

    assert ap.get("sc-42") is None
    assert server._autopilot_dto("sc-42") is None


def test_close_disarms_too(reg):
    """/close frees the title for the next session just like DELETE does."""
    reg["s"] = _Inst("s")
    ap.arm("s", "commit")

    r = client.post("/api/instances/s/close")

    assert r.status_code == 200
    assert ap.get("s") is None


def test_the_disarm_is_on_the_shared_removal_hook():
    """One door: DELETE, /close, /cleanup, failed starts and engine
    convergence all run _on_session_removed."""
    assert server._disarm_removed_session in server._SESSION_REMOVED_HOOKS
    ap.arm("gone", "push")
    server._on_session_removed("gone")
    assert ap.get("gone") is None


def test_a_record_for_a_session_that_never_existed_is_left_alone(reg):
    """Intake arms BEFORE its session exists. Removing some OTHER session must
    not touch a record that is still waiting for its own session."""
    reg["other"] = _Inst("other")
    ap.arm("sc-7", "pr", source="tix")
    client.delete("/api/instances/other")
    assert ap.get("sc-7") is not None
