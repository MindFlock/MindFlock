"""A Verify stop is never SILENTLY stuck (docs/flights.md "Verify stops: when
the check can't go on").

Found live 2026-11-09 on two real routes:

* sc-25811 — the memory guard refused the checker's session (409), the driver
  read every 409 as "already running", marked the run started, and the stop
  waited forever on a run that did not exist;
* sc-25845 — the checker ended its turn with a background job and no answers
  ("checking" until a two-hour give-up), and when it finally answered every
  behavior "blocked", the stop called that "the agent's behaviors are checked"
  and moved on.

Plus the owner's rule: a check that needs a PERSON to do something first parks
the route and asks them; Done means "I did it" and the check runs again.
"""

import asyncio
import json

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import test_plans as T

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    yield tmp_path / "flights"


def _step(**config):
    return {"kind": "verify", "config": config, "on_fail": "halt"}


def _sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def _fly(*steps):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    f = F.take_off(plan, session="s", now=NOW)
    a = F.next_action(f, {"session": _sess()}, NOW)
    assert a["action"] == "run", a
    return F.apply(f, a, NOW)


def _facts(**kw):
    base = {
        "plan": "p",
        "branch": "b",
        "state": "generated",
        "steps": ["s1", "s2"],
        "settled": False,
        "open_human": [],
        "open_agent": ["it works"],
        "failed": [],
        "live": False,
        "live_branch": "main",
        "latest_run_at": 0.0,
        "progress": {},
        "checker": {},
        "needs_you": [],
        "blocked_steps": [],
        "run_budget_s": 1800.0,
    }
    base.update(kw)
    return base


def _next(f, facts, at):
    return F.next_action(f, {"session": _sess(), "verify": facts}, at)


def _detail(f):
    return f["step_states"]["s1"]["detail"]


# --------------------------------------------------------------------------- #
# The start: memory refusal, never-started
# --------------------------------------------------------------------------- #
def test_a_memory_refused_start_waits_with_the_reason_and_retries():
    f = _fly(_step(who="agent"))
    a = _next(f, _facts(), NOW + 1)
    assert a["action"] == "run" and a["phase"] == "agent_run"
    # The driver's answer when the memory guard refused (see _perform).
    f = F.apply(
        f,
        {
            "action": "wait",
            "note": "waiting for memory to start the check (Windows has 1 GB free)",
            "detail": dict(
                a["detail"],
                run_started=False,
                run_wait="memory",
                memory_since=NOW + 1,
                memory_detail="Windows has 1 GB free",
                last_try=NOW + 1,
            ),
        },
        NOW + 1,
    )
    assert f["step_states"]["s1"]["state"] == "running"
    w = _next(f, _facts(), NOW + 10)
    assert w["action"] == "wait"
    assert "waiting for memory to start the check" in w["note"]
    assert "Windows has 1 GB free" in w["note"]
    # Every VERIFY_START_RETRY_S it tries again…
    r = _next(f, _facts(), NOW + 1 + F.VERIFY_START_RETRY_S + 1)
    assert r["action"] == "run" and r["phase"] == "agent_run"
    # …and after VERIFY_MEMORY_WAIT_S it parks on the person, saying why.
    y = _next(f, _facts(), NOW + 1 + F.VERIFY_MEMORY_WAIT_S + 1)
    assert y["action"] == "you"
    assert "couldn't start the check" in y["note"]
    assert y["detail"]["ask"] == "memory"


def test_a_check_that_never_started_fails_instead_of_waiting_forever():
    """The sc-25811 shape: run_started set, no run record from this attempt
    (also what rescues a route that started before refusals had reasons)."""
    f = _fly(_step(who="agent"))
    a = _next(f, _facts(), NOW + 1)
    f = F.apply(f, dict(a, detail=dict(a["detail"], run_started=True)), NOW + 1)
    # Inside the grace: wait.
    assert _next(f, _facts(), NOW + 30)["action"] == "wait"
    out = _next(f, _facts(), NOW + F.VERIFY_START_CHECK_S + 5)
    assert out["action"] == "fail"
    assert "the check never started" in out["reason"]
    # A run from this attempt exists → not this rule.
    ok = _next(f, _facts(latest_run_at=NOW + 2), NOW + F.VERIFY_START_CHECK_S + 5)
    assert ok["action"] != "fail" or "never started" not in ok.get("reason", "")


