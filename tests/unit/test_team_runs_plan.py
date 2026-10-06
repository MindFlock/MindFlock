"""The team-run planner (``team_runs.plan_actions``) — pure, table-tested.

Every transition in the task state machine is pinned from an observation dict:
no git, no tmux, no clock. The driver's own tests (test_team_run_driver) cover
the side effects these actions name.
"""

from __future__ import annotations

import pytest

from backend.web.core import team_runs as tr

NOW = 100_000.0


def _task(tid="t1", state="working", **kw):
    base = {
        "id": tid,
        "kind": "task",
        "text": "do " + tid,
        "title": "w-" + tid,
        "state": state,
        "started_at": NOW - 3600,
        "incarnation": NOW - 3600,
    }
    base.update(kw)
    return base


def _run(*tasks, **kw):
    base = {
        "id": "r_test01",
        "name": "Q4 payments",
        "state": "running",
        "policy": {"lane": "pr"},
        "concurrency": 3,
        "tasks": list(tasks),
    }
    base.update(kw)
    return tr._normalize(base)


def _o(**kw):
    """A task observation: a present, idle row of OUR incarnation."""
    base = {
        "present": True,
        "created_at": NOW - 3600,
        "activity": "idle",
        "activity_since": NOW - 30,
        "autopilot": {"state": "running", "depth": "pr", "step": "", "lane": "pr"},
        "progress": "p0",
        "queue_ids": [],
    }
    base.update(kw)
    return base


def _plan(run, tasks=None, now=NOW, **obs):
    o = {"tasks": tasks or {}}
    o.update(obs)
    return tr.plan_actions(run, o, now)


def _ops(acts, op):
    return [a for a in acts if a["op"] == op]


def _states(acts):
    return {a["task"]: (a["state"], a["reason"]) for a in _ops(acts, "state")}


# --------------------------------------------------------------------------- #
# Concurrency and the queue
# --------------------------------------------------------------------------- #
class TestStarts:
    def test_fills_free_slots_in_order(self):
        run = _run(*[_task("t%d" % i, "queued") for i in range(1, 6)])
        starts = [a["task"] for a in _ops(_plan(run), "start")]
        assert starts == ["t1", "t2", "t3"]

    def test_running_members_hold_their_slots(self):
        run = _run(
            _task("t1"), _task("t2"), _task("t3", "queued"), _task("t4", "queued")
        )
        acts = _plan(run, {"t1": _o(), "t2": _o(title="w-t2")})
        assert [a["task"] for a in _ops(acts, "start")] == ["t3"]

    def test_needs_you_holds_a_slot(self):
        """Deliberate: it keeps parallel prompts down while you are away."""
        run = _run(
            *[_task("t%d" % i, "needs_you", reason="restart") for i in (1, 2, 3)],
            _task("t4", "queued"),
        )
        assert _ops(_plan(run, {"t1": _o(), "t2": _o(), "t3": _o()}), "start") == []

    def test_start_now_jumps_the_cap_once(self):
        run = _run(
            _task("t1"),
            _task("t2", "queued"),
            _task("t3", "queued", start_now=True),
            concurrency=1,
        )
        assert [a["task"] for a in _ops(_plan(run, {"t1": _o()}), "start")] == ["t3"]

    def test_a_backoff_is_honoured(self):
        run = _run(_task("t1", "queued", retry_at=NOW + 10))
        assert _ops(_plan(run), "start") == []
        assert _ops(_plan(run, now=NOW + 11), "start")

    def test_a_paused_group_starts_nothing(self):
        run = _run(_task("t1", "queued"), paused=True, pause_reason="user")
        assert _ops(_plan(run), "start") == []

    def test_a_usage_limit_blocks_starts_on_that_provider_only(self):
        run = _run(_task("t1", "queued"))
        held = _plan(run, provider="claude", limited_providers=["claude"])
        assert _ops(held, "start") == []
        other = _plan(run, provider="codex", limited_providers=["claude"])
        assert _ops(other, "start")


