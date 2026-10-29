"""Flights × Intake (docs/flights.md "How a ticket becomes a flight").

The three Intake start routes, ``POST /api/flights/start`` with a ticket / PR /
issue subject, and the pipeline's auto-start all resolve a flight plan the same
way and arm it on the deterministic title BEFORE the session exists. These
tests drive the real routes over a TestClient with every store in tmp; the
launch's background task is captured and run by hand, with ``NewInstance``
stubbed so nothing is provisioned.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web import server
from backend.web.core import flights as F

client = TestClient(server.app)


class _Stop(Exception):
    """Raised by the NewInstance stub once it has the options."""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    store.invalidate()
    # A copy of the registry, never the live state.json's dict.
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_cached_session_title", lambda *a, **k: "")
    monkeypatch.setattr(
        server._worktree_reclaim, "reclaim_for_launch", lambda *a, **k: None
    )
    yield
    store.invalidate()


@pytest.fixture
def launch(monkeypatch):
    """Records what was armed and what the launch would have started with.
    ``launch.tasks`` are the captured background launches; ``launch.run()``
    runs them (NewInstance records its options and raises)."""
    rec = SimpleNamespace(tasks=[], opts=[], autopilot=[], events=[])

    def _new(opts):
        rec.opts.append(opts)
        raise _Stop("provisioning failed")

    def _autopilot(title, depth, source, item, message="", by="user"):
        rec.autopilot.append((title, depth, source))

    def _emit(event, **kw):
        rec.events.append((event, kw.get("session")))

    def _run():
        for coro in rec.tasks:
            asyncio.run(coro)
        rec.tasks.clear()

    rec.run = _run
    monkeypatch.setattr(server, "_register_task", rec.tasks.append)
    monkeypatch.setattr(server.session, "NewInstance", _new)
    monkeypatch.setattr(server, "_arm_intake_autopilot", _autopilot)
    monkeypatch.setattr(server._events.BUS, "emit", _emit)
    yield rec
    # Nothing launched leaks a provisioning row (the registry is process-wide).
    for coro in rec.tasks:
        coro.close()
    from backend.web.core import pending

    for title in list(pending.snapshot()):
        pending.drop(title)


def _settings(data: dict) -> None:
    store.invalidate()
    path = store.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    store.invalidate()


def _plan(steps, name="Mine") -> str:
    return F.save_plan({"name": name, "steps": steps})["id"]


def _stub_ticket(monkeypatch, title="sc-1"):
    story = SimpleNamespace(
        id="1",
        name="Fix it",
        app_url="https://app.example/story/1",
        repo_url="/nowhere",
        agent="claude",
        effort="",
    )
    ts = server._ticket_start

    async def _find(source, tid):
        return story

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ts, "find_ticket", _find)
    monkeypatch.setattr(ts, "session_title", lambda s: title)
    monkeypatch.setattr(ts, "branch_for", lambda s: "feature/" + title)
    monkeypatch.setattr(ts, "workspace_mode", lambda: "worktree")
    monkeypatch.setattr(ts, "build_prompt", lambda s: "do the ticket")
    monkeypatch.setattr(ts, "record_started", lambda s: None)
    monkeypatch.setattr(ts, "record_result", lambda *a, **k: None)
    monkeypatch.setattr(ts, "agent_for", lambda s: "claude")
    monkeypatch.setattr(ts, "effort_for", lambda s: "")
    monkeypatch.setattr(ts, "route_story", lambda *a, **k: None)
    monkeypatch.setattr(ts, "download_attachments", _none)
    monkeypatch.setattr(ts, "move_to_start_state", _none)
    from backend.ticket_ingestion import tracker_claims

    monkeypatch.setattr(tracker_claims, "tracker_holder", _none)
    return story


def _stub_pr(monkeypatch, title="pr-repo-7"):
    pr = SimpleNamespace(
        number=7,
        title="My PR",
        url="https://github.com/o/r/pull/7",
        head_ref="feature/x",
        repo="o/r",
    )
    pr_review = server._pr_review

    async def _find(repo, number):
        if number != 7:
            raise LookupError("No open PR #%s in %s" % (number, repo))
        return pr

    async def _prepare(p):
        return "/tmp/ws-pr-7", "review the PR", 2

    monkeypatch.setattr(pr_review, "find_pr", _find)
    monkeypatch.setattr(pr_review, "session_title", lambda p: title)
    monkeypatch.setattr(pr_review, "prepare_review", _prepare)
    monkeypatch.setattr(pr_review, "review_agent", lambda repo="": "claude")
    monkeypatch.setattr(pr_review, "record_reviewed", lambda p: None)
    return pr


def _stub_issue(monkeypatch, title="issue-r-9"):
    issue = SimpleNamespace(
        number=9, title="A bug", url="https://github.com/o/r/issues/9", repo="o/r"
    )
    iss = server._issue_start
    story = SimpleNamespace(id=9, repo_url="/nowhere", agent="claude")

    async def _find(repo, number):
        if number != 9:
            raise LookupError("No open issue #%s in %s" % (number, repo))
        return issue

    async def _prepare(i):
        return story, "do the issue", "feature/issue-9"

    monkeypatch.setattr(iss, "find_issue", _find)
    monkeypatch.setattr(iss, "session_title", lambda i: title)
    monkeypatch.setattr(iss, "branch_for", lambda i: "feature/issue-9")
    monkeypatch.setattr(iss, "workspace_mode", lambda: "worktree")
    monkeypatch.setattr(iss, "prepare_start", _prepare)
    monkeypatch.setattr(iss, "record_handled", lambda *a, **k: None)
    return issue


# --------------------------------------------------------------------------- #
# Resolution order
# --------------------------------------------------------------------------- #
def test_resolution_order():
    mine = _plan([{"kind": "agent"}, {"kind": "notify"}])
    r = server._intake_plan
    # 1. the start's explicit plan
    assert r("ticket", mine, "pr", "builtin:pr", "commit") == mine
    # 2. a per-start depth (a row's pick, a run's "off", an agent's autopilot=)
    assert r("ticket", "", "pr", mine, "commit") == "builtin:ticket@pr"
    assert r("ticket", "", "off", mine, "commit") == ""
    assert r("ticket", "", "merge", mine, "") == "builtin:ticket"
    # 3. the source's plan setting, while that plan exists
    assert r("ticket", "", "", mine, "commit") == mine
    assert r("ticket", "", "", "p_gone00", "commit") == "builtin:ticket@commit"
    # 4. the built-in cut at the source's depth; off / agent = no flight
    assert r("pr", "", "", "", "commit") == "builtin:pr@commit"
    assert r("issue", "", "", "", "push") == "builtin:ticket@push"
    for depth in ("", "off", "agent"):
        assert r("ticket", "", "", "", depth) == ""


def test_an_unreadable_store_is_no_flight(monkeypatch):
    def _boom(*a, **k):
        raise OSError("refusing to touch the real flights store")

    monkeypatch.setattr(F, "plan_for_depth", _boom)
    assert server._intake_plan("ticket", "", "", "", "pr") == ""


def test_the_settings_fields_round_trip():
    src = store.TicketingSource.from_dict(
        {"id": "sc", "provider": "shortcut", "plan": " p_1 "}
    )
    assert src.plan == "p_1" and src.to_dict()["plan"] == "p_1"
    assert "plan" not in store.TicketingSource.from_dict({"id": "sc"}).to_dict()
    gh = store.GithubSettings.from_dict(
        {
            "repo_settings": {"o/r": {"plan": "builtin:pr", "depth": "push"}},
            "issue_repo_settings": {"o/r": {"plan": "p_2"}},
        }
    )
    assert gh.repo_settings["o/r"]["plan"] == "builtin:pr"
    assert gh.issue_repo_settings["o/r"] == {"plan": "p_2"}
    assert gh.to_dict()["issue_repo_settings"] == {"o/r": {"plan": "p_2"}}


def test_the_source_plan_round_trips_through_the_sources_route():
    _settings({})
    body = {
        "sources": [
            {
                "id": "gh-o-r",
                "provider": "github_issues",
                "api_token": "t",
                "repo_url": "https://github.com/o/r",
                "plan": "builtin:ticket",
            }
        ]
    }
    r = client.put("/api/settings/ticketing/sources", json=body)
    assert r.status_code == 200, r.text
    got = client.get("/api/settings/ticketing/sources").json()["sources"]
    assert got[0]["plan"] == "builtin:ticket"
    store.invalidate()
    assert server._source_intake_plan("gh-o-r") == "builtin:ticket"


def test_the_repo_plan_round_trips_through_post_settings():
    _settings({})
    r = client.post(
        "/api/settings",
        json={
            "github": {
                "repo_settings": {"o/r": {"plan": "builtin:pr"}},
                "issue_repo_settings": {"o/r": {"plan": "builtin:ticket"}},
            }
        },
    )
    assert r.status_code == 200, r.text
    store.invalidate()
    gh = store.load_settings().github
    assert gh.repo_settings["o/r"]["plan"] == "builtin:pr"
    assert server._repo_intake_plan("o/r", "prs") == "builtin:pr"
    assert server._repo_intake_plan("o/r", "issues") == "builtin:ticket"


# --------------------------------------------------------------------------- #
# The ticket route
# --------------------------------------------------------------------------- #
def test_an_unknown_plan_is_refused_before_anything_is_claimed(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    r = client.post(
        "/api/tickets/start", json={"source": "sc", "id": "1", "plan": "p_nope00"}
    )
    assert r.status_code == 400 and "unknown plan" in r.json()["error"]
    assert not launch.tasks and not server._pending_has("sc-1")
    r = client.post("/api/tickets/start", json={"source": "sc", "id": "1", "plan": 5})
    assert r.status_code == 400


def test_a_plan_arms_the_flight_before_the_session_exists(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    r = client.post(
        "/api/tickets/start",
        json={"source": "sc", "id": "1", "plan": "builtin:ticket"},
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"started": True, "title": "sc-1"}
    # Armed at the 202, before the launch task has even run.
    assert launch.tasks and "sc-1" not in server.ENGINE.instances
    f = F.for_session("sc-1")
    assert f["plan_id"] == "builtin:ticket" and f["by"] == "intake"
    assert f["subject"] == {
        "kind": "ticket",
        "source": "sc",
        "id": "1",
        "title": "Fix it",
        "url": "https://app.example/story/1",
    }
    first = f["steps"][0]["id"]
    assert f["step_states"][first]["state"] == "running"
    # Its ship steps arm the lanes: the depth is NOT armed beside it.
    assert launch.autopilot == []
    # …and the provisioning row is already in flight.
    rows = {row["title"]: row for row in server._pending_rows()}
    assert rows["sc-1"]["flight"]["id"] == f["id"]
    assert ("flight.changed", "sc-1") in launch.events
    launch.run()


def test_a_plan_without_ship_steps_keeps_the_depth(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    _settings(
        {
            "ticketing": {
                "sources": [{"id": "sc", "provider": "shortcut", "depth": "push"}]
            }
        }
    )
    pid = _plan([{"kind": "agent"}, {"kind": "you", "config": {"text": "Look"}}])
    r = client.post("/api/tickets/start", json={"source": "sc", "id": "1", "plan": pid})
    assert r.status_code == 202
    assert F.for_session("sc-1")["plan_id"] == pid
    assert launch.autopilot == [("sc-1", "push", "tix")]


def test_no_plan_and_no_depth_is_todays_behaviour(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    r = client.post("/api/tickets/start", json={"source": "sc", "id": "1"})
    assert r.status_code == 202
    assert F.for_session("sc-1") is None
    assert launch.autopilot == [("sc-1", "", "tix")]
    launch.run()
    (opts,) = launch.opts
    assert opts.prompt.startswith("do the ticket")


def test_the_source_plan_and_depth_resolve_in_order(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    mine = _plan([{"kind": "agent"}, {"kind": "ship", "config": {"lane": "commit"}}])
    _settings(
        {
            "ticketing": {
                "sources": [
                    {"id": "sc", "provider": "shortcut", "depth": "push", "plan": mine}
                ]
            }
        }
    )
    assert (
        client.post("/api/tickets/start", json={"source": "sc", "id": "1"}).status_code
        == 202
    )
    assert F.for_session("sc-1")["plan_id"] == mine
    launch.run()  # fails → cancelled, frees the title
    # A depth picked on the row outranks the source's plan.
    r = client.post(
        "/api/tickets/start", json={"source": "sc", "id": "1", "depth": "commit"}
    )
    assert r.status_code == 202
    assert F.for_session("sc-1")["plan_id"] == "builtin:ticket@commit"


def test_a_depth_only_source_flies_the_built_in_cut(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    _settings(
        {
            "ticketing": {
                "sources": [{"id": "sc", "provider": "shortcut", "depth": "pr"}]
            }
        }
    )
    assert (
        client.post("/api/tickets/start", json={"source": "sc", "id": "1"}).status_code
        == 202
    )
    f = F.for_session("sc-1")
    assert f["plan_id"] == "builtin:ticket@pr"
    assert [s["config"].get("lane") for s in f["steps"] if s["kind"] == "ship"] == [
        "commit",
        "push",
        "pr",
    ]
    assert launch.autopilot == []


def test_create_failure_cancels_the_flight(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    client.post(
        "/api/tickets/start", json={"source": "sc", "id": "1", "plan": "builtin:ticket"}
    )
    fid = F.for_session("sc-1")["id"]
    launch.run()
    f = F.get(fid)
    assert f["state"] == "cancelled"
    assert (
        "could not be created" in f["reason"] and "provisioning failed" in f["reason"]
    )
    assert ("session.create_failed", "sc-1") in launch.events
    assert ("flight.finished", "sc-1") in launch.events
    # The title is free for the next start (a new flight, not a 409).
    assert (
        client.post(
            "/api/tickets/start",
            json={"source": "sc", "id": "1", "plan": "builtin:ticket"},
        ).status_code
        == 202
    )
    assert F.for_session("sc-1")["id"] != fid


def test_a_stale_flight_on_a_free_title_is_replaced(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    old = F.arm_pending("sc-1", "builtin:ticket", {"kind": "ticket"})
    client.post(
        "/api/tickets/start", json={"source": "sc", "id": "1", "plan": "builtin:pr"}
    )
    assert F.get(old["id"])["state"] == "cancelled"
    assert F.for_session("sc-1")["plan_id"] == "builtin:pr"


def test_the_first_agent_steps_prompt_is_appended(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    pid = _plan(
        [
            {"kind": "agent", "config": {"prompt": "Also add a test for {ticket}."}},
            {"kind": "notify"},
        ]
    )
    client.post("/api/tickets/start", json={"source": "sc", "id": "1", "plan": pid})
    launch.run()
    (opts,) = launch.opts
    assert opts.prompt.startswith("do the ticket\n\nAlso add a test for 1.")


# --------------------------------------------------------------------------- #
# The PR and issue routes (bodies extracted into core helpers)
# --------------------------------------------------------------------------- #
def test_pr_route_responses_are_unchanged(launch, monkeypatch):
    _stub_pr(monkeypatch)
    r = client.post("/api/github/prs/review", json={"repo": "o/r", "number": 7})
    assert r.status_code == 202
    assert r.content == b'{"started":true,"title":"pr-repo-7"}'
    assert F.for_session("pr-repo-7") is None
    assert launch.autopilot == [("pr-repo-7", "", "pr")]
    # A second start while the first is provisioning.
    r = client.post("/api/github/prs/review", json={"repo": "o/r", "number": 7})
    assert r.status_code == 409
    assert r.json() == {
        "error": "session pr-repo-7 already exists — close it to re-review",
        "title": "pr-repo-7",
    }
    launch.run()
    (opts,) = launch.opts
    assert opts.prompt == "review the PR" and opts.program == "claude"
    r = client.post("/api/github/prs/review", json={"repo": "o/r", "number": 8})
    assert r.status_code == 404
    assert r.json() == {"error": "No open PR #8 in o/r"}
    r = client.post("/api/github/prs/review", json={"repo": "nope", "number": 8})
    assert r.status_code == 400 and r.json() == {"error": "repo must be owner/name"}


def test_pr_route_flies_the_repo_plan(launch, monkeypatch):
    _stub_pr(monkeypatch)
    _settings({"github": {"repo_settings": {"o/r": {"plan": "builtin:pr"}}}})
    r = client.post("/api/github/prs/review", json={"repo": "o/r", "number": 7})
    assert r.status_code == 202
    f = F.for_session("pr-repo-7")
    assert f["plan_id"] == "builtin:pr"
    assert f["subject"]["kind"] == "pr" and f["subject"]["id"] == "7"
    assert f["subject"]["source"] == "o/r"
    assert launch.autopilot == []
    launch.run()
    assert F.get(f["id"])["state"] == "cancelled"


def test_issue_route_responses_are_unchanged(launch, monkeypatch):
    _stub_issue(monkeypatch)
    r = client.post("/api/github/issues/start", json={"repo": "o/r", "number": 9})
    assert r.status_code == 202
    assert r.content == b'{"started":true,"title":"issue-r-9"}'
    r = client.post("/api/github/issues/start", json={"repo": "o/r", "number": 9})
    assert r.status_code == 409
    assert r.json() == {
        "error": "session issue-r-9 already exists — close it to re-run",
        "title": "issue-r-9",
    }
    launch.run()
    (opts,) = launch.opts
    assert opts.prompt == "do the issue"
    r = client.post("/api/github/issues/start", json={"repo": "o/r", "number": 1})
    assert r.status_code == 404 and r.json() == {"error": "No open issue #1 in o/r"}


def test_issue_route_takes_a_plan(launch, monkeypatch):
    _stub_issue(monkeypatch)
    r = client.post(
        "/api/github/issues/start",
        json={"repo": "o/r", "number": 9, "plan": "builtin:ticket"},
    )
    assert r.status_code == 202
    f = F.for_session("issue-r-9")
    assert f["subject"]["kind"] == "issue" and f["plan_id"] == "builtin:ticket"


def test_a_routes_own_effort_still_reaches_both_halves(launch, monkeypatch):
    _stub_pr(monkeypatch)
    r = client.post(
        "/api/github/prs/review", json={"repo": "o/r", "number": 7, "effort": "high"}
    )
    assert r.status_code == 202
    launch.run()
    (opts,) = launch.opts
    assert opts.launch_args == server._start_launch_args("claude", "high")


# --------------------------------------------------------------------------- #
# POST /api/flights/start with a subject: the same bodies
# --------------------------------------------------------------------------- #
def test_flights_start_with_a_ticket_subject(launch, monkeypatch):
    _stub_ticket(monkeypatch)
    r = client.post(
        "/api/flights/start",
        json={
            "plan_id": "builtin:ticket",
            "subject": {"kind": "ticket", "source": "sc", "id": "1"},
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["title"] == "sc-1" and body["flight"]["plan_id"] == "builtin:ticket"
    assert body["flight"]["subject"]["title"] == "Fix it"


def test_flights_start_with_pr_and_issue_subjects(launch, monkeypatch):
    _stub_pr(monkeypatch)
    _stub_issue(monkeypatch)
    r = client.post(
        "/api/flights/start",
        json={
            "plan_id": "builtin:pr",
            "subject": {"kind": "pr", "source": "o/r", "id": "#7"},
        },
    )
    assert r.status_code == 202 and r.json()["flight"]["session"] == "pr-repo-7"
    r = client.post(
        "/api/flights/start",
        json={
            "plan_id": "builtin:ticket@push",
            "subject": {"kind": "issue", "source": "o/r", "id": "9"},
        },
    )
    assert r.status_code == 202
    assert r.json()["flight"]["plan_id"] == "builtin:ticket@push"
    # The start's own refusals pass through as they are.
    r = client.post(
        "/api/flights/start",
        json={
            "plan_id": "builtin:pr",
            "subject": {"kind": "pr", "source": "o/r", "id": "7"},
        },
    )
    assert r.status_code == 409 and r.json()["title"] == "pr-repo-7"
    r = client.post(
        "/api/flights/start",
        json={
            "plan_id": "builtin:pr",
            "subject": {"kind": "pr", "source": "o/r", "id": "x"},
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "bad_subject"


# --------------------------------------------------------------------------- #
# The pipeline (a separate process: the store is the hand-off)
# --------------------------------------------------------------------------- #
def _runner(tmp_path, monkeypatch):
    from backend.ticket_ingestion.config import (
        EngineConfig,
        PipelineConfig,
        TicketProviderConfig,
    )
    from backend.ticket_ingestion.session_runner import SessionRunner

    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "ws"))
    cfg = PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut", api_token="t", member_id="m"
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
        log_level="INFO",
        engine=EngineConfig(enabled=True, mode="worktree"),
    )
    runner = SessionRunner(cfg)
    armed: list = []
    monkeypatch.setattr(
        runner,
        "_arm_autopilot",
        lambda title, kind, lookup, item, msg: armed.append((title, lookup)),
    )
    return runner, armed


def _story():
    from datetime import datetime

    from tests._factories import make_ticket

    story = make_ticket(
        id=42,
        name="Fix the flux",
        description="A" * 50,
        acceptance_criteria=["works"],
        owner_ids=["u1"],
        app_url="https://app.shortcut.com/org/story/42",
        created_at=datetime(2026, 1, 1),
    )
    story.source_key = "sc"
    return story


def test_pipeline_arms_the_source_plan(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    runner, armed = _runner(tmp_path, monkeypatch)
    pid = _plan(
        [
            {"kind": "agent", "config": {"prompt": "Then say {subject}."}},
            {"kind": "ship", "config": {"lane": "push"}},
        ]
    )
    # Saved "by the server" after this process cached its settings.
    store.load_settings()
    path = store.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ticketing": {
                    "sources": [{"id": "sc", "provider": "shortcut", "plan": pid}]
                }
            }
        ),
        encoding="utf-8",
    )
    with patch.object(
        runner, "_create_instance", return_value=MagicMock(GetWorktreePath=lambda: None)
    ) as create:
        asyncio.run(runner.run(_story()))
    f = F.for_session("sc-42")
    assert f["plan_id"] == pid and f["by"] == "intake"
    assert f["subject"]["kind"] == "ticket" and f["subject"]["source"] == "sc"
    assert armed == []
    prompt = create.call_args.args[2]
    assert prompt.endswith("\n\nThen say Fix the flux.")


def test_pipeline_depth_only_flies_the_cut_and_no_depth_is_todays(
    tmp_path, monkeypatch
):
    runner, armed = _runner(tmp_path, monkeypatch)
    _settings({"github": {"repo_settings": {"org/repo": {"depth": "commit"}}}})
    tail = runner._arm(
        "pr-repo-7",
        "pr",
        "org/repo",
        "org/repo#7",
        "t",
        subject={"kind": "pr", "source": "org/repo", "id": "7"},
    )
    assert tail == ""
    assert F.for_session("pr-repo-7")["plan_id"] == "builtin:pr@commit"
    assert armed == []
    runner._arm(
        "pr-other-1",
        "pr",
        "org/other",
        "org/other#1",
        "t",
        subject={"kind": "pr", "source": "org/other", "id": "1"},
    )
    assert F.for_session("pr-other-1") is None
    assert armed == [("pr-other-1", "org/other")]


def test_pipeline_issue_reads_the_issue_repo_plan(tmp_path, monkeypatch):
    from backend.ticket_ingestion.session_runner import _issue_repo

    runner, armed = _runner(tmp_path, monkeypatch)
    story = SimpleNamespace(
        provider="github_issues", app_url="https://github.com/o/r/issues/9"
    )
    assert _issue_repo(story) == "o/r"
    assert _issue_repo(SimpleNamespace(provider="shortcut", app_url="x")) == ""
    _settings({"github": {"issue_repo_settings": {"o/r": {"plan": "builtin:ticket"}}}})
    runner._arm(
        "issue-r-9",
        "tix",
        "github_issues",
        "9",
        "A bug",
        subject={"kind": "issue", "source": "o/r", "id": "9"},
    )
    assert F.for_session("issue-r-9")["plan_id"] == "builtin:ticket"


def test_pipeline_create_failure_cancels_and_a_retry_reuses(tmp_path, monkeypatch):
    from unittest.mock import patch

    runner, armed = _runner(tmp_path, monkeypatch)
    _settings(
        {
            "ticketing": {
                "sources": [
                    {"id": "sc", "provider": "shortcut", "plan": "builtin:ticket"}
                ]
            }
        }
    )
    with patch.object(runner, "_create_instance", side_effect=RuntimeError("no tmux")):
        with pytest.raises(RuntimeError):
            asyncio.run(runner.run(_story()))
    (f,) = F.list_flights()
    assert f["state"] == "cancelled" and "no tmux" in f["reason"]
    # A flight left behind by a launch that died mid-way is reused, not doubled
    # (and the depth is not armed beside it).
    left = F.arm_pending("sc-42", "builtin:ticket", {"kind": "ticket"})
    runner._arm(
        "sc-42",
        "tix",
        "sc",
        "42",
        "x",
        subject={"kind": "ticket", "source": "sc", "id": "42"},
    )
    assert F.for_session("sc-42")["id"] == left["id"]
    assert armed == []
