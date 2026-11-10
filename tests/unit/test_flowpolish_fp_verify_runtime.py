"""Flow polish — an on-demand Verify (pressed on a session, no route) behaves
as well as one inside a route, and every exit works.

Pins, per finding of the fp-verify-runtime brief: the session Verify checks
unshipped work on its own branch (1); stall nudge/close for an on-demand
self-verify, with no clock while it waits on the queue (2); a dead window
releases through ``release_run`` with a sentence naming the session (3); a
broken results file is noted and the agent told once (4); cancel / close /
delete withdraw a queued check and only tell an agent that picked it up (5);
Fix in the session's own agent, then Verify again (6); "Not verified" reaches
the agent (7); re-runs carry walk / previous / steps (8); row ``queued`` and
the dismiss route (9); the ``verify_needs_you`` notification (10); no
push-time generation for an open session (11); finished checker sessions are
closed (12); the MCP tools (13).

Isolation: the root conftest points the plans store, prompt queue and
settings at tmp; the agent probes are pinned on the server.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
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
            {"id": "s1", "text": "Call the API", "expect": "200", "actor": "agent"},
            {"id": "s2", "text": "Look at the page", "expect": "", "actor": "human"},
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
    agent = {"activity": "idle", "running": True}
    typed: list = []
    monkeypatch.setattr(
        server, "_live_session_name", lambda n: n if agent["running"] else None
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

    async def _never_close(title):
        raise AssertionError("closed the person's session %s" % title)

    monkeypatch.setattr(server, "close_instance", _never_close)
    wt = tmp_path / "wt-sc-1"
    wt.mkdir()
    c = TestClient(server.app)
    c.made, c.typed, c.agent, c.instances, c.wt = made, typed, agent, instances, wt
    return c


# --------------------------------------------------------------------------- #
# 1. The session's Verify checks unshipped work on its branch
# --------------------------------------------------------------------------- #
def _verify_route(client, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server, "_ensure_verify_plan", lambda title: ("sc-1", ""))
    monkeypatch.setattr(server, "_inst_or_404", lambda t: (client.instances[t], None))
    monkeypatch.setattr(server._peer_guard, "refusal", lambda inst, what: None)
    client.instances["sc-1"] = _inst(client.wt)


def test_session_verify_checks_unshipped_work_on_its_branch(client, monkeypatch):
    _verify_route(client, monkeypatch)
    monkeypatch.setattr(T, "verify_target", lambda root: "https://prod.example")
    T.upsert(_plan())
    r = client.post("/api/instances/sc-1/verify", json={"notes": "the badge"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["started"] is True and body["session"] == "sc-1"
    assert body["queued"] is False
    run = T.get("sc-1")["runs"][-1]
    assert run["where"] == "branch"
    # The deployment never reached the prompt: the branch has the work.
    assert "prod.example" not in client.typed[0][1]


def test_session_verify_of_shipped_work_keeps_the_repo_target(client, monkeypatch):
    _verify_route(client, monkeypatch)
    T.upsert(_plan(state="due", live_at=50.0))
    r = client.post("/api/instances/sc-1/verify", json={})
    assert r.status_code == 202, r.text
    assert T.get("sc-1")["runs"][-1].get("where", "") == ""


def test_session_verify_takes_an_environment(client, monkeypatch):
    _verify_route(client, monkeypatch)
    T.upsert(_plan())
    r = client.post("/api/instances/sc-1/verify", json={"environment": "nowhere"})
    assert r.status_code == 400 and "nowhere" in r.json()["error"]
    r = client.post("/api/instances/sc-1/verify", json={"environment": 3})
    assert r.status_code == 400


def test_session_verify_reports_a_queued_check(client, monkeypatch):
    _verify_route(client, monkeypatch)
    client.agent["activity"] = "working"
    T.upsert(_plan())
    r = client.post("/api/instances/sc-1/verify", json={})
    assert r.status_code == 202 and r.json()["queued"] is True


# --------------------------------------------------------------------------- #
# 2. Stall timers for an on-demand self-verify
# --------------------------------------------------------------------------- #
@pytest.fixture
def stall(monkeypatch):
    """A running self-verify in sc-1, no flight, with the agent's activity and
    the nudge recorded."""
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    TPD._SELF_NUDGED_AT.clear()
    D._WALK_QUEUED_AT.pop("sc-1", None)
    obs = {"activity": "idle"}
    nudges: list = []
    monkeypatch.setattr(D, "_session_obs", lambda f, now, agent: dict(obs))
    monkeypatch.setattr(D, "_deliver", lambda title, text: nudges.append(title))
    monkeypatch.setattr(TPD, "_flight_owns_run", lambda pid: False)
    monkeypatch.setattr(TPD, "_tell_walk_closed", lambda title, reason: False)
    started = T.get("sc-1")["runs"][-1]["at"]
    yield SimpleNamespace(obs=obs, nudges=nudges, started=started)
    TPD._SELF_NUDGED_AT.clear()


def test_a_quiet_self_verify_is_nudged_once_then_closed(stall):
    t0 = stall.started
    assert TPD._self_run_stalled(T.get("sc-1"), t0 + 60) is False
    assert stall.nudges == []
    assert (
        TPD._self_run_stalled(T.get("sc-1"), t0 + F.VERIFY_STALL_NUDGE_S + 5) is False
    )
    assert stall.nudges == ["sc-1"]
    # Not nudged twice.
    TPD._self_run_stalled(T.get("sc-1"), t0 + F.VERIFY_STALL_NUDGE_S + 60)
    assert stall.nudges == ["sc-1"]
    late = t0 + F.VERIFY_STALL_NUDGE_S + F.VERIFY_STALL_CLOSE_S + 30
    assert TPD._self_run_stalled(T.get("sc-1"), late) is True
    plan = T.get("sc-1")
    assert plan["state"] != "running"
    run = plan["runs"][-1]
    assert run["outcome"] == "blocked"
    assert "stopped answering" in json.dumps(run)


def test_a_working_agent_is_not_nudged(stall):
    stall.obs["activity"] = "working"
    t0 = stall.started
    TPD._self_run_stalled(T.get("sc-1"), t0 + F.VERIFY_STALL_NUDGE_S + 5)
    assert stall.nudges == []


def test_a_queued_check_runs_no_clock(stall):
    PQ.enqueue("sc-1", T.WALK_MARK + " check it")
    far = stall.started + 10 * 3600
    assert TPD._self_run_stalled(T.get("sc-1"), far) is False
    assert stall.nudges == [] and T.get("sc-1")["state"] == "running"


def test_a_check_past_its_budget_is_closed(stall):
    t0 = stall.started
    over = t0 + T.RUN_BUDGET_MIN * 60 + F.VERIFY_BUDGET_GRACE_S + 10
    stall.obs["activity"] = "working"
    assert TPD._self_run_stalled(T.get("sc-1"), over) is True
    assert "ran out of time (30 min)" in json.dumps(T.get("sc-1")["runs"][-1])


def test_a_check_a_route_drives_is_left_to_the_route(stall, monkeypatch):
    monkeypatch.setattr(TPD, "_flight_owns_run", lambda pid: True)
    far = stall.started + 10 * 3600
    assert TPD._self_run_stalled(T.get("sc-1"), far) is False
    assert T.get("sc-1")["state"] == "running"


def test_a_string_finished_is_not_finished(monkeypatch, tmp_path):
    from backend.web import server

    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    monkeypatch.setattr(TPD, "_verify_window_gone", lambda t: False)
    monkeypatch.setattr(TPD, "_self_run_stalled", lambda plan: False)
    (tmp_path / T.RESULT_FILE).write_text(
        json.dumps({"plan": "sc-1", "finished": "false", "steps": []}),
        encoding="utf-8",
    )
    TPD._poll_running_test_plans(T.list_plans())
    assert T.get("sc-1")["state"] == "running"


# --------------------------------------------------------------------------- #
# 3. A dead window releases through release_run, naming the session
# --------------------------------------------------------------------------- #
def test_a_dead_window_releases_through_release_run(monkeypatch, tmp_path):
    from backend.web import server

    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    monkeypatch.setattr(TPD, "_verify_window_gone", lambda t: True)
    TPD._poll_running_test_plans(T.list_plans())
    # The outcome, whichever store function did it: the run is KEPT as
    # blocked with the reason, and unshipped work is not marked as shipped.
    plan = T.get("sc-1")
    run = plan["runs"][-1]
    assert run.get("outcome") == "blocked"
    reason = run.get("outcome_note") or ""
    assert "sc-1" in reason and "verify session" not in reason.lower()
    assert "press Verify" in reason
    assert not plan.get("live_at")


# --------------------------------------------------------------------------- #
# 4. A broken results file
# --------------------------------------------------------------------------- #
def test_a_broken_results_file_is_noted_and_the_agent_told_once(monkeypatch, tmp_path):
    from backend.web import server

    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    told: list = []
    monkeypatch.setattr(
        server,
        "_deliver_to_agent",
        lambda inst, title, text: told.append((title, text)) or ("queued", None),
    )
    noted: list = []
    monkeypatch.setattr(
        T, "note_file_error", lambda pid, msg: noted.append((pid, msg)), raising=False
    )
    (tmp_path / T.RESULT_FILE).write_text('{"plan": "sc-1", ', encoding="utf-8")
    clock = [1000.0]
    monkeypatch.setattr(TPD.time, "time", lambda: clock[0])
    TPD._RESULTS_FILE_BAD.clear()
    plan = T.get("sc-1")
    assert TPD._read_verify_results(plan) is None
    assert told == [] and noted == []  # one read mid-write is not proof
    clock[0] += TPD._RESULTS_FILE_GRACE_S + 1
    assert TPD._read_verify_results(plan) is None
    assert len(told) == 1 and told[0][0] == "sc-1"
    assert "not valid JSON" in told[0][1] or "isn't valid JSON" in told[0][1]
    assert not told[0][1].startswith(T.WALK_MARK)
    assert noted and noted[0][0] == "sc-1" and noted[0][1]
    clock[0] += 60
    TPD._read_verify_results(plan)
    assert len(told) == 1  # once
    (tmp_path / T.RESULT_FILE).write_text(
        json.dumps({"plan": "sc-1", "finished": False}), encoding="utf-8"
    )
    assert TPD._read_verify_results(plan) is not None
    assert noted[-1] == ("sc-1", "")  # cleared
    TPD._RESULTS_FILE_BAD.clear()


# --------------------------------------------------------------------------- #
# 5. Cancel / close / delete withdraw a queued check
# --------------------------------------------------------------------------- #
def test_cancel_withdraws_a_queued_check_and_says_nothing(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.agent["activity"] = "working"
    assert client.post("/api/test-plans/sc-1/run", json={}).status_code == 202
    assert TPD.walk_is_queued("sc-1")
    PQ.enqueue("sc-1", "the person's own next prompt")
    r = client.post("/api/test-plans/sc-1/cancel")
    body = r.json()
    assert r.status_code == 200 and body["verify_stopped"] is True
    assert body["closed"] is False and body["told"] is False
    assert not TPD.walk_is_queued("sc-1")
    # Only the check was withdrawn; the person's own prompt stays.
    texts = [i["text"] for i in PQ.get_state("sc-1")["items"]]
    assert texts == ["the person's own next prompt"]
    assert client.typed == []


def test_cancel_tells_an_agent_that_picked_the_check_up(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    assert client.post("/api/test-plans/sc-1/run", json={}).status_code == 202
    assert len(client.typed) == 1
    body = client.post("/api/test-plans/sc-1/cancel").json()
    assert body["verify_stopped"] is True and body["told"] is True
    assert "this check was closed" in client.typed[-1][1]


def test_close_run_withdraws_a_queued_check(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.agent["activity"] = "working"
    client.post("/api/test-plans/sc-1/run", json={})
    asyncio.run(TPD.close_run("sc-1", "not checked: closed"))
    assert not TPD.walk_is_queued("sc-1")
    assert client.typed == [] and PQ.get_state("sc-1")["items"] == []


def test_delete_tells_the_agent_and_keeps_the_session(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.post("/api/test-plans/sc-1/run", json={})
    r = client.delete("/api/test-plans/sc-1")
    assert r.status_code == 200, r.text
    assert r.json()["told"] is True and r.json()["closed"] is False
    assert "it was deleted" in client.typed[-1][1]
    assert T.get("sc-1") is None


# --------------------------------------------------------------------------- #
# 6. Fix in the session's own agent
# --------------------------------------------------------------------------- #
def _failed_plan():
    plan = _plan(state="done", live_at=50.0)
    plan["runs"] = [
        {
            "at": 100.0,
            "by": "agent",
            "session": "sc-1",
            "in_session": True,
            "self": True,
            "outcome": "failed",
            "results": {
                "s1": {"result": "fail", "note": "got a 500 from /api", "by": "agent"}
            },
        }
    ]
    return plan


def test_fix_goes_to_the_sessions_own_agent(client):
    T.upsert(_failed_plan())
    client.instances["sc-1"] = _inst(client.wt)
    TPD._FIX_PENDING.clear()
    r = client.post("/api/test-plans/sc-1/fix", json={})
    assert r.status_code == 202, r.text
    assert r.json() == {
        "ok": True,
        "session": "sc-1",
        "in_session": True,
        "queued": False,
    }
    assert client.made == []
    text = client.typed[-1][1]
    assert "Call the API" in text and "got a 500 from /api" in text
    assert not text.startswith(T.WALK_MARK)
    assert TPD._FIX_PENDING["sc-1"]["session"] == "sc-1"
    TPD._FIX_PENDING.clear()


def test_fix_in_a_new_session_when_asked(client, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server, "_reclaim_plan_worktree", lambda r, t: ("", ""))
    T.upsert(_failed_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post("/api/test-plans/sc-1/fix", json={"in_session": False})
    assert r.status_code == 202, r.text
    assert r.json()["session"] == "fix-sc-1" and r.json()["in_session"] is False
    assert client.made and client.made[0]["title"] == "fix-sc-1"


def test_fix_in_session_refuses_a_closed_session(client):
    T.upsert(_failed_plan())
    r = client.post("/api/test-plans/sc-1/fix", json={"in_session": True})
    assert r.status_code == 409
    assert r.json()["reason"] == "session_gone" and r.json()["detail"]


def test_verify_runs_again_when_the_fix_turn_ends(monkeypatch):
    from backend.web import server

    T.upsert(_failed_plan())
    TPD._FIX_PENDING.clear()
    TPD.expect_fix("sc-1", "sc-1", now=1000.0)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": object()})
    obs = {"activity": "working"}
    monkeypatch.setattr(D, "_session_obs", lambda f, now, agent: dict(obs))
    runs: list = []

    async def _run(pid, request, body):
        runs.append((pid, body))
        return JSONResponse({"ok": True}, status_code=202)

    monkeypatch.setattr(server, "run_test_plan", _run)
    asyncio.run(TPD._reverify_after_fix(now=1010.0))
    assert runs == [] and TPD._FIX_PENDING["sc-1"]["busy"] is True
    obs["activity"] = "idle"
    asyncio.run(TPD._reverify_after_fix(now=1020.0))
    assert len(runs) == 1 and runs[0][0] == "sc-1"
    assert "sc-1" not in TPD._FIX_PENDING


def test_a_fix_still_queued_waits(monkeypatch):
    from backend.web import server

    T.upsert(_failed_plan())
    TPD._FIX_PENDING.clear()
    TPD.expect_fix("sc-1", "sc-1", now=1000.0)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": object()})
    PQ.enqueue("sc-1", "MindFlock — Verify found a problem in the work you just did.")
    assert TPD._fix_turn_over(TPD._FIX_PENDING["sc-1"], 2000.0) is False
    TPD._FIX_PENDING.clear()


# --------------------------------------------------------------------------- #
# 7. "Not verified" reaches the agent
# --------------------------------------------------------------------------- #
def _needs_you_plan():
    plan = _plan()
    plan["runs"] = [
        {
            "at": 100.0,
            "by": "agent",
            "session": "sc-1",
            "in_session": True,
            "self": True,
            "outcome": "needs_you",
            "results": {"s1": {"result": "pass", "note": "200", "by": "agent"}},
        }
    ]
    return plan


def test_not_verified_is_delivered_to_the_session(client):
    T.upsert(_needs_you_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post(
        "/api/test-plans/sc-1/confirm",
        json={"verified": False, "note": "the badge is missing"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True
    text = client.typed[-1][1]
    assert "says it does not work: the badge is missing" in text


def test_not_verified_with_the_session_closed_is_not_delivered(client):
    T.upsert(_needs_you_plan())
    r = client.post("/api/test-plans/sc-1/confirm", json={"verified": False})
    assert r.status_code == 200 and r.json()["delivered"] is False


def test_confirm_passes_per_step_answers_when_given(client, monkeypatch):
    seen: list = []
    real = T.confirm_verify

    def _confirm(pid, verified, note="", steps=None):
        seen.append(steps)
        return real(pid, verified, note)

    monkeypatch.setattr(T, "confirm_verify", _confirm)
    T.upsert(_needs_you_plan())
    r = client.post(
        "/api/test-plans/sc-1/confirm",
        json={"verified": True, "steps": {"s2": "pass"}},
    )
    assert r.status_code == 200, r.text
    assert seen == [{"s2": "pass"}]
    bad = client.post(
        "/api/test-plans/sc-1/confirm", json={"verified": True, "steps": {"s2": "x"}}
    )
    assert bad.status_code == 400


# --------------------------------------------------------------------------- #
# 8. Re-runs carry walk, previous answers and "only these"
# --------------------------------------------------------------------------- #
def test_a_rerun_passes_walk_previous_and_only_these(client, monkeypatch):
    seen: dict = {}
    real = T.build_self_verify_prompt

    def _build(plan, walk=None, previous=None, **kw):
        seen.update(walk=walk, previous=previous, notes=kw.get("notes"))
        return real(plan, **kw)

    monkeypatch.setattr(T, "build_self_verify_prompt", _build)
    monkeypatch.setattr(
        T, "previous_for_prompt", lambda plan: "s1 failed last time", raising=False
    )
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post("/api/test-plans/sc-1/run", json={"walk": "all", "steps": ["s1"]})
    assert r.status_code == 202, r.text
    assert seen["walk"] == "all" and seen["previous"] == "s1 failed last time"
    assert "Re-check ONLY these steps: s1 (Call the API)" in seen["notes"]


def test_a_rerun_still_works_on_a_store_without_the_new_arguments(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post("/api/test-plans/sc-1/run", json={"walk": "all"})
    assert r.status_code == 202, r.text


def test_a_rerun_names_only_real_steps(client):
    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    r = client.post("/api/test-plans/sc-1/run", json={"steps": ["s9"]})
    assert r.status_code == 400 and "s9" in r.json()["error"]


# --------------------------------------------------------------------------- #
# 9. Row ``queued`` and the dismiss route
# --------------------------------------------------------------------------- #
def test_the_row_says_a_check_is_queued(client):
    from backend.web.core import snapshot

    T.upsert(_plan())
    client.instances["sc-1"] = _inst(client.wt)
    client.agent["activity"] = "working"
    client.post("/api/test-plans/sc-1/run", json={})
    v = snapshot._row_verify("sc-1")
    assert v["state"] == "verifying" and v["queued"] is True
    TPD.withdraw_walk("sc-1")
    assert snapshot._row_verify("sc-1")["queued"] is False


def test_dismiss_is_404_without_the_store_and_ok_with_it(client, monkeypatch):
    T.upsert(_plan())
    monkeypatch.delattr(T, "dismiss_verify", raising=False)
    assert client.post("/api/test-plans/sc-1/dismiss").status_code == 404
    seen: list = []
    monkeypatch.setattr(
        T, "dismiss_verify", lambda pid: seen.append(pid) or {"id": pid}, raising=False
    )
    r = client.post("/api/test-plans/sc-1/dismiss")
    assert r.status_code == 200 and r.json() == {"ok": True} and seen == ["sc-1"]
    monkeypatch.setattr(T, "dismiss_verify", lambda pid: None, raising=False)
    assert client.post("/api/test-plans/sc-1/dismiss").status_code == 404


# --------------------------------------------------------------------------- #
# 10. "Verify needs you" rings
# --------------------------------------------------------------------------- #
def test_a_needs_you_check_emits_needs_you_with_what_to_do(monkeypatch):
    from backend.web.core import events

    got: list = []
    monkeypatch.setattr(events.BUS, "emit", lambda ev, **kw: got.append((ev, kw)))
    plan = _needs_you_plan()
    TPD._announce_test_plan_checked(plan)
    ev, kw = got[-1]
    assert ev == "session.test_plan_checked" and kw["new"] == "needs_you"
    assert "Look at the page" in kw["data"]["detail"]
    assert "Confirm verified" in kw["data"]["detail"]


def test_the_needs_you_rule_is_on_by_default_and_says_what_to_do():
    from backend.web.addons import notify

    rule = next(r for r in notify.NOTIFY_RULES if r["id"] == "verify_needs_you")
    assert rule["default_enabled"] is True and rule["new"] == "needs_you"
    assert rule["event"] == "session.test_plan_checked"
    assert rule["title"] == "{session}: Verify needs you" and "{detail}" in rule["body"]
    for r in notify.NOTIFY_RULES:
        if r["id"].startswith("verify_"):
            text = " ".join(str(r[k]) for k in ("label", "title", "body"))
            assert "behavior" not in text.lower(), r["id"]
            assert "on its route" not in text, r["id"]


# --------------------------------------------------------------------------- #
# 11. No push-time generation for an open session
# --------------------------------------------------------------------------- #
@pytest.fixture
def pushed(tmp_path, monkeypatch):
    import subprocess

    from backend.web import server

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-b", "feat"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    (repo / ".mindflock.toml").write_text(
        "[workspace]\nverify_on_push = true\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "w"], check=True, capture_output=True
    )
    state = {"started": True}

    class _Wt:
        def GetRepoPath(self):
            return str(repo)

    class _Inst:
        Program = "claude"
        Status = None

        def GetWorktreePath(self):
            return str(repo)

        def GetGitWorktree(self):
            return _Wt()

        def Started(self):
            return state["started"]

    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _Inst()})
    calls: list = []
    for mod in (server, TPD):
        monkeypatch.setattr(mod, "_generate_test_plan", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(
            mod, "_start_test_plan_generation", lambda *a, **k: calls.append(a)
        )
    return SimpleNamespace(repo=repo, calls=calls, state=state)


def test_a_push_from_an_open_session_spends_no_model_call(pushed):
    TPD._ensure_test_plan_blocking("sc-1")
    plan = T.get("sc-1")
    assert plan is not None and plan["steps"] == [] and plan["state"] == "generated"
    assert pushed.calls == []


def test_a_closed_sessions_plan_gets_its_steps_written(pushed, monkeypatch):
    from backend.web import server

    TPD._ensure_test_plan_blocking("sc-1")
    TPD._generate_for_closed_test_plans(T.list_plans())
    assert pushed.calls == []  # still open
    monkeypatch.setattr(server.ENGINE, "instances", {})
    TPD._generate_for_closed_test_plans(T.list_plans())
    assert len(pushed.calls) == 1 and pushed.calls[0][0] == "sc-1"


def test_a_push_from_a_session_not_running_still_writes_steps(pushed):
    pushed.state["started"] = False
    TPD._ensure_test_plan_blocking("sc-1")
    assert len(pushed.calls) == 1


# --------------------------------------------------------------------------- #
# 12. Finished checker sessions are closed
# --------------------------------------------------------------------------- #
@pytest.fixture
def sweeper(monkeypatch):
    from backend.web import server
    from backend.web.core import hidden_sessions as _hs

    closed: list = []

    async def _close(title):
        closed.append(title)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(server, "close_instance", _close)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    flags: dict = {}
    monkeypatch.setattr(_hs, "hidden_by", lambda title: flags.get(title, ""))
    old = SimpleNamespace(
        CreatedAt=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
    )

    def register(title, hidden=""):
        server.ENGINE.instances[title] = old
        if hidden:
            flags[title] = hidden

    return closed, register


def _finished(plan_id, session, ended):
    return _plan(
        plan_id,
        state="done",
        run_session="",
        runs=[
            {
                "at": ended - 60,
                "outcome_at": ended,
                "outcome": "passed",
                "by": "agent",
                "session": session,
                "results": {},
            }
        ],
    )


def test_an_old_checker_whose_check_is_over_is_closed(sweeper):
    closed, register = sweeper
    T.upsert(_finished("sc-1", "verify-sc-1-aaaaaaa", time.time() - 3 * 86400))
    # An old checker without the flag, named by its run record.
    register("verify-sc-1-aaaaaaa")
    asyncio.run(TPD._sweep_orphan_verify_sessions())
    assert closed == ["verify-sc-1-aaaaaaa"]


def test_a_running_checker_and_a_persons_session_are_kept(sweeper):
    closed, register = sweeper
    T.upsert(_plan("sc-2", state="due", live_at=50.0))
    T.start_run("sc-2", "verify-sc-2-bbbbbbb")
    register("verify-sc-2-bbbbbbb", hidden="verify")
    # The person's own session walked a check in-session: never a candidate.
    T.upsert(_finished("sc-3", "sc-3", time.time() - 3 * 86400))
    register("sc-3")
    register("verify-mine")  # a person's own verify-titled session
    asyncio.run(TPD._sweep_orphan_verify_sessions())
    assert closed == []


# --------------------------------------------------------------------------- #
# 13. MCP: verify_session / confirm_verify
# --------------------------------------------------------------------------- #
def test_mcp_verify_session_posts_the_route(monkeypatch):
    from tests.unit._mcp_fakes import make_box, row

    box, api, _ = make_box([row("root"), row("orch", parent="root")])
    api.top_routes[("POST", "/api/instances/orch/verify")] = {
        "started": True,
        "plan": "orch",
        "session": "orch",
        "queued": True,
    }
    out = box.verify_session({"notes": "the badge"}, None)
    assert out["started"] is True and out["queued"] is True
    assert ("POST", "/api/instances/orch/verify", {"notes": "the badge"}) in api.calls


def test_mcp_confirm_verify_answers_the_rows_plan(monkeypatch):
    from tests.unit._mcp_fakes import make_box, row

    rows = [row("root"), row("orch", parent="root")]
    rows[1]["verify"] = {"state": "needs_you", "plan": "orch"}
    box, api, _ = make_box(rows)
    api.top_routes[("POST", "/api/test-plans/orch/confirm")] = {
        "ok": True,
        "delivered": True,
        "plan": {"runs": [{"outcome": "failed"}]},
    }
    out = box.confirm_verify({"verified": False, "note": "broken"}, None)
    assert out == {"ok": True, "title": "orch", "delivered": True, "verify": "failed"}
    assert (
        "POST",
        "/api/test-plans/orch/confirm",
        {"verified": False, "note": "broken"},
    ) in api.calls


def test_mcp_verify_refuses_a_session_the_caller_does_not_manage():
    from backend.mcp.protocol import ToolError
    from tests.unit._mcp_fakes import make_box, row

    box, api, _ = make_box([row("root"), row("orch", parent="root"), row("other")])
    with pytest.raises(ToolError):
        box.verify_session({"title": "other"}, None)
    with pytest.raises(ToolError):
        box.confirm_verify({"title": "other", "verified": True}, None)
    assert api.paths("POST") == []
