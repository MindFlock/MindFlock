"""Flow polish — Verify's core (``backend/web/core/test_plans.py``) made
honest and hard to fool.

Pins, finding by finding:

1. a dead window RELEASES a self / in-session run as blocked with the reason,
   never stamping ``live_at`` (``release_run``; ``mark_due(reason=…)`` on such
   a run does the same);
2. the results file is read tolerantly (synonyms, actor-less answers,
   ``description`` / ``step`` text, text-less answers kept) and a file that
   can't be read says so (``note_file_error``);
3. an agent-step pass with no evidence is blocked in a self-verify;
4. ``confirm_verify(steps=…)`` per-step answers, ``agent_note``, the outcome
   recomputed; ``record_result`` re-settles a self run waiting on a person;
   ``human_steps[].why``;
5. the prompt's ``walk`` / ``previous`` and ``previous_for_prompt``;
6. ``verify_status``'s ``progress_at`` / ``current`` / ``file_error`` /
   ``queued``;
7. only a pass expires; ``dismiss_verify``;
8. the in-session tree-check wording.

Isolation: the root conftest points the plans store at tmp.
"""

from __future__ import annotations

import json

import pytest

from backend.web.core import test_plans as T


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
        "steps": [],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


A1 = {"id": "s1", "text": "Run it", "expect": "ok", "actor": "agent"}
A2 = {"id": "s2", "text": "Curl it", "expect": "200", "actor": "agent"}
H1 = {"id": "s3", "text": "Look at the page", "expect": "a badge", "actor": "human"}


def _self_run(steps, plan_id="sc-1", **start):
    T.upsert(_plan(plan_id))
    T.start_run(plan_id, plan_id, in_session=True, self_authored=True, **start)
    T.adopt_reported_steps(plan_id, steps)
    return T.get(plan_id)


def _needs_you(plan_id="sc-1", **start) -> dict:
    _self_run([A1, A2, H1], plan_id, **start)
    return T.finish_run(
        plan_id,
        [
            dict(A1, result="pass", note="exit 0"),
            dict(A2, result="blocked", note="port 3000 refused"),
            H1,
        ],
    )


# --------------------------------------------------------------------------- #
# 1. Release without lying
# --------------------------------------------------------------------------- #
def test_release_keeps_the_run_as_blocked_and_never_stamps_live_at():
    _self_run([A1, A2])
    T.record_progress("sc-1", [dict(A1, result="pass", note="exit 0")])
    plan = T.release_run("sc-1", "The agent's window closed. It was mid-step.")
    run = plan["runs"][-1]
    assert run["outcome"] == "blocked"
    assert run["outcome_note"] == "The agent's window closed. It was mid-step."
    assert run["results"]["s1"]["result"] == "pass"  # what it answered stays
    assert plan["live_at"] == 0.0  # unshipped work never reads as shipped
    assert plan["state"] == "generated" and plan["run_session"] == ""
    v = T.verify_status(plan)
    assert v["state"] == "blocked"
    assert v["detail"] == "The agent's window closed."


def test_a_release_of_a_run_that_recorded_nothing_keeps_it():
    _self_run([])
    plan = T.release_run("sc-1", "The agent's window closed.")
    assert len(plan["runs"]) == 1
    assert T.verify_status(plan)["state"] == "blocked"


def test_a_release_does_not_resurrect_the_run_before_it():
    """The old give-up popped the dead run, and the pill showed the previous
    run's "passed"."""
    _self_run([A1])
    T.finish_run("sc-1", [dict(A1, result="pass", note="exit 0")])
    assert T.get("sc-1")["runs"][-1]["outcome"] == "passed"
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    plan = T.release_run("sc-1", "The agent's window closed.")
    assert len(plan["runs"]) == 2
    assert T.verify_status(plan)["state"] == "blocked"


def test_a_shipped_plan_lands_due_and_keeps_its_live_at():
    T.upsert(_plan(state="due", live_at=50.0))
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    plan = T.release_run("sc-1", "gone")
    assert plan["state"] == "due" and plan["live_at"] == 50.0


