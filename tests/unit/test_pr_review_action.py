"""Run the PR review on an open PR (``core.pr_review.run_pr_review``, ``POST
/api/pr-review``, ``flights.op_merge_plan``, the ``flights.pr_review_plan``
setting): docs/flights.md "Run the PR review on an open PR". Against a fake
server namespace and tmp stores, never the real engine or ``~/.mindflock``."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import held as _held
from backend.web.core import pr_review as R
from backend.web.routes import flights as routes

URL = "https://github.com/acme/widgets/pull/7"


class FakeInst:
    def __init__(self, branch="feat", paused=False):
        self.Path = "/repo"
        self.Branch = branch
        self._paused = paused

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)

    def Paused(self):
        return self._paused


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    monkeypatch.setattr(_held, "is_held", lambda title: False)
    monkeypatch.setattr(_events, "sessions_snapshot", lambda: [])
    yield
    S.invalidate()


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(R, "_server", lambda: ns)
    return ns


def run(coro):
    return asyncio.run(coro)


def review_plan(name="Team review"):
    """The shape of a team's review-only route."""
    return F.save_plan(
        {
            "name": name,
            "steps": [
                {
                    "kind": "review",
                    "config": {"mode": "standards", "focus": "our rules"},
                },
                {"kind": "agent", "config": {"prompt": "Write the review section"}},
                {"kind": "reviews", "config": {"require_reviewer": "coderabbit"}},
                {"kind": "you", "config": {"text": "Merge or ask for review"}},
            ],
        }
    )


def ids(stops):
    return [s["id"] for s in stops]


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_setting_round_trips_and_is_emitted_only_when_set():
    assert S.Settings().to_dict() == {}
    s = S.Settings.from_dict({"flights": {"pr_review_plan": "  p_abc  "}})
    assert s.flights.pr_review_plan == "p_abc"
    assert s.to_dict() == {"flights": {"pr_review_plan": "p_abc"}}
    S.update_settings(flights={"pr_review_plan": "Team review"})
    assert S.load_settings().flights.pr_review_plan == "Team review"
    assert R.configured_plan() == "Team review"
    S.update_settings(flights={"pr_review_plan": ""})
    assert S.load_settings().to_dict().get("flights") is None
    assert R.configured_plan() == ""


def test_plan_defaults_to_the_setting_by_id_or_name(srv):
    plan = review_plan()
    with pytest.raises(F.FlightError) as err:
        R.resolve_plan()
    assert (err.value.error, err.value.status) == ("no_plan", 400)
    S.update_settings(flights={"pr_review_plan": "team REVIEW"})
    assert R.resolve_plan()["id"] == plan["id"]
    S.update_settings(flights={"pr_review_plan": plan["id"]})
    out = run(R.run_pr_review(session_title="s"))
    assert out["mode"] == "started" and out["session"] == "s"
    # An explicit plan wins over the setting.
    other = F.save_plan({"name": "Other", "steps": [{"kind": "notify"}]})
    assert R.resolve_plan(other["id"])["id"] == other["id"]
    with pytest.raises(F.FlightError) as err:
        R.resolve_plan("nope")
    assert (err.value.error, err.value.status) == ("unknown_plan", 404)


# --------------------------------------------------------------------------- #
# A session with no route → started
# --------------------------------------------------------------------------- #
def test_session_without_a_route_starts_at_the_first_stop(srv):
    plan = review_plan()
    out = run(R.run_pr_review(session_title="s", plan=plan["id"]))
    assert out["mode"] == "started"
    f = F.get(out["flight"])
    assert f["session"] == "s" and f["state"] == "running"
    assert f["cursor"] == 0 and f["plan_id"] == plan["id"]
    assert ids(out["stops"]) == ["s1", "s2", "s3", "s4"]
    assert out["stops"][0] == {
        "id": "s1",
        "kind": "review",
        "title": "Standards review",
    }


