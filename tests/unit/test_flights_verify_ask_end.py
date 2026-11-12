"""A Verify stop that carries on and asks the person ONCE, at the end
(``ask: "end"``; docs/flights.md "Verify stops").

Found on the owner's real "PR review" route (2026-11-11): its first stop,
"Test locally" (who: both), parked within minutes on "1 step for you to
check, then confirm" and sat 5 to 75 minutes until the owner came by, while
40+ minutes of unattended reviews behind it had not even started — then the
route parked AGAIN at its last stop. With ``ask: "end"`` the stop is done,
the person's checks are owed on the flight, and one perch asks for them all:
the route's own last Wait-for-me stop (one Done settles both), else a perch
added at the end. A check the person says doesn't work sends the route back
to that Verify stop's failure handling; nothing is ever dropped silently.
"""

import pytest

from backend.web.core import flight_cookbook as C
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import test_plans as T

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    seen: list = []
    monkeypatch.setattr(
        D._events.BUS, "emit", lambda event, **kw: seen.append((event, kw))
    )
    monkeypatch.setattr(D, "wake", lambda: None)
    yield seen


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _verify(sid="s1", title="Test locally", on_fail="halt", **config):
    config.setdefault("ask", "end")
    return {
        "id": sid,
        "kind": "verify",
        "title": title,
        "config": config,
        "on_fail": on_fail,
    }


def _agent(sid, title="Review"):
    return {"id": sid, "kind": "agent", "title": title, "config": {"prompt": "go"}}


def _you(sid, text="Merge or ask for review"):
    return {"id": sid, "kind": "you", "title": "Merge", "config": {"text": text}}


def _sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def _fly(*steps, session="s"):
    plan = F.save_plan({"name": "PR review", "steps": list(steps)}, now=NOW)
    f = F.take_off(plan, session=session, now=NOW)
    a = F.next_action(f, {"session": _sess()}, NOW)
    assert a["action"] == "run", a
    f = F.apply(f, a, NOW)
    # The check starts (the driver's agent_run effect).
    a = F.next_action(f, {"session": _sess(), "verify": _facts()}, NOW + 1)
    assert a["action"] == "run" and a["phase"] == "agent_run", a
    return F.apply(
        f,
        dict(a, detail=dict(a["detail"], run_started=True, run_started_at=NOW + 1)),
        NOW + 1,
    )


def _facts(**kw):
    base = {
        "plan": "sc-1",
        "branch": "b",
        "state": "generated",
        "steps": ["s1", "s2", "s3"],
        "settled": False,
        "open_human": [],
        "open_agent": [],
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


ITEMS = [
    "Curl it — look for: 200 — the agent couldn't: port 3000 refused",
    "Look at the page — look for: a badge",
]


def _needs_you_facts(run_at=NOW + 2, **kw):
    return _facts(
        latest_run_at=run_at, outcome="needs_you", confirm_items=list(ITEMS), **kw
    )


def _step(f, facts=None, at=NOW + 100, owed=None):
    obs = {"session": _sess(), "verify": facts or {}}
    if owed is not None:
        obs["owed"] = owed
    a = F.next_action(f, obs, at)
    return a, F.apply(f, a, at)


def _store_put(f):
    """Write ``f`` to the store (as the driver's mutate would)."""
    return F.mutate(f["id"], lambda x: x.update(f))


# The plan store, as test_flowpolish_verifycore builds a real self-verify run.
A1 = {"id": "s1", "text": "Run it", "expect": "ok", "actor": "agent"}
A2 = {"id": "s2", "text": "Curl it", "expect": "200", "actor": "agent"}
H1 = {"id": "s3", "text": "Look at the page", "expect": "a badge", "actor": "human"}


def _real_needs_you(plan_id="sc-1") -> dict:
    T.upsert(
        {
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
            "steps": [],
            "runs": [],
            "run_session": "",
        }
    )
    T.start_run(plan_id, plan_id, in_session=True, self_authored=True)
    T.adopt_reported_steps(plan_id, [A1, A2, H1])
    plan = T.finish_run(
        plan_id,
        [
            dict(A1, result="pass", note="exit 0"),
            dict(A2, result="blocked", note="port 3000 refused"),
            H1,
        ],
    )
    assert plan["runs"][-1]["outcome"] == "needs_you"
    return plan


def _run_at(plan_id="sc-1") -> float:
    return float(T.get(plan_id)["runs"][-1]["at"])


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_ask_is_a_labelled_field_and_defaults_to_here():
    fields = {f["name"]: f for f in F.STEP_KINDS["verify"]["fields"]}
    ask = fields["ask"]
    assert ask["type"] == "select" and ask["default"] == "here"
    assert ask["label"] == "When it needs you"
    assert ask["options"] == ["here", "end"]
    assert ask["option_labels"] == {
        "here": "Wait here",
        "end": "Carry on, ask me at the end",
    }
    plain = F.normalize_step({"kind": "verify", "config": {}})
    assert plain["config"]["ask"] == "here"
    end = F.normalize_step({"kind": "verify", "config": {"ask": "END"}})
    assert end["config"]["ask"] == "end"


def test_a_bad_ask_is_refused_strictly_and_defaulted_leniently():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "verify", "config": {"ask": "later"}})
    assert "ask" in err.value.field
    lenient = F.normalize_step(
        {"kind": "verify", "config": {"ask": "later"}}, strict=False
    )
    assert lenient["config"]["ask"] == "here"