# --------------------------------------------------------------------------- #
# starting
# --------------------------------------------------------------------------- #
class TestStarting:
    def test_our_row_appearing_means_working(self):
        run = _run(_task("t1", "starting", started_at=NOW - 5, incarnation=0))
        acts = _plan(run, {"t1": _o(created_at=NOW - 4, branch="mindflock/w-t1")})
        (st,) = _ops(acts, "state")
        assert st["state"] == "working"
        assert st["fields"]["incarnation"] == NOW - 4
        assert st["fields"]["branch"] == "mindflock/w-t1"

    def test_an_older_session_under_the_title_is_not_ours(self):
        """Never adopt silently: a different created_at is somebody else's."""
        run = _run(_task("t1", "starting", started_at=NOW - 5, incarnation=0))
        acts = _plan(run, {"t1": _o(created_at=NOW - 7200)})
        assert _states(acts)["t1"] == ("needs_you", "restart")

    def test_a_pending_row_is_waited_for(self):
        run = _run(_task("t1", "starting", started_at=NOW - 5, incarnation=0))
        assert (
            _ops(_plan(run, {"t1": {"present": False, "pending": True}}), "state") == []
        )

    def test_create_failures_back_off_then_fail(self):
        t = _task("t1", "starting", started_at=NOW - 5, incarnation=0)
        obs = {"present": False, "create_failed": "boom", "create_failed_at": NOW - 1}
        (first,) = _ops(_plan(_run(t), {"t1": obs}), "state")
        assert first["state"] == "queued"
        assert first["fields"]["retry_at"] == NOW + 30
        t2 = dict(t, attempts={"create": 1, "ship": 0})
        (second,) = _ops(_plan(_run(t2), {"t1": obs}), "state")
        assert second["fields"]["retry_at"] == NOW + 60
        t3 = dict(t, attempts={"create": 2, "ship": 0})
        (third,) = _ops(_plan(_run(t3), {"t1": obs}), "state")
        assert (third["state"], third["reason"]) == ("failed", "create")
        assert third["detail"] == "boom"

    def test_an_old_failure_is_not_this_attempts(self):
        run = _run(_task("t1", "starting", started_at=NOW - 5, incarnation=0))
        obs = {"present": False, "create_failed": "old", "create_failed_at": NOW - 900}
        assert _ops(_plan(run, {"t1": obs}), "state") == []


# --------------------------------------------------------------------------- #
# The restart table (§3.5)
# --------------------------------------------------------------------------- #
class TestRestartTable:
    def test_our_row_rederives_the_state(self):
        run = _run(_task("t1", "working"))
        acts = _plan(run, {"t1": _o()}, boot=True)
        assert "t1" not in _states(acts)

    def test_same_title_different_created_at_needs_you(self):
        run = _run(_task("t1", "working"))
        acts = _plan(run, {"t1": _o(created_at=NOW - 10)}, boot=True)
        st = _ops(acts, "state")[0]
        assert (st["state"], st["reason"]) == ("needs_you", "restart")
        assert "used by another session" in st["detail"]

    def test_starting_with_a_pending_row_waits(self):
        run = _run(_task("t1", "starting", incarnation=0))
        acts = _plan(run, {"t1": {"present": False, "pending": True}}, boot=True)
        assert _ops(acts, "state") == []

    def test_no_row_but_the_branch_is_there_offers_a_recreate(self):
        """No double spawn: the branch's work is never silently doubled."""
        for st in ("starting", "working"):
            run = _run(_task("t1", st, incarnation=0 if st == "starting" else NOW))
            obs = {"present": False, "pending": False, "branch_exists": True}
            acts = _plan(run, {"t1": obs}, boot=True)
            got = _ops(acts, "state")[0]
            assert (got["state"], got["reason"]) == ("needs_you", "restart"), st
            assert "existing branch" in got["detail"]
            assert _ops(acts, "start") == []

    def test_starting_with_no_row_and_no_branch_retries_the_create(self):
        run = _run(_task("t1", "starting", incarnation=0))
        obs = {"present": False, "pending": False, "branch_exists": False}
        (st,) = _ops(_plan(run, {"t1": obs}, boot=True), "state")
        assert st["state"] == "queued"
        assert st["fields"]["attempts"]["create"] == 1

    def test_a_vanished_member_gets_a_grace_outside_boot(self):
        run = _run(_task("t1", "working"))
        acts = _plan(run, {"t1": {"present": False}})
        assert _ops(acts, "state") == []
        assert _ops(acts, "seen")[0]["fields"] == {"missing_since": NOW}
        run2 = _run(_task("t1", "working", missing_since=NOW - tr.MISSING_GRACE_S - 1))
        st = _ops(_plan(run2, {"t1": {"present": False}}), "state")[0]
        assert (st["state"], st["reason"]) == ("needs_you", "restart")


