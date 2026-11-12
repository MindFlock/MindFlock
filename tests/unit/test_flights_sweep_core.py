"""Stability sweep of the flights core (2026-11-12): each test pins a defect
found by reading the store, the transitions and the routes — it failed before
its fix."""

import pytest

from backend.web.core import flights as F

from tests.unit.test_flights_verify_ask_end import (  # noqa: F401 — fixture
    _agent,
    _fly,
    _needs_you_facts,
    _step,
    _store,
    _verify,
)

NOW = 1_800_000_000.0


def test_carry_on_at_the_end_never_lands_with_checks_owed():
    """The flying last stop removed leaves the route paused at its end;
    Carry on used to land it at once, dropping the checks a Verify stop
    (``ask: end``) owed the person — the route must never finish with a
    check owed (docs/flights.md "Verify stops"): it asks for them first."""
    f = _fly(_verify(), _agent("s2"))
    _a, f = _step(f, facts=_needs_you_facts())
    assert f["cursor"] == 1 and f["owed"]
    f = F.op_remove_step(f, "s2", now=NOW + 150)
    assert f["state"] == "paused" and f["cursor"] == len(f["steps"])
    f = F.op_resume(f, now=NOW + 160)
    assert f["state"] == "running", f["reason"]
    assert f["owed"] and f["steps"][f["cursor"]]["title"] == F.OWED_PERCH_TITLE


def _group_halted_on_b():
    """A parallel group [A, B]: A done, B failed — the route halted on B."""
    pid = {"id": "p_sweep1"}
    plan = F.save_plan(
        {
            "name": "side by side",
            "steps": [
                {
                    "kind": "command",
                    "title": "A",
                    "config": {"command": "a"},
                    "parallel": dict(pid),
                },
                {
                    "kind": "command",
                    "title": "B",
                    "config": {"command": "b"},
                    "parallel": dict(pid),
                },
            ],
        },
        now=NOW,
    )
    f = F.take_off(plan, session="s", now=NOW)
    a, b = (s["id"] for s in f["steps"])
    f = F.apply_at(f, 0, {"action": "run", "note": ""}, NOW + 1)
    f = F.apply_at(f, 1, {"action": "run", "note": ""}, NOW + 1)
    f = F.apply_at(f, 0, {"action": "done", "note": "ok"}, NOW + 2)
    f = F.apply_at(
        f, 1, {"action": "fail", "reason": "exit 1", "then": "halt"}, NOW + 3
    )
    assert f["state"] == "halted" and f["cursor"] == 0
    assert f["step_states"][a]["state"] == "done"
    return f, a, b


def test_retry_never_runs_a_done_member_of_a_halted_group_again():
    """Retry on the group's first member — what control_flight(op=retry)
    picks with no step: the cursor stands on the group's first member — used
    to reset that DONE stop and run it again (a second command, a second
    prompt), and the route then sat "running" on its failed member forever.
    Only a member with something to retry takes Retry."""
    f, a, b = _group_halted_on_b()
    with pytest.raises(F.FlightError) as err:
        F.op_retry(f, a, now=NOW + 10)
    assert err.value.error == "not_failed"
    assert f["step_states"][a]["state"] == "done"
    # The failed member still takes it.
    f = F.op_retry(f, b, now=NOW + 11)
    assert f["state"] == "running"
    assert f["step_states"][a]["state"] == "done"
    assert f["step_states"][b]["state"] == "pending"


def _stored_with_unknown_kind():
    """flights.json holding a route whose current stop is a kind this build
    does not know (a newer server's kind on a shared state dir, a removed or
    broken plug-in), as raw JSON."""
    import json
    import os

    raw = {
        "id": "f_newer",
        "plan_id": "adhoc",
        "plan_name": "From the newer server",
        "session": "s-new",
        "state": "running",
        "cursor": 1,
        "steps": [
            {"id": "s1", "kind": "notify", "title": "Tell", "config": {}},
            {"id": "s2", "kind": "zzfuture", "title": "Future", "config": {"a": 1}},
            {"id": "s3", "kind": "notify", "title": "After", "config": {}},
        ],
        "step_states": {
            "s1": {"state": "done"},
            "s2": {"state": "running", "detail": {"x": 1}},
            "s3": {"state": "pending"},
        },
        "started": NOW,
        "updated": NOW,
    }
    os.makedirs(F.flights_dir(), exist_ok=True)
    with open(F.flights_path(), "w", encoding="utf-8") as fh:
        json.dump({"flights": {"f_newer": raw}}, fh)
    return raw


