"""Regression tests for the team-runs / ship-lanes review (2026-10-05).

The owner's rule these pin: NO SURPRISE PUSHES. Nothing commits, pushes, opens
a PR or merges beyond what the user chose for THAT session or group — not a
copy window, not a held member, not an agent, not the Settings default.

Real routes and stores (pointed at tmp by conftest); sessions are real
``Instance`` objects over a throwaway repo, and every test OWNS
``ENGINE.instances`` (``setattr``).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone

import pytest

from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _mk(title, wt, branch="b"):
    from backend.session.instance import FromInstanceData
    from backend.session.storage import GitWorktreeData, InstanceData, Status

    t = datetime.now(timezone.utc)
    data = InstanceData(
        title=title,
        path=str(wt),
        branch=branch,
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=str(wt),
            worktree_path=str(wt),
            session_name=title,
            branch_name=branch,
        ),
    )
    return FromInstanceData(data, attach=False)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """``foo`` and its duplicate window ``foo-copy`` on ONE branch."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    _git(d, "checkout", "-q", "-b", "b")
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"foo": _mk("foo", d), "foo-copy": _mk("foo-copy", d)},
    )
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    # The owner's real Settings say merge: nothing below may ever reach it.
    monkeypatch.setattr(server, "_fasttrack_depth", lambda: "merge")
    monkeypatch.setattr(drv, "wake", lambda: None)
    return d


def _run(coro):
    resp = asyncio.run(coro)
    return resp.status_code, json.loads(resp.body)


def _group(title="foo", **policy):
    pol = {"lane": "pr", "ask_first": False}
    pol.update(policy)
    return tr.create(
        {
            "name": "g",
            "policy": pol,
            "tasks": [
                {"kind": "task", "text": "do x", "title": title, "state": "working"}
            ],
        },
        now=1000.0,
    )


# --------------------------------------------------------------------------- #
# Finding 1: no lane of its own → never the Settings default; copy windows,
# leads, one-for-all members and a paused group's members are not armed
# --------------------------------------------------------------------------- #
class TestNoSecondDriver:
    def test_copy_window_ship_now_is_refused_and_never_armed(self, pair):
        code, _ = _run(
            server.instance_set_lane("foo", {"lane": "commit", "ask_first": True})
        )
        assert code == 200
        code, body = _run(server.instance_ship_now("foo-copy", {}))
        assert code == 409 and body["driver"] == "foo"
        assert ap.get("foo-copy") is None
        assert ap.get("foo")["ask_first"] is True

    def test_copy_window_lane_and_fast_track_are_refused(self, pair):
        _run(server.instance_set_lane("foo", {"lane": "commit"}))
        code, body = _run(server.instance_set_lane("foo-copy", {"lane": "pr"}))
        assert code == 409 and "foo drives this branch" in body["error"]
        code, _ = _run(server.instance_fast_track("foo-copy", {"depth": "pr"}))
        assert code == 409
        assert ap.get("foo-copy") is None
        # "Leave it" only disarms: always allowed.
        code, _ = _run(server.instance_set_lane("foo-copy", {"lane": "leave"}))
        assert code == 200

    def test_ship_now_on_a_held_member_never_uses_the_settings_default(self, pair):
        """A paused group's member shows the group's lane but has no record:
        Ship now must neither fall back to Settings (merge) nor ship it."""
        run = _group(lane="commit", ask_first=True)
        drv.pause(run["id"])
        assert ap.get("foo") is None
        code, body = _run(server.instance_ship_now("foo", {}))
        assert code == 409 and "paused" in body["error"]
        code, body = _run(server.instance_ship_now("foo", {"lane": "commit"}))
        assert code == 409
        assert ap.get("foo") is None

    def test_a_lead_ships_only_through_the_release(self, pair):
        run = _group(title="other", grouping="together")
        with tr.edit(run["id"]) as r:
            r["lead"] = tr._normalize_lead({"title": "foo", "branch": "b"})
        for call in (
            server.instance_set_lane("foo", {"lane": "pr"}),
            server.instance_set_lane("foo", {"lane": "pr", "ask_first": False}),
            server.instance_fast_track("foo", {"depth": "pr"}),
            server.instance_ship_now("foo", {"lane": "pr"}),
        ):
            code, body = _run(call)
            assert code == 409 and "release" in body["error"]
        assert ap.get("foo") is None

    def test_a_one_for_all_member_has_no_lane_of_its_own(self, pair):
        _group(grouping="together")
        code, body = _run(server.instance_set_lane("foo", {"lane": "pr"}))
        assert code == 409 and "one PR" in body["error"]
        code, body = _run(server.instance_ship_now("foo", {"lane": "pr"}))
        assert code == 409
        assert ap.get("foo") is None


