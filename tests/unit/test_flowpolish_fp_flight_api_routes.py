"""Flow polish (fp-flight-api): the flights routes say the truth about a start
and point at the field they refuse.

* ``POST /api/flights/check`` — what a start with the same body would fly,
  from the same strict path, creating nothing; ``ok`` means the start is
  accepted.
* ``flight_driver.preflight`` (another worker's; absent = no problems) runs
  before a real start and its first problem is the refusal.
* Every refusal carries ``field``; a grounded route's per-stop PATCH refuses
  a bad option when it is saved, not at take-off.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
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
    monkeypatch.setattr(D, "_session_busy", lambda title: "")
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    # No preflight unless a test installs one (the other worker's contract).
    monkeypatch.delattr(D, "preflight", raising=False)
    yield


def _check(**body):
    r = client.post("/api/flights/check", json=body)
    assert r.status_code == 200
    return r.json()


def test_check_normalises_strictly_and_creates_nothing():
    out = _check(
        title="sess",
        steps=[
            {"kind": "agent", "id": "build", "config": {"prompt": "go"}},
            {"kind": "checks", "config": {"timeout_minutes": 20}},
        ],
    )
    assert out["ok"] is True and out["problems"] == []
    assert [s["id"] for s in out["steps"]] == ["s1", "s2"]
    assert out["steps"][1]["config"] == {"timeout_minutes": 20}
    # On an existing session the leading agent stop is the work so far.
    assert out["start"] == {
        "index": 1,
        "id": "s2",
        "kind": "checks",
        "title": "CI checks",
    }
    (skip,) = out["skipped"]
    assert skip["id"] == "s1" and "its prompt is not sent" in skip["reason"]
    assert out["in_flight"] is None
    assert F.list_flights() == []


def test_check_with_start_at_skips_nothing_or_names_why():
    out = _check(
        title="sess",
        start_at="s1",
        steps=[{"kind": "agent", "config": {"prompt": "go"}}, {"kind": "notify"}],
    )
    assert out["ok"] and out["start"]["id"] == "s1" and out["skipped"] == []
    out = _check(title="sess", start_at="s9", steps=[{"kind": "notify"}])
    assert not out["ok"]
    assert out["problems"][0]["field"] == "start_at"
    assert out["problems"][0]["error"] == "unknown_step"


def test_check_reports_the_strict_refusal_with_its_field():
    out = _check(title="sess", steps=[{"kind": "ship", "config": {"lane": "moon"}}])
    assert out["ok"] is False
    (p,) = out["problems"]
    assert p["field"] == "steps[0].config.lane" and "commit, push, pr" in p["detail"]
    # …exactly what the real start answers.
    r = client.post(
        "/api/flights/start",
        json={"title": "sess", "steps": [{"kind": "ship", "config": {"lane": "moon"}}]},
    )
    assert r.status_code == 400 and r.json()["field"] == "steps[0].config.lane"


def test_check_says_the_session_is_already_on_a_route():
    r = client.post(
        "/api/flights/start",
        json={"title": "sess", "steps": [{"kind": "you", "config": {"text": "ok"}}]},
    )
    assert r.status_code == 202
    fid = r.json()["flight"]["id"]
    out = _check(title="sess", steps=[{"kind": "notify"}])
    assert out["ok"] is False
    assert out["in_flight"]["id"] == fid
    p = out["problems"][0]
    assert (
        p["error"] == "in_flight" and p["field"] == "title" and p["session"] == "sess"
    )
    # The real start: the same refusal, with its field.
    r = client.post(
        "/api/flights/start", json={"title": "sess", "steps": [{"kind": "notify"}]}
    )
    assert r.status_code == 409 and r.json()["field"] == "title"


def test_check_unknown_session_plan_and_folder():
    out = _check(title="nope", steps=[{"kind": "notify"}])
    assert [p["error"] for p in out["problems"]] == ["unknown_session"]
    out = _check(plan_id="p_missing")
    assert out["problems"][0] == {
        "field": "plan_id",
        "detail": "no such saved route",
        "error": "unknown_plan",
    }
    out = _check(steps=[{"kind": "notify"}])
    assert out["problems"][0]["error"] == "needs_title"


def test_check_grounded_warns_about_what_must_be_filled_in():
    out = _check(grounded=True, steps=[{"kind": "split"}, {"kind": "join"}])
    assert out["ok"] is True
    assert [s["id"] for s in out["steps"]] == ["s1", "s2"]
    (w,) = out["warnings"]
    assert w["field"] == "steps[0].config.prompts" and w["detail"].startswith(
        "before it can fly"
    )


def test_check_and_start_run_the_drivers_preflight(monkeypatch):
    seen = []

    def preflight(plan, *, repo, session=None, attach=False, start_at=None):
        seen.append((len(plan["steps"]), repo, session, attach, start_at))
        return [
            {
                "field": "steps[0].config.where",
                "detail": "this repo has no environment 'stage' — it has: live",
                "session": "sess",
            }
        ]

    monkeypatch.setattr(D, "preflight", preflight, raising=False)
    steps = [{"kind": "verify", "config": {"where": "stage"}}]
    out = _check(title="sess", steps=steps)
    assert out["ok"] is False
    assert out["problems"] == [
        {
            "field": "steps[0].config.where",
            "detail": "this repo has no environment 'stage' — it has: live",
            "session": "sess",
        }
    ]
    assert "live" in out["environments"]
    r = client.post("/api/flights/start", json={"title": "sess", "steps": steps})
    assert r.status_code == 409
    body = r.json()
    assert body["field"] == "steps[0].config.where" and body["session"] == "sess"
    assert "no environment 'stage'" in body["detail"]
    assert seen[0] == (1, "/repo", "sess", True, None)
    assert F.list_flights() == []


def test_a_broken_preflight_never_blocks(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(D, "preflight", boom, raising=False)
    assert _check(title="sess", steps=[{"kind": "notify"}])["ok"] is True
    r = client.post(
        "/api/flights/start", json={"title": "sess", "steps": [{"kind": "notify"}]}
    )
    assert r.status_code == 202


def test_refusals_carry_a_field():
    r = client.post("/api/flights/start", json={"plan_id": "p_none"})
    assert r.status_code == 404 and r.json()["field"] == "plan_id"
    r = client.post(
        "/api/flights/start",
        json={"title": "sess", "start_at": "s7", "steps": [{"kind": "notify"}]},
    )
    assert r.status_code == 400 and r.json()["field"] == "start_at"
    r = client.post(
        "/api/flights/start",
        json={"start_on": "later", "title": "sess", "steps": [{"kind": "notify"}]},
    )
    assert r.json()["field"] == "start_on"


def _grounded(steps):
    r = client.post("/api/flights/start", json={"grounded": True, "steps": steps})
    assert r.status_code == 202
    return r.json()["flight"]["id"]


def test_grounded_patch_refuses_a_bad_value_when_saved():
    fid = _grounded([{"kind": "ship", "config": {"lane": "pr"}}, {"kind": "split"}])
    r = client.patch(
        "/api/flights/%s/steps/s1" % fid, json={"config": {"lane": "moon"}}
    )
    assert r.status_code == 400
    assert r.json()["field"] == "step.config.lane"
    assert F.get(fid)["steps"][0]["config"]["lane"] == "pr"
    # A block still to be filled in may wait for take-off …
    r = client.patch(
        "/api/flights/%s/steps/s2" % fid, json={"config": {"prompts": "", "count": 2}}
    )
    assert r.status_code == 200
    # … and a good value saves.
    r = client.patch(
        "/api/flights/%s/steps/s1" % fid, json={"config": {"lane": "merge"}}
    )
    assert r.status_code == 200
    assert F.get(fid)["steps"][0]["config"]["lane"] == "merge"


def test_running_route_patch_is_strict_with_field():
    r = client.post(
        "/api/flights/start",
        json={
            "title": "sess",
            "start_at": "s1",
            "steps": [
                {"kind": "you", "config": {"text": "ok"}},
                {"kind": "checks"},
            ],
        },
    )
    fid = r.json()["flight"]["id"]
    r = client.patch("/api/flights/%s/steps/s2" % fid, json={"on_fail": "sometimes"})
    assert r.status_code == 400 and r.json()["field"] == "step.on_fail"


def test_done_route_doc_says_done_never_passes_open_verify_steps():
    from backend.web.routes import flights as routes

    doc = routes.flights_step_done.__doc__
    assert "never passes a Verify stop's open" in doc
    assert "recorded as passed" not in doc
