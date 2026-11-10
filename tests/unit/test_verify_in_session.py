"""Verify walks IN THE SESSION that wrote the work.

Owner request 2026-11-10: a Verify run happens inside the ticket's own
session agent — the steps are handed to it as a prompt once they are
written, it puts them on its own todo list, and the person talks to it
there — instead of a separate ``verify-<plan>-<sha7>`` checker (which the
memory gate could refuse, and which needed its own checkout).

Since the same day's follow-up the prompt is SELF-VERIFY — the agent writes
and walks its own steps (pinned in tests/unit/test_verify_self.py). These
pin: the run route handing it to the owning session (sent when idle, queued when busy, queued + relaunched by the
drain when the agent is not running, refused when paused) and recording the
run with ``session`` = that session and ``in_session``; the fallback to a
checker when the session is gone or on another branch; the results coming
back through the same file and the tree check holding a descendant HEAD to
the plan's commit; close/cancel never ending the person's session; and a
flight's Verify stop starting in-session with no memory refusal.

Isolation: the root conftest points the plans store, prompt queue, held and
sleep files at tmp; tmux and the agent probes are pinned on the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import prompt_queue as PQ
from backend.web.core import test_plans as T
from backend.web.core import test_plans_driver as TPD

NOW = 1_800_000_000.0


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "tip_sha": "b" * 40,
        "live_branch": "main",
        "state": "generated",
        "live_at": 0.0,
        "generated_at": 100.0,
        "steps": [
            {
                "id": "s1",
                "text": "Call the API",
                "expect": "200",
                "actor": "agent",
            },
            {
                "id": "s2",
                "text": "Read the log line",
                "expect": "one line",
                "actor": "agent",
                "needs": ["s1"],
            },
            {"id": "s3", "text": "Look at the page", "expect": "", "actor": "human"},
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


def _inst(wt, branch="feature/sc-1", path="/repo", status=None):
    return SimpleNamespace(
        GetWorktreePath=lambda: str(wt),
        Branch=branch,
        Path=path,
        Status=status,
        Program="claude",
    )


# --------------------------------------------------------------------------- #
# The checker's prompt (the fallback when the session is gone)
# --------------------------------------------------------------------------- #
def test_checker_prompt_is_unchanged_by_default():
    p = T.build_run_prompt(_plan(), None, live=False, where="branch", prepared=True)
    assert not p.startswith(T.WALK_MARK)
    assert "TODO LIST" not in p
    assert "change NOTHING else" in p


# --------------------------------------------------------------------------- #
# POST /api/test-plans/{id}/run — the owning session
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_is_verify_repo_usable", lambda root: True)
    monkeypatch.setattr(
        server,
        "_prepare_verify_checkout",
        lambda repo, title, ref: {"ok": True, "path": repo, "sha": ref},
    )
    monkeypatch.setattr(server, "_kill_orphan_plan_tmux", lambda t: "")
    monkeypatch.setattr(server, "_free_stale_verify_worktree", lambda r, t: "")
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    # The agent's window: tmux and the activity probe, pinned.
    agent = {"activity": "idle", "running": True}
    typed: list = []
    monkeypatch.setattr(
        server,
        "_live_session_name",
        lambda name: name if agent["running"] else None,
    )
    monkeypatch.setattr(server, "_agent_activity", lambda inst, t: agent["activity"])
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: False)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_note_human_input", lambda t: None)
    monkeypatch.setattr(server, "_note_pane_input", lambda inst, t, text: None)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda t: None)

    def _send(name, text, submit):
        typed.append((name, text))
        return True

    monkeypatch.setattr(server, "_send_to_agent", _send)
    made: list = []

    async def _create(payload, request=None):
        made.append(payload)
        return JSONResponse({"ok": True}, status_code=202)

    monkeypatch.setattr(server, "create_instance", _create)
    wt = tmp_path / "wt-sc-1"
    wt.mkdir()
    c = TestClient(server.app)
    c.made, c.typed, c.agent, c.instances, c.wt = made, typed, agent, instances, wt
    return c


def test_run_hands_the_walk_to_the_owning_session(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "branch"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["session"] == "sc-1" and body["in_session"] is True
    assert body["queued"] is False
    # No checker was created; the prompt went to the session's own agent.
    assert client.made == []
    assert len(client.typed) == 1
    assert client.typed[0][1].startswith(T.WALK_MARK)
    plan = T.get("sc-1")
    assert plan["state"] == "running" and plan["run_session"] == "sc-1"
    run = plan["runs"][-1]
    assert run["session"] == "sc-1" and run["in_session"] is True
    assert run["where"] == "branch"
    assert body["plan"]["run_session"] == "sc-1"


def test_run_queues_the_walk_while_the_agent_is_mid_turn(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.agent["activity"] = "working"
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert r.json()["queued"] is True
    # Never typed over the running turn: it waits on the session's queue.
    assert client.typed == []
    items = PQ.get_state("sc-1")["items"]
    assert len(items) == 1 and items[0]["text"].startswith(T.WALK_MARK)
    assert TPD.walk_is_queued("sc-1")
    assert T.get("sc-1")["state"] == "running"


def test_run_queues_for_an_agent_that_is_not_running(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.agent["running"] = False
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert r.json()["queued"] is True and client.made == []
    # The drain relaunches the agent and sends it once the CLI is idle.
    st = PQ.get_state("sc-1")
    assert st["enabled"] and st["items"][0]["text"].startswith(T.WALK_MARK)


def test_run_refuses_a_paused_session_and_puts_the_plan_back(client):
    from backend.session.storage import Paused

    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt, status=Paused)
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["reason"] == "send_failed" and "paused" in body["error"]
    # Not a "busy" 409: no "session" key a caller could read as running.
    assert "session" not in body
    assert client.made == [] and client.typed == []
    plan = T.get("sc-1")
    assert plan["state"] == "generated" and plan["run_session"] == ""


def test_run_falls_back_to_a_checker_when_the_session_is_gone(client):
    T.upsert(_plan())
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert r.json().get("in_session") is None
    assert len(client.made) == 1
    assert client.made[0]["title"].startswith("verify-sc-1")
    assert client.made[0]["hidden_by"] == "verify"
    assert not client.made[0]["prompt"].startswith(T.WALK_MARK)
    run = T.get("sc-1")["runs"][-1]
    assert run["session"].startswith("verify-sc-1") and run["in_session"] is False


def test_run_falls_back_when_the_session_moved_to_another_branch(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt, branch="feature/other")
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert len(client.made) == 1 and client.typed == []


def test_a_duplicate_window_on_the_branch_walks_a_branch_plan(client):
    pid = T.branch_plan_id("/repo", "feature/sc-1")
    T.upsert(_plan(pid, branch="feature/sc-1"))
    client.instances["sc-1-copy"] = _inst(client.wt, branch="feature/sc-1")
    r = client.post("/api/test-plans/%s/run" % pid, json={})
    assert r.status_code == 202, r.text
    assert r.json()["session"] == "sc-1-copy" and client.made == []


def test_a_running_in_session_walk_is_busy_not_restarted(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    assert client.post("/api/test-plans/sc-1/run", json={}).status_code == 202
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409 and r.json()["reason"] == "busy"
    assert r.json()["session"] == "sc-1"
    assert len(client.typed) == 1


# --------------------------------------------------------------------------- #
# Results come back through the same file; the tree check
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    return (
        subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(
                os.environ,
                GIT_AUTHOR_NAME="t",
                GIT_AUTHOR_EMAIL="t@t",
                GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t",
            ),
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def repo(tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "feature/sc-1")
    (wt / "a.txt").write_text("a")
    _git(wt, "add", "a.txt")
    _git(wt, "commit", "-q", "-m", "a")
    first = _git(wt, "rev-parse", "HEAD")
    (wt / "b.txt").write_text("b")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "-q", "-m", "b")
    return wt, first, _git(wt, "rev-parse", "HEAD")


def _finished(wt, answers):
    (wt / T.RESULT_FILE).write_text(
        json.dumps({"plan": "sc-1", "finished": True, "results": answers})
    )


def _start_in_session(monkeypatch, wt, sha):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(wt)})
    T.upsert(_plan(tip_sha=sha, sha=sha))
    T.start_run("sc-1", "sc-1", where="branch", target="", in_session=True)
    monkeypatch.setattr(TPD, "_verify_window_gone", lambda t, now=None: False)
    monkeypatch.setattr(TPD, "_announce_test_plan_checked", lambda p: None)


ANSWERS = [
    {"id": "s1", "result": "pass", "note": "200 from :8123"},
    {"id": "s2", "result": "pass", "note": "one line"},
    {"id": "s3", "result": "blocked", "note": "needs a person"},
]


def test_results_from_the_sessions_worktree_close_the_run(monkeypatch, repo):
    wt, first, head = repo
    # The session committed after the plan was written: HEAD descends from it.
    _start_in_session(monkeypatch, wt, first)
    _finished(wt, ANSWERS)
    TPD._poll_running_test_plans(T.list_plans())
    plan = T.get("sc-1")
    run = plan["runs"][-1]
    assert plan["state"] != "running" and plan["run_session"] == ""
    assert run["results"]["s1"]["result"] == "pass"
    assert run["tested_sha"] == head and run["expected_sha"] == head
    assert not (wt / T.RESULT_FILE).exists()  # eaten, as for a checker


def test_a_head_that_lost_the_commit_downgrades_its_passes(monkeypatch, repo):
    wt, first, head = repo
    _git(wt, "reset", "-q", "--hard", first)  # the plan's commit is gone
    _start_in_session(monkeypatch, wt, head)
    _finished(wt, ANSWERS)
    TPD._poll_running_test_plans(T.list_plans())
    run = T.get("sc-1")["runs"][-1]
    assert run["results"]["s1"]["result"] == "blocked"
    assert "not checked on the live tree" in run["results"]["s1"]["note"]


def test_progress_is_read_from_the_sessions_file(monkeypatch, repo):
    wt, first, _head = repo
    _start_in_session(monkeypatch, wt, first)
    (wt / T.RESULT_FILE).write_text(
        json.dumps({"plan": "sc-1", "finished": False, "results": ANSWERS[:1]})
    )
    TPD._poll_running_test_plans(T.list_plans())
    plan = T.get("sc-1")
    assert plan["state"] == "running"
    assert T.run_progress(plan)["answered"] == 1


# --------------------------------------------------------------------------- #
# Close / cancel never end the person's session
# --------------------------------------------------------------------------- #
def test_end_verify_session_never_closes_an_owning_session(monkeypatch):
    from backend.web import server

    closed: list = []

    async def _close(title):
        closed.append(title)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(server, "close_instance", _close)
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"sc-1": object(), "verify-sc-1-abc1234": object(), "fix-sc-1": object()},
    )
    T.upsert(_plan())
    assert asyncio.run(TPD._end_verify_session("sc-1")) is False
    # A deleted plan's own session is still not a checker (by name).
    T.delete("sc-1")
    assert asyncio.run(TPD._end_verify_session("sc-1")) is False
    assert asyncio.run(TPD._end_verify_session("verify-sc-1-abc1234")) is True
    assert asyncio.run(TPD._end_verify_session("fix-sc-1")) is True
    assert closed == ["verify-sc-1-abc1234", "fix-sc-1"]


def test_close_run_keeps_the_session_and_tells_its_agent(monkeypatch, tmp_path):
    from backend.web import server

    told: list = []
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    monkeypatch.setattr(
        server,
        "_deliver_to_agent",
        lambda inst, title, text: told.append((title, text)) or ("queued", None),
    )

    async def _never(title):
        raise AssertionError("closed the person's session")

    monkeypatch.setattr(server, "close_instance", _never)
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True)
    T.record_progress("sc-1", [{"id": "s1", "result": "pass", "note": "ok"}])
    done = asyncio.run(TPD.close_run("sc-1", "not checked: the check ran out of time"))
    assert done["state"] != "running"
    run = done["runs"][-1]
    assert run["results"]["s1"]["result"] == "pass"
    assert run["results"]["s2"]["result"] == "blocked"
    assert told and told[0][0] == "sc-1" and "closed" in told[0][1]
    assert not told[0][1].startswith(T.WALK_MARK)


def test_cancel_keeps_the_session_and_tells_its_agent_to_stop(monkeypatch, tmp_path):
    from backend.web import server

    told: list = []
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    monkeypatch.setattr(
        server,
        "_deliver_to_agent",
        lambda inst, title, text: told.append((title, text)) or ("queued", None),
    )

    async def _never(title):
        raise AssertionError("closed the person's session")

    monkeypatch.setattr(server, "close_instance", _never)
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True)
    resp = asyncio.run(server.cancel_test_plan_run("sc-1"))
    body = json.loads(resp.body)
    assert body["ok"] is True and body["closed"] is False
    assert body["plan"]["state"] != "running"
    assert told and told[0][0] == "sc-1" and "cancelled" in told[0][1]


# --------------------------------------------------------------------------- #
# A flight's Verify stop
# --------------------------------------------------------------------------- #
@pytest.fixture
def flights_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))


def _flight(session="sc-1"):
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "verify", "config": {"who": "both"}}]},
        now=NOW,
    )
    f = F.take_off(plan, session=session, now=NOW)
    a = F.next_action(f, {"session": {"present": True, "status": "running"}}, NOW)
    return F.apply(f, a, NOW)


def test_verify_stop_starts_in_the_session_without_a_memory_refusal(
    flights_dir, monkeypatch
):
    async def fake_run(pid, req, payload):
        body = {"ok": True, "session": "sc-1", "in_session": True, "queued": True}
        return SimpleNamespace(status_code=202, body=json.dumps(body).encode())

    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(run_test_plan=fake_run))
    action = {"action": "run", "phase": "agent_run", "detail": {"plan": "sc-1"}}
    out = asyncio.run(D._perform(_flight(), action, NOW))
    d = out["detail"]
    assert out["action"] == "run" and d["run_started"] is True
    assert d["in_session"] is True and d["walk_session"] == "sc-1"
    assert d["run_wait"] is None
    assert "talk to it there" in out["note"]


def test_verify_stop_through_the_real_route_never_waits_for_memory(
    flights_dir, client, monkeypatch
):
    from backend.web import server

    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)

    async def _refuse(payload, request=None):
        raise AssertionError("created a checker for a session that is open")

    monkeypatch.setattr(server, "create_instance", _refuse)
    res = asyncio.run(D._start_verify_run("sc-1", "branch", "", "agent"))
    assert res["started"] is True and res["in_session"] is True
    assert res["session"] == "sc-1" and res["reason"] == ""


def test_a_queued_walk_holds_the_stall_clock(monkeypatch):
    then = time.time() - 3600
    progress = {"started_at": then, "progress_at": then, "answered": 0}
    monkeypatch.setattr(TPD, "walk_is_queued", lambda t: True)
    checker: dict = {"activity": "idle"}
    out = D._walk_clock("sc-1", "sc-1", progress, checker)
    assert out["started_at"] > time.time() - 60 and out["queued"] is True
    assert checker["activity"] == "queued"
    # Picked up: the clock runs from the last time it was seen queued.
    seen = D._WALK_QUEUED_AT["sc-1"]
    monkeypatch.setattr(TPD, "walk_is_queued", lambda t: False)
    out = D._walk_clock("sc-1", "sc-1", progress, {"activity": "working"})
    assert out["started_at"] == seen and out["progress_at"] == seen
    D._WALK_QUEUED_AT.pop("sc-1", None)


def test_verify_obs_names_the_walking_session(flights_dir, monkeypatch):
    plan = _plan(state="running", run_session="sc-1")
    plan["runs"] = [{"at": NOW, "session": "sc-1", "in_session": True, "results": {}}]
    monkeypatch.setattr(T, "get", lambda pid: plan)
    monkeypatch.setattr(D, "_session_obs", lambda s, now, full: {"activity": "working"})
    monkeypatch.setattr(TPD, "walk_is_queued", lambda t: False)
    v = D._verify_obs(_flight(), {"detail": {"plan": "sc-1"}}, {})
    assert v["in_session"] is True and v["walk_session"] == "sc-1"
    assert v["checker"]["activity"] == "working"