# --------------------------------------------------------------------------- #
# Finding 5: a person's per-member lane / ask-first survives every re-arm
# --------------------------------------------------------------------------- #
class TestPerMemberChoice:
    def test_rearm_keeps_the_users_lane_and_ask_first(self, pair):
        run = _group(lane="pr")
        t = run["tasks"][0]
        drv._arm(run, t)
        assert ap.get("foo")["lane"] == "pr"
        code, _ = _run(
            server.instance_set_lane("foo", {"lane": "commit", "ask_first": True})
        )
        assert code == 200
        # Any driver re-arm: a fix after a failed hook, resume, retry.
        run = tr.load(run["id"])
        drv._arm(run, tr.task_by_id(run, t["id"]))
        rec = ap.get("foo")
        assert (rec["lane"], rec["depth"], rec["ask_first"]) == (
            "commit",
            "agent",
            True,
        )
        assert tr.title_index()["foo"]["ask_first"] is True

    def test_fix_after_a_failed_hook_rearms_the_users_choice(self, pair):
        run = _group(lane="pr")
        drv._arm(run, run["tasks"][0])
        _run(server.instance_set_lane("foo", {"lane": "commit", "ask_first": True}))
        drv._fix(run["id"], run["tasks"][0]["id"], "mypy", "pre-commit failed", 2000.0)
        rec = ap.get("foo")
        assert rec["lane"] == "commit" and rec["ask_first"] is True

    def test_a_lane_set_while_paused_is_armed_on_resume_not_before(self, pair):
        run = _group(lane="pr")
        drv._arm(run, run["tasks"][0])
        drv.pause(run["id"])
        code, body = _run(
            server.instance_set_lane("foo", {"lane": "commit", "ask_first": True})
        )
        assert code == 200 and body["held"] is True
        assert ap.get("foo") is None  # nothing ships while paused
        drv.resume(run["id"])
        rec = ap.get("foo")
        assert rec["lane"] == "commit" and rec["ask_first"] is True


# --------------------------------------------------------------------------- #
# Finding 6: the /fast-track alias (⏩, and the MCP's set_autopilot) never
# silently drops "ask first", and records who chose the lane
# --------------------------------------------------------------------------- #
class TestFastTrackAlias:
    def test_fast_track_keeps_ask_first(self, pair):
        _run(server.instance_set_lane("foo", {"lane": "pr", "ask_first": True}))
        code, _ = _run(server.instance_fast_track("foo", {"depth": "pr"}))
        assert code == 200
        rec = ap.get("foo")
        assert rec["depth"] == "commit" and rec["ask_first"] is True

    def test_the_chooser_is_recorded(self, pair):
        _run(server.instance_fast_track("foo", {"depth": "commit", "by": "agent:foo"}))
        assert ap.get("foo")["by"] == "agent:foo"
        assert server._row_lane("foo")["by"] == "agent:foo"
        # An unknown agent name is not an agent: it reads as the user.
        _run(server.instance_fast_track("foo", {"depth": "commit", "by": "agent:x"}))
        assert ap.get("foo")["by"] == "user"


# --------------------------------------------------------------------------- #
# Finding 35: a later session reusing a finished member's title is not shown
# under the old group with its old lane
# --------------------------------------------------------------------------- #
def test_a_reused_title_is_not_rendered_under_the_old_group(pair):
    run = _group(lane="pr")
    with tr.edit(run["id"]) as r:
        r["tasks"][0].update(state="shipped", incarnation=500.0)
        r["state"] = "done"
    # "foo" today is a NEW session (created now, not at 500).
    assert server._row_run("foo") is None
    assert server._row_lane("foo") is None
    # The member itself (same incarnation) still is.
    created = server._created_epoch(server.ENGINE.instances["foo"])
    with tr.edit(run["id"]) as r:
        r["tasks"][0]["incarnation"] = created
    tr._INDEX["at"] = 0.0
    assert server._row_run("foo")["id"] == run["id"]