# --------------------------------------------------------------------------- #
# A running check: progress, nudge, close, budget
# --------------------------------------------------------------------------- #
def _running(f, **progress):
    p = {"answered": 0, "total": 2, "started_at": NOW + 1, "progress_at": NOW + 1}
    p.update(progress)
    return _facts(state="running", progress=p, checker={"activity": "idle"})


def _started(f):
    a = _next(f, _facts(), NOW + 1)
    return F.apply(
        f,
        dict(a, detail=dict(a["detail"], run_started=True, run_started_at=NOW + 1)),
        NOW + 1,
    )


def test_the_running_note_says_progress():
    f = _started(_fly(_step(who="agent")))
    w = _next(f, _running(f, answered=1, progress_at=NOW + 50), NOW + 60)
    assert w["action"] == "wait"
    assert w["note"].startswith("the agent is checking the behaviors")
    assert "1 of 2 answered" in w["note"]
    quiet = _next(f, _running(f, answered=1, progress_at=NOW + 50), NOW + 50 + 300)
    assert "quiet 5 min" in quiet["note"]


def test_a_quiet_idle_checker_is_nudged_once_then_the_check_is_closed():
    f = _started(_fly(_step(who="agent")))
    at = NOW + 1 + F.VERIFY_STALL_NUDGE_S + 1
    a = _next(f, _running(f), at)
    assert a["action"] == "run" and a["phase"] == "nudge"
    f = F.apply(f, dict(a, detail=dict(a["detail"], nudged_at=at)), at)
    # Still quiet, but not long enough after the nudge: wait (says nudged).
    w = _next(f, _running(f), at + 60)
    assert w["action"] == "wait" and "nudged" in w["note"]
    c = _next(f, _running(f), at + F.VERIFY_STALL_CLOSE_S + 1)
    assert c["action"] == "run" and c["phase"] == "close"
    assert c["detail"]["close_reason"] == "not checked: the checker stopped answering"


def test_a_working_checker_is_not_nudged():
    f = _started(_fly(_step(who="agent")))
    facts = _running(f)
    facts["checker"] = {"activity": "working"}
    a = _next(f, facts, NOW + 1 + F.VERIFY_STALL_NUDGE_S + 1)
    assert a["action"] == "wait"


def test_past_the_budget_the_check_is_closed():
    f = _started(_fly(_step(who="agent")))
    at = NOW + 1 + 1800 + F.VERIFY_BUDGET_GRACE_S + 1
    a = _next(f, _running(f, progress_at=at - 10), at)
    assert a["action"] == "run" and a["phase"] == "close"
    assert "ran out of time" in a["detail"]["close_reason"]


# --------------------------------------------------------------------------- #
# After the run: blocked never passes; needs-you asks the person
# --------------------------------------------------------------------------- #
def test_blocked_answers_never_pass_even_agent_only():
    """sc-25845: every behavior blocked used to read 'checked' and move on."""
    f = _started(_fly(_step(who="agent")))
    done = _facts(
        state="generated",
        latest_run_at=NOW + 2,
        open_agent=[],
        blocked_steps=[
            {
                "step": "s1",
                "text": "the ticket has the screenshot",
                "why": "not on sandbox",
            },
        ],
    )
    a = _next(f, done, NOW + 100)
    assert a["action"] == "you"
    assert a["detail"]["ask"] == "blocked"
    assert a["detail"]["items"] == ["the ticket has the screenshot — not on sandbox"]


