"""Regression tests for the team-runs review (2026-10-05): each-their-own
groups, against the driver test's fakes (``env``: sessions faked at the
create/launch seams; the run, autopilot, queue and ledger stores real, in tmp).

Pinned here: Retry never arms a stranger and never leaves the old session
shipping; a pause (the budget's included) stops new starts and keeps a typed
commit message; the ingestion ledger is never erased, never leaked, never
double-started; a closed session's branch is never silently reused.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

from backend.ticket_ingestion import state as ledger
from backend.ticket_ingestion.models import ProcessingRecord
from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import lanes
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr
from tests.unit.test_team_run_driver import (  # noqa: F401 — the fixture
    _Inst,
    _make,
    _row,
    _states,
    _step,
    env,
)


def _ticket(env, n):
    env.listing.append(
        {
            "source": "sc",
            "id": str(n),
            "slug": "sc-%d" % n,
            "session": "sc-%d" % n,
            "name": "T%d" % n,
        }
    )


def _statuses(env):
    return ledger.load_processed_story_statuses(env.root)


def _entries(env):
    try:
        return json.loads((env.root / "state.json").read_text())["processed_stories"]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
# Finding 4: Retry on a reused title never arms the stranger
# --------------------------------------------------------------------------- #
class TestRetryNeverArmsAStranger:
    def _reused(self, env):
        rid = _make(env, n=1, lane="pr")
        run = _step(rid)
        t = run["tasks"][0]["title"]
        _row(env, t, activity="idle")
        _step(rid)
        # The member is gone; an unrelated session now holds its title.
        ap.disarm(t)
        env.instances[t] = _Inst(t, created=time.time() + 500)
        run = _step(rid)
        assert run["tasks"][0]["state"] == "needs_you"
        assert "another session" in run["tasks"][0]["detail"]
        return rid, run["tasks"][0]

    def test_retry_is_refused_and_nothing_is_armed(self, env):
        rid, task = self._reused(env)
        with pytest.raises(drv.RunError) as err:
            drv.retry(rid, task["id"], fresh=False)
        assert err.value.status == 409 and "Retry fresh" in err.value.message
        assert ap.get(task["title"]) is None

    def test_retry_fresh_leaves_the_stranger_alone(self, env):
        rid, task = self._reused(env)
        ap.arm(task["title"], "commit", lane="commit", source="session")  # its owner's
        out = drv.retry(rid, task["id"], fresh=True)
        assert out["title"] != task["title"]
        assert ap.get(task["title"])["lane"] == "commit"  # untouched

    def test_a_record_the_run_armed_before_the_stranger_is_dropped(self, env):
        rid = _make(env, n=1, lane="pr")
        run = _step(rid)
        t = run["tasks"][0]["title"]
        assert ap.get(t)["source"] == "run"
        env.instances[t] = _Inst(t, created=time.time() + 500)
        run = _step(rid)
        assert run["tasks"][0]["reason"] == "restart"
        # Our record would have driven the stranger: gone.
        assert ap.get(t) is None


# --------------------------------------------------------------------------- #
# Finding 13: Retry fresh stops the old session from shipping
# --------------------------------------------------------------------------- #
def test_fresh_retry_disarms_the_old_session(env):
    rid = _make(env, n=1, lane="pr")
    run = _step(rid)
    old = run["tasks"][0]["title"]
    assert ap.get(old)["lane"] == "pr"
    with tr.edit(rid) as r:
        tr.apply(r, tr._to(r["tasks"][0], "needs_you", "stuck", "stalled"), time.time())
    drv.retry(rid, run["tasks"][0]["id"], fresh=True)
    assert ap.get(old) is None  # its session stays; it no longer ships
    assert old in env.instances


# --------------------------------------------------------------------------- #
# Finding 15: the pass that crosses the budget starts (and arms) nothing
# --------------------------------------------------------------------------- #
def test_the_budget_pause_starts_nothing_in_the_same_pass(env):
    rid = _make(env, n=3, concurrency=1, budget_usd=1.0)
    run = _step(rid)
    t0 = run["tasks"][0]["title"]
    _row(env, t0, activity="working")
    _step(rid)
    _row(env, t0, tokens_cost=5.0, activity="working")
    ap.update(t0, state="done", step="pr", url="https://x/pull/1")
    run = _step(rid)
    assert run["paused"] and run["pause_reason"] == "budget"
    assert [t for t in run["tasks"] if t["state"] == "starting"] == []
    assert len(env.created) == 1


def test_a_start_that_lands_after_a_pause_is_held_not_armed(env, monkeypatch):
    rid = _make(env, n=1, lane="pr")
    real = env.created

    async def _slow_create(payload):
        drv.pause(rid)  # the user pauses while the session is being created
        inst = _Inst(payload["title"])
        env.instances[payload["title"]] = inst
        real.append(payload)
        return 202, {
            "title": payload["title"],
            "created_at": inst.CreatedAt.timestamp(),
        }

    monkeypatch.setattr(server._session_create, "create_result", _slow_create)
    run = _step(rid)
    t = run["tasks"][0]
    assert t["held"] is True and ap.get(t["title"]) is None
    drv.resume(rid)
    assert ap.get(t["title"])["lane"] == "pr"


# --------------------------------------------------------------------------- #
# Finding 16: pause/resume keeps the commit message a person typed
# --------------------------------------------------------------------------- #
def test_pause_resume_keeps_an_edited_commit_message(env):
    rid = _make(env, n=1, lane="commit", ask_first=True)
    run = _step(rid)
    t = run["tasks"][0]["title"]
    ap.update(t, message="feat: the message I typed", message_auto=False)
    drv.pause(rid)
    assert ap.get(t) is None
    drv.resume(rid)
    rec = ap.get(t)
    assert rec["message"] == "feat: the message I typed"
    assert rec["message_auto"] is False


# --------------------------------------------------------------------------- #
# Finding 22: a model-written subject is never re-used as a human's message
# --------------------------------------------------------------------------- #
def test_a_generated_subject_is_not_carried_into_a_new_arm(env):
    rid = _make(env, n=1, lane="commit")
    run = _step(rid)
    t = run["tasks"][0]["title"]
    # Commit #1's subject, written from its diff and written back.
    ap.update(t, message="auth: fix token refresh race", message_auto=False)
    ap.update(t, message_written=True)
    ap.finish(t)
    lanes.arm_session(t, "commit", ask_first=True, require_workspace=False)
    assert ap.get(t)["message"] != "auth: fix token refresh race"
    # …nor by a team-run re-arm.
    ap.update(t, message="auth: fix token refresh race", message_written=True)
    drv._arm(tr.load(rid), tr.load(rid)["tasks"][0])
    assert ap.get(t)["message"] == "task number 1"
    assert ap.get(t)["message_auto"] is True


# --------------------------------------------------------------------------- #
# Finding 17: the ingestion ledger — never erased, never leaked, never doubled
# --------------------------------------------------------------------------- #
class TestLedger:
    def test_cancel_never_erases_a_tickets_history(self, env):
        _ticket(env, 7)
        ledger.record_processed_story(
            env.root,
            ProcessingRecord(
                story_id="sc-7",
                branch="sc-7",
                status="completed",
                processed_at=datetime.now(timezone.utc),
            ),
        )
        run, _ = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "7"}],
                    "policy": {"lane": "pr"},
                    "concurrency": 1,
                    "repo_path": env.repo,
                }
            )
        )
        assert _statuses(env) == {"sc-7": "in_flight"}
        drv.cancel(run["id"])
        # Handed back to its previous outcome — the history entry survives.
        assert _statuses(env) == {"sc-7": "completed"}
        assert [e["status"] for e in _entries(env)] == ["completed"]

    def test_a_ticket_ingestion_is_starting_is_left_out(self, env):
        _ticket(env, 8)
        ledger.record_processed_story(
            env.root,
            ProcessingRecord(
                story_id="sc-8",
                branch="sc-8",
                status="in_flight",
                processed_at=datetime.now(timezone.utc),
            ),
        )
        with pytest.raises(drv.RunError):  # nothing else to do
            asyncio.run(
                drv.create_run(
                    {
                        "items": [{"kind": "ticket", "source": "sc", "id": "8"}],
                        "policy": {"lane": "pr"},
                        "repo_path": env.repo,
                    }
                )
            )
        assert _statuses(env) == {"sc-8": "in_flight"}
        assert env.launched == []

    def test_a_run_never_hands_back_the_pipelines_marker(self, env):
        from backend.web.core import ticket_start

        ledger.record_processed_story(
            env.root,
            ProcessingRecord(
                story_id="sc-9",
                branch="sc-9",
                status="in_flight",
                processed_at=datetime.now(timezone.utc),
            ),
        )
        assert ticket_start.reserve("sc-9", "run:r_abcd") is False
        assert ticket_start.release_reservation("sc-9", "run:r_abcd") is False
        assert _statuses(env) == {"sc-9": "in_flight"}

    def test_a_lead_that_cannot_be_created_hands_its_tickets_back(
        self, env, monkeypatch
    ):
        _ticket(env, 31)

        async def _fail(payload):
            return 500, {"error": "boom"}

        monkeypatch.setattr(server._session_create, "create_result", _fail)
        with pytest.raises(drv.RunError):
            asyncio.run(
                drv.create_run(
                    {
                        "items": [{"kind": "ticket", "source": "sc", "id": "31"}],
                        "policy": {"lane": "pr", "grouping": "together"},
                        "repo_path": env.repo,
                    }
                )
            )
        assert tr.list_runs() == []
        assert _statuses(env) == {}

    def test_an_aborted_group_hands_its_queued_tickets_back(self, env):
        _ticket(env, 41)
        run, _ = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "41"}],
                    "policy": {"lane": "pr", "grouping": "together"},
                    "repo_path": env.repo,
                }
            )
        )
        rid = run["id"]
        lead = tr.load(rid)["lead"]["title"]
        env.instances.pop(lead)
        drv._DELETED[lead] = time.time() + 1
        run = _step(rid)
        assert run["state"] == "cancelled"
        assert _statuses(env) == {}

    def test_a_fresh_retry_reserves_the_ticket_not_its_new_title(self, env):
        _ticket(env, 5)
        run, _ = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "5"}],
                    "policy": {"lane": "pr"},
                    "repo_path": env.repo,
                }
            )
        )
        rid = run["id"]
        with tr.edit(rid) as r:
            tr.apply(r, tr._to(r["tasks"][0], "failed", "create", "x"), time.time())
        drv._settle_ledger  # noqa: B018 — (the pass hands it back on its own)
        _step(rid)
        assert _statuses(env) == {}
        drv.retry(rid, "t1", fresh=True)
        assert _statuses(env) == {"sc-5": "in_flight"}
        assert "sc-5-2" not in _statuses(env)

    def test_the_reaper_keeps_a_live_runs_reservation(self, env):
        from backend.web.core import ticket_start

        ticket_start.reserve("sc-3", "run:r_live")
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        reaped = ledger.reap_stale_in_flight(
            env.root,
            is_alive=lambda s: False,
            now=old.replace(year=2030),
            reservation_alive=lambda h: True,
        )
        assert reaped == [] and _statuses(env) == {"sc-3": "in_flight"}
        # Its holder gone: handed back (never "failed" — nothing ever started).
        reaped = ledger.reap_stale_in_flight(
            env.root, is_alive=lambda s: False, reservation_alive=lambda h: False
        )
        assert reaped == ["sc-3"] and _statuses(env) == {}


# --------------------------------------------------------------------------- #
# Finding 18: a task line never lands on a branch a closed session left
# --------------------------------------------------------------------------- #
def test_a_task_line_never_reuses_a_closed_sessions_branch(env):
    from backend.config import config as _config

    # An earlier, CLOSED session on this line kept its branch (and commits).
    old = tr.task_title("Fix the login bug")
    subprocess.run(
        ["git", "-C", env.repo, "branch", _config.LoadConfig().branch_prefix + old],
        check=True,
        capture_output=True,
    )
    rid = _make(env, n=0, items=[{"kind": "task", "text": "Fix the login bug"}])
    title = tr.load(rid)["tasks"][0]["title"]
    assert title != old and title.startswith(old)


# --------------------------------------------------------------------------- #
# Finding 21: adding lines to a one-for-all group
# --------------------------------------------------------------------------- #
def test_adding_to_a_split_before_its_plan_is_refused(env, monkeypatch):
    monkeypatch.setattr(server, "_mcp_unattachable", lambda program: None)
    run, _ = asyncio.run(
        drv.create_run(
            {
                "items": [{"kind": "task", "text": "Clean up auth"}],
                "policy": {"lane": "pr"},
                "repo_path": env.repo,
                "split": True,
            }
        )
    )
    with pytest.raises(drv.RunError) as err:
        asyncio.run(drv.add_tasks(run["id"], [{"kind": "task", "text": "extra"}]))
    assert "plan is not approved" in err.value.message


# --------------------------------------------------------------------------- #
# Finding 20: a restarted server reconciles at once; the boot pass says what
# it found; the lease spans processes
# --------------------------------------------------------------------------- #
class TestLeaseAndBoot:
    def _dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def test_a_dead_servers_lease_does_not_block_the_restart(self, env):
        rid = _make(env, n=1)
        path = tr._lease_path(rid)
        tr._write_json(
            path,
            {
                "owner": "old-boot",
                "owner_at": time.time(),
                "pid": self._dead_pid(),
                "host": tr._host(),
            },
        )
        assert tr.claim_lease(rid, "new-boot") is True
        assert tr.lease_holder(rid) == "new-boot"

    def test_a_live_servers_lease_still_holds(self, env):
        rid = _make(env, n=1)
        tr._write_json(
            tr._lease_path(rid),
            {"owner": "other", "owner_at": time.time(), "pid": 1, "host": tr._host()},
        )
        assert tr.claim_lease(rid, "me") is False

    def test_shutdown_hands_back_this_servers_leases(self, env):
        rid = _make(env, n=1)
        assert tr.claim_lease(rid, "boot-a") is True
        assert tr.release_leases("boot-a") == 1
        assert tr.lease_holder(rid) == ""

    def test_a_restart_escalation_the_boot_pass_finds_is_announced(self, env):
        """The boot pass seeds only what was ALREADY standing; a session it
        finds gone after the restart is said (on the next pass)."""
        rid = _make(env, n=1)
        _step(rid)
        _step(rid)
        title = tr.load(rid)["tasks"][0]["title"]
        env.instances.pop(title)
        run = _step(rid, boot=True)
        assert run["tasks"][0]["reason"] == "restart"
        _step(rid)
        evs = [e for e in env.emitted if e["event"] == "run.needs_you"]
        assert [e["new"] for e in evs] == ["restart"]
        assert evs[0]["data"]["key"]

    def test_a_standing_escalation_is_not_re_announced_at_boot(self, env):
        rid = _make(env, n=1)
        _step(rid)
        _step(rid)
        title = tr.load(rid)["tasks"][0]["title"]
        env.instances.pop(title)
        _step(rid)
        _step(rid)  # (missing grace) …
        with tr.edit(rid) as r:
            tr.apply(
                r, tr._to(r["tasks"][0], "needs_you", "restart", "gone"), time.time()
            )
        r = tr.load(rid)
        with tr.edit(rid) as rr:
            rr["announced"] = [tr.announce_key(rr, rr["tasks"][0], "restart")]
        before = len([e for e in env.emitted if e["event"] == "run.needs_you"])
        _step(rid, boot=True)
        _step(rid)
        after = len([e for e in env.emitted if e["event"] == "run.needs_you"])
        assert after == before
        assert r


# --------------------------------------------------------------------------- #
# Finding 19: one-for-all never commits under "Leave it" or a per-commit ask
# --------------------------------------------------------------------------- #
def test_one_for_all_refuses_leave_it(env):
    with pytest.raises(drv.RunError) as err:
        asyncio.run(
            drv.create_run(
                {
                    "items": [
                        {"kind": "task", "text": "a thing"},
                        {"kind": "task", "text": "another thing"},
                    ],
                    "policy": {"lane": "leave", "grouping": "together"},
                    "repo_path": env.repo,
                }
            )
        )
    assert "commits each line" in err.value.message
    assert tr.list_runs() == [] and env.created == []


def test_one_for_all_ask_first_is_the_release(env):
    run, _ = asyncio.run(
        drv.create_run(
            {
                "items": [
                    {"kind": "task", "text": "a thing"},
                    {"kind": "task", "text": "another thing"},
                ],
                "policy": {
                    "lane": "pr",
                    "grouping": "together",
                    "ask_first": True,
                    "release": "auto",
                },
                "repo_path": env.repo,
            }
        )
    )
    pol = tr.load(run["id"])["policy"]
    assert pol["release"] == "ask" and pol["ask_first"] is False


# --------------------------------------------------------------------------- #
# Finding 27: a ticket someone else is starting is never armed by the group
# --------------------------------------------------------------------------- #
def test_a_ticket_started_elsewhere_is_left_out_not_armed(env):
    _ticket(env, 12)
    run, _ = asyncio.run(
        drv.create_run(
            {
                "items": [{"kind": "ticket", "source": "sc", "id": "12"}],
                "policy": {"lane": "pr"},
                "repo_path": env.repo,
            }
        )
    )
    rid = run["id"]
    # An Intake click (or the pipeline) creates sc-12 before the group does.
    env.instances["sc-12"] = _Inst("sc-12", created=time.time())
    ap.arm("sc-12", "commit", lane="commit", source="tix")  # its own lane
    run = _step(rid)
    t = run["tasks"][0]
    assert t["state"] == "skipped" and "started elsewhere" in t["detail"]
    assert ap.get("sc-12")["lane"] == "commit"  # untouched


# --------------------------------------------------------------------------- #
# Finding 32: an aborted (cancelled) group is not stepped forever
# --------------------------------------------------------------------------- #
def test_an_aborted_group_leaves_the_loop(env, monkeypatch):
    run, _ = asyncio.run(
        drv.create_run(
            {
                "items": [{"kind": "task", "text": "a"}, {"kind": "task", "text": "b"}],
                "policy": {"lane": "pr", "grouping": "together"},
                "repo_path": env.repo,
            }
        )
    )
    rid = run["id"]
    lead = tr.load(rid)["lead"]["title"]
    env.instances.pop(lead)
    drv._DELETED[lead] = time.time() + 1
    assert _step(rid)["state"] == "cancelled"
    stepped = []
    real = drv.step_run

    async def _spy(run_id, boot=False):
        stepped.append(run_id)
        return await real(run_id, boot)

    monkeypatch.setattr(drv, "step_run", _spy)
    asyncio.run(drv.run_pass())
    assert rid not in stepped


# --------------------------------------------------------------------------- #
# Finding 10 (the queue-only case): a lead lost (not removed) is said, not
# waited on forever
# --------------------------------------------------------------------------- #
def test_a_lost_lead_is_announced_and_offers_cancel(env, monkeypatch):
    from backend.web.core import outbox as outbox_mod

    run, _ = asyncio.run(
        drv.create_run(
            {
                "items": [{"kind": "task", "text": "a"}, {"kind": "task", "text": "b"}],
                "policy": {"lane": "pr", "grouping": "together"},
                "repo_path": env.repo,
            }
        )
    )
    rid = run["id"]
    lead = tr.load(rid)["lead"]["title"]
    env.instances.pop(lead)  # lost across a restart — no delete event
    r = _step(rid)
    assert r["lead"]["missing_since"] > 0 and r["state"] == "running"
    real = time.time
    monkeypatch.setattr(drv.time, "time", lambda: real() + tr.MERGE_BLOCKED_S + 5)
    _step(rid)
    asks = [e for e in env.emitted if e["event"] == "run.needs_you"]
    assert [e["new"] for e in asks] == ["lead_gone"]
    out = outbox_mod.build([], [tr.load(rid)], {}, now=time.time(), today_start=0.0)
    (w,) = out["groups"]["waiting"]
    assert w["kind"] == "lead_gone" and w["actions"] == ["cancel_group"]


# --------------------------------------------------------------------------- #
# Live L1 (each-their-own): an agent that commits itself completes its lane
# --------------------------------------------------------------------------- #
def test_an_agent_that_committed_itself_ships_its_commit_lane(env):
    rid = _make(env, n=1, lane="commit")
    run = _step(rid)
    t = run["tasks"][0]["title"]
    env.worked[t] = 1.0
    _row(
        env,
        t,
        activity="idle",
        activity_since=time.time() - tr.DONE_QUIET_S - 5,
        stage="committed",
    )
    assert ap.get(t)["state"] == "running"  # "already at commit — waiting"
    assert _step(rid)["tasks"][0]["state"] == "working"
    _step(rid)
    assert ap.get(t)["state"] == "done"
    run = _step(rid)
    assert run["tasks"][0]["state"] == "shipped"
