"""PR review goes back to the session that BUILT the branch (the ticket's own
session, live or reopened from Recently closed) — docs/pr-review-rounds.md
"The same session"."""

import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from backend.web import server


class _Inst:
    def __init__(self, branch):
        self.Branch = branch


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    queued = []
    monkeypatch.setattr(
        server._prompt_queue, "enqueue", lambda t, x, **k: queued.append((t, x))
    )
    monkeypatch.setattr(server._prompt_queue, "set_flags", lambda *a, **k: None)
    c = TestClient(server.app)
    c.queued = queued
    return c


def test_a_live_ticket_session_gets_it_before_a_pr_session(client, monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {
            "pr-o-r-7": _Inst("feature/sc-1/x"),
            "sc-1": _Inst("feature/sc-1/x"),
            "verify-sc-1-abc": _Inst("feature/sc-1/x"),
            "other": _Inst("main"),
        },
    )
    r = client.post(
        "/api/sessions/by-branch/hand-off",
        json={"branch": "feature/sc-1/x", "text": "review it"},
    )
    assert r.status_code == 200 and r.json() == {"title": "sc-1", "how": "queued"}
    assert client.queued == [("sc-1", "review it")]


def test_a_closed_ticket_session_is_reopened_then_given_the_text(client, monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "a",
                "title": "sc-1",
                "branch": "feature/sc-1/x",
                "closed_at": "2026-11-08T10:00:00",
            },
            {
                "id": "b",
                "title": "sc-1",
                "branch": "feature/sc-1/x",
                "closed_at": "2026-11-09T10:00:00",
            },
            {
                "id": "c",
                "title": "pr-o-r-7",
                "branch": "feature/sc-1/x",
                "closed_at": "2026-11-09T12:00:00",
            },
        ],
    )
    opened = []

    async def reopen(entry_id):
        opened.append(entry_id)
        return server.JSONResponse({"title": "sc-1"})

    monkeypatch.setattr(server, "_reopen_closed_entry", reopen)
    r = client.post(
        "/api/sessions/by-branch/hand-off",
        json={"branch": "feature/sc-1/x", "text": "review it"},
    )
    assert r.json() == {"title": "sc-1", "how": "reopened"}
    assert opened == ["b"]  # the ticket's own, newest
    assert client.queued == [("sc-1", "review it")]


def test_nobody_built_it(client, monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    r = client.post(
        "/api/sessions/by-branch/hand-off", json={"branch": "feature/x", "text": "t"}
    )
    assert r.status_code == 404
    assert (
        client.post(
            "/api/sessions/by-branch/hand-off", json={"branch": "", "text": "t"}
        ).status_code
        == 400
    )


def test_a_review_round_goes_to_the_builder_first(monkeypatch):
    from backend.ticket_ingestion import session_runner as SR

    runner = SR.SessionRunner.__new__(SR.SessionRunner)
    pr = types.SimpleNamespace(number=7, head_ref="feature/sc-1/x", repo="o/r")

    async def handed(p, prompt):
        return "sc-1"

    monkeypatch.setattr(runner, "hand_to_builder", handed)
    assert asyncio.run(runner.deliver_pr_round(pr, "round")) == "handed"

    async def nobody(p, prompt):
        return ""

    queued = []
    monkeypatch.setattr(runner, "hand_to_builder", nobody)
    monkeypatch.setattr(runner, "_stored_titles", lambda: {"pr-o-r-7"})
    monkeypatch.setattr(SR, "pr_slug", lambda p: "o-r-7")
    import backend.web.core.prompt_queue as PQ

    monkeypatch.setattr(PQ, "enqueue", lambda t, x, **k: queued.append(t))
    monkeypatch.setattr(PQ, "set_flags", lambda *a, **k: None)
    assert asyncio.run(runner.deliver_pr_round(pr, "round")) == "queued"
    assert queued == ["pr-o-r-7"]


def test_hand_to_builder_needs_the_server(monkeypatch):
    from backend.ticket_ingestion import session_runner as SR

    runner = SR.SessionRunner.__new__(SR.SessionRunner)
    monkeypatch.delenv("MINDFLOCK_SERVER_PORT", raising=False)
    pr = types.SimpleNamespace(number=7, head_ref="feature/sc-1/x", repo="o/r")
    assert asyncio.run(runner.hand_to_builder(pr, "x")) == ""


# --------------------------------------------------------------------------- #
# A route's reviews stop already loops on the PR's comments
# (docs/pr-review-rounds.md "Routes"): the hand-off leaves them to it.
# --------------------------------------------------------------------------- #
def _flight(review_state, state="running", moved_on=False):
    return {
        "session": "sc-1",
        "state": state,
        "steps": [{"id": "s1", "kind": "agent"}, {"id": "s2", "kind": "reviews"}],
        "step_states": {
            "s1": {"state": "done", "detail": {}},
            "s2": {"state": review_state, "detail": {"moved_on": moved_on}},
        },
    }


def test_reviews_pending():
    from backend.web.core import flights as F

    assert F.reviews_pending(_flight("pending"))
    assert F.reviews_pending(_flight("running"))
    assert F.reviews_pending(_flight("waiting_you", state="waiting_you"))
    assert F.reviews_pending(_flight("failed", state="halted"))
    assert not F.reviews_pending(_flight("done"))
    assert not F.reviews_pending(_flight("skipped"))
    assert not F.reviews_pending(_flight("failed", moved_on=True))
    assert not F.reviews_pending(_flight("running", state="done"))
    assert not F.reviews_pending(_flight("running", state="cancelled"))
    no_reviews = _flight("pending")
    no_reviews["steps"] = no_reviews["steps"][:1]
    assert not F.reviews_pending(no_reviews)
    assert not F.reviews_pending(None)


def _branch_sessions(monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"pr-o-r-7": _Inst("feature/sc-1/x"), "sc-1": _Inst("feature/sc-1/x")},
    )


