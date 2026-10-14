"""The session's create task (Instance.Task) reaches /api/instances as
``task`` — on both snapshot paths, so the pane's "Task:" line is the same on
every browser and for an agent MindFlock can't read a transcript of."""

from __future__ import annotations

from backend.session.storage import Status


class _Inst:
    Program = "claude"
    Path = ""
    Branch = ""
    BaseBranch = ""
    InPlace = False

    def __init__(self, title, task=""):
        self.Title = title
        self.Status = Status.Running
        self.Task = task

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""


def test_both_snapshot_paths_carry_the_task(monkeypatch):
    from backend.web import server

    inst = _Inst("task-snap", "add a README section\nwith setup steps")
    assert server._session_snapshot_cheap(inst, {})["task"] == (
        "add a README section\nwith setup steps"
    )
    monkeypatch.setattr(server.ENGINE, "instances", {inst.Title: inst})
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    server._ACTIVITY_CACHE.pop(inst.Title, None)
    mine = [d for d in server._build_instances_snapshot() if d["title"] == inst.Title]
    assert mine and mine[0]["task"] == "add a README section\nwith setup steps"


def test_no_task_reads_as_null():
    from backend.web import server

    assert server._session_snapshot_cheap(_Inst("task-none"), {})["task"] is None
