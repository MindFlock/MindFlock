"""The team-run driver: what a pass DOES, against fakes.

Sessions are faked at the seams the driver really uses — ``session_create.
create_result`` and ``ticket_start.launch`` start them, ``ENGINE.instances``
holds them, the published ``/api/instances`` rows carry their activity — while
the run store, the autopilot store, the prompt queue and the ingestion ledger
are the real modules pointed at tmp. Every test OWNS ``ENGINE.instances``
(``setattr``, never ``setitem``): the driver enumerates it, and the live engine
is the developer's real state.json.

Covers: queue + concurrency, arming lanes (one driver: the autopilot), no
double spawn, restart reconcile (silent, the restart table, no double spawn
when the branch exists), title reuse never adopted, a deleted member never
re-created, fix prompts and nudges through the queue, pause/resume holding
lanes, cancel handing tickets back, duplicate windows keyed by (repo, branch),
retry fresh, skip, the budget pause, and the ledger reservation.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.session.storage import Status
from backend.ticket_ingestion import state as ledger
from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import events as events_mod
from backend.web.core import prompt_queue as pq
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr


class _Inst:
    def __init__(self, title, created=None, branch=None):
        self.Title = title
        self.Program = "claude"
        self.Branch = branch or "mindflock/" + title
        self.Path = ""
        self.InPlace = False
        self.Parent = ""
        self.Spawned = False
        self.Status = Status.Running
        self.CreatedAt = datetime.fromtimestamp(
            created if created is not None else datetime.now().timestamp(), timezone.utc
        )

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""


def _git_repo(path):
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.test"],
        ["config", "user.name", "t"],
        ["commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    rows: dict = {}
    monkeypatch.setattr(
        events_mod, "sessions_snapshot", lambda: [dict(r) for r in rows.values()]
    )
    pending: set = set()
    monkeypatch.setattr(
        server,
        "_pending_rows",
        lambda: [{"title": t, "pending": True} for t in pending],
    )
    monkeypatch.setattr(server, "_CREATE_FAILURES", {})
    worked: dict = {}
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: worked.get(t))
    monkeypatch.setattr(server, "_session_limited_until", lambda t: 0.0)
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._ticket_start, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(drv, "_DELETED", {})
    monkeypatch.setattr(drv, "_TURN_ENDED", {})
    monkeypatch.setattr(drv, "_UNSUB", None)
    created: list = []
    launched: list = []
    listing: list = []

    async def _create(payload):
        title = payload["title"]
        if title in instances:
            return 409, {"error": "instance %s already exists" % title}
        created.append(payload)
        inst = _Inst(title)
        instances[title] = inst
        return 202, {"title": title, "created_at": inst.CreatedAt.timestamp()}

    async def _launch(source, tid, **kw):
        launched.append(dict(kw, source=source, id=tid))
        title = kw.get("title") or "sc-" + tid
        if title in instances:
            raise server._ticket_start.LaunchError(
                409, {"error": "exists", "title": title}
            )
        return {"started": True, "title": title}

    async def _rows():
        return list(listing)

    monkeypatch.setattr(server._session_create, "create_result", _create)
    monkeypatch.setattr(server._ticket_start, "launch", _launch)
    monkeypatch.setattr(drv, "_ticket_rows", _rows)
    emitted: list = []
    unsub = events_mod.BUS.subscribe(emitted.append)
    drv.subscribe()
    yield SimpleNamespace(
        instances=instances,
        rows=rows,
        pending=pending,
        worked=worked,
        created=created,
        launched=launched,
        listing=listing,
        emitted=emitted,
        repo=_git_repo(tmp_path / "repo"),
        root=tmp_path,
    )
    unsub()
    if drv._UNSUB:
        drv._UNSUB()


def _run_events(env, name=None):
    return [
        e
        for e in env.emitted
        if e["event"].startswith("run.") and (name is None or e["event"] == name)
    ]


def _make(env, n=5, **kw):
    payload = {
        "name": "Q4",
        "items": [
            {"kind": "task", "text": "task number %d" % i} for i in range(1, n + 1)
        ],
        "policy": {
            "lane": kw.pop("lane", "pr"),
            "ask_first": kw.pop("ask_first", False),
        },
        "concurrency": kw.pop("concurrency", 3),
        "repo_path": env.repo,
    }
    payload.update(kw)
    run, warnings = asyncio.run(drv.create_run(payload))
    return run["id"]


def _step(rid, boot=False):
    asyncio.run(drv.step_run(rid, boot=boot))
    return tr.load(rid)


def _states(run):
    return [t["state"] for t in run["tasks"]]


def _row(env, title, **kw):
    base = {
        "title": title,
        "activity": "working",
        "activity_since": 1.0,
        "repo": "repo",
    }
    base.update(kw)
    env.rows[title] = base


# --------------------------------------------------------------------------- #
# Queue, concurrency and lanes
# --------------------------------------------------------------------------- #
class TestQueue:
    def test_starts_up_to_the_cap_and_arms_each_lane(self, env):
        rid = _make(env, n=5, concurrency=3)
        run = _step(rid)
        assert _states(run) == ["starting"] * 3 + ["queued"] * 2
        assert len(env.created) == 3
        for t in run["tasks"][:3]:
            rec = ap.get(t["title"])
            assert rec["depth"] == "pr" and rec["lane"] == "pr"
            assert rec["source"] == "run"
            assert t["incarnation"] > 0
        assert 'MindFlock group "Q4"' in env.created[0]["prompt"]

    def test_a_second_pass_sees_them_working_and_starts_nothing_more(self, env):
        rid = _make(env, n=5, concurrency=3)
        _step(rid)
        run = _step(rid)
        assert _states(run) == ["working"] * 3 + ["queued"] * 2
        assert len(env.created) == 3

    def test_a_shipped_member_frees_its_slot(self, env):
        rid = _make(env, n=4, concurrency=2)
        _step(rid)
        run = _step(rid)
        first = run["tasks"][0]["title"]
        ap.update(first, state="done", step="pr", url="https://x/pull/1")
        run = _step(rid)
        assert run["tasks"][0]["state"] == "shipped"
        assert run["tasks"][0]["pr_url"] == "https://x/pull/1"
        assert run["tasks"][2]["state"] == "starting"
        assert len(_run_events(env, "run.task_shipped")) == 1

    def test_ask_first_arms_the_held_rung(self, env):
        rid = _make(env, n=1, ask_first=True)
        run = _step(rid)
        rec = ap.get(run["tasks"][0]["title"])
        assert rec["depth"] == "commit" and rec["ask_first"] is True

    def test_leave_lane_arms_nothing_and_finishes_on_turn_end(self, env):
        rid = _make(env, n=1, lane="leave")
        run = _step(rid)
        title = run["tasks"][0]["title"]
        assert ap.get(title) is None
        _step(rid)
        events_mod.BUS.emit("session.turn_ended", session=title)
        run = _step(rid)
        assert run["tasks"][0]["state"] == "shipped"
        assert run["state"] == "done"
        assert len(_run_events(env, "run.finished")) == 1

    def test_the_row_reports_membership_and_lane(self, env):
        rid = _make(env, n=1, lane="leave")
        run = _step(rid)
        title = run["tasks"][0]["title"]
        assert server._row_run(title) == {
            "id": rid,
            "name": "Q4",
            "task": "t1",
            "role": "task",
            "grouping": "each",
        }
        assert server._row_lane(title) == {
            "target": "leave",
            "ask_first": False,
            "owner": title,
        }


class TestNoDoubleSpawn:
    def test_a_create_in_flight_is_not_repeated(self, env):
        env.listing.append(
            {
                "source": "sc",
                "id": "7",
                "slug": "sc-7",
                "session": "sc-7",
                "name": "Fix",
                "branch": "feature/sc-7/fix",
            }
        )
        rid = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "7"}],
                    "policy": {"lane": "pr"},
                }
            )
        )[0]["id"]
        _step(rid)
        env.pending.add("sc-7")  # provisioning: no instance yet
        run = _step(rid)
        _step(rid)
        assert len(env.launched) == 1
        assert run["tasks"][0]["state"] == "starting"
        assert env.launched[0]["depth"] == "off"  # the run arms its own lane
        assert ap.get("sc-7")["lane"] == "pr"

    def test_the_ledger_holds_a_queued_ticket_so_ingestion_skips_it(self, env):
        env.listing.append(
            {
                "source": "sc",
                "id": "8",
                "slug": "sc-8",
                "session": "sc-8",
                "name": "Fix",
            }
        )
        asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "8"}],
                    "policy": {"lane": "pr"},
                    "concurrency": 1,
                }
            )
        )
        assert "sc-8" in ledger.load_processed_story_ids(env.root)


# --------------------------------------------------------------------------- #
# Restart semantics
# --------------------------------------------------------------------------- #
class TestRestart:
    def _starting(self, env, branch_exists):
        rid = _make(env, n=1)
        with tr.edit(rid) as r:
            t = r["tasks"][0]
            t.update(state="starting", started_at=1000.0, incarnation=0.0, branch="b")
        drv._branch_exists = lambda repo, b: branch_exists  # type: ignore[assignment]
        return rid

    def test_reconcile_is_silent(self, env, monkeypatch):
        monkeypatch.setattr(drv, "_branch_exists", lambda repo, b: True)
        rid = _make(env, n=1)
        with tr.edit(rid) as r:
            r["tasks"][0].update(state="working", started_at=1000.0, incarnation=1000.0)
        env.emitted.clear()
        asyncio.run(drv.reconcile())
        run = tr.load(rid)
        assert run["tasks"][0]["state"] == "needs_you"
        assert _run_events(env) == []
        # The reconcile pass itself says nothing — but what it FOUND (a member
        # gone after the restart) is new, so the next ordinary pass (after the
        # boot quiet window) says it, once. Only what was already standing
        # before the restart is seeded silently.
        _step(rid)
        assert [e["new"] for e in _run_events(env, "run.needs_you")] == ["restart"]
        _step(rid)
        assert len(_run_events(env, "run.needs_you")) == 1

    def test_branch_still_there_offers_recreate_never_double_spawns(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(drv, "_branch_exists", lambda repo, b: True)
        rid = _make(env, n=1)
        with tr.edit(rid) as r:
            r["tasks"][0].update(
                state="starting", started_at=1000.0, incarnation=0.0, branch="b"
            )
        run = _step(rid, boot=True)
        t = run["tasks"][0]
        assert (t["state"], t["reason"]) == ("needs_you", "restart")
        assert "existing branch" in t["detail"]
        assert env.created == []

    def test_no_branch_retries_the_create(self, env, monkeypatch):
        monkeypatch.setattr(drv, "_branch_exists", lambda repo, b: False)
        rid = _make(env, n=1)
        with tr.edit(rid) as r:
            r["tasks"][0].update(state="starting", started_at=1000.0, incarnation=0.0)
        run = _step(rid, boot=True)
        t = run["tasks"][0]
        assert t["state"] == "queued" and t["attempts"]["create"] == 1
        assert t["retry_at"] > 0
        assert env.created == []  # after the backoff, not in the same pass

    def test_our_session_survives_a_restart_unchanged(self, env):
        rid = _make(env, n=1)
        _step(rid)
        run = _step(rid)
        assert run["tasks"][0]["state"] == "working"
        run = _step(rid, boot=True)
        assert run["tasks"][0]["state"] == "working"
        assert len(env.created) == 1

    def test_a_reused_title_is_never_adopted_silently(self, env):
        rid = _make(env, n=1)
        _step(rid)
        _step(rid)
        title = tr.load(rid)["tasks"][0]["title"]
        ap.disarm(title)
        env.instances[title] = _Inst(title, created=5.0)  # a different session
        run = _step(rid)
        t = run["tasks"][0]
        assert (t["state"], t["reason"]) == ("needs_you", "restart")
        assert "used by another session" in t["detail"]
        assert ap.get(title) is None  # never armed on the stranger

    def test_finished_is_announced_once_across_restarts(self, env):
        rid = _make(env, n=1, lane="leave")
        run = _step(rid)
        events_mod.BUS.emit("session.turn_ended", session=run["tasks"][0]["title"])
        _step(rid)
        _step(rid)
        asyncio.run(drv.run_pass())
        asyncio.run(drv.reconcile())
        asyncio.run(drv.run_pass())
        assert len(_run_events(env, "run.finished")) == 1
        assert tr.load(rid)["summary"]["announced"] is True

    def test_a_finish_inside_boot_quiet_is_announced_after_it(self, env, monkeypatch):
        rid = _make(env, n=1, lane="leave")
        run = _step(rid)
        events_mod.BUS.emit("session.turn_ended", session=run["tasks"][0]["title"])
        monkeypatch.setattr(server, "_in_boot_quiet", lambda: True)
        _step(rid)
        assert _run_events(env, "run.finished") == []
        monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
        asyncio.run(drv.run_pass())
        assert len(_run_events(env, "run.finished")) == 1


# --------------------------------------------------------------------------- #
# Escalations and the one emitter
# --------------------------------------------------------------------------- #
class TestAnnouncements:
    def _working(self, env, **kw):
        rid = _make(env, n=1, **kw)
        _step(rid)
        run = _step(rid)
        return rid, run["tasks"][0]["title"]

    def test_needs_you_fires_once_per_task_reason_incarnation(self, env):
        rid, title = self._working(env)
        ap.halt(title, "no origin remote — add one to push")
        _step(rid)
        _step(rid)
        evs = _run_events(env, "run.needs_you")
        assert len(evs) == 1
        assert evs[0]["session"] == title and evs[0]["new"] == "ship_halted"
        assert evs[0]["data"]["detail"].startswith("Q4: ")
        data = evs[0]["data"]
        assert data["run"] == rid and data["task"] == "t1" and data["name"] == "Q4"
        assert data["ref"] == title and data["reason"] == "ship_halted"
        assert data["incarnation"] > 0 and data["text"]
        # A restart re-reads the persisted keys: still once.
        asyncio.run(drv.run_pass())
        assert len(_run_events(env, "run.needs_you")) == 1

    def test_a_dialog_is_never_announced_by_the_run(self, env):
        rid, title = self._working(env)
        _row(env, title, activity="clarify")
        run = _step(rid)
        assert (run["tasks"][0]["state"], run["tasks"][0]["reason"]) == (
            "needs_you",
            "prompt",
        )
        assert _run_events(env, "run.needs_you") == []

    def test_a_deleted_member_is_cancelled_and_never_recreated(self, env):
        rid, title = self._working(env)
        env.instances.pop(title)
        events_mod.BUS.emit("session.deleted", session=title)
        run = _step(rid)
        assert run["tasks"][0]["state"] == "cancelled"
        assert run["tasks"][0]["detail"] == "removed by you"
        _step(rid)
        assert len(env.created) == 1

    def test_a_hook_failure_gets_a_fix_prompt_and_a_rearm(self, env):
        rid, title = self._working(env)
        ap.halt(title, "pre-commit failed at mypy")
        run = _step(rid)
        assert run["tasks"][0]["attempts"]["ship"] == 1
        texts = [i["text"] for i in pq.list_queue(title)]
        assert texts and "failed at `mypy`" in texts[0]
        assert ap.get(title)["state"] == "running"
        assert _run_events(env, "run.needs_you") == []

    def test_a_rearm_keeps_the_message_a_person_wrote(self, env):
        # Found driving a real server: an approval with an edited message hit
        # a failed commit, the fix re-armed the lane, and the edited message
        # was replaced by the task-line placeholder.
        rid, title = self._working(env)
        ap.update(title, message="Delta notes: as edited", message_auto=False)
        ap.halt(title, "pre-commit failed at mypy")
        _step(rid)
        rec = ap.get(title)
        assert rec["state"] == "running"
        assert rec["message"] == "Delta notes: as edited"
        assert not rec["message_auto"]

    def test_a_stalled_member_is_nudged_through_the_queue(self, env):
        rid, title = self._working(env)
        env.worked[title] = 1.0
        with tr.edit(rid) as r:
            r["tasks"][0].update(progress="p", progress_at=1.0)
        _row(env, title, activity="idle", activity_since=1.0)
        env.rows[title]["diff_stat"] = None
        # The fingerprint the driver computes for this row:
        with tr.edit(rid) as r:
            r["tasks"][0]["progress"] = drv._progress_of(env.rows[title])
        run = _step(rid)
        assert run["tasks"][0]["nudges"] == 1
        assert [i["text"] for i in pq.list_queue(title)] == [tr.NUDGE_TEXT]

    def test_budget_pauses_holds_and_asks_once(self, env):
        rid = _make(env, n=2, budget_usd=1.0)
        _step(rid)
        run = _step(rid)
        for t in run["tasks"]:
            _row(env, t["title"], tokens_cost=0.6)
        run = _step(rid)
        assert run["paused"] and run["pause_reason"] == "budget"
        assert all(ap.get(t["title"]) is None for t in run["tasks"])  # held
        _step(rid)
        evs = [e for e in _run_events(env, "run.needs_you") if e["new"] == "budget"]
        assert len(evs) == 1
        drv.resume(rid, budget_usd=5.0)
        run = tr.load(rid)
        assert not run["paused"] and run["budget_usd"] == 5.0
        assert all(ap.get(t["title"])["lane"] == "pr" for t in run["tasks"])


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
class TestOperations:
    def test_pause_holds_and_resume_rearms(self, env):
        rid = _make(env, n=1)
        run = _step(rid)
        title = run["tasks"][0]["title"]
        drv.pause(rid)
        assert ap.get(title) is None and tr.load(rid)["tasks"][0]["held"]
        assert _states(_step(rid)) == ["working"]  # observes, ships nothing
        drv.resume(rid)
        assert ap.get(title)["lane"] == "pr"

    def test_cancel_keeps_sessions_and_hands_tickets_back(self, env):
        env.listing.extend(
            [
                {
                    "source": "sc",
                    "id": "1",
                    "slug": "sc-1",
                    "session": "sc-1",
                    "name": "A",
                },
                {
                    "source": "sc",
                    "id": "2",
                    "slug": "sc-2",
                    "session": "sc-2",
                    "name": "B",
                },
            ]
        )
        rid = asyncio.run(
            drv.create_run(
                {
                    "items": [
                        {"kind": "ticket", "source": "sc", "id": "1"},
                        {"kind": "ticket", "source": "sc", "id": "2"},
                    ],
                    "policy": {"lane": "pr"},
                    "concurrency": 1,
                }
            )
        )[0]["id"]
        _step(rid)
        env.instances["sc-1"] = _Inst("sc-1")
        _step(rid)
        assert ledger.load_processed_story_statuses(env.root)["sc-2"] == "in_flight"
        summary, kept = drv.cancel(rid)
        assert summary["state"] == "cancelled"
        assert kept == ["sc-1"]
        assert ap.get("sc-1") is None
        assert "sc-2" not in ledger.load_processed_story_statuses(env.root)
        assert "sc-1" in env.instances  # the session stays
        assert _run_events(env, "run.finished") == []

    def test_skip_detaches_without_touching_the_lane(self, env):
        rid = _make(env, n=2, concurrency=1)
        run = _step(rid)
        title = run["tasks"][0]["title"]
        task = drv.skip(rid, "t1")
        assert task["state"] == "skipped"
        assert ap.get(title)["lane"] == "pr"  # its lane is unchanged
        assert drv.skip(rid, "t2")["state"] == "skipped"
        with pytest.raises(drv.RunError) as err:
            drv.skip(rid, "t2")
        assert err.value.status == 409

    def test_start_now_ignores_the_cap_once(self, env):
        rid = _make(env, n=3, concurrency=1)
        _step(rid)
        drv.start_now(rid, "t3")
        run = _step(rid)
        assert _states(run) == ["working", "queued", "starting"]

    def test_retry_fresh_starts_a_new_title_and_keeps_the_old(self, env):
        rid = _make(env, n=1)
        run = _step(rid)
        old = run["tasks"][0]["title"]
        with tr.edit(rid) as r:
            r["tasks"][0].update(state="needs_you", reason="restart")
        task = drv.retry(rid, "t1", fresh=True)
        assert task["title"] == old + "-2" and task["state"] == "queued"
        run = _step(rid)
        assert old in env.instances and old + "-2" in env.instances

    def test_retry_refuses_a_healthy_task(self, env):
        rid = _make(env, n=1)
        _step(rid)
        with pytest.raises(drv.RunError) as err:
            drv.retry(rid, "t1")
        assert err.value.status == 409

    def test_adopt_refuses_a_branch_another_group_owns(self, env):
        """Duplicate windows: foo-copy is the same branch's work as foo."""
        env.instances["foo"] = _Inst("foo", branch="feat/x")
        env.instances["foo-copy"] = _Inst("foo-copy", branch="feat/x")
        _row(env, "foo", branch="feat/x")
        _row(env, "foo-copy", branch="feat/x")
        a = _make(env, n=1)
        b = _make(env, n=1)
        task = drv.adopt(a, "foo")
        assert task["state"] == "working" and ap.get("foo")["lane"] == "pr"
        with pytest.raises(drv.RunError) as err:
            drv.adopt(b, "foo-copy")
        assert (
            err.value.status == 409 and "branch already in group" in err.value.message
        )
        assert ap.get("foo-copy") is None  # the copy is never armed

    def test_a_live_ticket_session_is_adopted_not_restarted(self, env):
        env.listing.append(
            {"source": "sc", "id": "3", "slug": "sc-3", "session": "sc-3", "name": "C"}
        )
        env.instances["sc-3"] = _Inst("sc-3")
        run, warnings = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "source": "sc", "id": "3"}],
                    "policy": {"lane": "commit"},
                }
            )
        )
        assert run["tasks"][0]["state"] == "working"
        assert "added to the group, not restarted" in warnings[0]
        assert ap.get("sc-3")["lane"] == "commit"
        _step(run["id"])
        assert env.launched == []

    def test_wait_returns_on_needs_you_or_times_out(self, env):
        rid = _make(env, n=1)
        out = asyncio.run(drv.wait_run(rid, "needs_you", -1, 0.0))
        assert out["reason"] == "timeout"
        with tr.edit(rid) as r:
            r["tasks"][0].update(state="needs_you", reason="stuck")
        out = asyncio.run(drv.wait_run(rid, "needs_you", -1, 5.0))
        assert out["reason"] == "needs_you"
        rev = out["run"]["rev"]
        assert (
            asyncio.run(drv.wait_run(rid, "change", rev - 1, 0.0))["reason"] == "change"
        )