# --------------------------------------------------------------------------- #
# Working
# --------------------------------------------------------------------------- #
class TestWorking:
    def test_a_member_you_deleted_is_cancelled_and_never_recreated(self):
        run = _run(_task("t1", "working"))
        acts = _plan(run, {"t1": {"present": False, "deleted_at": NOW - 2}})
        assert _states(acts)["t1"] == ("cancelled", "")
        assert _ops(acts, "start") == []

    def test_a_dialog_waits_on_you_and_clears_itself(self):
        run = _run(_task("t1", "working"))
        assert _states(_plan(run, {"t1": _o(activity="clarify")}))["t1"] == (
            "needs_you",
            "prompt",
        )
        back = _run(_task("t1", "needs_you", reason="prompt"))
        assert _states(_plan(back, {"t1": _o(activity="working")}))["t1"] == (
            "working",
            "",
        )

    def test_a_blocked_report_disarms_and_asks(self):
        run = _run(_task("t1", "working"))
        rep = {"status": "blocked", "summary": "need the API key", "ts": NOW - 1}
        acts = _plan(run, {"t1": _o(report=rep)})
        assert _ops(acts, "disarm")
        assert _states(acts)["t1"] == ("needs_you", "blocked")

    def test_an_old_report_is_not_news(self):
        run = _run(_task("t1", "working"))
        rep = {"status": "blocked", "summary": "x", "ts": NOW - 7200}
        assert _ops(_plan(run, {"t1": _o(report=rep)}), "disarm") == []

    def test_the_autopilot_acting_means_shipping(self):
        run = _run(_task("t1", "working"))
        ap = {"state": "running", "depth": "pr", "step": "push", "lane": "pr"}
        assert _states(_plan(run, {"t1": _o(autopilot=ap)}))["t1"] == ("shipping", "")

    def test_lane_reached_means_shipped(self):
        run = _run(_task("t1", "shipping"))
        ap = {"state": "done", "depth": "pr", "step": "pr", "url": "https://x/pull/7"}
        st = _ops(_plan(run, {"t1": _o(autopilot=ap)}), "state")[0]
        assert st["state"] == "shipped"
        assert st["fields"]["pr_url"] == "https://x/pull/7"

    def test_ask_first_parks_for_approval(self):
        run = _run(_task("t1", "working"), policy={"lane": "pr", "ask_first": True})
        ap = {"state": "done", "depth": "commit", "ask_first": True, "lane": "pr"}
        assert _states(_plan(run, {"t1": _o(autopilot=ap)}))["t1"] == (
            "needs_you",
            "approve",
        )

    def test_approval_rearms_back_to_working(self):
        run = _run(_task("t1", "needs_you", reason="approve"))
        ap = {"state": "running", "depth": "pr", "step": "", "lane": "pr"}
        assert _states(_plan(run, {"t1": _o(autopilot=ap)}))["t1"] == ("working", "")

    def test_the_user_disarming_switches_the_task_to_leave_it(self):
        run = _run(_task("t1", "working"))
        acts = _plan(run, {"t1": _o(autopilot=None)})
        assert {"lane": "leave"} in [a.get("fields") for a in _ops(acts, "seen")]

    def test_a_held_member_is_not_mistaken_for_a_disarm(self):
        run = _run(_task("t1", "working", held=True))
        acts = _plan(run, {"t1": _o(autopilot=None)})
        assert not any(a.get("fields") == {"lane": "leave"} for a in acts)

    def test_a_paused_group_only_observes(self):
        run = _run(_task("t1", "working"), paused=True)
        ap = {"state": "halted", "reason": "pre-commit failed at black"}
        acts = _plan(run, {"t1": _o(autopilot=ap)})
        assert _ops(acts, "fix") == [] and _ops(acts, "state") == []