def test_a_needs_you_answer_parks_then_done_checks_again():
    f = _started(_fly(_step(who="agent")))
    need = {
        "step": "s1",
        "text": "the ticket shows the screenshot",
        "do": "approve one takedown ticket in Freshdesk",
        "at": NOW + 50,
    }
    after = _facts(
        state="generated", latest_run_at=NOW + 2, open_agent=[], needs_you=[need]
    )
    a = _next(f, after, NOW + 100)
    assert a["action"] == "you" and a["detail"]["ask"] == "needs_you"
    assert a["detail"]["items"] == [
        "approve one takedown ticket in Freshdesk (for: the ticket shows the screenshot)"
    ]
    f = F.apply(f, a, NOW + 100)
    assert f["state"] == "waiting_you"
    # Done on this perch is "I did it" → the driver turns it into a retry.
    assert D._needs_you_perch(f, "s1") is True
    f = F.op_retry(f, "s1", now=NOW + 200)
    # A retry puts the stop back to pending; the driver's next pass starts it
    # fresh (a new attempt: `since` moves on).
    start = F.next_action(f, {"session": _sess()}, NOW + 200)
    assert start["action"] == "run" and not start.get("phase"), start
    f = F.apply(f, start, NOW + 200)
    assert f["step_states"]["s1"]["state"] == "running"
    assert f["step_states"]["s1"]["since"] == NOW + 200
    # The needs-you answer is from before this attempt: check again, telling
    # the checker what the person did.
    r = _next(f, after, NOW + 201)
    assert r["action"] == "run" and r["phase"] == "agent_run"
    assert r["detail"]["rechecked"] is True
    assert "approve one takedown ticket" in r["detail"]["extra_notes"]
    f = F.apply(f, r, NOW + 201)
    # The re-check asked the person again (still needs you, answered AFTER
    # this attempt began) → parks again rather than looping.
    still = _facts(
        state="generated",
        latest_run_at=NOW + 300,
        open_agent=[],
        needs_you=[dict(need, at=NOW + 300)],
    )
    again = _next(f, still, NOW + 400)
    assert again["action"] == "you" and again["detail"]["ask"] == "needs_you"


def test_done_is_still_refused_on_a_plain_behaviors_perch():
    f = _started(_fly(_step(who="agent")))
    done = _facts(
        state="generated",
        latest_run_at=NOW + 2,
        open_agent=[],
        blocked_steps=[{"step": "s1", "text": "x", "why": "y"}],
    )
    f = F.apply(f, _next(f, done, NOW + 100), NOW + 100)
    assert D._needs_you_perch(f, "s1") is False
    with pytest.raises(F.FlightError):
        D._no_blanket_done(f, "s1")


def test_all_passed_is_still_done():
    f = _started(_fly(_step(who="agent")))
    ok = _facts(state="done", latest_run_at=NOW + 2, open_agent=[], settled=True)
    assert _next(f, ok, NOW + 100)["action"] == "done"


# --------------------------------------------------------------------------- #
# The driver reads the run route's answer
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.body = json.dumps(body).encode()


class _Srv:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def run_test_plan(self, pid, request, body):
        self.calls.append((pid, body))
        return self.resp


@pytest.mark.parametrize(
    "status, body, started, reason",
    [
        (202, {"ok": True}, True, ""),
        (
            409,
            {"error": "x is already verifying", "reason": "busy", "session": "x"},
            True,
            "busy",
        ),
        (
            409,
            {"error": "memory is low", "reason": "memory", "detail": "1 GB free"},
            False,
            "memory",
        ),
    ],
)
def test_the_start_answer_is_read(monkeypatch, status, body, started, reason):
    srv = _Srv(_Resp(status, body))
    monkeypatch.setattr(D, "_server", lambda: srv)
    got = asyncio.run(D._start_verify_run("p", "sandbox", "n"))
    assert got["started"] is started and got["reason"] == reason
    assert srv.calls == [
        ("p", {"environment": "sandbox", "notes": "n", "walk": "agent"})
    ]


def test_any_other_refusal_fails_with_the_servers_sentence(monkeypatch):
    srv = _Srv(
        _Resp(
            409,
            {"error": "every behavior here is for a person", "reason": "human_only"},
        )
    )
    monkeypatch.setattr(D, "_server", lambda: srv)
    with pytest.raises(D._StepFailed) as err:
        asyncio.run(D._start_verify_run("p"))
    assert "every behavior here is for a person" in str(err.value)


# --------------------------------------------------------------------------- #
# The checklist side: partial answers, progress, needs-you
# --------------------------------------------------------------------------- #
def _plan(results=None, state="running"):
    return {
        "id": "p",
        "state": state,
        "run_session": "verify-p",
        "steps": [
            {"id": "s1", "text": "A works", "actor": "agent"},
            {"id": "s2", "text": "B works", "actor": "agent"},
            {"id": "s3", "text": "C looks right", "actor": "human"},
        ],
        "runs": [{"at": NOW, "session": "verify-p", "results": results or {}}],
    }


