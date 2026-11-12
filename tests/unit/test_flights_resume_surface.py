"""Routes on rails, the surfaces: ``POST /api/flights/check``'s ``resume``
and ``start_at: "resume"``, ``POST /api/flights/{id}/again``, ``mindflock
flight start --at`` / ``flight again``, and MCP ``start_flight(at="resume")``
/ ``control_flight(op="again")``. The logic itself is pinned in
tests/unit/test_flights_resume.py. Temp stores and fakes only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import cli
from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from tests.unit import test_cli_flights as CLI
from tests.unit import test_mcp_flights as M

client = TestClient(server.app)


def _inst(path="/repo", branch="b"):
    return SimpleNamespace(
        Path=path,
        Branch=branch,
        GetWorktreePath=lambda: path,
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: path),
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {"sess": _inst()})
    monkeypatch.setattr(D, "_session_busy", lambda title: "")
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.delattr(D, "preflight", raising=False)
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    return client


ROUTE = [
    {"kind": "agent", "title": "Work it"},
    {"kind": "notify", "title": "Ping", "config": {"text": "hi"}},
    {"kind": "wait", "title": "Breathe", "config": {"minutes": 1}},
    {"kind": "you", "title": "Approve", "config": {"text": "ok?"}},
]


def _stopped(k=2, session="sess"):
    p = F.save_plan({"name": "Chores", "steps": [dict(s) for s in ROUTE]})
    f = F.take_off(p, session=session, repo_root="/repo", branch="b")

    def fn(x):
        for s in x["steps"][:k]:
            x["step_states"][s["id"]]["state"] = "done"
        x["cursor"] = k
        F.op_cancel(x, F.SESSION_REMOVED)

    return p, F.mutate(f["id"], fn)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def test_check_answers_resume_and_start_at_resume_starts_there(api):
    p, old = _stopped()
    r = api.post("/api/flights/check", json={"plan_id": p["id"], "title": "sess"})
    out = r.json()
    assert out["resume"] == {
        "stop": "s3",
        "index": 2,
        "title": "Breathe",
        "from_flight": old["id"],
        "session": "sess",
        "state": "cancelled",
        "reason": "its session was deleted",
        "at": old["finished"],
        "in_progress": False,
    }
    # Without start_at it still starts the normal way.
    assert out["start"]["id"] == "s2"
    out = api.post(
        "/api/flights/check",
        json={"plan_id": p["id"], "title": "sess", "start_at": "resume"},
    ).json()
    assert out["ok"] is True and out["start"]["id"] == "s3"
    assert out["skipped"][1]["reason"] == (
        "route %s got past it (it carries on from there)" % old["id"]
    )
    r = api.post(
        "/api/flights/start",
        json={"plan_id": p["id"], "title": "sess", "start_at": "resume"},
    )
    assert r.status_code == 202
    assert r.json()["flight"]["cursor"] == 2


def test_check_says_null_when_there_is_nothing_to_carry_on(api):
    p = F.save_plan({"name": "Chores", "steps": [dict(s) for s in ROUTE]})
    out = api.post(
        "/api/flights/check",
        json={"plan_id": p["id"], "title": "sess", "start_at": "resume"},
    ).json()
    assert out["resume"] is None and out["ok"] is True
    assert out["start"]["id"] == "s2"


def test_again_route(api):
    p, old = _stopped()
    r = api.post("/api/flights/%s/again" % old["id"], json={})
    assert r.status_code == 202
    body = r.json()
    assert body["from"] == old["id"] and body["how"] == "same"
    assert body["session"] == "sess" and body["flight"]["cursor"] == 2
    # It is now on a route: a second "again" of the old one is refused.
    r = api.post("/api/flights/%s/again" % old["id"], json={"at": "s1"})
    assert r.status_code == 409 and r.json()["error"] == "in_flight"


def test_again_route_refusals(api, monkeypatch):
    p, old = _stopped()
    r = api.post("/api/flights/%s/again" % old["id"], json={"at": "s9"})
    assert r.status_code == 400
    assert r.json() == {
        "error": "unknown_step",
        "detail": "no stop s9 on this route",
        "field": "at",
    }
    assert api.post("/api/flights/f_nope/again", json={}).status_code == 404
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(D, "_ref_ok", lambda folder, ref: False)
    r = api.post("/api/flights/%s/again" % old["id"], json={})
    assert r.status_code == 409 and r.json()["error"] == "session_gone"
    r = api.post("/api/flights/%s/again" % old["id"], json={"title": "nope"})
    assert r.status_code == 404 and r.json()["error"] == "unknown_session"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake(monkeypatch):
    return CLI.FakeServer(monkeypatch)


def test_cli_start_at_resume_and_at_a_stop(fake, capsys):
    fake.gets["/api/flights/plans"] = {
        "plans": [
            {
                "id": "p_1",
                "name": "Chores",
                "session": {"repo_path": "/r"},
                "steps": [
                    {"id": "s1", "title": "Work it"},
                    {"id": "s2", "title": "Ping"},
                ],
            }
        ]
    }
    assert (
        cli.main(["flight", "start", "Chores", "--title", "sc-42", "--at", "resume"])
        == 0
    )
    assert fake.sent("POST")[-1] == (
        "/api/flights/start",
        {"plan_id": "p_1", "title": "sc-42", "start_at": "resume"},
    )
    assert cli.main(["flight", "start", "Chores", "--title", "sc-42", "--at", "2"]) == 0
    assert fake.sent("POST")[-1][1]["start_at"] == "s2"


def test_cli_again_by_session_of_an_ended_route(fake, capsys):
    ended = CLI._flight(state="cancelled", reason="its session was deleted")
    fake.gets["/api/flights?all=1"] = {"flights": [ended]}
    fake.posts["/api/flights/f_1a2b3c/again"] = {
        "flight": CLI._flight(id="f_new", cursor=1, state="waiting_you"),
        "from": "f_1a2b3c",
        "session": "sc-42",
        "how": "same",
    }
    assert cli.main(["flight", "again", "sc-42", "--at", "Merge"]) == 0
    assert fake.sent("POST")[-1] == ("/api/flights/f_1a2b3c/again", {"at": "s3"})
    out = capsys.readouterr().out
    assert out.strip() == (
        "f_new flies f_1a2b3c again — sc-42, at You: approve the merge"
    )


def test_cli_again_says_the_refusal(fake, capsys):
    from backend import client as _client

    fake.gets["/api/flights?all=1"] = {"flights": [CLI._flight(state="cancelled")]}
    fake.posts["/api/flights/f_1a2b3c/again"] = _client.ApiError(
        409,
        "conflict",
        {
            "error": "session_gone",
            "detail": "sc-42 is gone and nothing on this "
            "machine holds its work — fly it again on another session",
        },
    )
    assert cli.main(["flight", "again", "f_1a"]) == 1
    assert "fly it again on another session" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def test_mcp_control_flight_again():
    ended = M._flight(state="cancelled", session="w1", cursor=1)
    box, api, clock = M._box(flights=[ended])
    hit = []

    def again(payload, q):
        hit.append(payload)
        return {
            "flight": M._flight("f_2", session="w1", cursor=1),
            "from": "f_1",
            "session": "w1",
            "how": "same",
        }

    api.top_routes[("POST", "/api/flights/f_1/again")] = again
    out = box.control_flight({"id": "f_1", "op": "again"}, M._ctx(clock))
    assert hit == [{"by": "agent:orch"}]
    assert out["id"] == "f_2" and out["again_of"] == "f_1" and out["how"] == "same"
    box.control_flight({"id": "f_1", "op": "again", "step": "s1"}, M._ctx(clock))
    assert hit[-1] == {"by": "agent:orch", "at": "s1"}


def test_mcp_start_flight_dry_run_reads_the_resume_back():
    box, api, clock = M._box()
    api.top_routes[("GET", "/api/flights/plans")] = {"plans": []}
    seen = []

    def check(payload, q):
        seen.append(payload)
        return {
            "ok": True,
            "steps": [M._step("s1"), M._step("s2", "you", "Approve")],
            "start": {"id": "s2", "title": "Approve"},
            "skipped": [{"id": "s1", "reason": "x"}],
            "problems": [],
            "warnings": [],
            "resume": {
                "stop": "s2",
                "index": 1,
                "title": "Approve",
                "from_flight": "f_9",
                "state": "cancelled",
                "reason": "its session was deleted",
            },
        }

    api.top_routes[("POST", "/api/flights/check")] = check
    out = box.start_flight(
        {
            "steps": [{"kind": "agent"}, {"kind": "you", "title": "Approve"}],
            "at": "resume",
            "dry_run": True,
        },
        M._ctx(clock),
    )
    assert seen[0]["start_at"] == "resume"
    assert out["resume"] == (
        's2 "Approve" — route f_9 stopped there (its session was deleted); '
        "starting there"
    )


def test_check_promises_no_resume_to_a_start_that_makes_its_session(api):
    """Review fix 1: no title = a new session on a fresh branch — the start
    will not carry on, so the check does not say it will."""
    tix = {"kind": "ticket", "source": "sc", "id": "7"}
    p, old = _stopped()
    F.mutate(old["id"], lambda x: x.update(subject=dict(tix)))
    out = api.post(
        "/api/flights/check",
        json={"plan_id": p["id"], "repo_path": "/repo", "start_at": "resume"},
    ).json()
    assert out["resume"] is None and out["start"]["id"] == "s1"
    out = api.post(
        "/api/flights/check", json={"plan_id": p["id"], "subject": tix}
    ).json()
    assert out["resume"] is None
    (warn,) = [w for w in out["warnings"] if w.get("error") == "carries_on_if_found"]
    assert warn["detail"] == (
        "if its branch is still checked out on this machine, the route carries on "
        "there at Breathe (route %s stopped there); otherwise a new session starts "
        "at the first stop" % old["id"]
    )