def test_the_cookbook_says_it():
    assert "ask=here|end" in C.STOPS["verify"]["fields"]
    phrase = C.step_phrase({"kind": "verify", "config": {"ask": "end"}})
    assert phrase.endswith("(asks you at the end)")
    assert "asks you" not in C.step_phrase({"kind": "verify", "config": {}})


# --------------------------------------------------------------------------- #
# The stop: done, the person's checks owed on the flight
# --------------------------------------------------------------------------- #
def test_here_is_todays_park():
    f = _fly(_verify(ask="here"), _agent("s2"))
    a, f = _step(f, facts=_needs_you_facts())
    assert a["action"] == "you" and a["detail"]["ask"] == "confirm"
    assert a["note"] == "2 steps for you to check, then confirm"
    assert f["state"] == "waiting_you" and not f.get("owed")


def test_end_carries_on_and_owes_the_checks():
    f = _fly(_verify(), _agent("s2"))
    a, f = _step(f, facts=_needs_you_facts())
    assert a["action"] == "done"
    ss = f["step_states"]["s1"]
    assert ss["state"] == "done"
    assert ss["note"] == "2 checks for you, asked at the end"
    assert ss["detail"]["owes"] == 2
    # The route moved on to the reviews, flying.
    assert f["cursor"] == 1 and f["state"] == "running"
    (e,) = f["owed"]
    assert e == {
        "step": "s1",
        "title": "Test locally",
        "plan": "sc-1",
        "items": ITEMS,
        "since": NOW + 100,
        "run_at": NOW + 2,
        "perch": "",
    }
    assert F.owed_line(f) == "2 checks owed to you at the end"


def test_the_owed_list_survives_a_restart():
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    _store_put(f)
    again = F.get(f["id"])
    assert again["owed"] == f["owed"]
    # Unknown keys are dropped; an entry without a stop id is dropped.
    raw = dict(again, owed=[dict(f["owed"][0], junk=1), {"plan": "x"}])
    norm = F._normalize_flight(f["id"], raw)
    assert norm["owed"] == f["owed"]


def test_the_agents_own_failure_still_fails_right_there():
    f = _fly(_verify(), _agent("s2"))
    failed = _facts(
        latest_run_at=NOW + 2,
        outcome="failed",
        failed=["Curl it — saw: 500"],
    )
    a, f = _step(f, facts=failed)
    assert a["action"] == "fail"
    assert f["state"] == "halted" and f["step_states"]["s1"]["state"] == "failed"
    assert "Failed · 1: Curl it — saw: 500" in f["reason"]
    assert not f.get("owed")


def test_a_sign_off_with_every_step_passed_is_owed_too():
    f = _fly(_verify(who="you"), _agent("s2"))
    a, f = _step(f, facts=_facts(latest_run_at=NOW + 2, outcome="needs_you"))
    assert a["action"] == "done"
    assert f["owed"][0]["items"] == ["Every step passed — confirm it works"]
    assert f["step_states"]["s1"]["note"] == "1 check for you, asked at the end"


