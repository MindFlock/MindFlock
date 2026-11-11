"""Verify fixes from the 0.14.7 code review (each confirmed with a probe).

Every test here failed before its fix:

1. a failed Verify hand-off (and any other end without a report) used to
   leave the plan with only a person's own steps — the generated ones were
   dropped when the self-verify opened, and nothing put them back;
2. a released or cancelled self-verify run was POPPED, so the run before it
   — a ``needs_you`` — came back over the new run's steps, and Confirm
   wrote ``pass`` into it for a check that never ran;
3. an automatic message (the Verify walk, its close notice) woke a sleeping
   session past the launch gate, and a gate that said "wait for memory" was
   a failure rather than a queued text;
4. two presses of Run opened two runs (and handed the prompt over twice);
5. the liveness pass's ``mark_due`` cleared an in-flight run;
6. a released run's "blocked" pill lingered from its START, so a run given
   up on after two hours never showed one;
7. the row index memo missed a same-size rewrite inside one mtime tick.

Isolation: the root conftest points the plans store, prompt queue, held and
sleep files at tmp.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.web.core import launch_gate as G
from backend.web.core import prompt_queue as Q
from backend.web.core import test_plans as T
from backend.web.core import test_plans_driver as TPD

GEN1 = {"id": "s1", "text": "Open settings, count the toggles", "actor": "agent"}
GEN2 = {"id": "s2", "text": "Look at the badge", "actor": "human"}
MINE = {"id": "m1", "text": "Check the email", "actor": "human", "manual": True}
A1 = {"id": "a1", "text": "Run it", "expect": "ok", "actor": "agent"}
H1 = {"id": "h1", "text": "Look at the page", "expect": "a badge", "actor": "human"}
X1 = {"id": "x1", "text": "Curl the new route", "expect": "200", "actor": "agent"}


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "tip_sha": "a" * 40,
        "live_branch": "main",
        "state": "generated",
        "generated_at": 100.0,
        "steps": [GEN1, GEN2, MINE],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


def _ids(plan) -> list:
    return [s["id"] for s in plan["steps"]]


def _inst(wt, title="sc-1"):
    return SimpleNamespace(
        Title=title,
        GetWorktreePath=lambda: str(wt),
        Branch="feature/sc-1",
        Path="/repo",
        Status=None,
        Program="claude",
    )


# --------------------------------------------------------------------------- #
# 1. An end without a report gives the plan its steps back
# --------------------------------------------------------------------------- #
def _end(how: str) -> dict:
    if how == "cancel":
        return T.cancel_run("sc-1", "couldn't hand the check to sc-1: paused")
    if how == "fail":
        return T.fail_run("sc-1", "sc-1", "boom")
    if how == "give_up":
        return T.mark_due("sc-1", reason="no answer in 2 hours")
    if how == "session_gone":
        T.prune(live_titles=[])
        return T.get("sc-1")
    if how == "closed":
        return T.finish_run("sc-1", [], closed="the check ran out of time")
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["cancel", "fail", "give_up", "session_gone", "closed"])
def test_a_self_verify_that_ends_without_a_report_restores_the_steps(how):
    T.upsert(_plan())
    started = T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    assert _ids(started) == ["m1"]  # the generated ones are held back
    plan = _end(how)
    # The checklist the checker fallback needs is back...
    assert _ids(plan) == ["s1", "s2", "m1"]
    # ...the run is kept and settled blocked, never left "verifying"...
    run = plan["runs"][-1]
    assert run.get("self") and run["outcome"] == "blocked"
    assert "prior_steps" not in run
    assert T.verify_status(plan)["state"] == "blocked"
    # ...and it survives a reload.
    assert _ids(T.get("sc-1")) == ["s1", "s2", "m1"]


def test_steps_the_agent_reported_are_kept_when_its_run_is_cancelled():
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1, H1])
    plan = T.cancel_run("sc-1")
    assert _ids(plan) == ["a1", "h1", "m1"]
    assert plan["runs"][-1]["outcome"] == "blocked"
    assert plan["runs"][-1]["outcome_note"] == T.CANCELLED_NOTE


def test_a_normal_finish_drops_the_snapshot():
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1])
    plan = T.finish_run("sc-1", [dict(A1, result="pass", note="ok")])
    assert _ids(plan) == ["a1", "m1"]
    assert "prior_steps" not in plan["runs"][-1]


@pytest.fixture
def client(monkeypatch, tmp_path):
    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    typed: list = []
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    monkeypatch.setattr(server, "_agent_activity", lambda inst, t: "idle")
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: False)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_note_human_input", lambda t: None)
    monkeypatch.setattr(server, "_note_pane_input", lambda inst, t, text: None)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda t: None)
    monkeypatch.setattr(server, "_current_branch", lambda wt: "feature/sc-1")
    monkeypatch.setattr(server, "_git_head_sha", lambda wt: "c" * 40)
    monkeypatch.setattr(TPD, "_test_plan_intent", lambda title: "")

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
    instances["sc-1"] = _inst(wt)
    c = TestClient(server.app)
    c.server, c.made, c.typed, c.instances, c.wt = server, made, typed, instances, wt
    return c


def test_a_failed_hand_off_keeps_the_steps_for_the_checker_fallback(
    client, monkeypatch
):
    monkeypatch.setattr(
        client.server, "_deliver_walk", lambda title, prompt: ("", "paused")
    )
    T.upsert(_plan())
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409 and r.json()["reason"] == "send_failed"
    plan = T.get("sc-1")
    assert plan["state"] != "running" and _ids(plan) == ["s1", "s2", "m1"]
    run = plan["runs"][-1]
    assert run["outcome"] == "blocked"
    assert "couldn't hand the check to sc-1" in run["outcome_note"]
    # The session closes: the checker fallback has steps to walk, so it is
    # not the dead end "no_steps" (it goes on to look for the repo).
    client.instances.clear()
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.json().get("reason") != "no_steps"


# --------------------------------------------------------------------------- #
# 2. A released run never brings back the run before it
# --------------------------------------------------------------------------- #
def _needs_you_then_reverify() -> None:
    T.upsert(_plan(steps=[A1, H1]))
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1, H1])
    run1 = T.finish_run("sc-1", [dict(A1, result="pass", note="ok"), H1])
    assert run1["runs"][-1]["outcome"] == "needs_you"
    # The re-verify: its agent reports a different step, then the run ends.
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    assert T.adopt_reported_steps("sc-1", [X1]) is not None


@pytest.mark.parametrize("how", ["cancel", "fail", "give_up"])
def test_a_released_reverify_does_not_resurrect_the_old_needs_you(how):
    _needs_you_then_reverify()
    plan = _end(how)
    assert len(plan["runs"]) == 2
    assert plan["runs"][-1]["outcome"] == "blocked"
    v = T.verify_status(plan)
    assert v["state"] == "blocked"
    # Confirm cannot write x1=pass into the old run.
    with pytest.raises(T.NotNeedsYou):
        T.confirm_verify("sc-1", True)
    old = plan["runs"][0]
    assert "x1" not in old["results"] and old["outcome"] == "needs_you"


# --------------------------------------------------------------------------- #
# 3. An automatic message waits its turn in the launch gate
# --------------------------------------------------------------------------- #
@pytest.fixture
def asleep(monkeypatch, tmp_path):
    from backend.web import server

    inst = _inst(tmp_path)
    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": inst})
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda t: True)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda t: None)
    wakes: list = []

    def _ensure_awake(inst, title, why, srv=None, *, auto=False):
        wakes.append(auto)
        return (False, G.WAITING_FOR_MEMORY) if auto else (True, None)

    monkeypatch.setattr(server._sleep, "ensure_awake", _ensure_awake)
    return SimpleNamespace(server=server, inst=inst, wakes=wakes)


def test_the_walk_prompt_wakes_through_the_gate_and_is_queued(asleep):
    told, reason = TPD._deliver_walk("sc-1", T.WALK_MARK + " check it")
    assert asleep.wakes == [True]
    assert (told, reason) == ("queued", "")
    items = Q.get_state("sc-1")["items"]
    assert [i["text"] for i in items] == [T.WALK_MARK + " check it"]


def test_the_close_notice_wakes_through_the_gate(asleep):
    TPD._tell_walk_closed("sc-1", "out of time")
    assert asleep.wakes == [True]
    assert any(
        "this check was closed" in i["text"] for i in Q.get_state("sc-1")["items"]
    )


def test_a_person_s_send_still_wakes_at_once(asleep):
    told, _ = asleep.server._deliver_to_agent(asleep.inst, "sc-1", "hi", boot=True)
    assert asleep.wakes == [False] and told == "queued"


def test_an_unqueueable_auto_text_reports_the_wait(asleep):
    told, reason = asleep.server._deliver_to_agent(
        asleep.inst, "sc-1", "draft", submit=False, auto=True
    )
    assert told is False and reason == G.WAITING_FOR_MEMORY


# --------------------------------------------------------------------------- #
# 4. A verify starts once
# --------------------------------------------------------------------------- #
def test_start_run_is_a_compare_and_set():
    T.upsert(_plan())
    first = T.start_run("sc-1", "sc-1", in_session=True, self_authored=True, expect="")
    with pytest.raises(T.RunBusy):
        T.start_run("sc-1", "sc-1", in_session=True, self_authored=True, expect="")
    assert len(T.get("sc-1")["runs"]) == 1
    # Replacing the run the caller saw (a husk) is allowed.
    again = T.start_run("sc-1", "verify-x", expect=T.run_token(first))
    assert again["run_session"] == "verify-x" and len(again["runs"]) == 2
    # The replaced self-verify gave its steps back before the new run began.
    assert _ids(again) == ["s1", "s2", "m1"]
    assert again["runs"][0]["outcome"] == "blocked"


def test_two_presses_open_one_run_and_send_one_prompt(client, monkeypatch):
    def _slow_head(wt):
        time.sleep(0.3)
        return "c" * 40

    monkeypatch.setattr(client.server, "_git_head_sha", _slow_head)
    T.upsert(_plan())

    async def _both():
        transport = httpx.ASGITransport(app=client.server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await asyncio.gather(
                ac.post("/api/test-plans/sc-1/run", json={}),
                ac.post("/api/test-plans/sc-1/run", json={}),
            )

    r1, r2 = asyncio.run(_both())
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [202, 409], (r1.text, r2.text)
    busy = r1 if r1.status_code == 409 else r2
    assert busy.json()["reason"] == "busy"
    assert len(T.get("sc-1")["runs"]) == 1
    assert len(client.typed) == 1


def test_a_run_opened_meanwhile_keeps_its_results_file(client, monkeypatch):
    """Another server on this state dir starts the run between the route's
    check and its clear: the route stops before deleting that run's file."""
    results = client.wt / T.RESULT_FILE
    results.write_text(json.dumps({"plan": "sc-1", "finished": False}))

    def _head_while_another_starts(wt):
        T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
        return "c" * 40

    monkeypatch.setattr(client.server, "_git_head_sha", _head_while_another_starts)
    T.upsert(_plan())
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409 and r.json()["reason"] == "busy"
    assert results.exists()
    assert len(T.get("sc-1")["runs"]) == 1 and client.typed == []