class TestShipHalts:
    def test_a_hook_failure_gets_a_fix_prompt_then_escalates(self):
        ap = {"state": "halted", "reason": "pre-commit failed at mypy"}
        run = _run(_task("t1", "shipping"))
        (fix,) = _ops(_plan(run, {"t1": _o(autopilot=ap)}), "fix")
        assert fix["hook"] == "mypy"
        twice = _run(_task("t1", "shipping", attempts={"create": 0, "ship": 2}))
        st = _ops(_plan(twice, {"t1": _o(autopilot=ap)}), "state")[0]
        assert (st["state"], st["reason"]) == ("needs_you", "ship_halted")
        assert st["detail"] == "mypy failed twice"

    @pytest.mark.parametrize(
        "reason",
        [
            "no origin remote — add one to push",
            "committed, but this session is on main — make a branch to push or PR",
            "needs gh or a GitHub token to file the PR",
            "push refused: red zone config/ breached",
        ],
    )
    def test_what_only_a_person_can_fix_escalates_at_once(self, reason):
        run = _run(_task("t1", "shipping"))
        acts = _plan(run, {"t1": _o(autopilot={"state": "halted", "reason": reason})})
        assert _ops(acts, "fix") == []
        assert _states(acts)["t1"] == ("needs_you", "ship_halted")

    def test_nothing_to_ship_yet_rearms_instead_of_failing(self):
        ap = {
            "state": "halted",
            "reason": "the agent finished without changing anything",
        }
        acts = _plan(_run(_task("t1", "working")), {"t1": _o(autopilot=ap)})
        assert _ops(acts, "arm") and "t1" not in _states(acts)

    def test_red_ci_on_the_pr_is_shipped_not_merged(self):
        run = _run(_task("t1", "shipping"), policy={"lane": "merge"})
        ap = {
            "state": "halted",
            "reason": "CI failed on the PR — not merging",
            "url": "u",
        }
        st = _ops(_plan(run, {"t1": _o(autopilot=ap)}), "state")[0]
        assert st["state"] == "shipped" and st["fields"]["flag"] == "checks_failed"


class TestStuck:
    def _worked(self, **kw):
        return _task("t1", "working", worked=True, progress="p0", **kw)

    def test_idle_with_proof_of_work_and_no_progress_is_nudged(self):
        run = _run(self._worked(progress_at=NOW - 700))
        acts = _plan(run, {"t1": _o(activity_since=NOW - 700)})
        assert _ops(acts, "nudge")

    def test_a_cpu_only_phantom_never_escalates(self):
        """No corroborated work (a CPU blip paints the chip, arms nothing)."""
        run = _run(_task("t1", "working", progress="p0", progress_at=NOW - 9000))
        acts = _plan(run, {"t1": _o(activity_since=NOW - 9000, worked=False)})
        assert _ops(acts, "nudge") == [] and _ops(acts, "state") == []

    def test_not_before_the_dwell(self):
        run = _run(self._worked(progress_at=NOW - 100))
        assert _ops(_plan(run, {"t1": _o(activity_since=NOW - 100)}), "nudge") == []

    def test_progress_resets_the_clock(self):
        run = _run(self._worked(progress_at=NOW - 9000))
        acts = _plan(run, {"t1": _o(activity_since=NOW - 9000, progress="p1")})
        assert _ops(acts, "nudge") == []
        seen = _ops(acts, "seen")[0]["fields"]
        assert seen["progress"] == "p1" and seen["progress_at"] == NOW

    def test_a_queued_nudge_must_be_delivered_before_the_next_one(self):
        run = _run(self._worked(progress_at=NOW - 9000, nudges=1, nudge_id="q1"))
        held = _plan(run, {"t1": _o(activity_since=NOW - 9000, queue_ids=["q1"])})
        assert _ops(held, "nudge") == []
        delivered = _plan(run, {"t1": _o(activity_since=NOW - 9000, queue_ids=[])})
        # Delivery is recorded now, and the dwell restarts from it.
        assert any("nudge_seen_at" in (a.get("fields") or {}) for a in delivered)
        assert _ops(delivered, "nudge") == []

    def test_after_two_delivered_nudges_it_escalates(self):
        run = _run(
            self._worked(
                progress_at=NOW - 9000,
                nudges=2,
                nudge_id="q2",
                nudge_seen_at=NOW - 700,
            )
        )
        st = _ops(_plan(run, {"t1": _o(activity_since=NOW - 9000)}), "state")[0]
        assert (st["state"], st["reason"]) == ("needs_you", "stuck")

    def test_a_usage_limit_is_not_idleness(self):
        run = _run(self._worked(progress_at=NOW - 9000))
        acts = _plan(
            run, {"t1": _o(activity="limit", activity_since=NOW - 9000, limited=True)}
        )
        assert _ops(acts, "nudge") == []

    def test_real_progress_brings_a_stuck_task_back(self):
        run = _run(self._worked(progress_at=NOW - 9000, nudges=2))
        run["tasks"][0].update(state="needs_you", reason="stuck")
        acts = _plan(run, {"t1": _o(progress="p9")})
        assert _states(acts)["t1"] == ("working", "")