def test_a_leading_empty_agent_stop_is_the_work_already_done(srv):
    plan = F.save_plan(
        {"name": "R", "steps": [{"kind": "agent"}, {"kind": "review"}, {"kind": "you"}]}
    )
    out = run(R.run_pr_review(session_title="s", plan=plan["id"]))
    f = F.get(out["flight"])
    assert f["cursor"] == 1 and f["step_states"]["s1"]["state"] == "skipped"
    assert ids(out["stops"]) == ["s2", "s3"]
    # An agent stop WITH a prompt is where it starts (its prompt is sent).
    srv.ENGINE.instances["t"] = FakeInst()
    plan = F.save_plan(
        {"name": "R2", "steps": [{"kind": "agent", "config": {"prompt": "x"}}]}
    )
    out = run(R.run_pr_review(session_title="t", plan=plan["id"]))
    assert F.get(out["flight"])["cursor"] == 0
    # Nothing but empty agent stops: nothing to run.
    srv.ENGINE.instances["u"] = FakeInst()
    plan = F.save_plan({"name": "R3", "steps": [{"kind": "agent"}]})
    with pytest.raises(F.FlightError) as err:
        run(R.run_pr_review(session_title="u", plan=plan["id"]))
    assert err.value.status == 400


def test_started_session_gets_its_pr_as_the_subject(srv, monkeypatch):
    monkeypatch.setattr(
        _events, "sessions_snapshot", lambda: [{"title": "s", "pr_url": URL}]
    )
    out = run(R.run_pr_review(session_title="s", plan=review_plan()["id"]))
    subj = F.get(out["flight"])["subject"]
    assert (subj["kind"], subj["source"], subj["id"]) == ("pr", "acme/widgets", "7")


# --------------------------------------------------------------------------- #
# A session mid-route → inserted after the current stop, deduped
# --------------------------------------------------------------------------- #
def _ticket_route(srv):
    """A ticket route in the air on "s": the agent's work is the current
    stop, a ship push, the CodeRabbit loop and a merge ahead."""
    plan = F.save_plan(
        {
            "name": "Ticket",
            "steps": [
                {"kind": "agent", "config": {"prompt": "do it"}},
                {"kind": "ship", "config": {"lane": "push"}},
                {"kind": "reviews", "config": {"require_reviewer": "coderabbit"}},
                {"kind": "you", "config": {"text": "Merge or ask for review"}},
            ],
        }
    )
    return F.take_off(plan, session="s", seeded=True)


def test_mid_route_inserts_after_the_current_stop_before_ship_deduped(srv):
    f = _ticket_route(srv)
    plan = review_plan()
    out = run(R.run_pr_review(session_title="s", plan=plan["id"]))
    assert out["mode"] == "inserted" and out["flight"] == f["id"]
    after = F.get(f["id"])
    kinds = [s["kind"] for s in after["steps"]]
    # The CodeRabbit loop and the merge perch were already ahead: left out.
    # The rest goes right after the current stop, in front of the ship rung.
    assert kinds == ["agent", "review", "agent", "ship", "reviews", "you"]
    assert [s["kind"] for s in out["stops"]] == ["review", "agent"]
    # Fresh ids past the route's own, one group named after the plan.
    assert ids(out["stops"]) == ["s5", "s6"]
    assert after["steps"][1]["id"] == "s5" and after["steps"][2]["id"] == "s6"
    g = after["steps"][1]["group"]
    assert g["name"] == "Team review" and g["plan_id"] == plan["id"]
    assert after["steps"][2]["group"] == g
    assert after["step_states"]["s5"]["state"] == "pending"
    # The current stop is untouched; the flight is still the one route.
    assert after["cursor"] == 0 and after["steps"][0]["id"] == "s1"
    assert F.for_session("s")["id"] == f["id"]
    # Again: everything is already there now — nothing added.
    again = run(R.run_pr_review(session_title="s", plan=plan["id"]))
    assert again["mode"] == "inserted" and again["stops"] == []
    assert len(F.get(f["id"])["steps"]) == 6


def test_merge_counts_done_and_ahead_not_skipped_or_failed():
    plan = F.save_plan(
        {
            "name": "Mine",
            "steps": [
                {"kind": "review", "config": {"mode": "security"}},
                {"kind": "review", "config": {"mode": "standards"}},
                {"kind": "notify", "config": {"text": "hi"}},
                {"kind": "wait", "config": {"minutes": 1}},
            ],
        }
    )
    f = F.take_off(plan, session="s")
    f["step_states"]["s1"]["state"] = "done"
    f["step_states"]["s2"]["state"] = "skipped"
    f["step_states"]["s3"]["state"] = "running"
    f["cursor"] = 2
    review = F.save_plan(
        {
            "name": "Review",
            "steps": [
                {"kind": "review", "config": {"mode": "security"}},
                {"kind": "review", "config": {"mode": "standards"}},
                {"kind": "notify", "config": {"text": "hi"}},
                {"kind": "wait", "config": {"minutes": 1}},
                {"kind": "wait", "config": {"minutes": 2}},
            ],
        }
    )
    added = []
    F.op_merge_plan(f, review, added=added)
    # security ran (done), notify is on now, the 1-minute wait is ahead;
    # standards was skipped, so it comes again; the 2-minute wait is new.
    assert [
        (s["kind"], s["config"].get("mode") or s["config"].get("minutes"))
        for s in added
    ] == [
        ("review", "standards"),
        ("wait", 2),
    ]
    assert [s["id"] for s in f["steps"]] == ["s1", "s2", "s3", "s5", "s6", "s4"]


