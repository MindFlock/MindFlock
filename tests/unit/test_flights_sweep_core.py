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