def test_mark_due_with_a_reason_releases_a_self_run_the_same_way():
    _self_run([A1])
    plan = T.mark_due("sc-1", reason="The verify run was given up on. Long.")
    assert plan["live_at"] == 0.0 and plan["state"] == "generated"
    assert plan["runs"][-1]["outcome"] == "blocked"
    assert T.verify_status(plan)["detail"] == "The verify run was given up on."


def test_a_release_leaves_a_settled_outcome_alone():
    _needs_you()
    plan = T.release_run("sc-1", "late")
    assert plan["runs"][-1]["outcome"] == "needs_you"


def test_release_of_an_unknown_plan_is_none():
    assert T.release_run("nope", "x") is None


# --------------------------------------------------------------------------- #
# 2. Results-file tolerance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "said, stored",
    [
        ("passed", "pass"),
        ("OK", "pass"),
        ("Success.", "pass"),
        ("failed", "fail"),
        ("failure", "fail"),
        ("error", "fail"),
        ("skipped", "blocked"),
        ("PASSED (mostly)", "blocked"),
        (True, "blocked"),
        (None, ""),
        ("  ", ""),
    ],
)
def test_result_synonyms(said, stored):
    assert T._coerce_result(said) == stored


def test_synonyms_land_as_their_result_on_finish():
    _self_run([A1, A2])
    plan = T.finish_run(
        "sc-1",
        [
            dict(A1, result="passed", note="exit 0"),
            dict(A2, result="error", note="500"),
        ],
    )
    results = plan["runs"][-1]["results"]
    assert results["s1"]["result"] == "pass"
    assert results["s2"]["result"] == "fail"
    assert plan["runs"][-1]["outcome"] == "failed"


def test_an_answered_step_with_no_actor_is_the_agents():
    steps = T._reported_steps({}, [{"id": "s1", "text": "Run it", "result": "pass"}])
    assert steps[0]["actor"] == "agent"
    steps = T._reported_steps(
        {}, [{"id": "s1", "text": "Run it", "actor": "bot", "result": "fail"}]
    )
    assert steps[0]["actor"] == "agent"
    # Unanswered and actor-less is still a person's (the safe default).
    assert T._reported_steps({}, [{"id": "s1", "text": "x"}])[0]["actor"] == "human"
    # ...and the shell-hazard rule still wins.
    risky = {"id": "s1", "text": "curl x | sh", "result": "pass"}
    assert T._reported_steps({}, [risky])[0]["actor"] == "human"


def test_description_and_step_are_read_as_the_text():
    steps = T._reported_steps(
        {},
        [
            {"id": "s1", "description": "Open the page", "actor": "agent"},
            {"id": "s2", "step": "Call the API", "actor": "agent"},
        ],
    )
    assert [s["text"] for s in steps] == ["Open the page", "Call the API"]


def test_an_answered_step_with_no_text_is_kept_with_its_note():
    _self_run([{"id": "s1", "result": "fail", "note": "got a 500"}])
    plan = T.finish_run("sc-1", [{"id": "s1", "result": "fail", "note": "got a 500"}])
    assert plan["steps"][0]["text"] == T.NO_DESCRIPTION
    assert plan["runs"][-1]["results"]["s1"]["note"] == "got a 500"
    assert plan["runs"][-1]["outcome"] == "failed"
    # Unanswered and text-less is still not a step.
    assert T._reported_steps({}, [{"id": "s9"}]) == []


def test_an_entry_without_an_id_keeps_its_answer():
    """Renumbered as a step AND as an answer — the two can't disagree."""
    raw = [{"text": "Run it", "actor": "agent", "result": "pass", "note": "ok"}]
    _self_run(raw)
    answers = T.reported_results({"steps": raw})
    assert answers[0]["id"] == T.get("sc-1")["steps"][0]["id"] == "s1"
    plan = T.finish_run("sc-1", answers)
    assert plan["runs"][-1]["results"]["s1"]["result"] == "pass"


