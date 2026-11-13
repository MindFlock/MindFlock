"""Verify stability sweep — defects found in the store and the due loop.

Each test here pins one defect the sweep found and fixed; the docstring says
what a person saw. Hermetic like ``test_test_plans``: the store is a tmp file.
"""

from __future__ import annotations

import os

import pytest

from backend.web.core import test_plans as tp


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "test_plans.json"
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(path))
    return path


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/tmp/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "live_branch": "main",
        "state": "generated",
        "error": "",
        "generated_at": 100.0,
        "live_at": 0.0,
        "steps": [
            {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
            {"id": "s2", "text": "Look", "expect": "Right", "actor": "human"},
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


def _stamp(path) -> tuple:
    st = os.stat(path)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


# --------------------------------------------------------------------------- #
# A mutation that changes nothing must not rewrite the store
# --------------------------------------------------------------------------- #
# `_mutate` saved the whole file on every call, so the "writes only on change"
# promise of set_live_problem / retarget_live_branch / record_progress was
# false: the due loop rewrote the store once or twice a minute per waiting plan
# (and every 4 s per progress read while a run was open), and every rewrite
# threw away the row index memo (`_verify_plans_by_title`), so the next session
# poll re-read and re-normalised the whole store.


def test_unchanged_live_problem_does_not_rewrite_the_store(store):
    tp.upsert(_plan("sc-1"))
    before = _stamp(store)
    assert tp.set_live_problem("sc-1", "") is None
    assert _stamp(store) == before


def test_unchanged_retarget_does_not_rewrite_the_store(store):
    tp.upsert(_plan("sc-1"))
    before = _stamp(store)
    assert tp.retarget_live_branch("sc-1", "main") is None
    assert _stamp(store) == before


def test_unchanged_progress_does_not_rewrite_the_store(store):
    tp.upsert(_plan("sc-1"))
    tp.start_run("sc-1", "verify-sc-1")
    tp.record_progress("sc-1", [{"id": "s1", "result": "pass", "note": "ran"}])
    before = _stamp(store)
    tp.record_progress("sc-1", [{"id": "s1", "result": "pass", "note": "ran"}])
    assert _stamp(store) == before
    # ...and a real change still lands.
    tp.record_progress("sc-1", [{"id": "s1", "result": "fail", "note": "broke"}])
    assert _stamp(store) != before
    assert tp.get("sc-1")["runs"][-1]["results"]["s1"]["result"] == "fail"


def test_the_row_index_memo_survives_an_unchanged_pass(store, monkeypatch):
    """The per-poll cost the memo exists to remove: a liveness pass that
    changed nothing must leave the next row build on the memo."""
    tp.upsert(_plan("sc-1"))
    tp.start_run("sc-1", "verify-sc-1")
    assert tp.verify_for_session("sc-1") is not None
    calls = {"n": 0}
    real = tp.list_plans

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(tp, "list_plans", counting)
    tp.set_live_problem("sc-1", "")
    tp.retarget_live_branch("sc-1", "main")
    assert tp.verify_for_session("sc-1") is not None
    assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# A checker's check BEFORE MERGE that dies is not the work shipping
# --------------------------------------------------------------------------- #
# The due loop releases a checker run whose window died, or that passed the
# two-hour give-up, through `mark_due(pid, reason)`. For the person's own
# session that is `_release` (never stamps `live_at`), and a deleted session
# goes through `_after_run_state` ("a check before merge goes back to waiting
# for the merge — never to due"). A separate checker's run took neither: it
# stamped `live_at` and landed in `due`, so unmerged work read as shipped, sat
# in Needs you as "shipped — check it", and the liveness pass (which only asks
# `generated` plans) never looked at it again.


def test_a_dead_checker_before_merge_goes_back_to_waiting(store):
    tp.upsert(_plan("sc-1"))
    started = tp.start_run("sc-1", "verify-sc-1-aaaaaaa")
    assert started["runs"][-1]["prelive"] is True
    released = tp.mark_due("sc-1", reason="Verify gave up after 2 hours.")
    assert released["live_at"] == 0.0
    assert released["state"] == "generated"
    assert released["run_session"] == ""
    # ...and the person can still read why.
    assert released["error"] == "Verify gave up after 2 hours."


def test_a_dead_checker_after_merge_still_comes_due(store):
    """The other arm is unchanged: shipped work goes back to `due`."""
    tp.upsert(_plan("sc-1", state="due", live_at=500.0))
    tp.start_run("sc-1", "verify-sc-1-aaaaaaa")
    released = tp.mark_due("sc-1", reason="Verify gave up after 2 hours.")
    assert released["live_at"] == 500.0
    assert released["state"] == "due"


# --------------------------------------------------------------------------- #
# Progress cells are provisional until a report confirms them
# --------------------------------------------------------------------------- #
# A run that ends without a report (session closed mid-walk, a cancel, a
# give-up, a stall close) kept the agent's `finished: false` cells — "blocked:
# in progress: re-walking now" on every step of sc-25247 — and
# `open_human_steps` counted each as a handover to the person. A progress cell
# is now stamped `progress`, a finished report clears the stamp, and a stamped
# `blocked` in a run no longer in flight reads as not checked. The read rule
# itself is pinned against the UI model by test_verify_shared_fixture.py.


def _two_step_running():
    tp.upsert(_plan("sc-1"))
    tp.start_run("sc-1", "verify-sc-1")


def test_progress_cells_are_stamped_and_a_report_clears_the_stamp(store):
    _two_step_running()
    p = tp.record_progress("sc-1", [{"id": "s1", "result": "blocked", "note": "x"}])
    assert p["runs"][-1]["results"]["s1"].get("progress") is True
    done = tp.finish_run(
        "sc-1", [{"id": "s1", "result": "blocked", "note": "needs you: log in"}]
    )
    assert "progress" not in done["runs"][-1]["results"]["s1"]
    assert [s["id"] for s in tp.open_human_steps(done)] == ["s1", "s2"]


def test_a_released_run_hands_over_only_what_was_reported(store):
    _two_step_running()
    tp.record_progress(
        "sc-1", [{"id": "s1", "result": "blocked", "note": "in progress: re-walking"}]
    )
    tp.prune([])  # the session was closed mid-walk
    plan = tp.get("sc-1")
    assert plan["state"] == "generated"
    assert [s["id"] for s in tp.open_human_steps(plan)] == ["s2"]


def test_a_stall_close_leaves_progress_cells_not_checked(store, monkeypatch):
    from backend.web.core import test_plans_driver as tpd

    _two_step_running()
    tp.record_progress(
        "sc-1",
        [
            {"id": "s1", "result": "blocked", "note": "in progress: re-walking"},
        ],
    )
    monkeypatch.setattr(tpd, "_clear_verify_results", lambda t: None)
    done, _session, _in = tpd._close_run_blocking("sc-1", "not checked: stalled")
    cell = done["runs"][-1]["results"]["s1"]
    assert cell.get("progress") is True
    assert [s["id"] for s in tp.open_human_steps(done)] == ["s2"]


def test_a_clicked_off_answer_leaves_no_phantom_behind(store):
    """No run, answer a step, click it off, answer again: one run, not two."""
    tp.upsert(_plan("sc-1"))
    tp.record_result("sc-1", "s2", "pass")
    tp.record_result("sc-1", "s2", "")
    assert tp._current_run(tp.get("sc-1")) is None
    tp.record_result("sc-1", "s2", "pass")
    plan = tp.get("sc-1")
    assert len(plan["runs"]) == 1
    assert plan["runs"][0]["results"]["s2"]["result"] == "pass"