# --------------------------------------------------------------------------- #
# 5. The liveness pass leaves a check in flight alone
# --------------------------------------------------------------------------- #
def test_mark_due_without_a_reason_leaves_a_running_plan_alone():
    T.upsert(_plan())
    T.start_run("sc-1", "verify-sc-1")
    plan = T.mark_due("sc-1")
    assert plan["state"] == "running" and plan["run_session"] == "verify-sc-1"
    assert not plan["live_at"]
    assert plan["runs"][-1]["outcome"] == "verifying"


def test_the_liveness_pass_does_not_release_a_run_started_meanwhile(monkeypatch):
    """The pass filters a snapshot to "generated", then asks origin; a verify
    started while it asks must not lose its session."""
    T.upsert(_plan(merged_at=1.0))
    snapshot = T.list_plans()
    T.start_run("sc-1", "verify-sc-1")
    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(TPD, "_verify_enabled", lambda: True)
    monkeypatch.setattr(TPD._test_plans, "resolve_live_branch", lambda root: "main")
    monkeypatch.setattr(TPD._test_plans, "resolve_deploy_delay", lambda root: 0.0)
    monkeypatch.setattr(TPD._test_plans, "deploy_ready", lambda plan, delay: True)
    monkeypatch.setattr(TPD, "_notify_test_plan_due", lambda plan: None)
    TPD._check_test_plans_for_liveness(snapshot)
    plan = T.get("sc-1")
    assert plan["state"] == "running" and plan["run_session"] == "verify-sc-1"