def test_merge_goes_after_the_current_parallel_group_and_before_a_land():
    plan = F.save_plan(
        {
            "name": "P",
            "steps": [
                {"kind": "agent"},
                {"kind": "wait", "config": {"minutes": 1}, "parallel": {"id": "p_a"}},
                {"kind": "notify", "config": {"text": "x"}, "parallel": {"id": "p_a"}},
                {"kind": "notify", "config": {"text": "y"}},
                {"kind": "land"},
            ],
        }
    )
    f = F.take_off(plan, session="s")
    f["cursor"] = 1
    f["step_states"]["s1"]["state"] = "done"
    f["step_states"]["s2"]["state"] = "running"
    assert F.merge_point(f) == 4  # past the group, in front of the Land
    f["cursor"] = 3
    f["step_states"]["s4"]["state"] = "running"
    review = F.save_plan({"name": "R", "steps": [{"kind": "review"}]})
    F.op_merge_plan(f, review)
    assert [s["kind"] for s in f["steps"]][4:] == ["review", "land"]


def test_a_loop_whose_target_was_left_out_goes_too():
    plan = F.save_plan({"name": "P", "steps": [{"kind": "agent"}, {"kind": "review"}]})
    f = F.take_off(plan, session="s")
    review = F.save_plan(
        {
            "name": "R",
            "steps": [
                {"kind": "review"},
                {"kind": "notify", "config": {"text": "z"}},
                {"kind": "loop", "config": {"back_to": "s1", "times": 2}},
            ],
        }
    )
    added = []
    F.op_merge_plan(f, review, added=added)
    assert [s["kind"] for s in added] == ["notify"]


def test_a_finished_route_refuses_and_a_full_one_too():
    plan = F.save_plan({"name": "P", "steps": [{"kind": "notify"}]})
    f = F.take_off(plan, session="s")
    f["state"] = "done"
    with pytest.raises(F.FlightError) as err:
        F.op_merge_plan(f, F.save_plan({"name": "R", "steps": [{"kind": "review"}]}))
    assert err.value.status == 409


# --------------------------------------------------------------------------- #
# Only a PR URL
# --------------------------------------------------------------------------- #
def test_pr_url_reuses_the_session_on_its_head_branch(srv, monkeypatch):
    srv.ENGINE.instances = {"other": FakeInst("main"), "mine": FakeInst("feat-7")}

    async def head(slug, number):
        assert (slug, number) == ("acme/widgets", 7)
        return "feat-7"

    monkeypatch.setattr(R, "pr_head", head)
    monkeypatch.setattr(R, "_repo_slug", lambda title: "acme/widgets")

    async def no_launch(*a, **kw):
        raise AssertionError("must not create a second session")

    monkeypatch.setattr(R, "launch", no_launch)
    out = run(R.run_pr_review(pr_url=URL, plan=review_plan()["id"]))
    assert out["mode"] == "started" and out["session"] == "mine"
    subj = F.get(out["flight"])["subject"]
    assert (subj["kind"], subj["id"], subj["url"]) == ("pr", "7", URL)


def test_pr_url_matches_a_row_showing_that_pr_without_a_lookup(srv, monkeypatch):
    srv.ENGINE.instances = {"mine": FakeInst("whatever")}
    monkeypatch.setattr(
        _events,
        "sessions_snapshot",
        lambda: [
            {"title": "mine", "pr_url": "https://github.com/ACME/widgets/pull/7/"}
        ],
    )

    async def boom(*a, **kw):
        raise AssertionError("no GitHub lookup needed")

    monkeypatch.setattr(R, "pr_head", boom)
    out = run(R.run_pr_review(pr_url=URL, plan=review_plan()["id"]))
    assert out["session"] == "mine"