class TestCreateValidation:
    @pytest.mark.parametrize(
        "payload, message",
        [
            ({"items": []}, "nothing to do"),
            (
                {"items": [{"kind": "task", "text": "x"}], "policy": {"lane": "yolo"}},
                "unknown lane",
            ),
            (
                {
                    "items": [{"kind": "task", "text": "x"}],
                    "policy": {"grouping": "together"},
                },
                "one-for-all needs a single repository",
            ),
            (
                {
                    "items": [
                        {"kind": "task", "text": "x"},
                        {"kind": "task", "text": "y"},
                    ],
                    "split": True,
                },
                "split needs exactly one line",
            ),
            (
                {"items": [{"kind": "ticket", "ref": "PAY-1"}], "split": True},
                "split needs exactly one line",
            ),
            (
                {"items": [{"kind": "task", "text": "x"}], "lead": "someone"},
                "lead is only for a split",
            ),
            ({"items": [{"kind": "task", "text": "x"}]}, "repo_path is required"),
            (
                {"items": [{"kind": "task", "text": "x"}], "concurrency": 9},
                "concurrency",
            ),
        ],
    )
    def test_refusals(self, env, payload, message):
        with pytest.raises(drv.RunError) as err:
            asyncio.run(drv.create_run(payload))
        assert message in err.value.message

    def test_an_unresolved_ticket_is_never_turned_into_a_task(self, env, monkeypatch):
        monkeypatch.setattr(drv, "_configured_sources", lambda: [])
        run, _ = asyncio.run(
            drv.create_run(
                {
                    "items": [{"kind": "ticket", "ref": "NOPE-1"}],
                    "policy": {"lane": "pr"},
                }
            )
        )
        t = run["tasks"][0]
        assert t["kind"] == "ticket" and t["title"] == ""
        assert t["detail"] == "not found in any source"