# --------------------------------------------------------------------------- #
# The single ask
# --------------------------------------------------------------------------- #
def _to_the_end(f, at=NOW + 200):
    """Fly the agent stop s2 to done (by hand), then the next pass."""
    a = {"action": "done", "note": "agent finished"}
    f = F.apply(f, a, at)
    return f


def test_no_you_stop_adds_one_perch_at_the_end():
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    # Not finished: a perch was added at the end and the cursor stands on it.
    assert f["state"] == "running"
    perch = f["steps"][-1]
    assert perch["kind"] == "you" and perch["title"] == "Your checks"
    assert f["owed_perch"] == perch["id"] == "s3"
    assert f["cursor"] == 2
    a, f = _step(f, at=NOW + 201)
    assert a["action"] == "you"
    assert a["note"] == "Before this route finishes: 2 checks for you"
    assert f["state"] == "waiting_you"
    d = f["step_states"]["s3"]["detail"]
    assert d["ask"] == "owed"
    assert d["owed"] == [
        {"step": "s1", "title": "Test locally", "plan": "sc-1", "items": ITEMS}
    ]
    assert f["owed"][0]["perch"] == "s3"
    text, items = F.you_text(f)
    assert text == "Before this route finishes: 2 checks for you"
    assert items == ["Test locally: " + i for i in ITEMS]
    # Still listed while it waits.
    assert F.owed_line(f) == "2 checks owed to you at the end"