def test_pr_url_with_no_session_creates_one_like_intake(srv, monkeypatch):
    srv.ENGINE.instances = {"elsewhere": FakeInst("feat-7")}

    async def head(slug, number):
        return "feat-7"

    # Same branch name, another repo: not this PR's session.
    monkeypatch.setattr(R, "pr_head", head)
    monkeypatch.setattr(R, "_repo_slug", lambda title: "someone/else")
    plan = review_plan()
    calls = []

    async def fake_launch(repo, number, **kw):
        calls.append((repo, number, kw))
        F.arm_pending(
            "pr-widgets-7", kw["plan"], {"kind": "pr", "source": repo, "id": "7"}
        )
        return {"started": True, "title": "pr-widgets-7"}

    monkeypatch.setattr(R, "launch", fake_launch)
    out = run(R.run_pr_review(pr_url=URL, plan=plan["id"]))
    assert calls == [("acme/widgets", 7, {"plan": plan["id"]})]
    assert out["mode"] == "created" and out["session"] == "pr-widgets-7"
    assert out["flight"] == F.for_session("pr-widgets-7")["id"]
    assert ids(out["stops"]) == ["s1", "s2", "s3", "s4"]


def test_launch_refusals_pass_through(srv, monkeypatch):
    srv.ENGINE.instances = {}

    async def head(slug, number):
        return ""

    async def refuse(repo, number, **kw):
        raise R.LaunchError(404, {"error": "No open PR #7 in acme/widgets"})

    monkeypatch.setattr(R, "pr_head", head)
    monkeypatch.setattr(R, "launch", refuse)
    with pytest.raises(R.LaunchError) as err:
        run(R.run_pr_review(pr_url=URL, plan=review_plan()["id"]))
    assert err.value.status == 404


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_errors(srv, monkeypatch):
    plan = review_plan()["id"]

    def refused(**kw):
        with pytest.raises(F.FlightError) as err:
            run(R.run_pr_review(plan=plan, **kw))
        return err.value.error, err.value.status

    assert refused() == ("needs_target", 400)
    assert refused(pr_url="https://gitlab.com/a/b/-/merge_requests/1") == (
        "bad_pr_url",
        400,
    )
    assert refused(session_title="ghost") == ("unknown_session", 404)
    srv.ENGINE.instances["p"] = FakeInst(paused=True)
    assert refused(session_title="p") == ("paused", 409)
    monkeypatch.setattr(_held, "is_held", lambda title: title == "s")
    assert refused(session_title="s") == ("held", 409)
    with pytest.raises(F.FlightError) as err:
        run(R.run_pr_review(session_title="s"))
    assert (err.value.error, err.value.status) == ("no_plan", 400)


def test_parse_pr_url():
    assert R.parse_pr_url(URL) == ("acme/widgets", 7)
    assert R.parse_pr_url("https://github.com/a/b.git/pull/12/files") == ("a/b", 12)
    with pytest.raises(F.FlightError):
        R.parse_pr_url("https://github.com/a/b/issues/3")