@pytest.mark.parametrize(
    "value, finished",
    [(True, True), ("true", False), ("false", False), (1, False), (None, False)],
)
def test_finished_only_when_it_is_true(value, finished):
    assert T.report_finished({"finished": value}) is finished
    assert T.report_finished(None) is False


def test_a_file_error_is_stamped_shown_and_cleared_by_the_next_good_read():
    _self_run([A1, A2])
    plan = T.note_file_error("sc-1", "Expecting ',' delimiter: line 3")
    assert plan["runs"][-1]["file_error"] == "Expecting ',' delimiter: line 3"
    # Survives a load (_normalize_run keeps it).
    plan = T.get("sc-1")
    v = T.verify_status(plan)
    assert v["file_error"] == "Expecting ',' delimiter: line 3"
    assert v["detail"] == (
        "The agent's results file can't be read: Expecting ',' delimiter: line 3."
    )
    plan = T.record_progress("sc-1", [dict(A1, result="pass", note="exit 0")])
    assert "file_error" not in plan["runs"][-1]
    assert "file_error" not in T.verify_status(plan)


def test_adopting_the_steps_also_clears_a_file_error():
    _self_run([A1])
    T.note_file_error("sc-1", "bad")
    T.adopt_reported_steps("sc-1", [A1])
    assert "file_error" not in T.get("sc-1")["runs"][-1]


def test_note_file_error_of_an_unknown_plan_is_none():
    assert T.note_file_error("nope", "bad") is None


# --------------------------------------------------------------------------- #
# 3. Evidence is required
# --------------------------------------------------------------------------- #
def test_a_self_verify_pass_without_evidence_is_blocked():
    _self_run([A1, A2])
    plan = T.record_progress("sc-1", [dict(A1, result="pass")])
    cell = plan["runs"][-1]["results"]["s1"]
    assert cell["result"] == "blocked" and cell["note"] == T.NO_EVIDENCE
    plan = T.finish_run(
        "sc-1",
        [dict(A1, result="pass", note="  "), dict(A2, result="pass", note="200")],
    )
    run = plan["runs"][-1]
    assert run["results"]["s1"] == dict(
        run["results"]["s1"], result="blocked", note=T.NO_EVIDENCE
    )
    assert run["results"]["s2"]["result"] == "pass"
    assert run["outcome"] == "needs_you"
    v = T.verify_status(plan)
    assert v["human_steps"][0]["why"] == (
        "the agent said it passed but gave no evidence"
    )


def test_a_checker_run_pass_without_a_note_still_stands():
    T.upsert(_plan(steps=[A1], state="due", live_at=5.0))
    T.start_run("sc-1", "verify-sc-1")
    plan = T.finish_run("sc-1", [dict(A1, result="pass")])
    assert plan["runs"][-1]["results"]["s1"]["result"] == "pass"


# --------------------------------------------------------------------------- #
# 4. Confirm
# --------------------------------------------------------------------------- #
def test_human_steps_say_why_the_agent_left_a_step():
    v = T.verify_status(_needs_you())
    by_id = {h["id"]: h for h in v["human_steps"]}
    assert by_id["s2"]["why"] == "agent couldn't: port 3000 refused"
    assert "why" not in by_id["s3"]  # a person's own step: nothing to explain


def test_confirm_per_step_answers_and_one_fail_means_failed():
    _needs_you()
    plan = T.confirm_verify("sc-1", True, steps={"s2": "pass", "s3": "fail"})
    run = plan["runs"][-1]
    assert run["results"]["s2"]["result"] == "pass"
    assert run["results"]["s3"]["result"] == "fail"
    assert run["results"]["s3"]["note"] == "a person said it doesn't work"
    assert run["outcome"] == "failed"


def test_confirm_steps_the_dict_leaves_out_take_the_all_at_once_answer():
    _needs_you()
    plan = T.confirm_verify("sc-1", True, steps={"s3": "pass"})
    run = plan["runs"][-1]
    assert run["results"]["s2"]["result"] == "pass"
    assert run["outcome"] == "passed"


