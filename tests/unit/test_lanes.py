"""Ship lanes (backend.web.core.lanes) and the routes that set them.

A lane is carried out by the autopilot — the same record the ⏩ button arms —
so these tests pin: the lane → depth mapping (including "ask me before it
ships", which holds the run one rung short of its first outward step), the
row's ``lane`` block and the copy-window rule, and the ``/lane``,
``/ship-now`` and ``/fast-track`` routes against a real git worktree.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import lanes


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    return d


@pytest.fixture
def inst(wt, monkeypatch):
    from datetime import datetime, timezone

    from backend.session.instance import FromInstanceData
    from backend.session.storage import GitWorktreeData, InstanceData, Status

    t = datetime.now(timezone.utc)
    data = InstanceData(
        title="ln-session",
        path=str(wt),
        branch="b",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=str(wt),
            worktree_path=str(wt),
            session_name="ln-session",
            branch_name="b",
        ),
    )
    i = FromInstanceData(data, attach=False)
    monkeypatch.setattr(server.ENGINE, "instances", {"ln-session": i})
    return i


def _run(coro):
    resp = asyncio.run(coro)
    return resp.status_code, json.loads(resp.body)


# --------------------------------------------------------------------------- #
# Pure mapping
# --------------------------------------------------------------------------- #
class TestMapping:
    def test_lane_names_and_the_autopilot_vocabulary_agree(self):
        assert lanes.normalize_lane("PR") == "pr"
        assert lanes.normalize_lane("agent") == "leave"
        assert lanes.normalize_lane("off") == "leave"
        assert lanes.normalize_lane("teleport") == ""
        assert [lanes.depth_of(x) for x in lanes.LANES] == [
            "agent",
            "commit",
            "push",
            "pr",
            "merge",
        ]
        assert lanes.lane_of_depth("agent") == "leave"
        assert lanes.lane_of_depth("off") == ""

    @pytest.mark.parametrize(
        "lane, held",
        [
            ("leave", "agent"),
            ("commit", "agent"),  # the commit itself is what you approve
            ("push", "commit"),  # the push is
            ("pr", "commit"),
            ("merge", "commit"),
        ],
    )
    def test_ask_first_holds_one_rung_short_of_the_first_outward_step(self, lane, held):
        assert lanes.held_depth(lane, True) == held
        assert lanes.held_depth(lane, False) == lanes.depth_of(lane)

    def test_awaiting_approval_only_when_parked_short_of_the_lane(self):
        rec = {"ask_first": True, "state": "done", "lane": "pr", "depth": "commit"}
        assert lanes.awaiting_approval(rec)
        assert not lanes.awaiting_approval(dict(rec, state="running"))
        assert not lanes.awaiting_approval(dict(rec, ask_first=False))
        # Raised to the lane and finished there: shipped, not waiting.
        assert not lanes.awaiting_approval(dict(rec, depth="pr"))
        assert not lanes.awaiting_approval(None)

    def test_a_record_from_before_lanes_reads_its_depth_as_its_lane(self):
        ap.arm("old", "push")
        ap.update("old", lane="")
        assert lanes.lane_of("old") == {
            "target": "push",
            "ask_first": False,
            "owner": "old",
            "by": "user",
        }
        assert lanes.lane_of("nobody") is None


class TestDuplicates:
    def test_a_copy_window_shows_the_owners_lane(self):
        rows = [
            {
                "title": "foo",
                "repo": "app",
                "branch": "feat/x",
                "lane": {"target": "pr", "ask_first": False, "owner": "foo"},
            },
            {"title": "foo-copy", "repo": "app", "branch": "feat/x", "lane": None},
            {"title": "bar", "repo": "app", "branch": "feat/y", "lane": None},
        ]
        lanes.fill_duplicates(rows)
        assert rows[1]["lane"] == {"target": "pr", "ask_first": False, "owner": "foo"}
        assert rows[2]["lane"] is None

    def test_a_row_with_its_own_lane_keeps_it_and_remote_rows_are_skipped(self):
        own = {"target": "commit", "ask_first": True, "owner": "b"}
        rows = [
            {
                "title": "a",
                "repo": "r",
                "branch": "x",
                "lane": {"target": "pr", "ask_first": False, "owner": "a"},
            },
            {"title": "b", "repo": "r", "branch": "x", "lane": dict(own)},
            {
                "title": "dev::c",
                "repo": "r",
                "branch": "x",
                "lane": None,
                "device": "d",
            },
        ]
        lanes.fill_duplicates(rows)
        assert rows[1]["lane"] == own
        assert rows[2]["lane"] is None

    def test_no_key_no_grouping(self):
        assert lanes.branch_key({"repo": "r", "branch": ""}) == ""
        assert lanes.branch_key({"repo": "r", "branch": "b"}) == "r::b"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class TestLaneRoute:
    def test_sets_the_lane_and_arms_the_autopilot(self, inst):
        code, body = _run(server.instance_set_lane("ln-session", {"lane": "pr"}))
        assert code == 200 and body["ok"] is True
        assert body["lane"] == {
            "target": "pr",
            "ask_first": False,
            "owner": "ln-session",
            "by": "user",
        }
        assert body["autopilot"]["depth"] == "pr"
        assert ap.get("ln-session")["lane"] == "pr"

    def test_ask_first_arms_the_held_rung_and_remembers_the_target(self, inst):
        code, body = _run(
            server.instance_set_lane("ln-session", {"lane": "pr", "ask_first": True})
        )
        assert code == 200
        assert body["autopilot"]["depth"] == "commit"
        assert body["lane"]["target"] == "pr" and body["lane"]["ask_first"] is True

    def test_leave_disarms(self, inst):
        _run(server.instance_set_lane("ln-session", {"lane": "merge"}))
        code, body = _run(server.instance_set_lane("ln-session", {"lane": "leave"}))
        assert code == 200
        assert body["lane"] is None and body["autopilot"] is None
        assert ap.get("ln-session") is None

    def test_validation(self, inst):
        assert _run(server.instance_set_lane("ln-session", {"lane": "x"}))[0] == 400
        assert (
            _run(
                server.instance_set_lane(
                    "ln-session", {"lane": "pr", "ask_first": "yes"}
                )
            )[0]
            == 400
        )
        assert _run(server.instance_set_lane("nope", {"lane": "pr"}))[0] == 404

    def test_the_row_carries_the_lane(self, inst):
        _run(server.instance_set_lane("ln-session", {"lane": "commit"}))
        row = server._instance_json(inst, cheap=True)
        assert row["lane"]["target"] == "commit"

    def test_fast_track_records_its_depth_as_the_lane(self, inst):
        resp = asyncio.run(server.instance_fast_track("ln-session", {"depth": "push"}))
        assert resp.status_code == 200
        assert lanes.lane_of("ln-session")["target"] == "push"


class TestShipNow:
    def test_refused_mid_turn(self, inst, monkeypatch):
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
        code, body = _run(server.instance_ship_now("ln-session", {}))
        assert code == 409
        assert body["error"] == "the agent is mid-turn — stop it first"

    def test_refused_on_a_dialog(self, inst, monkeypatch):
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: "clarify")
        assert _run(server.instance_ship_now("ln-session", {}))[0] == 409

    def test_approval_raises_a_held_run_to_its_lane_without_the_dwell(
        self, inst, monkeypatch
    ):
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
        _run(server.instance_set_lane("ln-session", {"lane": "pr", "ask_first": True}))
        ap.finish("ln-session")  # parked at the held rung
        assert lanes.awaiting_approval(ap.get("ln-session"))

        code, body = _run(server.instance_ship_now("ln-session", {}))

        assert code == 200
        rec = ap.get("ln-session")
        assert rec["depth"] == "pr" and rec["state"] == "running"
        assert rec["ask_first"] is False
        # Armed under THIS server's lease, dwell pre-earned: the first driver
        # pass is not a takeover and may act at once.
        assert rec["owner"] == server._SERVER_BOOT_ID
        claimed, took = ap.claim("ln-session", server._SERVER_BOOT_ID)
        assert took is False
        snap = {
            "stage": "agent",
            "activity": "idle",
            "dirty": True,
            "now": rec["idle_since"] + ap.IDLE_SETTLE_S + 2,
        }
        action, _ = ap.next_action(claimed, snap)
        assert action == "commit"

    def test_no_lane_of_its_own_is_refused_never_the_settings_default(
        self, inst, monkeypatch
    ):
        """Ship now on a row with no record of its own (a copy window, a held
        group member) must not ship at the Settings fast-track rung — that is
        nobody's choice for THIS session."""
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
        monkeypatch.setattr(server, "_fasttrack_depth", lambda: "merge")
        code, body = _run(server.instance_ship_now("ln-session", {}))
        assert code == 409 and "no lane of its own" in body["error"]
        assert ap.get("ln-session") is None
        # An explicit lane (what the row showed) is what ships.
        code, body = _run(server.instance_ship_now("ln-session", {"lane": "commit"}))
        assert code == 200 and body["lane"]["target"] == "commit"

    def test_a_lane_that_ships_nothing_is_refused(self, inst, monkeypatch):
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
        code, body = _run(server.instance_ship_now("ln-session", {"lane": "leave"}))
        assert code == 400