# --------------------------------------------------------------------------- #
# POST /api/pr-review
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_route_returns_the_result_and_maps_errors(srv, client, monkeypatch):
    plan = review_plan()
    r = client.post("/api/pr-review", json={"session": "s", "plan": plan["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "started" and body["session"] == "s"
    assert body["flight"] and [s["kind"] for s in body["stops"]] == [
        "review",
        "agent",
        "reviews",
        "you",
    ]
    assert F.get(body["flight"])["by"] == "user"
    r = client.post("/api/pr-review", json={"session": "s"})
    assert r.status_code == 400 and r.json()["error"] == "no_plan"
    r = client.post("/api/pr-review", json={"session": "ghost", "plan": plan["id"]})
    assert r.status_code == 404 and r.json()["error"] == "unknown_session"
    srv.ENGINE.instances["p"] = FakeInst(paused=True)
    r = client.post("/api/pr-review", json={"session": "p", "plan": plan["id"]})
    assert r.status_code == 409 and r.json()["error"] == "paused"

    async def refuse(**kw):
        raise R.LaunchError(409, {"error": "session pr-widgets-7 already exists"})

    monkeypatch.setattr(R, "run_pr_review", refuse)
    r = client.post("/api/pr-review", json={"pr_url": URL})
    assert r.status_code == 409 and "already exists" in r.json()["error"]


def test_route_passes_an_agent_as_by(srv, client):
    plan = review_plan()
    r = client.post(
        "/api/pr-review",
        json={"session": "s", "plan": plan["id"], "by": "agent:lead"},
    )
    assert F.get(r.json()["flight"])["by"] == "agent:lead"
    srv.ENGINE.instances["t"] = FakeInst()
    r = client.post(
        "/api/pr-review", json={"session": "t", "plan": plan["id"], "by": "intake"}
    )
    assert F.get(r.json()["flight"])["by"] == "user"


def test_settings_file_carries_the_plan(tmp_path):
    S.update_settings(flights={"pr_review_plan": "p_x"})
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["flights"] == {"pr_review_plan": "p_x"}


# --------------------------------------------------------------------------- #
# MCP run_pr_review
# --------------------------------------------------------------------------- #
def _mcp_box(me="orch", scope="children", setting="", plans=()):
    from tests.unit._mcp_fakes import make_box, row

    rows = [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("other"),
    ]
    box, api, clock = make_box(rows, me=me, scope=scope)
    api.top_routes[("GET", "/api/flights/kinds")] = F.kinds_dto()
    api.top_routes[("GET", "/api/flights/plans")] = {"plans": list(plans)}
    api.top_routes[("GET", "/api/settings")] = {
        "settings": {"flights": {"pr_review_plan": setting}} if setting else {}
    }
    sent = []

    def post(payload, q):
        sent.append(payload)
        return {
            "mode": "inserted",
            "session": payload.get("session") or "pr-widgets-7",
            "flight": "f_1",
            "stops": [{"id": "s5", "kind": "review", "title": "Standards review"}],
        }

    api.top_routes[("POST", "/api/pr-review")] = post
    return box, sent


def _mcp_plan(pid="p_team", name="Team review", steps=None):
    return {
        "id": pid,
        "name": name,
        "steps": steps or [{"id": "s1", "kind": "review", "config": {}}],
    }


def test_mcp_tool_is_registered_and_sends_the_contract_body():
    from tests.unit._mcp_fakes import FakeCtx

    from backend.mcp.tools import build_tools

    box, sent = _mcp_box(plans=[_mcp_plan()])
    tool = {t.name: t for t in build_tools(box)}["run_pr_review"]
    assert tool.annotations["openWorldHint"] is True
    out = box.run_pr_review({"session": "w1", "plan": "team review"}, FakeCtx(None))
    assert sent == [{"by": "agent:orch", "session": "w1", "plan": "p_team"}]
    assert out["mode"] == "inserted" and out["flight"] == "f_1"
    assert out["stops"] == ["s5 review Standards review"]
    # No session and no URL: yourself. The plan comes from the setting.
    box, sent = _mcp_box(setting="p_team", plans=[_mcp_plan()])
    box.run_pr_review({}, FakeCtx(None))
    assert sent[0]["session"] == "orch" and sent[0]["plan"] == "p_team"
    # Only a URL: the server finds or makes the session.
    box, sent = _mcp_box(plans=[_mcp_plan()])
    out = box.run_pr_review({"pr_url": URL}, FakeCtx(None))
    assert sent == [{"by": "agent:orch", "pr_url": URL}]


def test_mcp_tool_scope_and_confirm():
    from tests.unit._mcp_fakes import FakeCtx

    from backend.mcp.protocol import ToolError

    box, sent = _mcp_box(plans=[_mcp_plan()])
    with pytest.raises(ToolError):
        box.run_pr_review({"session": "other"}, FakeCtx(None))
    with pytest.raises(ToolError) as err:
        box.run_pr_review({"session": "w1", "plan": "nope"}, FakeCtx(None))
    assert "no saved route" in str(err.value)
    merge = _mcp_plan(
        steps=[
            {"id": "s1", "kind": "ship", "title": "Merge", "config": {"lane": "merge"}}
        ]
    )
    box, sent = _mcp_box(plans=[merge])
    with pytest.raises(ToolError) as err:
        box.run_pr_review({"pr_url": URL, "plan": "p_team"}, FakeCtx(None))
    assert "can't be undone" in str(err.value) and sent == []
    box.run_pr_review(
        {"pr_url": URL, "plan": "p_team", "confirm_merge": True}, FakeCtx(None)
    )
    assert sent and sent[0]["plan"] == "p_team"