def test_a_person_overriding_the_agent_keeps_the_agents_note():
    _needs_you()
    plan = T.confirm_verify("sc-1", True, steps={"s1": "fail"}, note="it hangs")
    run = plan["runs"][-1]
    cell = run["results"]["s1"]
    assert cell["result"] == "fail" and cell["by"] == "human"
    assert cell["note"] == "it hangs" and cell["agent_note"] == "exit 0"
    assert run["results"]["s2"]["agent_note"] == "port 3000 refused"
    assert run["outcome"] == "failed"
    # Survives a load.
    assert T.get("sc-1")["runs"][-1]["results"]["s1"]["agent_note"] == "exit 0"


def test_confirm_none_keeps_todays_meaning():
    _needs_you()
    plan = T.confirm_verify("sc-1", False, note="no badge")
    run = plan["runs"][-1]
    assert run["outcome"] == "failed"
    assert {run["results"][k]["result"] for k in ("s2", "s3")} == {"fail"}
    assert run["results"]["s1"]["result"] == "pass"  # the agent's pass stands


def test_confirm_a_sign_off_run_passes():
    _self_run([A1], confirm=True)
    T.finish_run("sc-1", [dict(A1, result="pass", note="exit 0")])
    assert T.get("sc-1")["runs"][-1]["outcome"] == "needs_you"
    assert T.confirm_verify("sc-1", True)["runs"][-1]["outcome"] == "passed"


@pytest.mark.parametrize(
    "steps", [{"s1": "maybe"}, ["s1"], {"s9": "pass"}], ids=["value", "shape", "id"]
)
def test_confirm_refuses_bad_steps(steps):
    _needs_you()
    with pytest.raises(ValueError):
        T.confirm_verify("sc-1", True, steps=steps)
    assert T.get("sc-1")["runs"][-1]["outcome"] == "needs_you"


def test_record_result_settles_a_self_run_waiting_on_a_person():
    _needs_you()
    plan = T.record_result("sc-1", "s3", "fail", note="no badge")
    assert plan["runs"][-1]["outcome"] == "failed"
    T.record_result("sc-1", "s3", "pass")
    plan = T.record_result("sc-1", "s2", "pass")
    assert plan["runs"][-1]["outcome"] == "passed"
    assert plan["runs"][-1]["results"]["s2"]["agent_note"] == "port 3000 refused"


def test_record_result_keeps_a_sign_off_run_waiting_for_the_sign_off():
    _needs_you(confirm=True)
    T.record_result("sc-1", "s2", "pass")
    plan = T.record_result("sc-1", "s3", "pass")
    assert plan["runs"][-1]["outcome"] == "needs_you"
    assert T.verify_status(plan)["detail"] == "Every step passed — confirm it works."


def test_record_result_leaves_a_running_check_alone():
    _self_run([A1, H1])
    plan = T.record_result("sc-1", "s3", "fail")
    assert plan["runs"][-1]["outcome"] == "verifying"


# --------------------------------------------------------------------------- #
# 5. The prompt
# --------------------------------------------------------------------------- #
def test_the_prompt_demands_checkable_evidence_and_is_provider_neutral():
    p = T.build_self_verify_prompt(_plan())
    assert "TODO LIST" not in p and "track them however you track tasks" in p
    assert "NOT the unit tests" in p
    assert "the command you ran and the output line" in p
    assert "the URL and what was on it" in p and "the log line" in p
    assert "A pass without evidence counts as blocked" in p
    assert "TELL THE PERSON" in p and "what to look for" in p
    assert '"finished": true (the JSON value' in p