def test_pending_rows_carry_the_lane(monkeypatch):
    from backend.web.core import pending

    monkeypatch.setattr(server.ENGINE, "instances", {})
    ap.arm("sc-9", "pr", source="tix", lane="pr")
    pending.add("sc-9", "tix", branch="feature/sc-9/x")
    try:
        rows = {r["title"]: r for r in pending.rows(server.ENGINE)}
        assert rows["sc-9"]["lane"]["target"] == "pr"
    finally:
        pending.drop("sc-9")


def test_ship_now_commits_the_approval_cards_edited_message(inst, monkeypatch):
    """The Outbox approval card lets you edit the message: it is a human's
    sentence, committed as written, never replaced by a generated one."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    _run(server.instance_set_lane("ln-session", {"lane": "commit", "ask_first": True}))
    ap.finish("ln-session")
    code, _ = _run(
        server.instance_ship_now("ln-session", {"commit_message": "web: dark theme"})
    )
    assert code == 200
    rec = ap.get("ln-session")
    assert rec["depth"] == "commit" and rec["message"] == "web: dark theme"
    assert rec["message_auto"] is False
    bad = _run(server.instance_ship_now("ln-session", {"commit_message": 3}))
    assert bad[0] == 400


def test_lane_on_a_session_still_loading_is_a_retryable_409(monkeypatch):
    """The New dialog creates the session, then sets its lane once the
    worktree exists, retrying on 409 — so "not yet" must be a 409, never a
    404 or 400."""
    loading = type("I", (), {"GetWorktreePath": lambda self: ""})()
    monkeypatch.setattr(server.ENGINE, "instances", {"fresh": loading})
    code, body = _run(server.instance_set_lane("fresh", {"lane": "pr"}))
    assert code == 409 and body["error"] == "workspace not ready"
    assert ap.get("fresh") is None
