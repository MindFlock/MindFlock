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