def test_walk_all_has_the_agent_attempt_every_step():
    p = T.build_self_verify_prompt(_plan(), walk="all")
    assert "Attempt EVERY step yourself" in p
    assert "including steps a person would normally do" in p
    assert '"needs you:' in p and "ONLY when no tool of yours" in p
    assert "TELL THE PERSON" not in p
    example = json.loads(p[p.index("{") :])
    assert "result" in example["steps"][1]  # it answers a person's step too
    # Default walk keeps the hand-over rule.
    p = T.build_self_verify_prompt(_plan(), walk="agent")
    assert "Attempt EVERY step" not in p
    assert "result" not in json.loads(p[p.index("{") :])["steps"][1]


def test_the_prompt_lists_the_previous_failures_first():
    previous = [
        {"id": "s2", "text": "Curl it", "result": "fail", "note": "got 500"},
        {"id": "s4", "text": "Open it", "result": "blocked", "note": ""},
    ]
    p = T.build_self_verify_prompt(_plan(), previous=previous)
    assert "re-check these first and keep their ids" in p
    assert "s2 (fail) Curl it — last time: got 500" in p
    assert "s4 (blocked) Open it\n" in p
    assert "re-check these" not in T.build_self_verify_prompt(_plan(), previous=[])


def test_previous_for_prompt_reads_the_last_finished_run():
    _needs_you()
    plan = T.confirm_verify("sc-1", True, steps={"s3": "fail"}, note="no badge")
    prev = T.previous_for_prompt(plan)
    assert prev == [
        {"id": "s3", "text": "Look at the page", "result": "fail", "note": "no badge"}
    ]


def test_previous_for_prompt_counts_unanswered_steps_of_a_stopped_check():
    _self_run([A1, A2, H1])
    T.record_progress("sc-1", [dict(A1, result="fail", note="boom")])
    plan = T.release_run("sc-1", "window closed")
    prev = {p["id"]: p for p in T.previous_for_prompt(plan)}
    assert prev["s1"]["result"] == "fail"
    assert prev["s2"] == {
        "id": "s2",
        "text": "Curl it",
        "result": "blocked",
        "note": "not answered before the check stopped",
    }
    assert "s3" not in prev  # a person's step it was never asked to walk


def test_previous_for_prompt_is_empty_while_a_check_is_in_flight():
    _self_run([A1])
    T.record_progress("sc-1", [dict(A1, result="fail", note="boom")])
    assert T.previous_for_prompt(T.get("sc-1")) == []
    assert T.previous_for_prompt({}) == []


def test_a_persons_pass_is_carried_for_the_ids_the_agent_keeps():
    _needs_you()
    T.confirm_verify("sc-1", True, steps={"s3": "pass", "s2": "fail"})
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    run = T.get("sc-1")["runs"][-1]
    assert run["results"]["s3"]["result"] == "pass"  # a person's pass
    assert "s2" not in run["results"]  # their fail is being re-checked
    assert "s1" not in run["results"]  # the agent's own is never carried
    # The agent keeps s3 → the answer survives; it drops it → it goes.
    T.adopt_reported_steps("sc-1", [A2, H1])
    assert T.get("sc-1")["runs"][-1]["results"]["s3"]["by"] == "human"
    T.adopt_reported_steps("sc-1", [A2])
    assert "s3" not in T.get("sc-1")["runs"][-1]["results"]


def test_a_carried_pass_settles_the_persons_step_on_finish():
    _needs_you()
    T.confirm_verify("sc-1", True)
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    T.adopt_reported_steps("sc-1", [A1, H1])
    plan = T.finish_run("sc-1", [dict(A1, result="pass", note="exit 0"), H1])
    assert plan["runs"][-1]["outcome"] == "passed"


# --------------------------------------------------------------------------- #
# 6. Status fields
# --------------------------------------------------------------------------- #
def test_progress_at_and_current_follow_the_agent():
    plan = _self_run([A1, A2, H1])
    v = T.verify_status(plan)
    assert v["progress_at"] == 0.0
    assert v["current"] == {"id": "s1", "text": "Run it"}
    plan = T.record_progress("sc-1", [dict(A1, result="pass", note="exit 0")])
    v = T.verify_status(plan)
    assert v["progress_at"] == plan["runs"][-1]["progress_at"] > 0
    assert v["current"] == {"id": "s2", "text": "Curl it"}
    plan = T.record_progress("sc-1", [dict(A2, result="pass", note="200")])
    # Every agent step answered: next is the person's.
    assert T.verify_status(plan)["current"] == {"id": "s3", "text": "Look at the page"}