# --------------------------------------------------------------------------- #
# Live L4: the ask-first card shows the EXACT message, and that is what lands
# --------------------------------------------------------------------------- #
def test_an_ask_first_card_carries_the_exact_message_that_lands(pair, monkeypatch):
    from backend.web.core import lanes, outbox

    _run(server.instance_set_lane("foo", {"lane": "commit", "ask_first": True}))
    (pair / "new.txt").write_text("work\n")
    rec = ap.get("foo")
    assert rec["depth"] == "agent"
    monkeypatch.setattr(
        server, "_autopilot_written_message", lambda t, wt, r: "notes: add new.txt"
    )
    server._autopilot_draft_for_approval("foo", str(pair), rec)
    ap.finish("foo")
    rows = [{"title": "foo", "repo": "r", "branch": "b"}]
    p = outbox.build(rows, [], {"foo": ap.get("foo")}, now=0, today_start=0)
    (w,) = p["groups"]["waiting"]
    assert w["preview"]["commit_message"] == "notes: add new.txt"
    # Approving it unedited commits exactly that text — no second draft.
    lanes.ship_now("foo")
    rec = ap.get("foo")
    assert rec["message"] == "notes: add new.txt" and rec["message_auto"] is False
    assert rec["depth"] == "commit"


def test_a_persons_message_is_never_replaced_by_a_draft(pair, monkeypatch):
    _run(server.instance_set_lane("foo", {"lane": "commit", "ask_first": True}))
    ap.update("foo", message="mine", message_auto=False)
    (pair / "new.txt").write_text("work\n")
    monkeypatch.setattr(server, "_autopilot_written_message", lambda *a: "draft")
    server._autopilot_draft_for_approval("foo", str(pair), ap.get("foo"))
    assert ap.get("foo")["message"] == "mine"


# --------------------------------------------------------------------------- #
# Live L2: a lane never drives a checkout two sessions share
# --------------------------------------------------------------------------- #
def test_a_lane_on_a_shared_in_place_checkout_is_refused(tmp_path, monkeypatch):
    class _InPlace:
        InPlace = True
        Branch = "main"

        def __init__(self, title):
            self.Title = title

        def GetWorktreePath(self):  # noqa: N802
            return str(tmp_path)

    monkeypatch.setattr(
        server.ENGINE, "instances", {"eta": _InPlace("eta"), "theta": _InPlace("theta")}
    )
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    code, body = _run(
        server.instance_set_lane("eta", {"lane": "commit", "ask_first": True})
    )
    assert code == 409 and body["shared_with"] == "theta"
    code, body = _run(server.instance_ship_now("theta", {"lane": "commit"}))
    assert code == 409
    assert ap.get("eta") is None and ap.get("theta") is None


# --------------------------------------------------------------------------- #
# Live L7: a commit whose hooks re-run is ONE stage change, not four bell rows
# --------------------------------------------------------------------------- #
def test_a_precommit_flicker_announces_only_the_net_stage_change(monkeypatch):
    from backend.web.core import events as events_mod

    monkeypatch.setattr(server, "_EVENT_SNAPSHOT", {})
    monkeypatch.setattr(server, "_note_turn_boundary", lambda *a, **k: None)
    seen = []
    unsub = events_mod.BUS.subscribe(
        lambda e: (
            seen.append((e["old"], e["new"]))
            if e["event"] == "session.stage_changed" and e["session"] == "fl"
            else None
        )
    )
    clock = [server.time.monotonic() + 1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    try:
        for stage in ("agent", "precommit", "agent", "precommit", "committed"):
            server._emit_state_changes("fl", "running", "idle", stage)
            clock[0] += 0.2
        assert seen == [("agent", "committed")]
        # A precommit that PERSISTS is announced once the window passes.
        server._emit_state_changes("fl", "running", "idle", "precommit")
        clock[0] += server._STAGE_SETTLE_SECONDS + 0.1
        server._emit_state_changes("fl", "running", "idle", "precommit")
        assert seen[-1] == ("committed", "precommit")
    finally:
        unsub()