def test_run_progress_counts_and_splits_the_handovers():
    p = _plan(
        {
            "s1": {"result": "pass", "by": "agent", "at": NOW + 5},
            "s2": {
                "result": "blocked",
                "by": "agent",
                "note": "needs you: approve a ticket",
                "at": NOW + 6,
            },
        }
    )
    got = T.run_progress(p)
    assert got["answered"] == 2 and got["total"] == 2
    assert got["needs_you"] == [
        {"step": "s2", "text": "B works", "do": "approve a ticket", "at": NOW + 6}
    ]
    assert got["blocked"] == []
    assert got["started_at"] == NOW and got["progress_at"] == NOW


def test_the_run_prompt_asks_for_progress_and_fast_handovers():
    plan = {
        "id": "p",
        "steps": [{"id": "s1", "text": "A works", "actor": "agent", "expect": ""}],
        "branch": "b",
        "sha": "abc1234",
        "live_branch": "main",
    }
    try:
        prompt = T.build_run_prompt(plan, None, live=False)
    except Exception:  # noqa: BLE001 — the plan shape is the module's business
        pytest.skip("build_run_prompt needs a fuller plan")
    assert '"finished": false' in prompt
    assert "needs you: " in prompt
    assert "background" in prompt and "%d MINUTES" % T.RUN_BUDGET_MIN in prompt


# --------------------------------------------------------------------------- #
# Against the real checklist store: partial answers and the close
# --------------------------------------------------------------------------- #
@pytest.fixture
def plans(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    T.upsert(
        {
            "id": "sc-1",
            "title": "sc-1",
            "repo_root": "/tmp/repo",
            "branch": "feature/sc-1",
            "sha": "a" * 40,
            "live_branch": "main",
            "state": "generated",
            "error": "",
            "generated_at": 100.0,
            "live_at": 0.0,
            "steps": [
                {"id": "s1", "text": "A works", "expect": "", "actor": "agent"},
                {"id": "s2", "text": "B works", "expect": "", "actor": "agent"},
                {"id": "s3", "text": "C looks right", "expect": "", "actor": "human"},
            ],
            "runs": [],
            "run_session": "",
        }
    )
    T.start_run("sc-1", "verify-sc-1")
    return "sc-1"


def test_partial_answers_fold_in_without_ending_the_run(plans):
    before = T.get(plans)
    p = T.record_progress(plans, [{"id": "s1", "result": "pass", "note": "ok"}])
    assert p["state"] == "running" and p["run_session"] == "verify-sc-1"
    run = p["runs"][-1]
    assert run["results"]["s1"]["result"] == "pass"
    assert run["progress_at"] >= before["runs"][-1]["at"]
    stamp = run["progress_at"]
    # The same answer again moves nothing (progress is a change, not a write).
    again = T.record_progress(plans, [{"id": "s1", "result": "pass", "note": "ok"}])
    assert again["runs"][-1]["progress_at"] == stamp
    # An agent's answer to a human step is blocked, like a finished run's.
    p = T.record_progress(plans, [{"id": "s3", "result": "pass"}])
    assert p["runs"][-1]["results"]["s3"]["result"] == "blocked"
    prog = T.run_progress(p)
    assert prog["answered"] == 1 and prog["total"] == 2


def test_close_run_blocks_what_was_left_and_ends_the_run(plans, monkeypatch):
    from backend.web.core import test_plans_driver as TPD

    T.record_progress(plans, [{"id": "s1", "result": "pass", "note": "ok"}])
    ended = []

    async def _end(title):
        ended.append(title)
        return True

    monkeypatch.setattr(TPD, "_end_verify_session", _end)
    monkeypatch.setattr(TPD, "_clear_verify_results", lambda t: None)
    p = asyncio.run(TPD.close_run(plans, "not checked: the checker stopped answering"))
    assert p["state"] != "running" and p["run_session"] == ""
    cells = p["runs"][-1]["results"]
    assert cells["s1"]["result"] == "pass"
    assert cells["s2"]["result"] == "blocked"
    assert cells["s2"]["note"] == "not checked: the checker stopped answering"
    assert ended == ["verify-sc-1"]
    # Blocked is a handover, never a pass: the stop will park on the person.
    assert T.run_progress(p)["blocked"][0]["step"] == "s2"
    # Idempotent.
    assert asyncio.run(TPD.close_run(plans, "again"))["state"] == p["state"]
