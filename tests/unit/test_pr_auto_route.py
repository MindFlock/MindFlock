"""The route an automatically picked-up PR flies (docs/ingestion-pipeline.md
"A PR the monitor picks up flies a route", docs/flights.md "Which route a PR
flies"): ``core.pr_route``'s resolution (repo route > repo rung > the flock's
``flights.pr_review_plan`` > the built-in default; ``off`` = none), the
server's ``pr_review.intake_auto`` on a new / route-less / mid-route session,
and the pipeline's hand-off (``SessionRunner.hand_to_server``) with its
fallback when the server is out of reach.

Every store is in tmp and the server is a fake namespace: never the real
engine, state.json or ``~/.mindflock``."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.config import settings as S
from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import held as _held
from backend.web.core import pr_review as R
from backend.web.core import pr_route as PR
from backend.web.core import prompt_queue as Q

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
def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.delenv("MINDFLOCK_SERVER_PORT", raising=False)
    S.invalidate()
    monkeypatch.setattr(_held, "is_held", lambda title: False)
    monkeypatch.setattr(_events, "sessions_snapshot", lambda: [])
    # The session's live facts: idle, so "busy" comes from its queue alone.
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: {})
    yield
    S.invalidate()


def _settings(data: dict) -> None:
    """Write settings.json as ANOTHER process would (the UI's server): the
    reader must see it without this process's cache being told."""
    S.load_settings()
    path = S.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def _review_route(name="Team review") -> str:
    return F.save_plan(
        {
            "name": name,
            "steps": [
                {"kind": "agent"},
                {"kind": "review", "config": {"mode": "standards"}},
                {"kind": "reviews", "config": {"require_reviewer": "coderabbit"}},
            ],
        }
    )["id"]


# --------------------------------------------------------------------------- #
# Resolution: repo route > repo rung > flock route > built-in default; off
# --------------------------------------------------------------------------- #
def test_unconfigured_flies_the_builtin_default():
    assert PR.DEFAULT_PLAN == "builtin:pr"
    assert PR.flock_plan() == "builtin:pr"
    assert PR.for_repo("o/r") == "builtin:pr"
    steps = [s["kind"] for s in F.get_plan("builtin:pr")["steps"]]
    assert steps == ["agent", "ship", "ship", "reviews", "checks"]


def test_resolution_order():
    mine = _review_route()
    other = _review_route("Other")
    _settings(
        {
            "flights": {"pr_review_plan": mine},
            "github": {
                "repo_settings": {
                    "o/own": {"plan": other, "depth": "push"},
                    "o/rung": {"depth": "push"},
                    "o/agent": {"depth": "agent"},
                    "o/gone": {"plan": "p_gone00"},
                    "o/off": {"depth": "off"},
                }
            },
        }
    )
    # 1. the repo's own route (over its rung), while it is saved
    assert PR.for_repo("o/own") == other
    assert PR.for_repo("O/OWN") == other  # case aside
    # 2. the repo's rung: a ship rung is the built-in cut; Agent only = none
    assert PR.for_repo("o/rung") == "builtin:pr@push"
    assert PR.for_repo("o/agent") == ""
    # 3. the flock's PR route — for a repo with no card, a gone route, "off"
    assert PR.for_repo("o/none") == mine
    assert PR.for_repo("o/gone") == mine
    assert PR.for_repo("o/off") == mine
    # ... by name too, and "off" is no route at all
    _settings({"flights": {"pr_review_plan": "team REVIEW"}})
    assert PR.for_repo("o/none") == mine
    _settings({"flights": {"pr_review_plan": "off"}})
    assert PR.for_repo("o/none") == "" and PR.is_off("Off")
    # 4. a flock route no longer saved → the built-in default
    _settings({"flights": {"pr_review_plan": "p_gone00"}})
    assert PR.for_repo("o/none") == "builtin:pr"


def test_hand_start_and_auto_start_agree():
    """server._intake_plan (Intake's Begin review) and the pipeline's
    _plan_for answer the same, re-reading a save made by another process."""
    from backend.ticket_ingestion.config import (
        EngineConfig,
        PipelineConfig,
        TicketProviderConfig,
    )
    from backend.ticket_ingestion.session_runner import SessionRunner
    from backend.web import server

    runner = SessionRunner(
        PipelineConfig(
            ticketing=TicketProviderConfig(
                provider="shortcut", api_token="t", member_id="m"
            ),
            repo_url="git@github.com:o/r.git",
            workspace_dir="/nonexistent-workspaces",
            min_description_length=20,
            log_file="/dev/null",
            log_level="INFO",
            engine=EngineConfig(enabled=True, mode="worktree"),
        )
    )

    def both(repo):
        hand = server._intake_plan(
            "pr",
            "",
            "",
            server._repo_intake_plan(repo, "prs"),
            server._repo_intake_depth(repo, "prs"),
        )
        auto = runner._plan_for("pr", repo, {"kind": "pr", "source": repo})
        assert hand == auto, (repo, hand, auto)
        return auto

    mine = _review_route()
    assert both("o/r") == "builtin:pr"
    _settings({"flights": {"pr_review_plan": mine}})
    S.invalidate()  # this process's server half reads its own (fresh) cache
    assert both("o/r") == mine
    _settings(
        {
            "flights": {"pr_review_plan": mine},
            "github": {"repo_settings": {"o/r": {"depth": "commit"}}},
        }
    )
    S.invalidate()
    assert both("o/r") == "builtin:pr@commit"
    _settings({"flights": {"pr_review_plan": "off"}})
    S.invalidate()
    assert both("o/r") == ""
    # A per-start pick still outranks everything (the hand start's own).
    assert server._intake_plan("pr", mine, "", "", "") == mine
    assert server._intake_plan("pr", "", "off", "", "") == ""


def test_the_pipeline_rereads_a_ui_save():
    """The settings cache is per process: a route picked in the UI after the
    pipeline last read settings applies to its next PR."""
    assert PR.for_repo("o/r") == "builtin:pr"  # cached now
    mine = _review_route()
    _settings({"flights": {"pr_review_plan": mine}})
    assert PR.for_repo("o/r") == mine


# --------------------------------------------------------------------------- #
# The server side: pr_review.intake_auto
# --------------------------------------------------------------------------- #
PR_OBJ = SimpleNamespace(
    number=7,
    title="My PR",
    url=URL,
    head_ref="feat",
    head_sha="abc",
    repo="acme/widgets",
)


@pytest.fixture
def srv(monkeypatch):
    from backend.web import server

    launched: list = []

    async def _launch(repo, number, **kw):
        launched.append((repo, number, kw))
        # What a real launch arms before its session exists.
        plan = ns._intake_plan(
            "pr",
            "",
            "",
            ns._repo_intake_plan(repo, "prs"),
            ns._repo_intake_depth(repo, "prs"),
        )
        if plan:
            F.arm_pending("pr-widgets-7", plan, {"kind": "pr"}, by="intake")
        return {"started": True, "title": "pr-widgets-7"}

    async def _find_pr(repo, number):
        if number != 7:
            raise LookupError("No open PR #%d in %s" % (number, repo))
        return PR_OBJ

    ns = SimpleNamespace(
        ENGINE=SimpleNamespace(instances={}),
        _publish_autopilot=lambda title: None,
        _intake_plan=server._intake_plan,
        _repo_intake_plan=server._repo_intake_plan,
        _repo_intake_depth=server._repo_intake_depth,
        _load_recently_closed=lambda: [],
        launched=launched,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(R, "_server", lambda: ns)
    monkeypatch.setattr(R, "find_pr", _find_pr)
    monkeypatch.setattr(R, "launch", _launch)
    monkeypatch.setattr(R, "record_reviewed", lambda pr: None)
    monkeypatch.setattr(R, "pr_head", _no_head)
    return ns


async def _no_head(slug, number):
    return ""


def _auto(text="# PR #7\n\nreview comments", ids=("c1",)):
    return run(R.intake_auto("acme/widgets", 7, text=text, comment_ids=list(ids)))


def test_no_session_creates_one_like_intake(srv):
    out = _auto()
    assert out["mode"] == "created" and out["session"] == "pr-widgets-7"
    assert out["plan"] == "builtin:pr" and out["prompt"] == "first"
    assert srv.launched == [("acme/widgets", 7, {})]
    assert F.for_session("pr-widgets-7")["plan_id"] == "builtin:pr"
    with pytest.raises(R.LaunchError) as err:
        run(R.intake_auto("acme/widgets", 8))
    assert err.value.status == 404


def test_session_without_a_route_starts_it_with_the_prompt_as_its_work(srv):
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    out = _auto()
    assert out["mode"] == "started" and out["prompt"] == "queued"
    assert out["plan"] == "builtin:pr"
    f = F.for_session("pr-widgets-7")
    assert f["plan_id"] == "builtin:pr" and f["by"] == "intake"
    assert (f["subject"]["kind"], f["subject"]["id"]) == ("pr", "7")
    # The leading Agent stop is the queued review prompt — not skipped as
    # work already done, so commit/push wait for the agent to address it.
    assert f["cursor"] == 0
    assert f["step_states"]["s1"]["state"] == "running"
    q = Q.get_state("pr-widgets-7")
    assert [i["text"] for i in q["items"]] == ["# PR #7\n\nreview comments"]
    assert q["enabled"] and not q.get("held")  # released for the drain


def test_the_ticket_session_that_built_the_branch_is_used(srv, monkeypatch):
    """No pr-… session: the live session on the PR's head branch takes it."""
    srv.ENGINE.instances["sc-42"] = FakeInst(branch="feat")
    monkeypatch.setattr(R, "pr_head", _head)
    monkeypatch.setattr(R, "_repo_slug", lambda t: "acme/widgets")
    out = _auto()
    assert out["session"] == "sc-42" and out["mode"] == "started"
    assert srv.launched == []


async def _head(slug, number):
    return "feat"


def _ticket_route(title, reviews_state="pending", seen=()):
    plan = F.save_plan(
        {
            "name": "Ticket",
            "steps": [
                {"kind": "agent", "config": {"prompt": "do it"}},
                {"kind": "ship", "config": {"lane": "push"}},
                {"kind": "reviews", "config": {"quiet_minutes": 10}},
            ],
        }
    )
    f = F.take_off(plan, session=title, seeded=True)
    if reviews_state == "running":

        def _at_reviews(x):
            x["cursor"] = 2
            for sid in ("s1", "s2"):
                x["step_states"][sid]["state"] = "done"
            x["step_states"]["s3"].update(
                state="running", detail={"seeded": True, "seen": list(seen)}
            )

        f = F.mutate(f["id"], _at_reviews)
    return f


def test_mid_route_merges_the_stops_and_queues_the_prompt(srv):
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    before = _ticket_route("pr-widgets-7")
    out = _auto()
    assert out["mode"] == "inserted" and out["flight"] == before["id"]
    after = F.get(before["id"])
    # The default's commit and checks went in; its push and reviews were
    # already ahead (deduped), and its empty "the work" Agent stop is the
    # queued prompt, never merged as a stop nobody types into.
    assert sorted(s["kind"] for s in out["stops"]) == ["checks", "ship"]
    assert [s for s in after["steps"] if s["kind"] == "agent"] == [after["steps"][0]]
    # A pending Reviews stop seeds what is on the PR as seen: the comments
    # must come in by prompt.
    assert out["prompt"] == "queued"
    assert len(Q.get_state("pr-widgets-7")["items"]) == 1
    # Never a second route on the session.
    assert len([f for f in F.list_flights() if f["session"] == "pr-widgets-7"]) == 1


def test_a_running_reviews_stop_sends_the_comments_itself(srv):
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    _ticket_route("pr-widgets-7", reviews_state="running", seen=("old",))
    out = _auto(ids=("old", "c1"))
    assert out["prompt"] == "reviews"
    assert Q.get_state("pr-widgets-7")["items"] == []


def test_a_running_reviews_stop_that_saw_them_all_gets_the_prompt(srv):
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    _ticket_route("pr-widgets-7", reviews_state="running", seen=("c1",))
    out = _auto(ids=("c1",))
    assert out["prompt"] == "queued"


def test_routes_off_is_the_plain_prompt(srv):
    _settings({"flights": {"pr_review_plan": "off"}})
    S.invalidate()
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    out = _auto()
    assert out["mode"] == "queued" and out["plan"] == "" and out["flight"] == ""
    assert F.for_session("pr-widgets-7") is None
    assert len(Q.get_state("pr-widgets-7")["items"]) == 1


def test_a_paused_session_just_gets_the_prompt(srv):
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst(paused=True)
    out = _auto()
    assert out["mode"] == "queued" and "paused" in out["note"]
    assert F.for_session("pr-widgets-7") is None


def test_the_route_mode_on_the_intake_endpoint(srv, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server._pr_review, "intake_auto", R.intake_auto)
    srv.ENGINE.instances["pr-widgets-7"] = FakeInst()
    client = TestClient(server.app)
    r = client.post(
        "/api/github/prs/review",
        json={
            "repo": "acme/widgets",
            "number": 7,
            "auto": True,
            "text": "comments",
            "comments": ["c1"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "started"
    r = client.post(
        "/api/github/prs/review",
        json={"repo": "acme/widgets", "number": 8, "auto": True},
    )
    assert r.status_code == 404 and "No open PR" in r.json()["error"]


# --------------------------------------------------------------------------- #
# The pipeline side: SessionRunner.hand_to_server and run_pr's fallback
# --------------------------------------------------------------------------- #
def _runner(tmp_path, monkeypatch):
    from backend.ticket_ingestion.config import (
        EngineConfig,
        PipelineConfig,
        TicketProviderConfig,
    )
    from backend.ticket_ingestion.session_runner import SessionRunner

    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "ws"))
    runner = SessionRunner(
        PipelineConfig(
            ticketing=TicketProviderConfig(
                provider="shortcut", api_token="t", member_id="m"
            ),
            repo_url="git@github.com:acme/widgets.git",
            workspace_dir=tmp_path / "workspaces",
            min_description_length=20,
            log_file=tmp_path / "pipeline.log",
            log_level="INFO",
            engine=EngineConfig(enabled=True, mode="worktree"),
        )
    )
    old = SimpleNamespace(builder=[], created=[])

    async def _builder(pr, prompt):
        old.builder.append(prompt)
        return ""

    async def _provision(pr, launch_cursor=False):
        return SimpleNamespace(directory=tmp_path / "pr-ws")

    async def _room(what):
        return None

    from backend.ticket_ingestion import session_runner as SR

    monkeypatch.setattr(runner, "hand_to_builder", _builder)
    monkeypatch.setattr(runner._pr_provisioner, "provision", _provision)
    monkeypatch.setattr(SR, "_wait_for_launch_room", _room)
    monkeypatch.setattr(
        runner,
        "_create_pr_instance",
        lambda title, head, d, prompt, repo="": old.created.append((title, prompt)),
    )
    monkeypatch.setattr(runner, "_arm_autopilot", lambda *a: None)
    return runner, old


def _pr():
    from datetime import datetime, timezone

    from backend.ticket_ingestion.models import PRComment, PullRequest

    pr = PullRequest(
        number=7,
        head_ref="feat",
        head_sha="abc",
        base_ref="main",
        title="My PR",
        url=URL,
        author="me",
        created_at=datetime.now(timezone.utc),
        repo="acme/widgets",
    )
    c = PRComment(
        id=101,
        kind="review",
        author="rev",
        body="rename this",
        url=URL + "#c101",
        path="a.py",
        line=3,
        diff_hunk="",
    )
    return pr, [c]


def test_server_reachable_hands_over_and_types_nothing(tmp_path, monkeypatch):
    from backend import client

    runner, old = _runner(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "8765")
    sent = []

    def _post(base, path, payload=None, timeout=0):
        sent.append((base, path, payload))
        return {"mode": "started", "session": "sc-42", "plan": "builtin:pr"}

    monkeypatch.setattr(client, "post", _post)
    pr, comments = _pr()
    assert run(runner.run_pr(pr, comments)) == "feat"
    ((base, path, body),) = sent
    assert base == "http://127.0.0.1:8765" and path == "/api/github/prs/review"
    assert body["auto"] is True and body["repo"] == "acme/widgets"
    assert body["number"] == 7 and body["comments"] == ["101"]
    assert "rename this" in body["text"]
    # No bare prompt typed, no session of the pipeline's own.
    assert old.builder == [] and old.created == []


def test_server_unreachable_falls_back_to_todays_behaviour(tmp_path, monkeypatch):
    from backend import client

    runner, old = _runner(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "8765")

    def _down(*a, **k):
        raise client.ServerNotFound("connection refused")

    monkeypatch.setattr(client, "post", _down)
    _settings({"flights": {"pr_review_plan": "off"}})
    pr, comments = _pr()
    run(runner.run_pr(pr, comments))
    assert len(old.builder) == 1 and "rename this" in old.builder[0]
    ((title, prompt),) = old.created
    assert title == "pr-widgets-7" and "rename this" in prompt
    assert F.for_session("pr-widgets-7") is None  # routes off: no flight


def test_fallback_still_arms_the_resolved_route(tmp_path, monkeypatch):
    """Out of reach, the pipeline's own session still flies the route the
    server would have armed (the flights store is a shared file; the
    server's driver flies it once it is back)."""
    runner, old = _runner(tmp_path, monkeypatch)  # no MINDFLOCK_SERVER_PORT
    pr, comments = _pr()
    run(runner.run_pr(pr, comments))
    assert len(old.created) == 1
    assert F.for_session("pr-widgets-7")["plan_id"] == "builtin:pr"


def test_hand_off_answers(tmp_path, monkeypatch):
    from backend import client

    runner, old = _runner(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "8765")
    pr, comments = _pr()

    def _raise(err):
        def _post(*a, **k):
            raise err

        return _post

    # Already being started (409) and a timeout count as handed: never twice.
    monkeypatch.setattr(client, "post", _raise(client.ApiError(409, "starting")))
    assert run(runner.hand_to_server(pr, comments, "x")) == "starting"
    monkeypatch.setattr(client, "post", _raise(client.RequestTimeout("slow")))
    assert run(runner.hand_to_server(pr, comments, "x")) == "unconfirmed"
    # The route refused the PR (gone, GitHub down): the retry cap applies.
    monkeypatch.setattr(
        client,
        "post",
        _raise(client.ApiError(404, "No open PR", {"error": "No open PR"})),
    )
    with pytest.raises(RuntimeError):
        run(runner.hand_to_server(pr, comments, "x"))
    # Not the route answering (auth gate, an old server): the fallback.
    for err in (
        client.ApiError(401, "unauthorized", {"error": "unauthorized"}),
        client.ApiError(404, "Not Found", {"detail": "Not Found"}),
    ):
        monkeypatch.setattr(client, "post", _raise(err))
        assert run(runner.hand_to_server(pr, comments, "x")) == ""
