"""The team-run MCP tools (backend.mcp.runs) against an in-memory API.

``start_team_run`` previews, refuses unresolved tickets (never turning one
into a task), and creates a server-owned group — sessions the caller does NOT
manage. ``wait_for_run`` long-polls in rounds. ``control_run`` steers only a
group the caller (or its user) started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.mcp import playbooks
from backend.mcp.protocol import ToolError
from backend.mcp.tools import build_tools
from tests.unit._mcp_fakes import FakeCtx, make_box, row

_RUN = {
    "id": "r_abc123",
    "name": "PAY tickets",
    "state": "running",
    "paused": False,
    "pause_reason": "",
    "policy": {"lane": "pr", "ask_first": False, "grouping": "each", "release": "ask"},
    "counts": {
        "queued": 1,
        "active": 1,
        "needs_you": 0,
        "shipped": 0,
        "failed": 0,
        "total": 2,
    },
    "cost_usd": 0.0,
    "created_at": 1.0,
    "created_by": "agent:orch",
    "rev": 3,
    "tasks": [
        {
            "id": "t1",
            "kind": "ticket",
            "ticket_id": "PAY-1",
            "text": "Fix refunds",
            "title": "jira-PAY-1",
            "state": "starting",
            "reason": "",
            "detail": "",
            "pr_url": "",
        },
        {
            "id": "t2",
            "kind": "task",
            "text": "Rate limit the webhooks",
            "title": "rate-limit-webhooks",
            "state": "queued",
            "reason": "",
            "detail": "",
            "pr_url": "",
        },
    ],
}


def _box(me="orch", scope=None, run=None):
    rows = [row("orch"), row("other")]
    box, api, clock = make_box(rows, me=me, scope=scope)
    run = dict(run or _RUN)
    api.top_routes[("POST", "/api/runs/preview")] = lambda p, q: {
        "items": [
            {
                "kind": "ticket",
                "source": "jira",
                "id": "PAY-1",
                "ref": "PAY-1",
                "error": None,
            },
            {"kind": "task", "text": "Rate limit the webhooks"},
        ],
        "name_suggestion": "PAY tickets",
        "lane_default": "pr",
        "warnings": [],
    }
    created = []

    def _create(p, q):
        created.append(p)
        return {"run": run, "warnings": ["w1"]}

    api.top_routes[("POST", "/api/runs")] = _create
    api.top_routes[("GET", "/api/runs/r_abc123")] = lambda p, q: {"run": run}
    return box, api, clock, created


class TestStart:
    def test_creates_a_server_owned_group(self):
        box, api, clock, created = _box()
        out = box.start_team_run(
            {
                "items": ["PAY-1", "Rate limit the webhooks"],
                "lane": "pr",
                "concurrency": 2,
            },
            FakeCtx(clock),
        )
        (payload,) = created
        assert payload["items"] == [
            {"kind": "ticket", "source": "jira", "id": "PAY-1"},
            {"kind": "task", "text": "Rate limit the webhooks"},
        ]
        assert payload["policy"]["lane"] == "pr" and payload["concurrency"] == 2
        assert payload["created_by"] == "agent:orch"
        assert payload["repo_path"] == "/nonexistent/repo"  # the caller's repo
        assert payload["name"] == "PAY tickets"
        assert out["run_id"] == "r_abc123"
        assert out["tasks"][0] == {
            "id": "t1",
            "ref": "PAY-1",
            "title": "jira-PAY-1",
            "state": "starting",
        }
        assert out["warnings"] == ["w1"]
        assert "MindFlock owns these sessions" in out["note"]
        # Server-owned: the caller does not manage them.
        assert not box.policy.spawned_by_me & {"jira-PAY-1", "rate-limit-webhooks"}

    def test_the_default_lane_is_the_users(self):
        box, api, clock, created = _box()
        box.start_team_run({"items": ["PAY-1"]}, FakeCtx(clock))
        assert "lane" not in created[0]["policy"]

    def test_unresolved_tickets_are_refused_not_turned_into_tasks(self):
        box, api, clock, created = _box()
        api.top_routes[("POST", "/api/runs/preview")] = lambda p, q: {
            "items": [
                {"kind": "ticket", "ref": "NOPE-9", "error": "not found in any source"}
            ],
        }
        with pytest.raises(ToolError) as err:
            box.start_team_run({"items": ["NOPE-9"]}, FakeCtx(clock))
        assert "NOPE-9 — not found in any source" in str(err.value)
        assert created == []

    def test_merge_needs_confirmation(self):
        box, api, clock, created = _box()
        with pytest.raises(ToolError):
            box.start_team_run({"items": ["PAY-1"], "lane": "merge"}, FakeCtx(clock))
        box.start_team_run(
            {"items": ["PAY-1"], "lane": "merge", "confirm_merge": True}, FakeCtx(clock)
        )
        assert created[0]["policy"]["lane"] == "merge"

    def test_readonly_scope_refuses(self):
        box, api, clock, created = _box(scope="readonly")
        with pytest.raises(ToolError) as err:
            box.start_team_run({"items": ["PAY-1"]}, FakeCtx(clock))
        assert "readonly" in str(err.value)

    def test_an_external_caller_must_name_the_repo_for_task_lines(self):
        box, api, clock, created = _box(me=None)
        with pytest.raises(ToolError) as err:
            box.start_team_run({"items": ["Rate limit the webhooks"]}, FakeCtx(clock))
        assert "repo_path" in str(err.value)


class TestRead:
    def test_get_run_is_compact(self):
        box, api, clock, _ = _box()
        out = box.get_run({"run_id": "r_abc123"}, FakeCtx(clock))
        assert out["counts"]["total"] == 2
        assert out["tasks"][1] == {
            "id": "t2",
            "title": "rate-limit-webhooks",
            "state": "queued",
            "reason": "",
            "detail": "",
            "pr_url": "",
            "ref": "Rate limit the webhooks",
        }

    def test_list_runs(self):
        box, api, clock, _ = _box()
        api.top_routes[("GET", "/api/runs")] = lambda p, q: {
            "runs": [{"id": "r_1", "active": q}]
        }
        out = box.list_runs({"active_only": True}, FakeCtx(clock))
        assert out["runs"][0]["active"] == {"active": "1"}

    def test_wait_long_polls_in_rounds_until_a_reason(self):
        box, api, clock, _ = _box()
        rounds = []

        def _get(p, q):
            if "wait" not in q:
                return {"run": _RUN}
            rounds.append(q)
            clock.now += float(q["wait"])
            if len(rounds) < 3:
                return {"run": _RUN, "reason": "timeout"}
            return {"run": dict(_RUN, state="done"), "reason": "done"}

        api.top_routes[("GET", "/api/runs/r_abc123")] = _get
        out = box.wait_for_run(
            {"run_id": "r_abc123", "until": "done", "timeout_s": 600}, FakeCtx(clock)
        )
        assert out["reason"] == "done" and out["run"]["state"] == "done"
        assert [q["wait"] for q in rounds] == ["25", "25", "25"]
        assert rounds[0]["until"] == "done" and rounds[0]["rev"] == "3"

    def test_wait_times_out(self):
        box, api, clock, _ = _box()

        def _get(p, q):
            if "wait" in q:
                clock.now += float(q["wait"])
                return {"run": _RUN, "reason": "timeout"}
            return {"run": _RUN}

        api.top_routes[("GET", "/api/runs/r_abc123")] = _get
        out = box.wait_for_run({"run_id": "r_abc123", "timeout_s": 30}, FakeCtx(clock))
        assert out["reason"] == "timeout"


class TestControl:
    def test_steers_its_own_group(self):
        box, api, clock, _ = _box()
        hit = []
        api.top_routes[("POST", "/api/runs/r_abc123/pause")] = (
            lambda p, q: hit.append("pause") or {}
        )
        api.top_routes[("POST", "/api/runs/r_abc123/tasks/t1/retry")] = (
            lambda p, q: hit.append(("retry", p)) or {}
        )
        api.top_routes[("POST", "/api/runs/r_abc123/resume")] = (
            lambda p, q: hit.append(("resume", p)) or {}
        )
        box.control_run({"run_id": "r_abc123", "action": "pause"}, FakeCtx(clock))
        box.control_run(
            {"run_id": "r_abc123", "action": "retry", "task_id": "t1", "fresh": True},
            FakeCtx(clock),
        )
        out = box.control_run(
            {"run_id": "r_abc123", "action": "resume", "budget_usd": 30}, FakeCtx(clock)
        )
        assert hit == [
            "pause",
            ("retry", {"fresh": True}),
            ("resume", {"budget_usd": 30.0}),
        ]
        assert out["id"] == "r_abc123"

    def test_a_task_action_needs_a_task(self):
        box, api, clock, _ = _box()
        with pytest.raises(ToolError):
            box.control_run({"run_id": "r_abc123", "action": "skip"}, FakeCtx(clock))

    def test_another_agents_group_is_refused(self):
        box, api, clock, _ = _box(me="other")
        with pytest.raises(ToolError) as err:
            box.control_run({"run_id": "r_abc123", "action": "cancel"}, FakeCtx(clock))
        assert "started by orch" in str(err.value)

    def test_a_group_the_user_started_may_be_steered(self):
        box, api, clock, _ = _box(me="other", run=dict(_RUN, created_by="user"))
        api.top_routes[("POST", "/api/runs/r_abc123/cancel")] = lambda p, q: {}
        box.control_run({"run_id": "r_abc123", "action": "cancel"}, FakeCtx(clock))

    @pytest.mark.parametrize("action", ["release", "resume"])
    def test_an_agent_never_releases_or_resumes_a_group_its_user_started(self, action):
        """The release is the one outward step of a one-for-all group, and a
        pause (or the budget stop) is the user's: an agent — the group's own
        lead included, which has the run id in its brief — may not take
        either for them."""
        box, api, clock, _ = _box(me="other", run=dict(_RUN, created_by="user"))
        hit = []
        api.top_routes[("POST", "/api/runs/r_abc123/" + action)] = (
            lambda p, q: hit.append(action) or {}
        )
        with pytest.raises(ToolError) as err:
            box.control_run({"run_id": "r_abc123", "action": action}, FakeCtx(clock))
        assert "started by your user" in str(err.value)
        assert hit == []

    def test_its_own_group_it_may_release(self):
        box, api, clock, _ = _box()
        hit = []
        api.top_routes[("POST", "/api/runs/r_abc123/release")] = (
            lambda p, q: hit.append("release") or {}
        )
        box.control_run({"run_id": "r_abc123", "action": "release"}, FakeCtx(clock))
        assert hit == ["release"]


def test_the_run_tools_are_known_everywhere():
    box, _, _, _ = _box()
    names = {t.name for t in build_tools(box)}
    for name in (
        "start_team_run",
        "get_run",
        "list_runs",
        "wait_for_run",
        "control_run",
    ):
        assert name in names and name in playbooks.TOOL_NAMES
    doc = (Path(__file__).resolve().parents[2] / "docs" / "mcp.md").read_text()
    for name in names:
        assert "### `%s`" % name in doc, name
    assert "The %d tools" % len(names) in doc


# --------------------------------------------------------------------------- #
# The split lead's two tools
# --------------------------------------------------------------------------- #
_SPLIT = dict(
    _RUN,
    split=True,
    lead={"title": "orch", "branch": "mf/orch"},
    plan=None,
    tasks=[],
)
_PIECES = [
    {"title": "tokens", "prompt": "rotate", "paths": ["auth/tokens*"]},
    {"title": "sessions", "prompt": "store", "paths": ["auth/session*"]},
]


class TestSplitLeadTools:
    def test_propose_posts_the_pieces_as_the_lead(self):
        box, api, clock, _ = _box(me="orch", run=_SPLIT)
        posted = []

        def _plan(p, q):
            posted.append(p)
            return {"plan": {"pieces": p["pieces"]}, "problems": []}

        api.top_routes[("POST", "/api/runs/r_abc123/plan")] = _plan
        out = box.propose_run_plan(
            {"run_id": "r_abc123", "pieces": _PIECES, "why": "two seams"},
            FakeCtx(clock),
        )
        assert out["ok"] is True and out["problems"] == []
        assert "do not spawn sessions" in out["note"]
        assert posted == [{"pieces": _PIECES, "why": "two seams", "from": "orch"}]

    def test_problems_come_back_to_fix_not_as_an_error(self):
        from backend import client

        box, api, clock, _ = _box(me="orch", run=_SPLIT)
        problems = [{"piece": "sessions", "error": "overlaps tokens on auth/x.py"}]

        def _plan(p, q):
            raise client.ApiError(
                422, "the plan has 1 problem", {"error": "…", "problems": problems}
            )

        api.top_routes[("POST", "/api/runs/r_abc123/plan")] = _plan
        out = box.propose_run_plan(
            {"run_id": "r_abc123", "pieces": _PIECES}, FakeCtx(clock)
        )
        assert out == {
            "ok": False,
            "problems": problems,
            "note": "fix these and call propose_run_plan again",
        }

    @pytest.mark.parametrize("tool", ["propose_run_plan", "report_integrated"])
    def test_only_the_lead_may_call_them(self, tool):
        box, api, clock, _ = _box(me="other", run=_SPLIT)
        args = {
            "run_id": "r_abc123",
            "pieces": _PIECES,
            "task_id": "t1",
            "head_sha": "abcd",
        }
        if tool == "propose_run_plan":
            args = {"run_id": "r_abc123", "pieces": _PIECES}
        else:
            args = {"run_id": "r_abc123", "task_id": "t1", "head_sha": "abcd"}
        with pytest.raises(ToolError) as err:
            getattr(box, tool)(args, FakeCtx(clock))
        assert "not the lead" in str(err.value)

    def test_a_run_without_a_lead_refuses(self):
        box, api, clock, _ = _box(me="orch")
        with pytest.raises(ToolError):
            box.propose_run_plan(
                {"run_id": "r_abc123", "pieces": _PIECES}, FakeCtx(clock)
            )

    def test_report_integrated_relays_the_servers_verdict(self):
        box, api, clock, _ = _box(me="orch", run=_SPLIT)
        seen = []
        verdict = {"ok": True, "verified": False}

        def _integ(p, q):
            seen.append(p)
            return dict(verdict)

        api.top_routes[("POST", "/api/runs/r_abc123/integrated")] = _integ
        out = box.report_integrated(
            {"run_id": "r_abc123", "task_id": "t2", "head_sha": "abc123"},
            FakeCtx(clock),
        )
        assert out["verified"] is False and "not in your HEAD" in out["note"]
        assert seen == [{"task_id": "t2", "head_sha": "abc123", "from": "orch"}]
        verdict["verified"] = True
        out = box.report_integrated(
            {"run_id": "r_abc123", "task_id": "t2", "head_sha": "abc123"},
            FakeCtx(clock),
        )
        assert out == {"ok": True, "verified": True}

    def test_schemas_and_names(self):
        box, _, _, _ = _box()
        by = {t.name: t for t in build_tools(box)}
        for name in ("propose_run_plan", "report_integrated"):
            assert name in playbooks.TOOL_NAMES
            assert by[name].annotations["readOnlyHint"] is False
        s = by["propose_run_plan"].input_schema
        assert s["required"] == ["run_id", "pieces"]
        item = s["properties"]["pieces"]["items"]
        assert item["required"] == ["title", "prompt", "paths"]
        assert s["properties"]["pieces"]["maxItems"] == 8

    def test_get_run_shows_the_plan_to_the_lead(self):
        run = dict(
            _SPLIT,
            plan={"state": "proposed", "round": 2, "pieces": _PIECES},
            check={"state": "ok", "tests": 3},
            release={"state": "none"},
        )
        box, api, clock, _ = _box(me="orch", run=run)
        out = box.get_run({"run_id": "r_abc123"}, FakeCtx(clock))
        assert out["lead"] == "orch"
        assert out["plan"] == {
            "state": "proposed",
            "round": 2,
            "pieces": [
                {"title": "tokens", "paths": ["auth/tokens*"]},
                {"title": "sessions", "paths": ["auth/session*"]},
            ],
        }
        assert out["check"] == {"state": "ok", "tests": 3}
        assert "release" not in out
