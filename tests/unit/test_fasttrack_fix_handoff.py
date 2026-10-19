"""Fast-track hands a fixable failure back to the agent and CARRIES ON.

A failed pre-commit hook (or check, red CI, a conflict) used to tell the agent
what failed and halt in the same pass. The driver skips a halted run, so the
agent's fix — even its own successful commit — never reached the push and PR
the run was armed for. Now the run stays armed (``fixing``), waits out the fix
turn, and picks the ladder up from wherever git is, at most
``MAX_FIX_ROUNDS`` times; a fix turn that changes nothing halts.
"""

import asyncio
import os

import pytest

from backend.web.core import autopilot as ap
from backend.web.core import autopilot_driver as driver
from backend.web.core import mailbox


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", os.path.join(tmp_path, "ap.json"))
    monkeypatch.setattr(driver, "_run_member", lambda title: None)
    monkeypatch.setattr(driver, "_emit_autopilot", lambda title: None)
    monkeypatch.setattr(driver, "_emit_autopilot_event", lambda title: None)


def _snap(**over):
    s = {
        "stage": "agent",
        "failed_step": "",
        "failed_hook": "",
        "dirty": False,
        "beyond_base": 1,
        "activity": "idle",
        "limited": False,
        "queue_pending": False,
        "background_waiting": False,
        "check": None,
        "check_required": False,
        "pr_checks": "ok",
        "merge_blockers": [],
        "on_base_branch": False,
        "branch": "feature/x",
        "has_origin": True,
        "head": "aaa",
        "now": 10_000.0,
    }
    s.update(over)
    return s


def _fixing(**over):
    """A record whose failure was handed back at t=1000 and whose agent was
    seen working since and has been idle long enough to count as done."""
    r = ap._blank()
    r.update(
        {
            "depth": "pr",
            "state": "running",
            "commits": 1,
            "step": "agent",
            "fixing": "precommit",
            "fix_reason": "pre-commit failed at mypy",
            "fix_rounds": 1,
            "fix_since": 1000.0,
            "fix_head": "aaa",
            "worked_at": 2000.0,
            "idle_since": 10_000.0 - ap.IDLE_SETTLE_S - 1,
        }
    )
    r.update(over)
    return r


# --- the pure gate ----------------------------------------------------------- #
def test_it_waits_for_the_agent_to_pick_the_note_up():
    action, detail = ap.next_action(
        _fixing(worked_at=500.0, idle_since=None), _snap(stage="interrupt", dirty=True)
    )
    assert action == "wait"
    assert detail["reason"].startswith("waiting for the agent to pick up the fix")
    assert "pre-commit failed at mypy" in detail["reason"]


def test_it_waits_while_the_agent_works_and_until_it_settles():
    working = ap.next_action(_fixing(), _snap(stage="interrupt", activity="working"))
    assert working == (
        "wait",
        {"reason": "the agent is fixing it: pre-commit failed at mypy"},
    )
    fresh = ap.next_action(_fixing(idle_since=None), _snap(stage="interrupt"))
    assert fresh[0] == "wait" and fresh[1]["mark_idle"] is True
    settling = ap.next_action(_fixing(idle_since=9_990.0), _snap(stage="interrupt"))
    assert settling[0] == "wait" and "confirming" in settling[1]["reason"]
    limited = ap.next_action(_fixing(), _snap(limited=True))
    assert limited[0] == "wait" and "usage limit" in limited[1]["reason"]


def test_the_agents_own_commit_goes_straight_on_to_the_push():
    """The reported bug: the agent fixed the hook and committed, and nothing
    pushed or opened the PR."""
    action, detail = ap.next_action(
        _fixing(), _snap(stage="committed", head="bbb", dirty=False)
    )
    assert action == "push" and detail["fixed"] is True


def test_an_uncommitted_fix_is_recommitted():
    action, detail = ap.next_action(
        _fixing(), _snap(stage="interrupt", failed_hook="mypy", dirty=True)
    )
    assert action == "commit" and detail["fixed"] is True


def test_a_fixed_check_runs_again_before_the_push():
    rec = _fixing(fixing="check", fix_reason="checks failed — fix them and re-run")
    action, detail = ap.next_action(
        rec,
        _snap(
            stage="committed",
            head="bbb",
            check_required=True,
            check={"state": "failed", "stale": True},
        ),
    )
    assert action == "run_check" and detail["fixed"] is True


def test_a_fix_turn_that_changed_nothing_halts_with_the_first_reason():
    rec = _fixing(
        depth="merge", fixing="ci", fix_reason="CI failed on the PR — not merging"
    )
    action, detail = ap.next_action(rec, _snap(stage="pr", pr_checks="failed"))
    assert action == "stop" and detail["unfixed"] is True
    assert detail["reason"] == "CI failed on the PR — not merging"
    assert not detail.get("fix"), "never told the same thing twice"


def test_the_target_reached_by_the_fix_finishes_the_run():
    action, detail = ap.next_action(
        _fixing(depth="commit"), _snap(stage="committed", head="bbb")
    )
    assert action == "done" and detail["fixed"] is True


