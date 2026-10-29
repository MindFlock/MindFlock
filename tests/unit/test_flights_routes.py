"""Flights HTTP API (``backend/web/routes/flights.py``) against the contract
table in docs/flights.md — a TestClient over the real app with every store
redirected to tmp (no lifespan: the loops never start)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_describe as _describe
from backend.web.core import flights as F
from backend.web.core import outbox

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    # A copy of the registry, never the live state.json's dict.
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {
            "sess": SimpleNamespace(
                Path="/repo",
                Branch="b",
                GetWorktreePath=lambda: "/repo",
                GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: "/repo"),
            )
        },
    )
    yield


def _plan(**kw):
    body = {
        "name": "Mine",
        "steps": [{"kind": "agent"}, {"kind": "you", "config": {"text": "Approve"}}],
    }
    body.update(kw)
    return client.post("/api/flights/plans", json=body)


def test_kinds():
    r = client.get("/api/flights/kinds")
    assert r.status_code == 200
    kinds = {k["kind"]: k for k in r.json()["kinds"]}
    assert set(kinds) == set(F.STEP_KINDS)
    assert kinds["you"]["actor"] == "you" and kinds["ship"]["actor"] == "robot"
    lane = next(f for f in kinds["ship"]["fields"] if f["name"] == "lane")
    assert lane == {
        "name": "lane",
        "type": "select",
        "label": "How far",
        "options": ["commit", "push", "pr", "merge"],
        "default": "push",
    }


def test_plans_crud():
    plans = client.get("/api/flights/plans").json()["plans"]
    assert [p["id"] for p in plans] == list(F.BUILTIN_IDS)
    r = _plan()
    assert r.status_code == 200
    p = r.json()["plan"]
    assert p["id"].startswith("p_") and [s["id"] for s in p["steps"]] == ["s1", "s2"]
    r = client.post("/api/flights/plans", json=dict(p, name="Renamed"))
    assert r.json()["plan"]["name"] == "Renamed"
    bad = _plan(steps=[{"kind": "ship", "config": {"lane": "moon"}}])
    assert bad.status_code == 400
    assert bad.json() == {
        "error": "a ship step goes to commit, push, pr or merge",
        "field": "steps[0].config.lane",
    }
    assert (
        client.post("/api/flights/plans", json=dict(p, id="p_none")).status_code == 404
    )
    assert client.delete("/api/flights/plans/" + p["id"]).status_code == 204
    assert client.delete("/api/flights/plans/" + p["id"]).status_code == 404
    # Built-ins: edit, delete = reset, reset.
    t = F.get_plan("builtin:pr")
    client.post("/api/flights/plans", json=dict(t, name="PR flow"))
    assert client.delete("/api/flights/plans/builtin:pr").status_code == 204
    assert F.get_plan("builtin:pr")["name"] == "Pull request"
    r = client.post("/api/flights/plans/builtin:pr/reset")
    assert r.status_code == 200 and r.json()["plan"]["name"] == "Pull request"
    assert client.post("/api/flights/plans/p_x/reset").status_code == 404


def test_start_attach_and_the_flight_lifecycle():
    p = _plan().json()["plan"]
    r = client.post("/api/flights/start", json={"plan_id": p["id"], "title": "sess"})
    assert r.status_code == 202
    f = r.json()["flight"]
    assert f["cursor"] == 1 and f["state"] == "running" and f["session"] == "sess"
    assert (
        client.post(
            "/api/flights/start", json={"plan_id": p["id"], "title": "sess"}
        ).json()["error"]
        == "in_flight"
    )
    assert client.get("/api/flights/" + f["id"]).json()["flight"]["id"] == f["id"]
    assert client.get("/api/flights/f_nope").status_code == 404
    # The session row's flight field.
    assert server._flight_dto("sess")["step"]["kind"] == "you"
    # Park on the you step (what the driver does), then the bell row + Done.
    F.mutate(f["id"], lambda x: F.apply(x, F.next_action(x, {}, 1.0), 1.0))
    ob = outbox.build([], [], {}, now=2.0, today_start=0.0, flights=F.list_flights())
    w = ob["groups"]["waiting"]
    assert (
        w
        and w[0]["kind"] == "flight_you"
        and w[0]["actions"] == ["done", "skip", "open"]
    )
    assert w[0]["flight"]["id"] == f["id"] and w[0]["reason"] == "Approve"
    assert client.post("/api/flights/%s/steps/s1/done" % f["id"]).status_code == 409
    r = client.post("/api/flights/%s/steps/s2/done" % f["id"], json={"note": "ok"})
    assert r.status_code == 200 and r.json()["flight"]["state"] == "done"
    assert client.delete("/api/flights/" + f["id"]).status_code == 204
    assert client.delete("/api/flights/" + f["id"]).status_code == 404


def test_operations():
    p = _plan(steps=[{"kind": "notify"}, {"kind": "checks"}, {"kind": "wait"}]).json()[
        "plan"
    ]
    f = client.post(
        "/api/flights/start",
        json={"plan_id": p["id"], "title": "sess", "start_at": "s1"},
    ).json()["flight"]
    fid = f["id"]
    assert (
        client.post("/api/flights/%s/steps/s1/skip" % fid).json()["flight"]["cursor"]
        == 1
    )
    assert (
        client.post("/api/flights/%s/jump" % fid, json={"step": "s3"}).json()["flight"][
            "cursor"
        ]
        == 2
    )
    assert client.post("/api/flights/%s/jump" % fid, json={}).status_code == 400
    assert (
        client.post("/api/flights/%s/jump" % fid, json={"step": "zz"}).status_code
        == 404
    )
    assert (
        client.post("/api/flights/%s/pause" % fid).json()["flight"]["state"] == "paused"
    )
    assert client.delete("/api/flights/" + fid).json()["error"] == "in_flight"
    assert client.post("/api/flights/%s/cancel" % fid).status_code == 200
    assert client.delete("/api/flights/" + fid).status_code == 204
    f = client.post("/api/flights/start", json={"plan_id": p["id"], "title": "sess"})
    fid = f.json()["flight"]["id"]
    assert (
        client.post("/api/flights/%s/pause" % fid).json()["flight"]["state"] == "paused"
    )
    assert server._flight_dto("sess")["state"] == "paused"
    assert (
        client.post("/api/flights/%s/resume" % fid).json()["flight"]["state"]
        == "running"
    )
    assert (
        client.post("/api/flights/%s/steps/s2/retry" % fid).json()["error"]
        == "not_failed"
    )
    assert (
        client.post("/api/flights/%s/cancel" % fid).json()["flight"]["state"]
        == "cancelled"
    )
    assert client.post("/api/flights/%s/cancel" % fid).status_code == 409


def test_start_refusals():
    assert (
        client.post("/api/flights/start", json={"plan_id": "nope"}).status_code == 404
    )
    # A ticket / PR / issue subject goes through Intake (test_flights_intake.py
    # drives it end to end); what it refuses before reaching Intake:
    r = client.post(
        "/api/flights/start",
        json={"plan_id": "nope", "subject": {"kind": "pr", "source": "o/r", "id": "1"}},
    )
    assert r.status_code == 404 and r.json()["error"] == "unknown_plan"
    r = client.post(
        "/api/flights/start",
        json={"plan_id": "builtin:pr", "subject": {"kind": "pr", "source": "o/r"}},
    )
    assert r.status_code == 400 and r.json()["error"] == "bad_subject"
    r = client.post(
        "/api/flights/start", json={"plan_id": "builtin:ticket", "title": "ghost"}
    )
    assert r.status_code == 404
    r = client.post("/api/flights/start", json={"plan_id": "builtin:verify"})
    assert r.status_code == 400 and r.json()["error"] == "no_repo"


def test_list_default_and_all():
    p = _plan(steps=[{"kind": "notify"}]).json()["plan"]
    f = client.post(
        "/api/flights/start", json={"plan_id": p["id"], "title": "sess"}
    ).json()["flight"]
    assert [x["id"] for x in client.get("/api/flights").json()["flights"]] == [f["id"]]
    assert len(client.get("/api/flights?all=1").json()["flights"]) == 1


def test_describe_route(monkeypatch):
    monkeypatch.setattr(
        _describe,
        "describe",
        lambda text, want: {"steps": [{"kind": "wait"}], "model": "claude"},
    )
    r = client.post("/api/flights/describe", json={"text": "wait", "want": "step"})
    assert r.status_code == 200 and r.json()["model"] == "claude"

    def no_model(text, want):
        raise _describe.DescribeError("no_model", 409, "no CLI")

    monkeypatch.setattr(_describe, "describe", no_model)
    r = client.post("/api/flights/describe", json={"text": "wait"})
    assert r.status_code == 409 and r.json()["error"] == "no_model"

    def boom(text, want):
        raise RuntimeError("kaput")

    monkeypatch.setattr(_describe, "describe", boom)
    r = client.post("/api/flights/describe", json={"text": "wait"})
    assert r.status_code == 422 and r.json()["error"] == "could_not_describe"


def test_static_paths_are_not_read_as_ids():
    paths = server.app.openapi()["paths"]
    for path in (
        "/api/flights/kinds",
        "/api/flights/plans",
        "/api/flights/start",
        "/api/flights/describe",
        "/api/flights/{flight_id}/steps/{step_id}/done",
        "/api/flights/{flight_id}/jump",
    ):
        assert path in paths, path


def test_flight_events_have_notify_rules():
    from backend.web.addons import notify
    from backend.web.core import events

    for name in ("flight.changed", "flight.needs_you", "flight.finished"):
        assert name in events.EVENT_NAMES
    rules = {r["id"]: r for r in notify.NOTIFY_RULES}
    assert rules["flight_needs_you"]["event"] == "flight.needs_you"
    assert rules["flight_finished"]["event"] == "flight.finished"


def test_flight_plans_store_is_synced():
    from backend.web.core import settings_sync, sync_stores

    assert "flight_plans" in settings_sync._stores()
    _plan(name="Synced")
    entries = sync_stores.flight_plans_list()
    assert set(F.BUILTIN_IDS) <= set(entries)
    pid = next(k for k, v in entries.items() if v["name"] == "Synced")
    assert "updated" not in sync_stores.flight_plans_canon(entries[pid])
    sync_stores.flight_plans_write("p_from_mac", dict(entries[pid], id="p_from_mac"))
    assert F.get_plan("p_from_mac")["name"] == "Synced"
    sync_stores.flight_plans_delete("p_from_mac")
    assert F.get_plan("p_from_mac") is None


def test_autopilot_accepts_the_flight_source():
    from backend.web.core import autopilot

    assert "flight" in autopilot.SOURCES
    assert autopilot._normalize({"source": "flight"})["source"] == "flight"