# --------------------------------------------------------------------------- #
# 6. A released run's pill lingers from the release
# --------------------------------------------------------------------------- #
def test_a_run_given_up_on_shows_blocked_for_ten_minutes_from_the_release(
    monkeypatch,
):
    clock = [1_000_000.0]
    monkeypatch.setattr(T, "time", SimpleNamespace(time=lambda: clock[0]))
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1, X1])
    T.record_progress("sc-1", [dict(A1, result="pass", note="ok")])
    clock[0] += 2 * 3600 + 60  # the two-hour give-up
    plan = T.mark_due("sc-1", reason="no answer in 2 hours")
    run = plan["runs"][-1]
    assert run["outcome"] == "blocked" and run["outcome_at"] == clock[0]
    assert run["outcome_note"] == "no answer in 2 hours"
    v = T.verify_status(plan, now=clock[0] + 5)
    assert v is not None and v["state"] == "blocked"
    assert v["detail"] == "No answer in 2 hours."
    assert T.verify_status(plan, now=clock[0] + T.VERIFY_SHOW_S + 1) is None


# --------------------------------------------------------------------------- #
# 7. The row index sees a same-size rewrite inside one mtime tick
# --------------------------------------------------------------------------- #
def test_the_row_index_sees_a_same_size_rewrite_in_one_mtime_tick():
    stamp = time.time()

    def _write(outcome: str) -> None:
        T.upsert(
            _plan(
                steps=[A1],
                state="running",
                run_session="sc-1",
                runs=[
                    {
                        "at": 5.0,
                        "session": "sc-1",
                        "in_session": True,
                        "self": True,
                        "results": {},
                        "outcome": outcome,
                        "outcome_at": stamp,
                    }
                ],
            )
        )

    _write("verifying")
    path = T.store_path()
    before = os.stat(path)
    assert T.verify_for_session("sc-1")["state"] == "verifying"
    _write("needs_you")  # same length as "verifying"
    after = os.stat(path)
    assert after.st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # one coarse tick
    assert T.verify_for_session("sc-1")["state"] == "needs_you"