# --- the note ---------------------------------------------------------------- #
def test_a_handoff_note_says_the_run_carries_on():
    detail = {"fix": "precommit", "hook": "mypy", "step": "mypy"}
    note = ap.failure_note(detail, resumes=True)
    assert note.startswith("Fast-track paused: the pre-commit step mypy failed")
    assert "Fast-track carries on from your commit" in note
    assert "tell the user" in note and "\n" not in note
    assert ap.failure_note(detail).startswith("Fast-track stopped:")


# --- the store --------------------------------------------------------------- #
def test_hand_to_agent_keeps_the_run_and_counts_the_round():
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=2, issues={"push": 2}, attempts={"mypy": 1})
    detail = {"fix": "precommit", "reason": "pre-commit failed at mypy"}
    rec = ap.hand_to_agent("s1", detail, head="aaa", now=50.0)
    assert rec["state"] == "running" and rec["fixing"] == "precommit"
    assert (rec["fix_rounds"], rec["fix_since"], rec["fix_head"]) == (1, 50.0, "aaa")
    assert rec["issues"] == {} and rec["attempts"] == {} and rec["commits"] == 2
    assert "pre-commit failed at mypy" in rec["note"]
    assert ap.dto("s1")["state"] == "running"
    assert ap.hand_to_agent("s1", {"reason": "no origin"}) is None, "only fixable stops"
    ap.update("s1", fix_rounds=ap.MAX_FIX_ROUNDS)
    assert not ap.can_hand_off(ap.get("s1"))
    assert ap.halt("s1", "x")["fixing"] == "", "a halt ends the hand-off"


# --- the driver -------------------------------------------------------------- #
def _notes(title="s1"):
    return [
        m
        for m in mailbox.fetch(title, mark_read=False)["messages"]
        if m["kind"] == "note"
    ]


def _step(monkeypatch, tmp_path, snap, acted=None):
    seen = (ap.get("s1"), object(), str(tmp_path), snap)
    monkeypatch.setattr(driver, "_autopilot_observe", lambda title: seen)

    async def act(title, wt, rec, snap, action, detail):
        (acted if acted is not None else []).append(action)

    monkeypatch.setattr(driver, "_autopilot_act", act)
    asyncio.run(driver._autopilot_step("s1"))


def test_a_failed_hook_is_handed_off_then_the_fix_is_pushed(tmp_path, monkeypatch):
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, step="commit")
    # 1. The hook fails: the agent is told and the run stays armed.
    _step(
        monkeypatch, tmp_path, _snap(stage="interrupt", failed_hook="mypy", dirty=True)
    )
    rec = ap.get("s1")
    assert rec["state"] == "running" and rec["fixing"] == "precommit"
    (note,) = _notes()
    assert note["text"].startswith("Fast-track paused:")
    # 2. The agent works the fix and commits it itself, then settles.
    ap.update("s1", worked_at=rec["fix_since"] + 1, idle_since=0.0)
    acted = []
    now = rec["fix_since"] + 100
    _step(monkeypatch, tmp_path, _snap(stage="committed", head="bbb", now=now), acted)
    assert acted == ["push"]
    rec = ap.get("s1")
    assert rec["state"] == "running" and rec["fixing"] == "", "hand-off is over"
    assert len(_notes()) == 1


def test_out_of_handoffs_it_halts_as_before(tmp_path, monkeypatch):
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, fix_rounds=ap.MAX_FIX_ROUNDS)
    _step(
        monkeypatch, tmp_path, _snap(stage="interrupt", failed_hook="mypy", dirty=True)
    )
    rec = ap.get("s1")
    assert rec["state"] == "halted" and rec["reason"] == "pre-commit failed at mypy"
    assert rec["note"] == driver._TOLD_AGENT
    (note,) = _notes()
    assert note["text"].startswith("Fast-track stopped:")


def test_an_unchanged_fix_turn_halts_once_with_its_own_note(tmp_path, monkeypatch):
    ap.arm("s1", depth="merge", message="m")
    ap.update(
        "s1",
        commits=1,
        step="pr",
        fixing="ci",
        fix_reason="CI failed on the PR — not merging",
        fix_rounds=1,
        fix_since=100.0,
        fix_head="aaa",
        worked_at=200.0,
        idle_since=0.0,
    )
    _step(monkeypatch, tmp_path, _snap(stage="pr", pr_checks="failed", now=5000.0))
    rec = ap.get("s1")
    assert rec["state"] == "halted"
    assert rec["reason"] == "CI failed on the PR — not merging"
    assert rec["note"] == driver._UNFIXED and rec["fixing"] == ""
    assert _notes() == [], "no second note for the same failure"


def test_notes_off_still_halts(tmp_path, monkeypatch):
    from backend.config import settings as settings_mod

    s = settings_mod.load_settings()
    s.repository.fasttrack_failure_notes = False
    settings_mod.save_settings(s)
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1)
    _step(
        monkeypatch, tmp_path, _snap(stage="interrupt", failed_hook="mypy", dirty=True)
    )
    rec = ap.get("s1")
    assert rec["state"] == "halted" and rec["fixing"] == ""
    assert _notes() == []


def test_a_team_run_member_still_halts_for_its_run(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "_run_member", lambda title: {"title": title})
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1)
    _step(
        monkeypatch, tmp_path, _snap(stage="interrupt", failed_hook="mypy", dirty=True)
    )
    assert ap.get("s1")["state"] == "halted"
