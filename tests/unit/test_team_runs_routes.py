"""The ``/api/runs*`` and ``/api/outbox`` routes: exact shapes (the frontend is
built against them — docs/web-api.md, SPEC §5) and status codes.

Sessions are faked exactly as in test_team_run_driver (whose ``env`` fixture
this reuses): every test owns ``ENGINE.instances`` and nothing is launched.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr
from tests.unit.test_team_run_driver import _Inst, env  # noqa: F401 — fixture

client = TestClient(server.app)


@pytest.fixture
def no_sources(monkeypatch):
    monkeypatch.setattr(drv, "_configured_sources", lambda: [])


def _create(env, n=2, **kw):
    body = {
        "name": "Q4 payments",
        "items": [{"kind": "task", "text": "line %d" % i} for i in range(1, n + 1)],
        "policy": {
            "lane": "pr",
            "ask_first": False,
            "grouping": "each",
            "release": "ask",
        },
        "concurrency": 3,
        "program": "claude",
        "repo_path": env.repo,
        "budget_usd": 20,
        "split": False,
    }
    body.update(kw)
    return client.post("/api/runs", json=body)


class TestPreview:
    def test_shape(self, env, no_sources):
        env.listing.append(
            {
                "source": "jira-payments",
                "id": "PAY-412",
                "slug": "jira-PAY-412",
                "session": "jira-PAY-412",
                "name": "Retry refund webhooks with backoff",
                "repo_url": "git@github.com:quickpay/quickpay.git",
                "branch": "feature/jira-PAY-412/retry",
            }
        )
        env.instances["jira-PAY-412"] = _Inst("jira-PAY-412")
        r = client.post(
            "/api/runs/preview",
            json={
                "text": "PAY-412 PAY-499\nPer-user rate limit on /webhooks",
                "repo_path": env.repo,
                "program": "claude",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"items", "name_suggestion", "lane_default", "warnings"}
        t412, t499, task = body["items"]
        assert t412 == {
            "kind": "ticket",
            "source": "jira-payments",
            "id": "PAY-412",
            "ref": "PAY-412",
            "title": "Retry refund webhooks with backoff",
            "repo": "quickpay",
            "title_hint": "jira-PAY-412",
            "has_session": True,
            "error": None,
        }
        assert t499["kind"] == "ticket" and t499["error"] == "not found in any source"
        assert task == {
            "kind": "task",
            "text": "Per-user rate limit on /webhooks",
            "repo": "repo",
            "title_hint": "per-user-rate-limit-webhooks",
        }
        assert body["lane_default"] in ("leave", "commit", "push", "pr", "merge")
        assert body["warnings"] == [
            "PAY-412 already has a session (jira-PAY-412) — it will be added to "
            "the group, not restarted"
        ]

    def test_text_must_be_a_string(self, env):
        assert client.post("/api/runs/preview", json={"text": 3}).status_code == 400


class TestCreateAndRead:
    def test_201_with_the_run_and_warnings(self, env):
        r = _create(env)
        assert r.status_code == 201
        body = r.json()
        assert set(body) == {"run", "warnings"}
        run = body["run"]
        for key in (
            "id",
            "name",
            "created_at",
            "created_by",
            "state",
            "paused",
            "pause_reason",
            "repo_root",
            "program",
            "policy",
            "concurrency",
            "budget_usd",
            "lead",
            "plan",
            "tasks",
            "release",
            "events",
            "summary",
            "counts",
        ):
            assert key in run, key
        assert "v" not in run
        assert run["created_by"] == "user" and run["budget_usd"] == 20.0
        assert run["release"]["state"] == "none" and run["release"]["pr_url"] == ""
        assert run["check"]["state"] == "pending"
        assert run["split"] is False and run["lead"] is None and run["plan"] is None
        assert run["tasks"][0]["row_present"] is False
        assert run["counts"]["queued"] == 2

    @pytest.mark.parametrize(
        "setting, grouping, want",
        [
            # Out of the box (unset reads as Off): nothing is fast-tracked
            # unless asked — and one-for-all, which can't be Off, commits.
            ("off", "each", "leave"),
            ("off", "together", "commit"),
            # An explicit stored value keeps working exactly as before.
            ("pr", "each", "pr"),
            ("pr", "together", "pr"),
            ("merge", "each", "merge"),
        ],
    )
    def test_no_lane_given_takes_the_settings_default_off_when_unset(
        self, env, monkeypatch, setting, grouping, want
    ):
        """The MCP's start_team_run sends no lane unless the agent picks one:
        the run takes THE default (Settings → Workspace), Off when unset."""
        monkeypatch.setattr(server, "_fasttrack_default", lambda: setting)
        r = _create(env, policy={"grouping": grouping, "release": "ask"})
        assert r.status_code == 201, r.text
        assert r.json()["run"]["policy"]["lane"] == want

    def test_an_explicit_leave_is_still_refused_for_one_for_all(self, env):
        r = _create(env, policy={"lane": "leave", "grouping": "together"})
        assert r.status_code == 400 and "one PR for all" in r.json()["error"]

    def test_the_preview_default_is_off_when_unset(self, env, monkeypatch):
        monkeypatch.setattr(server, "_fasttrack_default", lambda: "off")
        assert (
            client.post("/api/runs/preview", json={"text": "a\nb"}).json()[
                "lane_default"
            ]
            == "leave"
        )
        monkeypatch.setattr(server, "_fasttrack_default", lambda: "pr")
        assert (
            client.post("/api/runs/preview", json={"text": "a\nb"}).json()[
                "lane_default"
            ]
            == "pr"
        )

    @pytest.mark.parametrize(
        "over, status, message",
        [
            ({"items": []}, 400, "nothing to do"),
            ({"policy": {"lane": "yolo"}}, 400, "unknown lane"),
            ({"split": True}, 400, "split needs exactly one line"),
            ({"lead": "someone"}, 400, "lead is only for a split"),
            ({"program": "nope-cli"}, 400, "unknown agent"),
        ],
    )
    def test_refusals(self, env, over, status, message):
        r = _create(env, **over)
        assert r.status_code == status
        assert message in r.json()["error"]

    def test_list_get_and_404(self, env):
        rid = _create(env).json()["run"]["id"]
        runs = client.get("/api/runs").json()["runs"]
        assert [x["id"] for x in runs] == [rid]
        assert set(runs[0]) == {
            "id",
            "name",
            "state",
            "paused",
            "pause_reason",
            "policy",
            "counts",
            "cost_usd",
            "created_at",
        }
        assert client.get("/api/runs/" + rid).json()["run"]["id"] == rid
        assert client.get("/api/runs/r_missing").status_code == 404

    def test_long_poll_times_out_with_a_reason(self, env):
        rid = _create(env).json()["run"]["id"]
        r = client.get("/api/runs/%s?wait=0.1&until=needs_you" % rid)
        assert r.json()["reason"] == "timeout" and r.json()["run"]["id"] == rid
        assert client.get("/api/runs/%s?wait=1&until=x" % rid).status_code == 400


class TestControl:
    def test_pause_resume_cancel(self, env):
        rid = _create(env).json()["run"]["id"]
        r = client.post("/api/runs/%s/pause" % rid, json={"reason": "user"})
        assert r.status_code == 200 and r.json()["run"]["paused"] is True
        r = client.post("/api/runs/%s/resume" % rid, json={})
        assert r.json()["run"]["paused"] is False
        r = client.post("/api/runs/%s/cancel" % rid, json={})
        assert r.status_code == 200
        assert r.json()["run"]["state"] == "cancelled"
        assert r.json()["kept_sessions"] == []
        assert client.post("/api/runs/%s/pause" % rid).status_code == 409

    def test_task_routes(self, env):
        rid = _create(env, n=3, concurrency=1).json()["run"]["id"]
        asyncio.run(drv.step_run(rid))
        r = client.post("/api/runs/%s/tasks/t3/start-now" % rid)
        assert r.status_code == 200 and set(r.json()) == {"task"}
        assert client.post("/api/runs/%s/tasks/t1/start-now" % rid).status_code == 409
        assert (
            client.post("/api/runs/%s/tasks/t1/retry" % rid, json={}).status_code == 409
        )
        assert (
            client.post(
                "/api/runs/%s/tasks/t1/retry" % rid, json={"fresh": "y"}
            ).status_code
            == 400
        )
        r = client.post("/api/runs/%s/tasks/t2/skip" % rid)
        assert r.json()["task"]["state"] == "skipped"
        assert client.post("/api/runs/%s/tasks/t9/skip" % rid).status_code == 404

    def test_add_lines(self, env):
        rid = _create(env, n=1).json()["run"]["id"]
        r = client.post(
            "/api/runs/%s/tasks" % rid,
            json={"items": [{"kind": "task", "text": "more"}]},
        )
        assert r.status_code == 200
        assert [t["text"] for t in r.json()["run"]["tasks"]] == ["line 1", "more"]
        assert client.post("/api/runs/%s/tasks" % rid, json={}).status_code == 400

    def test_adopt(self, env):
        rid = _create(env, n=1).json()["run"]["id"]
        assert (
            client.post("/api/runs/%s/adopt" % rid, json={"title": "nope"}).status_code
            == 404
        )
        env.instances["web-dark-mode"] = _Inst("web-dark-mode")
        r = client.post("/api/runs/%s/adopt" % rid, json={"title": "web-dark-mode"})
        assert r.status_code == 200 and r.json()["task"]["title"] == "web-dark-mode"
        other = _create(env, n=1).json()["run"]["id"]
        r = client.post("/api/runs/%s/adopt" % other, json={"title": "web-dark-mode"})
        assert r.status_code == 409
        assert r.json()["error"] == "branch already in group Q4 payments"

    def test_split_and_release_routes_answer_honestly(self, env):
        rid = _create(env, n=1).json()["run"]["id"]
        assert (
            client.post("/api/runs/%s/plan" % rid, json={"pieces": []}).status_code
            == 409
        )
        assert client.post("/api/runs/%s/plan/approve" % rid).status_code == 409
        r = client.post("/api/runs/%s/release" % rid, json={})
        assert r.status_code == 409 and "not ready to release" in r.json()["error"]
        assert client.post("/api/runs/r_missing/plan", json={}).status_code == 404


def test_outbox_shape(env):
    _create(env, n=1)
    r = client.get("/api/outbox?group=all")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"counts", "groups", "summaries"}
    assert set(body["counts"]) == {"waiting", "shipping", "shipped", "queued"}
    assert set(body["groups"]) == {"waiting", "shipping", "shipped", "queued"}
    assert body["counts"]["queued"] == 1


def test_routes_are_registered():
    paths = {getattr(r, "path", "") for r in server.app.routes}
    for p in (
        "/api/runs/preview",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/pause",
        "/api/runs/{run_id}/resume",
        "/api/runs/{run_id}/cancel",
        "/api/runs/{run_id}/tasks",
        "/api/runs/{run_id}/tasks/{task_id}/retry",
        "/api/runs/{run_id}/tasks/{task_id}/start-now",
        "/api/runs/{run_id}/tasks/{task_id}/skip",
        "/api/runs/{run_id}/adopt",
        "/api/runs/{run_id}/plan",
        "/api/runs/{run_id}/plan/approve",
        "/api/runs/{run_id}/release",
        "/api/instances/{title}/lane",
        "/api/instances/{title}/ship-now",
        "/api/outbox",
    ):
        assert p in paths, p


def test_the_driver_loop_is_off_under_pytest():
    """ENGINE is the developer's real state in a test run; a lifespan-started
    TestClient must not drive the owner's groups."""
    assert server._team_run_loop_enabled() is False
    assert tr.runs_dir().startswith("/") and "mindflock" in tr.runs_dir()