def test_a_stop_of_an_unknown_kind_is_never_erased_or_skipped():
    """Reading the store dropped a stop of a kind this build lacks: its
    cursor then stood on the NEXT stop (the unknown one silently skipped),
    and the next write — any route's — erased the stop for every server.
    Such a route is kept exactly as stored and never driven here."""
    import json

    raw = _stored_with_unknown_kind()
    assert F.get("f_newer") is None  # not this server's to fly
    plan = F.save_plan(
        {"name": "other", "steps": [{"kind": "notify", "config": {}}]}, now=NOW
    )
    F.take_off(plan, session="s-other", now=NOW + 1)  # rewrites the store
    with open(F.flights_path(), encoding="utf-8") as fh:
        stored = json.load(fh)["flights"]
    assert stored["f_newer"] == raw
    assert len(stored) == 2


def test_saving_one_route_never_erases_anothers_unknown_stop():
    """Every save rewrites plans.json from the normalised plans, which had
    dropped a stop of a kind this build lacks: saving ANY route erased that
    stop from every other saved route (a newer server's, a removed plug-in's).
    Routes this save does not touch go back as stored."""
    import json
    import os

    raw = {
        "id": "p_newer",
        "name": "Newer",
        "steps": [
            {"id": "s1", "kind": "zzfuture", "title": "Future", "config": {"a": 1}},
            {"id": "s2", "kind": "notify", "title": "Tell", "config": {}},
        ],
    }
    os.makedirs(F.flights_dir(), exist_ok=True)
    with open(F.plans_path(), "w", encoding="utf-8") as fh:
        json.dump({"plans": [raw]}, fh)
    F.save_plan({"name": "mine", "steps": [{"kind": "notify", "config": {}}]})
    with open(F.plans_path(), encoding="utf-8") as fh:
        stored = {p["id"]: p for p in json.load(fh)["plans"]}
    assert stored["p_newer"] == raw


def _group_with_two_failed():
    """A parallel group [A, B, C]: A and B failed (the route halted), C done."""
    pid = {"id": "p_sweep2"}
    plan = F.save_plan(
        {
            "name": "three side by side",
            "steps": [
                {
                    "kind": "command",
                    "title": t,
                    "config": {"command": t.lower()},
                    "parallel": dict(pid),
                }
                for t in ("A", "B", "C")
            ],
        },
        now=NOW,
    )
    f = F.take_off(plan, session="s", now=NOW)
    for k in range(3):
        f = F.apply_at(f, k, {"action": "run", "note": ""}, NOW + 1)
    f = F.apply_at(f, 2, {"action": "done", "note": "ok"}, NOW + 2)
    for k, why in ((0, "a broke"), (1, "b broke")):
        f = F.apply_at(
            f, k, {"action": "fail", "reason": why, "then": "halt"}, NOW + 3 + k
        )
    assert f["state"] == "halted"
    return f, [s["id"] for s in f["steps"]]


def test_retrying_one_of_two_failed_members_keeps_the_route_halted():
    """Retry on one failed member used to set the route running while the
    other stayed failed: the driver then waited on that failed stop forever
    ("running", nothing to do). The route stays halted, saying which stop
    still failed, until every failed member is retried or skipped."""
    f, (a, b, c) = _group_with_two_failed()
    f = F.op_retry(f, a, now=NOW + 10)
    assert f["step_states"][a]["state"] == "pending"
    assert f["state"] == "halted"
    assert "b broke" in f["reason"]
    f = F.op_retry(f, b, now=NOW + 11)
    assert f["state"] == "running" and f["reason"] == ""
    assert f["step_states"][c]["state"] == "done"


def test_skipping_the_other_failed_member_then_flies_the_retried_one():
    f, (a, b, _c) = _group_with_two_failed()
    f = F.op_retry(f, a, now=NOW + 10)
    f = F.op_skip(f, b, now=NOW + 11)
    assert f["state"] == "running"
    assert F.open_members(f) == [0]


def test_letting_the_agent_fix_one_of_two_failed_members_keeps_it_halted():
    """ "Let the agent fix it" on one failed member had the same hole as
    Retry: the route ran on with the other failed member nobody was asked
    about. It stays halted on that one; the fix waits its turn."""
    f, (a, b, _c) = _group_with_two_failed()
    f = F.op_fix(f, a, now=NOW + 10)
    assert f["step_states"][a]["detail"]["phase"] == "fix"
    assert f["state"] == "halted" and "b broke" in f["reason"]
    f = F.op_retry(f, b, now=NOW + 11)
    assert f["state"] == "running"