class TestLeaveLane:
    def test_done_is_the_clis_own_turn_ended(self):
        run = _run(_task("t1", "working"), policy={"lane": "leave"})
        acts = _plan(run, {"t1": _o(autopilot=None, turn_ended_at=NOW - 1)})
        assert _states(acts)["t1"] == ("shipped", "")

    def test_a_turn_end_before_this_start_does_not_count(self):
        run = _run(_task("t1", "working"), policy={"lane": "leave"})
        acts = _plan(run, {"t1": _o(autopilot=None, turn_ended_at=NOW - 7200)})
        assert "t1" not in _states(acts)

    def test_arming_a_lane_on_a_leave_task_is_honoured(self):
        run = _run(_task("t1", "working"), policy={"lane": "leave"})
        ap = {"state": "running", "depth": "commit", "lane": "commit"}
        acts = _plan(run, {"t1": _o(autopilot=ap)})
        assert {"lane": "commit"} in [a.get("fields") for a in _ops(acts, "seen")]


class TestRunLevel:
    def test_budget_reached_pauses(self):
        run = _run(_task("t1"), budget_usd=20.0)
        assert _ops(_plan(run, {"t1": _o()}, cost=20.5), "pause") == [
            {"op": "pause", "reason": "budget"}
        ]
        assert _ops(_plan(run, {"t1": _o()}, cost=3.0), "pause") == []

    def test_all_terminal_finishes(self):
        run = _run(_task("t1", "shipped"), _task("t2", "skipped"))
        assert _ops(_plan(run), "finish")[0]["state"] == "done"
        bad = _run(_task("t1", "shipped"), _task("t2", "failed"))
        assert _ops(_plan(bad), "finish")[0]["state"] == "done_with_failures"

    def test_a_transition_this_pass_counts_toward_finishing(self):
        run = _run(_task("t1", "shipping"))
        ap = {"state": "done", "depth": "pr", "step": "pr", "url": "u"}
        assert _ops(_plan(run, {"t1": _o(autopilot=ap)}), "finish")

    def test_a_finished_run_plans_nothing(self):
        run = _run(_task("t1", "queued"), state="done")
        assert _plan(run) == []


class TestApply:
    def test_a_state_change_is_logged_and_returned(self):
        run = _run(_task("t1", "working"))
        act = tr._to(run["tasks"][0], "needs_you", "stuck", "stalled twice")
        assert tr.apply(run, act, NOW) == ("working", "needs_you")
        t = run["tasks"][0]
        assert (t["state"], t["reason"], t["detail"]) == (
            "needs_you",
            "stuck",
            "stalled twice",
        )
        assert run["events"][-1]["kind"] == "needs_you"

    def test_coming_back_from_stuck_resets_the_nudges(self):
        run = _run(_task("t1", "needs_you", reason="stuck", nudges=2, nudge_id="q"))
        tr.apply(run, tr._to(run["tasks"][0], "working"), NOW)
        assert run["tasks"][0]["nudges"] == 0 and run["tasks"][0]["nudge_id"] == ""

    def test_finish_writes_the_summary(self):
        run = _run(
            _task("t1", "shipped", pr_url="https://x/pull/1"),
            _task("t2", "failed", detail="Jira 502"),
        )
        tr.apply(run, {"op": "finish", "state": "done_with_failures"}, NOW)
        assert run["state"] == "done_with_failures" and run["finished_at"] == NOW
        s = run["summary"]
        assert s["shipped"] == 1 and s["failed"] == 1 and s["announced"] is False
        assert "https://x/pull/1" in s["text_md"] and "Jira 502" in s["text_md"]

    def test_the_summary_says_why_a_line_left(self):
        # Found on a real server: a cancelled group's summary said every line
        # was "removed by you", including the one whose session it kept.
        run = _run(
            _task("t1", "cancelled", detail="group cancelled — its session was kept"),
            _task("t2", "cancelled"),
            _task("t3", "skipped"),
        )
        tr.apply(run, {"op": "finish", "state": "cancelled"}, NOW)
        md = tr.summarize(run, NOW)["text_md"]
        assert "(group cancelled — its session was kept)" in md
        assert "(removed by you)" in md and "(skipped)" in md

    def test_events_are_capped(self):
        run = _run(_task("t1"))
        for i in range(tr.EVENTS_MAX + 20):
            tr.log_event(run, NOW + i, "note", "t1", str(i))
        assert len(run["events"]) == tr.EVENTS_MAX
        assert run["events"][-1]["text"] == str(tr.EVENTS_MAX + 19)