def test_current_is_none_when_every_step_has_an_answer():
    _self_run([A1])
    plan = T.finish_run("sc-1", [dict(A1, result="pass", note="exit 0")])
    assert T.verify_status(plan)["current"] is None


def test_queued_says_the_prompt_waits_for_the_agents_turn():
    plan = _self_run([])
    assert T.verify_status(plan, queued=True)["detail"] == (
        "Waiting for the agent's turn to end."
    )
    # Only while verifying.
    plan = _needs_you("sc-2")
    assert "Waiting" not in T.verify_status(plan, queued=True)["detail"]


# --------------------------------------------------------------------------- #
# 7. Pill lifetime
# --------------------------------------------------------------------------- #
def test_only_a_pass_expires():
    _self_run([A1])
    plan = T.finish_run("sc-1", [dict(A1, result="pass", note="exit 0")])
    at = plan["runs"][-1]["outcome_at"]
    assert T.verify_status(plan, now=at + T.VERIFY_SHOW_S + 1) is None
    _self_run([A1], "sc-2")
    plan = T.finish_run("sc-2", [dict(A1, result="fail", note="boom")])
    at = plan["runs"][-1]["outcome_at"]
    assert T.verify_status(plan, now=at + 86400)["state"] == "failed"
    _self_run([A1], "sc-3")
    plan = T.release_run("sc-3", "gone")
    assert T.verify_status(plan, now=at + 86400)["state"] == "blocked"


def test_dismiss_hides_a_settled_pill_until_the_next_verify():
    _self_run([A1])
    T.finish_run("sc-1", [dict(A1, result="fail", note="boom")])
    plan = T.dismiss_verify("sc-1")
    assert plan["runs"][-1]["dismissed"] is True
    assert T.verify_status(T.get("sc-1")) is None  # kept through a load
    assert T.verify_for_session("sc-1") is None
    T.start_run("sc-1", "sc-1", in_session=True, self_authored=True)
    assert T.verify_status(T.get("sc-1"))["state"] == "verifying"


def test_dismiss_does_not_hide_a_check_that_waits_on_a_person():
    _needs_you()
    plan = T.dismiss_verify("sc-1")
    assert "dismissed" not in plan["runs"][-1]
    assert T.verify_status(plan)["state"] == "needs_you"
    _self_run([A1], "sc-2")
    assert "dismissed" not in T.dismiss_verify("sc-2")["runs"][-1]
    assert T.dismiss_verify("nope") is None


def test_a_released_verifying_run_can_be_dismissed():
    """An older build's release left the run marked verifying."""
    _self_run([A1])
    raw = T.get("sc-1")
    raw["state"], raw["run_session"] = "generated", ""
    T.upsert(raw)
    assert T.verify_status(T.get("sc-1"))["state"] == "blocked"
    T.dismiss_verify("sc-1")
    assert T.verify_status(T.get("sc-1")) is None


# --------------------------------------------------------------------------- #
# 8. Wording
# --------------------------------------------------------------------------- #
def test_an_in_session_tree_mismatch_names_the_branch_not_the_live_tree():
    T.upsert(_plan(steps=[A1]))
    T.start_run("sc-1", "sc-1", in_session=True)
    plan = T.finish_run(
        "sc-1",
        [dict(A1, result="pass", note="exit 0")],
        tested_sha="dead" * 10,
        expected_sha="beef" * 10,
    )
    note = plan["runs"][-1]["results"]["s1"]["note"]
    assert note.startswith("not checked on this branch's newest commit — ")
    assert "live tree" not in note


def test_results_markdown_says_steps():
    assert "No steps written yet." in T.results_markdown(_plan())