def test_a_flying_reviews_stop_gets_nothing_queued(client, monkeypatch):
    _branch_sessions(monkeypatch)
    monkeypatch.setattr(
        server._flights,
        "for_session",
        lambda t, unfinished_only=True: _flight("running") if t == "sc-1" else None,
    )
    r = client.post(
        "/api/sessions/by-branch/hand-off",
        json={"branch": "feature/sc-1/x", "text": "review it"},
    )
    assert r.status_code == 200 and r.json() == {"title": "sc-1", "how": "flight"}
    assert client.queued == []


@pytest.mark.parametrize(
    "flight",
    [
        None,
        _flight("done", state="done"),
        _flight("running", state="cancelled"),
        _flight("done"),
    ],
)
def test_finished_or_past_the_stop_is_handed_as_before(client, monkeypatch, flight):
    _branch_sessions(monkeypatch)
    monkeypatch.setattr(
        server._flights, "for_session", lambda t, unfinished_only=True: flight
    )
    r = client.post(
        "/api/sessions/by-branch/hand-off",
        json={"branch": "feature/sc-1/x", "text": "review it"},
    )
    assert r.json() == {"title": "sc-1", "how": "queued"}
    assert client.queued == [("sc-1", "review it")]


def test_a_real_flight_owns_its_reviews_until_it_passes_the_stop(
    client, monkeypatch, tmp_path
):
    from backend.web.core import flights as F

    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    _branch_sessions(monkeypatch)
    plan = F.save_plan(
        {
            "name": "P",
            "steps": [
                {"kind": "agent", "config": {"prompt": "x"}},
                {"kind": "reviews", "config": {}},
            ],
        },
        now=1_800_000_000.0,
    )
    f = F.take_off(plan, session="sc-1", now=1_800_000_000.0)
    post = lambda: client.post(  # noqa: E731
        "/api/sessions/by-branch/hand-off",
        json={"branch": "feature/sc-1/x", "text": "review it"},
    ).json()
    assert post()["how"] == "flight" and client.queued == []

    def _pass_reviews(fl):
        for s in fl["steps"]:
            fl["step_states"][s["id"]]["state"] = "done"

    F.mutate(f["id"], _pass_reviews)
    assert post() == {"title": "sc-1", "how": "queued"}
    assert client.queued == [("sc-1", "review it")]


def test_the_pipeline_reads_flight_and_tolerates_an_older_server(monkeypatch):
    from backend.ticket_ingestion import session_runner as SR

    from datetime import datetime

    from backend.ticket_ingestion.models import PullRequest

    runner = SR.SessionRunner.__new__(SR.SessionRunner)
    pr = PullRequest(
        7,
        "feature/sc-1/x",
        "abc",
        "main",
        "T",
        "https://pr/7",
        "me",
        datetime(2026, 1, 1),
        repo="o/r",
    )

    async def flight(p, prompt):
        return SR.Handed("sc-1", "flight")

    monkeypatch.setattr(runner, "hand_to_builder", flight)
    assert asyncio.run(runner.deliver_pr_round(pr, "round")) == "flight"
    # The first pass: nothing launched, the PR counts as handled.
    assert asyncio.run(runner.run_pr(pr, [])) == "feature/sc-1/x"
    # An older server answers without "how": a plain hand-off.
    assert SR.handed_how("sc-1") == "queued"
    assert SR.handed_how(SR.Handed("sc-1", "reopened")) == "reopened"


def test_hand_to_builder_carries_the_servers_how(monkeypatch):
    import io
    import urllib.request

    from backend.ticket_ingestion import session_runner as SR

    runner = SR.SessionRunner.__new__(SR.SessionRunner)
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "1")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "t")
    pr = types.SimpleNamespace(number=7, head_ref="feature/sc-1/x", repo="o/r")
    answers = iter(
        [
            {"title": "sc-1", "how": "flight"},
            {"title": "sc-1"},  # an older server
            {"error": "nobody"},
        ]
    )

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=0: _Resp(json.dumps(next(answers)).encode()),
    )
    got = asyncio.run(runner.hand_to_builder(pr, "x"))
    assert got == "sc-1" and SR.handed_how(got) == "flight"
    got = asyncio.run(runner.hand_to_builder(pr, "x"))
    assert got == "sc-1" and SR.handed_how(got) == "queued"
    assert asyncio.run(runner.hand_to_builder(pr, "x")) == ""
