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
