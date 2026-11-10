"""SELF-VERIFY: Verify is one prompt to the session's own agent.

Owner request 2026-11-10: "verification [should] be a pill like other states
where it says verifying, and just have it be like an agent that comes up with
some actionable testing steps within the session itself and then works on
them … if a human needs to do anything the agent should just tell them.
Humans must validate that validation is complete if there are human steps."

These pin:

* the prompt — no pre-written steps; the agent writes its own steps into the
  results file and walks them, and tells the person about theirs;
* the results file → the outcome: all pass → ``passed``, an agent fail →
  ``failed``, a person's step → ``needs_you``;
* ``POST /api/test-plans/{id}/confirm`` moving ``needs_you`` → passed/failed
  and answering 409 otherwise;
* the row's ``verify`` field (the contract the frontend builds against);
* the flight's Verify stop waiting on ``needs_you`` and advancing on
  ``passed``;
* ``POST /api/instances/{title}/verify`` making the plan and starting it.

Isolation: the root conftest points the plans store, prompt queue, held and
sleep files at tmp; tmux and the agent probes are pinned on the server.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import snapshot as snapshot_mod
from backend.web.core import test_plans as T
from backend.web.core import test_plans_driver as TPD

NOW = 1_800_000_000.0

GENERATED = "Open the settings dialog and count the toggles"


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
        "origin": "manual",
        "generated_at": 100.0,
        "steps": [
            # A generated step (the old author's): dropped by a self-verify.
            {"id": "s1", "text": GENERATED, "expect": "3", "actor": "agent"},
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


def _inst(wt, branch="feature/sc-1", path="/repo"):
    return SimpleNamespace(
        Title="sc-1",
        GetWorktreePath=lambda: str(wt),
        Branch=branch,
        Path=path,
        Status=None,
        Program="claude",
    )


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
def test_the_prompt_has_no_steps_and_asks_the_agent_to_write_and_walk_its_own():
    p = T.build_self_verify_prompt(_plan(), notes="log in as qa")
    assert p.startswith(T.WALK_MARK)
    assert "verify the work you just did" in p
    # No pre-written steps: the generated one is not handed over.
    assert GENERATED not in p and "Steps:" not in p
    # It writes its own, to the results file, and walks them.
    assert T.RESULT_FILE in p
    assert "Come up with" in p and "NOT the unit tests" in p
    assert "track them however you track tasks" in p
    assert "TODO LIST" not in p
    assert '"actor": "agent"' in p and '"actor": "human"' in p
    assert '"result": "pass", "fail" or "blocked"' in p
    # A person's step: it TELLS the person what to do and what to look for.
    assert "TELL THE PERSON" in p
    assert '"finished": true' in p
    assert "Notes for this check: log in as qa" in p
    # The example's ids are placeholders, never a copyable "s1".
    example = json.loads(p[p.index("{") :])
    assert all(s["id"] == "<step id>" for s in example["steps"])
    assert example["plan"] == "sc-1"


def test_the_prompt_keeps_a_persons_own_steps():
    plan = _plan(
        steps=[
            {"id": "m1", "text": "Check the email", "actor": "human", "manual": True}
        ]
    )
    p = T.build_self_verify_prompt(plan)
    assert "m1 [human] Check the email" in p


def test_the_prompt_names_a_deployment_under_test():
    p = T.build_self_verify_prompt(
        _plan(), where="sandbox", target="https://sandbox.example"
    )
    assert "THE sandbox ENVIRONMENT" in p and "https://sandbox.example" in p
    assert "Do not start a local copy" in p


# --------------------------------------------------------------------------- #
# The results file → the outcome
# --------------------------------------------------------------------------- #
def _start(monkeypatch, wt, **kw):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(wt)})
    monkeypatch.setattr(TPD, "_verify_window_gone", lambda t, now=None: False)
    monkeypatch.setattr(TPD, "_verify_run_trees", lambda plan: ("", ""))
    announced: list = []
    monkeypatch.setattr(TPD, "_announce_test_plan_checked", announced.append)
    T.upsert(_plan(**kw))
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    return announced


def _report(wt, steps, finished=True):
    (wt / T.RESULT_FILE).write_text(
        json.dumps({"plan": "sc-1", "finished": finished, "steps": steps})
    )
    TPD._poll_running_test_plans(T.list_plans())
    return T.get("sc-1")


A1 = {"id": "s1", "text": "Run it", "expect": "ok", "actor": "agent"}
A2 = {"id": "s2", "text": "Curl it", "expect": "200", "actor": "agent"}
H1 = {"id": "s3", "text": "Look at the page", "expect": "a badge", "actor": "human"}


def test_start_drops_the_generated_steps_and_opens_a_verifying_run(
    monkeypatch, tmp_path
):
    _start(monkeypatch, tmp_path)
    plan = T.get("sc-1")
    assert plan["steps"] == []
    run = plan["runs"][-1]
    assert run["self"] is True and run["outcome"] == "verifying"
    assert T.verify_status(plan)["state"] == "verifying"


def test_progress_adopts_the_agents_steps_and_stays_verifying(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path)
    plan = _report(
        tmp_path, [dict(A1, result="pass", note="exit 0"), A2, H1], finished=False
    )
    assert [s["id"] for s in plan["steps"]] == ["s1", "s2", "s3"]
    assert [s["actor"] for s in plan["steps"]] == ["agent", "agent", "human"]
    assert plan["state"] == "running"
    v = T.verify_status(plan)
    assert v["state"] == "verifying"
    assert v["steps"] == {"total": 3, "agent_done": 1, "human": 1}


def test_all_agent_steps_passed_and_no_person_step_is_passed(monkeypatch, tmp_path):
    announced = _start(monkeypatch, tmp_path)
    plan = _report(
        tmp_path,
        [dict(A1, result="pass", note="exit 0"), dict(A2, result="pass", note="200")],
    )
    assert plan["runs"][-1]["outcome"] == "passed"
    assert plan["state"] != "running" and plan["run_session"] == ""
    assert T.verify_status(plan)["state"] == "passed"
    assert announced and not (tmp_path / T.RESULT_FILE).exists()


def test_an_agent_step_that_failed_is_failed(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path)
    plan = _report(
        tmp_path,
        [
            dict(A1, result="pass", note="exit 0"),
            dict(A2, result="fail", note="got 500"),
            H1,
        ],
    )
    assert plan["runs"][-1]["outcome"] == "failed"
    v = T.verify_status(plan)
    assert v["state"] == "failed"
    assert v["failed_steps"] == [{"id": "s2", "text": "Curl it", "note": "got 500"}]
    assert v["detail"] == "1 step failed: Curl it."


def test_a_persons_step_is_needs_you_and_records_no_answer(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path)
    plan = _report(
        tmp_path,
        [
            dict(A1, result="pass", note="exit 0"),
            dict(A2, result="pass", note="200"),
            H1,
        ],
    )
    run = plan["runs"][-1]
    assert run["outcome"] == "needs_you"
    assert "s3" not in run["results"]  # theirs to confirm, not "blocked"
    v = T.verify_status(plan)
    assert v["state"] == "needs_you"
    assert v["human_steps"] == [
        {"id": "s3", "text": "Look at the page", "expect": "a badge"}
    ]


def test_an_agent_step_it_could_not_settle_waits_on_a_person(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path)
    plan = _report(tmp_path, [dict(A1, result="blocked", note="no :3000")])
    assert plan["runs"][-1]["outcome"] == "needs_you"
    assert T.verify_status(plan)["human_steps"][0]["id"] == "s1"


def test_no_steps_at_all_is_blocked_never_passed(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path)
    plan = _report(tmp_path, [])
    assert plan["runs"][-1]["outcome"] == "blocked"
    assert T.verify_status(plan)["state"] == "blocked"


def test_who_you_asks_a_person_even_when_everything_passed(monkeypatch, tmp_path):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {"sc-1": _inst(tmp_path)})
    monkeypatch.setattr(TPD, "_verify_window_gone", lambda t, now=None: False)
    monkeypatch.setattr(TPD, "_verify_run_trees", lambda plan: ("", ""))
    monkeypatch.setattr(TPD, "_announce_test_plan_checked", lambda p: None)
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True, confirm=True)
    plan = _report(tmp_path, [dict(A1, result="pass", note="exit 0")])
    assert plan["runs"][-1]["outcome"] == "needs_you"


def test_a_closed_check_is_blocked_with_the_reason(monkeypatch, tmp_path):
    import asyncio

    _start(monkeypatch, tmp_path)
    _report(tmp_path, [A1], finished=False)
    monkeypatch.setattr(TPD, "_tell_walk_closed", lambda t, r: None)
    asyncio.run(TPD.close_run("sc-1", "not checked: the checker stopped answering"))
    plan = T.get("sc-1")
    assert plan["runs"][-1]["outcome"] == "blocked"
    v = T.verify_status(plan)
    assert v["state"] == "blocked"
    assert v["detail"] == "Not checked: the checker stopped answering."


# --------------------------------------------------------------------------- #
# POST /api/test-plans/{id}/confirm
# --------------------------------------------------------------------------- #
def _needs_you(plan_id="sc-1") -> dict:
    T.upsert(_plan(plan_id, steps=[A1, H1]))
    T.start_run(plan_id, plan_id, in_session=True, self_authored=True)
    T.adopt_reported_steps(plan_id, [A1, H1])
    return T.finish_run(plan_id, [dict(A1, result="pass", note="ok"), H1])


@pytest.fixture
def api():
    from backend.web import server

    return TestClient(server.app)


def test_confirm_verified_moves_needs_you_to_passed(api):
    assert _needs_you()["runs"][-1]["outcome"] == "needs_you"
    r = api.post("/api/test-plans/sc-1/confirm", json={"verified": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    run = body["plan"]["runs"][-1]
    assert run["outcome"] == "passed"
    assert run["results"]["s3"]["result"] == "pass"
    assert run["results"]["s3"]["by"] == "human"
    assert T.verify_status(T.get("sc-1"))["state"] == "passed"


def test_confirm_not_verified_moves_needs_you_to_failed_with_the_note(api):
    _needs_you()
    r = api.post(
        "/api/test-plans/sc-1/confirm",
        json={"verified": False, "note": "the badge is missing"},
    )
    assert r.status_code == 200, r.text
    plan = T.get("sc-1")
    run = plan["runs"][-1]
    assert run["outcome"] == "failed"
    assert run["outcome_note"] == "the badge is missing"
    assert run["results"]["s3"] == dict(
        run["results"]["s3"], result="fail", note="the badge is missing", by="human"
    )
    v = T.verify_status(plan)
    assert v["state"] == "failed"
    assert v["failed_steps"][0]["note"] == "the badge is missing"
    # The failure reads like any other (Fix…, the flight's halt).
    assert [f["step"]["id"] for f in T.failed_steps(plan)] == ["s3"]


def test_confirm_is_409_unless_the_check_waits_on_a_person(api):
    T.upsert(_plan())
    # No check at all.
    r = api.post("/api/test-plans/sc-1/confirm", json={"verified": True})
    assert r.status_code == 409 and r.json()["error"]
    # Still verifying.
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    r = api.post("/api/test-plans/sc-1/confirm", json={"verified": True})
    assert r.status_code == 409
    # Already settled.
    _needs_you()
    assert (
        api.post("/api/test-plans/sc-1/confirm", json={"verified": True}).status_code
        == 200
    )
    r = api.post("/api/test-plans/sc-1/confirm", json={"verified": False})
    assert r.status_code == 409
    assert T.get("sc-1")["runs"][-1]["outcome"] == "passed"


def test_confirm_refuses_a_bad_body_and_an_unknown_plan(api):
    _needs_you()
    assert api.post("/api/test-plans/sc-1/confirm", json={}).status_code == 400
    assert (
        api.post("/api/test-plans/sc-1/confirm", json={"verified": "yes"}).status_code
        == 400
    )
    assert (
        api.post("/api/test-plans/nope/confirm", json={"verified": True}).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# The row's ``verify`` (the contract)
# --------------------------------------------------------------------------- #
CONTRACT_KEYS = {
    "state",
    "plan",
    "steps",
    "human_steps",
    "failed_steps",
    "detail",
    "at",
    "progress_at",
    "current",
}


def test_the_row_carries_verify_in_the_contracts_shape(monkeypatch, tmp_path):
    from backend.session.instance import InstanceOptions, new_instance
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    inst = new_instance(
        InstanceOptions(title="sc-1", path=str(tmp_path), program="bash")
    )
    other = new_instance(
        InstanceOptions(title="other", path=str(tmp_path), program="bash")
    )
    _needs_you()
    # The tick's row builder (the /api/instances snapshot), cheap path.
    row = server._session_snapshot_cheap(inst, {})
    v = row["verify"]
    assert snapshot_mod._row_verify("sc-1") == v
    assert set(v) == CONTRACT_KEYS
    assert v["state"] == "needs_you" and v["plan"] == "sc-1"
    assert v["steps"] == {"total": 2, "agent_done": 1, "human": 1}
    assert v["human_steps"] == [
        {"id": "s3", "text": "Look at the page", "expect": "a badge"}
    ]
    assert v["failed_steps"] == []
    assert isinstance(v["detail"], str) and v["detail"].endswith(".")
    assert "\n" not in v["detail"]
    assert isinstance(v["at"], float) and v["at"] > 0
    # No verify open or recent → null.
    assert server._session_snapshot_cheap(other, {})["verify"] is None
    # A pending row (no session yet) carries the key too.
    monkeypatch.setattr(server._pending, "rows", lambda: [{"title": "new"}])
    assert server._pending_rows()[0]["verify"] is None


def test_a_settled_pill_shows_for_ten_minutes_then_null():
    _needs_you()
    plan = T.confirm_verify("sc-1", True)
    at = plan["runs"][-1]["outcome_at"]
    assert T.verify_status(plan, now=at + 599)["state"] == "passed"
    assert T.verify_status(plan, now=at + 601) is None
    # needs_you never times out: it waits on a person.
    _needs_you("sc-2")
    plan2 = T.get("sc-2")
    assert T.verify_status(plan2, now=plan2["runs"][-1]["at"] + 86400)["state"] == (
        "needs_you"
    )


def test_a_run_without_an_outcome_shows_no_pill():
    """A run written before the pill existed never grows one."""
    T.upsert(
        _plan(
            state="done",
            runs=[{"at": 5.0, "session": "sc-1", "results": {}, "by": "agent"}],
        )
    )
    assert T.verify_status(T.get("sc-1")) is None
    assert T.verify_for_session("sc-1") is None


def test_a_released_run_reads_blocked_with_the_plans_sentence(monkeypatch):
    T.upsert(_plan())
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1])
    T.record_progress("sc-1", [dict(A1, result="pass", note="ok")])
    T.mark_due("sc-1", reason="The verify session's agent window is gone. More.")
    v = T.verify_status(T.get("sc-1"))
    assert v["state"] == "blocked"
    assert v["detail"] == "The verify session's agent window is gone."


# --------------------------------------------------------------------------- #
# The flight's Verify stop
# --------------------------------------------------------------------------- #
def _fly(**config):
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "verify", "config": config}]}, now=NOW
    )
    f = F.take_off(plan, session="s", now=NOW)
    a = F.next_action(f, {"session": _sess()}, NOW)
    return F.apply(f, dict(a, detail={"plan": "p"}), NOW)


def _sess():
    return {"present": True, "status": "running", "activity": "idle"}


def _facts(**kw):
    base = {
        "plan": "p",
        "branch": "b",
        "state": "generated",
        "steps": [],
        "failed": [],
        "live": False,
        "live_branch": "main",
        "latest_run_at": 0.0,
        "progress": {},
        "checker": {},
        "run_budget_s": 1800.0,
        "outcome": "",
        "confirm_items": [],
    }
    base.update(kw)
    return base


def _next(f, facts, at):
    return F.next_action(f, {"session": _sess(), "verify": facts}, at)


@pytest.fixture
def flights_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))


def test_the_stop_starts_the_check_waits_on_needs_you_and_advances_on_passed(
    flights_dir,
):
    f = _fly(who="both")
    # No check from this attempt yet: start the self-verify.
    a = _next(f, _facts(), NOW + 1)
    assert a["action"] == "run" and a["phase"] == "agent_run"
    f = F.apply(
        f,
        dict(a, detail=dict(a["detail"], run_started=True, run_started_at=NOW + 1)),
        NOW + 1,
    )
    # Verifying: it waits.
    running = _facts(state="running", outcome="verifying", latest_run_at=NOW + 2)
    assert _next(f, running, NOW + 30)["action"] == "wait"
    # needs_you: it parks on the person — never a silent pass.
    need = _facts(
        outcome="needs_you",
        latest_run_at=NOW + 2,
        confirm_items=["Look at the page — look for: a badge"],
    )
    a = _next(f, need, NOW + 60)
    assert a["action"] == "you" and a["detail"]["ask"] == "confirm"
    assert a["detail"]["items"] == ["Look at the page — look for: a badge"]
    f = F.apply(f, a, NOW + 60)
    assert f["state"] == "waiting_you"
    assert _next(f, need, NOW + 120)["action"] == "wait"
    # The person confirmed: passed → done.
    assert _next(f, dict(need, outcome="passed"), NOW + 180)["action"] == "done"


def test_the_stop_halts_on_failed_with_the_failing_steps(flights_dir):
    f = _fly(who="agent")
    failed = _facts(
        outcome="failed", latest_run_at=NOW + 2, failed=["Curl it", "Run it"]
    )
    a = _next(f, failed, NOW + 60)
    assert a["action"] == "fail"
    assert a["reason"] == "Failed · 2: Curl it; Run it"


def test_a_check_already_running_is_waited_on_not_restarted(flights_dir):
    f = _fly()
    running = _facts(state="running", outcome="verifying", latest_run_at=NOW - 50)
    a = _next(f, running, NOW + 5)
    assert a["action"] == "wait" and a["detail"]["adopted_run_at"] == NOW - 50
    f = F.apply(f, a, NOW + 5)
    done = _facts(outcome="passed", latest_run_at=NOW - 50)
    assert _next(f, done, NOW + 60)["action"] == "done"


def test_an_older_check_does_not_count_for_this_attempt(flights_dir):
    f = _fly()
    old = _facts(outcome="passed", latest_run_at=NOW - 500)
    a = _next(f, old, NOW + 5)
    assert a["action"] == "run" and a["phase"] == "agent_run"


def test_done_on_the_confirm_perch_confirms_the_check(flights_dir, monkeypatch):
    _needs_you("p")
    plan = F.save_plan({"name": "P", "steps": [{"kind": "verify"}]}, now=NOW)
    f = F.take_off(plan, session="s", now=NOW)
    need = _facts(outcome="needs_you", latest_run_at=NOW + 2, confirm_items=["x"])

    def park(x):
        start = F.next_action(x, {"session": _sess()}, NOW)
        F.apply(x, dict(start, detail={"plan": "p"}), NOW)
        F.apply(x, _next(x, need, NOW + 60), NOW + 60)

    f = F.mutate(f["id"], park)
    assert D._confirm_perch(f, "s1") == "p"
    monkeypatch.setattr(D, "wake", lambda: None)
    D.operate(f["id"], "done", "s1", "looks right")
    run = T.get("p")["runs"][-1]
    assert run["outcome"] == "passed" and run["outcome_note"] == "looks right"


def test_verify_obs_carries_the_outcome_and_what_to_confirm(flights_dir, monkeypatch):
    _needs_you("p")
    monkeypatch.setattr(D, "_verify_env", lambda flight, where: (None, False))
    f = _fly()
    obs = D._verify_obs(f, {"detail": {"plan": "p"}}, {})
    assert obs["outcome"] == "needs_you"
    assert obs["confirm_items"] == ["Look at the page — look for: a badge"]


def test_the_stop_starts_with_the_plan_not_a_generation(monkeypatch, flights_dir):
    asked: list = []
    srv = SimpleNamespace(
        _ensure_verify_plan=lambda title: asked.append(title) or ("s", "")
    )
    monkeypatch.setattr(D, "_server", lambda: srv)
    plan = F.save_plan({"name": "P", "steps": [{"kind": "verify"}]}, now=NOW)
    f = F.take_off(plan, session="s", now=NOW)
    assert D._ensure_checklist(f) == {"plan": "s"}
    assert asked == ["s"]
    srv._ensure_verify_plan = lambda title: ("", "this session isn't on a branch")
    with pytest.raises(D._StepFailed, match="isn't on a branch"):
        D._ensure_checklist(f)


def test_who_you_starts_the_run_asking_for_a_persons_sign_off(monkeypatch):
    import asyncio

    got: list = []

    async def run(pid, req, payload):
        got.append(payload)
        return SimpleNamespace(status_code=202, body=b"{}")

    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(run_test_plan=run))
    asyncio.run(D._start_verify_run("p", "branch", "", "agent", confirm=True))
    asyncio.run(D._start_verify_run("p", "branch", "", "agent"))
    assert got[0]["confirm"] is True and "confirm" not in got[1]


# --------------------------------------------------------------------------- #
# The routes that start it
# --------------------------------------------------------------------------- #
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
    c = TestClient(server.app)
    c.made, c.typed, c.instances, c.wt = made, typed, instances, wt
    return c


def test_instance_verify_creates_the_plan_and_starts_it(client):
    client.instances["sc-1"] = _inst(client.wt)
    assert T.get("sc-1") is None
    r = client.post("/api/instances/sc-1/verify", json={"notes": "use :3000"})
    assert r.status_code == 202, r.text
    assert r.json()["started"] is True and r.json()["plan"] == "sc-1"
    plan = T.get("sc-1")
    assert plan["branch"] == "feature/sc-1" and plan["origin"] == "manual"
    assert plan["state"] == "running" and plan["run_session"] == "sc-1"
    run = plan["runs"][-1]
    assert run["self"] is True and run["in_session"] is True
    assert plan["tip_sha"] == "c" * 40
    # One prompt, into the session's own agent: verify the work you just did.
    assert client.made == []
    assert len(client.typed) == 1
    prompt = client.typed[0][1]
    assert prompt.startswith(T.WALK_MARK) and "verify the work you just did" in prompt
    assert "Notes for this check: use :3000" in prompt
    assert T.verify_for_session("sc-1")["state"] == "verifying"


def test_instance_verify_reuses_the_sessions_plan(client):
    client.instances["sc-1"] = _inst(client.wt)
    T.upsert(_plan())
    r = client.post("/api/instances/sc-1/verify", json={})
    assert r.status_code == 202, r.text
    assert r.json()["plan"] == "sc-1"
    assert len(T.list_plans()) == 1
    # The generated steps never reached the agent, and are gone.
    assert GENERATED not in client.typed[0][1]
    assert T.get("sc-1")["steps"] == []
    # Already verifying: the run route's busy answer.
    r = client.post("/api/instances/sc-1/verify", json={})
    assert r.status_code == 409 and r.json()["reason"] == "busy"


def test_instance_verify_404s_for_no_session(client):
    assert client.post("/api/instances/ghost/verify", json={}).status_code == 404


def test_the_run_route_self_verifies_a_plan_with_no_steps(client):
    """Starting Verify no longer needs a written plan: the open session's
    agent writes its own steps."""
    client.instances["sc-1"] = _inst(client.wt)
    T.upsert(_plan(steps=[]))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "branch"})
    assert r.status_code == 202, r.text
    assert r.json()["in_session"] is True and r.json()["started"] is True
    assert T.get("sc-1")["runs"][-1]["self"] is True


def test_the_run_route_still_needs_steps_for_a_fallback_checker(client):
    """The session is gone: a checker can't verify work it didn't do, so a
    plan with no steps is still refused there."""
    T.upsert(_plan(steps=[]))
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409 and r.json()["reason"] == "no_steps"