def test_a_later_you_stop_asks_for_them_with_its_own_ask():
    f = _fly(_verify(), _agent("s2"), _you("s3"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    assert len(f["steps"]) == 3  # no perch added
    a, f = _step(f, at=NOW + 201)
    assert a["action"] == "you"
    assert a["note"] == (
        "Merge or ask for review · before this route finishes: 2 checks for you"
    )
    d = f["step_states"]["s3"]["detail"]
    assert d["ask"] == "owed" and d["items"] == []
    assert d["owed"][0]["items"] == ITEMS
    # Done settles both: one perch, and the route lands.
    f = F.op_done(f, "s3", now=NOW + 300)
    assert f["state"] == "done"
    assert f["owed"] == []
    assert any(
        h["event"] == "noted" and "you confirmed 2 checks: Test locally" in h["note"]
        for h in f["history"]
    )


def test_skipping_the_perch_leaves_the_checks_unconfirmed_and_finishes():
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    _a, f = _step(f, at=NOW + 201)
    f = F.op_skip(f, "s3", now=NOW + 300)
    assert f["state"] == "done" and f["owed"] == []
    assert any("left unconfirmed" in h["note"] for h in f["history"])


def test_confirmed_elsewhere_settles_the_perch_by_itself():
    """The person confirmed from the session's own Verify pill: the route's
    own perch has nothing left to ask and lands."""
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    _a, f = _step(f, at=NOW + 201)
    # Still waiting on the person: nothing changes.
    w, _ = _step(
        f, at=NOW + 250, owed={"sc-1": {"outcome": "needs_you", "run_at": NOW + 2}}
    )
    assert w["action"] == "wait"
    a, f = _step(
        f, at=NOW + 260, owed={"sc-1": {"outcome": "passed", "run_at": NOW + 2}}
    )
    assert a["action"] == "owed" and a["settled"] == ["s1"]
    assert f["state"] == "done"
    assert f["step_states"]["s3"]["note"] == "your checks were confirmed"


def test_a_check_said_not_to_work_goes_back_to_its_stops_failure_handling():
    f = _fly(_verify(), _agent("s2"), _you("s3"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    _a, f = _step(f, at=NOW + 201)
    a, f = _step(
        f, at=NOW + 300, owed={"sc-1": {"outcome": "failed", "run_at": NOW + 2}}
    )
    assert a["action"] == "owed" and a["back"] == "s1"
    # Back on the Verify stop, the stops after it to fly again.
    assert f["cursor"] == 0 and f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "running"
    assert f["step_states"]["s2"]["state"] == "pending"
    assert f["step_states"]["s3"]["state"] == "pending"
    assert f["owed"] == []
    # The stop reads its check — failed by the person — and fails through its
    # on_fail (halt).
    failed = _facts(
        latest_run_at=NOW + 2,
        outcome="failed",
        failed=["Look at the page — saw: no badge"],
    )
    b, f = _step(f, facts=failed, at=NOW + 301)
    assert b["action"] == "fail"
    assert f["state"] == "halted"
    assert "Failed · 1: Look at the page — saw: no badge" in f["reason"]


def test_back_to_a_stop_that_lets_the_agent_fix_it():
    f = _fly(_verify(on_fail="agent"), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    f = _to_the_end(f)
    _a, f = _step(f, at=NOW + 201)
    _a, f = _step(
        f, at=NOW + 300, owed={"sc-1": {"outcome": "failed", "run_at": NOW + 2}}
    )
    # The route's own end perch is gone; it comes back if anything is owed.
    assert [s["id"] for s in f["steps"]] == ["s1", "s2"]
    failed = _facts(latest_run_at=NOW + 2, outcome="failed", failed=["Look"])
    b, f = _step(f, facts=failed, at=NOW + 301)
    assert b["action"] == "fail" and b.get("then") == "agent"
    assert f["step_states"]["s1"]["detail"]["phase"] == "fix"


# --------------------------------------------------------------------------- #
# The dtos
# --------------------------------------------------------------------------- #
def test_the_row_and_the_flight_dto_carry_the_owed_list():
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    _store_put(f)
    row = F.row_dto("s")
    assert row["owed"] == [
        {"step": "s1", "title": "Test locally", "plan": "sc-1", "items": ITEMS}
    ]
    assert row["owed_note"] == "2 checks owed to you at the end"
    dto = D.flight_dto(F.get(f["id"]))
    assert dto["owed_note"] == "2 checks owed to you at the end"
    assert dto["owed"][0]["perch"] == ""
    # Here-mode keeps its shape: no owed keys at all.
    g = _fly(_verify(ask="here"), session="t")
    assert "owed" not in g and "owed_note" not in D.flight_dto(g)


# --------------------------------------------------------------------------- #
# The driver: Done confirms on the plan, as POST …/confirm does
# --------------------------------------------------------------------------- #
def _parked_on_owed(plan_id="sc-1", *steps):
    f = _fly(_verify(), _agent("s2"), *steps)
    _a, f = _step(f, facts=_needs_you_facts())
    # The owed entry names the real run (its clock is today's, not NOW's).
    f["owed"][0]["run_at"] = _run_at(plan_id)
    f = _to_the_end(f)
    _a, f = _step(f, at=NOW + 201)
    return _store_put(f)


def test_done_on_the_perch_confirms_the_plan_and_lands(_store):
    seen = _store
    _real_needs_you()
    f = _parked_on_owed("sc-1", _you("s3"))
    assert D._owed_obs(f, "s3") == {
        "sc-1": {"outcome": "needs_you", "run_at": _run_at()}
    }
    after = D.operate(f["id"], "done", "s3", "looks right")
    assert after["state"] == "done"
    run = T.get("sc-1")["runs"][-1]
    assert run["outcome"] == "passed"
    assert run["results"]["s3"]["by"] == "human"
    assert run["outcome_note"] == "looks right"
    checked = [kw for ev, kw in seen if ev == "session.test_plan_checked"]
    assert checked and checked[0]["data"]["verified"] is True
    assert checked[0]["data"]["outcome"] == "passed"


def test_done_after_a_person_said_it_fails_is_refused(_store):
    _real_needs_you()
    f = _parked_on_owed("sc-1")
    T.confirm_verify("sc-1", False, note="no badge")  # the "it doesn't work" path
    with pytest.raises(F.FlightError) as err:
        D.operate(f["id"], "done", f["owed_perch"])
    assert err.value.error == "owed_failed"
    assert "Test locally: a person said it doesn't work" in str(err.value.detail)
    assert F.get(f["id"])["state"] == "waiting_you"  # the driver takes it back


def test_cancelling_with_checks_owed_leaves_the_plan_waiting_on_you():
    _real_needs_you()
    f = _parked_on_owed("sc-1")
    D.operate(f["id"], "cancel")
    assert F.get(f["id"])["state"] == "cancelled"
    assert T.get("sc-1")["runs"][-1]["outcome"] == "needs_you"
