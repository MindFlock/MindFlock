"""Round-5 first-hour API contracts the wave-2 UI codes against.

* ``GET /api/tickets`` → ``source_status[<source>]`` = ``{connect_error,
  last_test_ok, last_test_at}`` for every configured ticket source, so a source
  that can't connect never reads "No tickets are assigned to you".
* ``POST /api/templates/{name}/run`` → 409 ``{detail, reason}`` when the
  template can't launch, recorded as a failed run; ``POST /api/templates`` →
  409 ``agent_missing`` / ``github_required``; ``GET /api/templates`` →
  ``agent_installed`` per template and a ``status`` on every history entry.
* ``POST /api/instances`` → 400 ``{detail: "No folder at <path>", reason:
  "no_folder"}`` for a folder that doesn't exist, unless ``create_folder``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import templates as tpl_addon
from backend.web.core import template_schedule as ts

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# Intake: per-source connect_error
# --------------------------------------------------------------------------- #
def _src(id_, last_test=None, provider="jira"):
    return SimpleNamespace(id=id_, provider=provider, last_test=last_test or {})


@pytest.fixture
def ticket_sources(monkeypatch):
    from backend.config import settings as settings_mod

    holder: dict = {"sources": []}
    monkeypatch.setattr(
        settings_mod,
        "load_settings",
        lambda: SimpleNamespace(ticketing=SimpleNamespace(sources=holder["sources"])),
    )
    return holder


def test_source_status_says_why_a_source_cant_connect(ticket_sources):
    ticket_sources["sources"] = [
        _src("jira", {"ok": False, "at": "2026-10-14T10:00:00+00:00", "message": "x"}),
        _src("sc", {"ok": True, "at": "2026-10-14T09:00:00+00:00"}, "shortcut"),
        _src("lin", None, "linear"),
        _src(
            "new",
            {"ok": False, "at": "2026-10-14T11:00:00+00:00", "message": "HTTP 404"},
        ),
    ]
    listing = {
        "sources": ["sc", "lin"],
        "errors": [{"source": "jira", "error": "Jira API returned HTTP 404"}],
    }
    out = server._ticket_source_status(listing)
    # The listing's own error wins.
    assert out["jira"] == {
        "connect_error": "can't connect — Jira API returned HTTP 404",
        "last_test_ok": False,
        "last_test_at": "2026-10-14T10:00:00+00:00",
    }
    # Listed cleanly: connected.
    assert out["sc"]["connect_error"] is None and out["sc"]["last_test_ok"] is True
    # Never tested: nulls, not False.
    assert out["lin"] == {
        "connect_error": None,
        "last_test_ok": None,
        "last_test_at": None,
    }
    # Not reached by a cached listing that predates it: its stored Test says why.
    assert out["new"]["connect_error"] == "can't connect — HTTP 404"


def test_tickets_route_carries_source_status(ticket_sources, monkeypatch):
    ticket_sources["sources"] = [_src("jira", {"ok": False, "message": "HTTP 401"})]

    async def listing():
        return {
            "sources": [],
            "tickets": [],
            "errors": [{"source": "jira", "error": "Jira rejected the token"}],
        }

    server._ASSIGNED_TICKETS_CACHE.pop("v", None)
    monkeypatch.setattr(server._ticket_start, "list_assigned_tickets", listing)
    try:
        body = client.get("/api/tickets").json()
    finally:
        server._ASSIGNED_TICKETS_CACHE.pop("v", None)
    assert body["errors"] == [{"source": "jira", "error": "Jira rejected the token"}]
    assert body["source_status"]["jira"]["connect_error"] == (
        "can't connect — Jira rejected the token"
    )


# --------------------------------------------------------------------------- #
# Templates: launch preconditions
# --------------------------------------------------------------------------- #
@pytest.fixture
def tpl_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_TEMPLATES_FILE", str(tmp_path / "tpl.json"))
    monkeypatch.setenv("MINDFLOCK_TEMPLATE_RUNS_FILE", str(tmp_path / "runs.json"))
    made: list = []

    async def create(payload):
        made.append(payload)
        return 202, {"title": payload["title"]}

    monkeypatch.setattr(ts, "_default_create", create)
    monkeypatch.setattr(ts, "_title_taken", lambda t: False)
    monkeypatch.setattr(ts, "automation_here", lambda: True)
    monkeypatch.setattr(ts, "automation_elsewhere", lambda: "")
    installed = {"codex": False, "claude": True}
    monkeypatch.setattr(
        tpl_addon,
        "_agent_installed",
        lambda program: installed.get(program.split()[0], True),
    )
    gh = {"connected": True}
    monkeypatch.setattr(tpl_addon, "_github_connected", lambda: gh["connected"])
    return SimpleNamespace(made=made, gh=gh)


def _tpl(name="pr-bot", **kw):
    return {"name": name, "repo_path": "/repo", "prompt": "do it", **kw}


def test_run_now_with_a_missing_agent_is_409_and_a_failed_run(tpl_store):
    assert client.post("/api/templates", json=_tpl(program="codex")).status_code == 200
    r = client.post("/api/templates/pr-bot/run")
    assert r.status_code == 409
    assert r.json() == {
        "error": "codex isn't installed",
        "detail": "Couldn't start pr-bot: codex isn't installed",
        "reason": "agent_missing",
        "agent": "codex",
    }
    assert tpl_store.made == []  # no session was made
    got = client.get("/api/templates").json()
    row = got["runs"]["pr-bot"]
    assert row["last_status"] == "failed" and row["last_reason"] == "agent_missing"
    assert row["last_error"] == "codex isn't installed"
    assert row["history"][0]["status"] == "failed"
    assert row["history"][0]["reason"] == "agent_missing"
    assert got["templates"][0]["agent_installed"] is False


def test_history_entries_all_say_how_they_ended(tpl_store):
    client.post("/api/templates", json=_tpl("ok", program="claude"))
    assert client.post("/api/templates/ok/run").status_code == 202
    got = client.get("/api/templates").json()
    row = got["runs"]["ok"]
    assert row["history"][0]["status"] == "started"
    assert row["last_status"] == "started" and "last_reason" not in row
    assert got["templates"][0]["agent_installed"] is True


def test_a_create_refusal_on_run_now_gets_a_sentence(tpl_store, monkeypatch):
    async def refuse(payload):
        return 400, {"error": "No folder at /repo", "reason": "no_folder"}

    monkeypatch.setattr(ts, "_default_create", refuse)
    client.post("/api/templates", json=_tpl("ok", program="claude"))
    r = client.post("/api/templates/ok/run")
    assert r.status_code == 400
    assert r.json()["detail"] == "Couldn't start ok: No folder at /repo"
    assert r.json()["reason"] == "no_folder"
    row = client.get("/api/templates").json()["runs"]["ok"]
    assert row["last_status"] == "failed" and row["last_reason"] == "launch_failed"
    missing = client.post("/api/templates/nope/run")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "There's no template named nope."


def test_scheduling_a_template_whose_agent_is_missing_is_409(tpl_store):
    r = client.post(
        "/api/templates",
        json=_tpl(program="codex", schedule={"at": "21:00", "days": "daily"}),
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "agent_missing"
    assert r.json()["detail"] == (
        "codex isn't installed — install it or pick another agent before "
        "scheduling this template"
    )
    # Unscheduled, it can be saved (Run now says so when pressed).
    assert client.post("/api/templates", json=_tpl(program="codex")).status_code == 200


def test_fast_track_to_a_pr_without_github_is_409(tpl_store):
    tpl_store.gh["connected"] = False
    r = client.post("/api/templates", json=_tpl(program="claude", autopilot="pr"))
    assert r.status_code == 409
    assert r.json()["reason"] == "github_required"
    assert r.json()["detail"] == (
        "Fast-track “Open a PR” needs GitHub — connect GitHub, or pick Commit or Push"
    )
    assert (
        client.post(
            "/api/templates", json=_tpl(program="claude", autopilot="push")
        ).status_code
        == 200
    )
    tpl_store.gh["connected"] = True
    assert (
        client.post(
            "/api/templates", json=_tpl(program="claude", autopilot="pr")
        ).status_code
        == 200
    )


def test_agent_installed_reads_a_registered_agent_and_a_raw_command(monkeypatch):
    from backend.web.core import settings_hooks

    monkeypatch.setattr(settings_hooks, "provider_installed", lambda n: n == "claude")
    monkeypatch.setattr(settings_hooks, "installed_path", lambda b: "")
    assert tpl_addon._agent_installed("claude") is True
    assert tpl_addon._agent_installed("codex --fast") is False
    assert tpl_addon._agent_installed("/opt/bin/my-agent") is False
    assert tpl_addon._agent_name("/opt/bin/my-agent --x") == "my-agent"


# --------------------------------------------------------------------------- #
# New session: no implicit folders
# --------------------------------------------------------------------------- #
def test_new_session_in_a_missing_folder_is_400_no_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})

    def must_not_make(opts):  # pragma: no cover — the refusal comes first
        raise AssertionError("a session was made for a missing folder")

    monkeypatch.setattr(server.session, "NewInstance", must_not_make)
    typo = tmp_path / "notgit-typo"
    for extra in ({}, {"init_repo": True}, {"create_folder": "yes"}):
        r = client.post(
            "/api/instances",
            json={"title": "t", "program": "bash", "repo_path": str(typo), **extra},
        )
        assert r.status_code == 400, r.text
        want = "No folder at %s" % os.path.realpath(str(typo))
        assert r.json() == {
            "error": want,
            "detail": want,
            "reason": "no_folder",
            "path": os.path.realpath(str(typo)),
        }
    assert not typo.exists()


def test_create_folder_true_makes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    asked: list = []

    def prepare(repo, init, **kw):
        asked.append(kw)
        raise ValueError("stop here")

    monkeypatch.setattr(server, "_prepare_plain_repo", prepare)
    r = client.post(
        "/api/instances",
        json={
            "title": "t",
            "program": "bash",
            "repo_path": str(tmp_path / "new"),
            "create_folder": True,
        },
    )
    assert r.status_code == 400 and asked == [{"create_folder": True}]


def test_tracker_step_ticks_only_from_a_passing_recorded_test(monkeypatch):
    """Round 5 (r5-intake blocker): a wizard abandoned after a failing Test —
    or a source saved with "Continue anyway" — must never read as "⑤ Connect
    your tracker ✓ done". The recorded verdict marks the step only when it
    passed; the PUT marks it only for a stored source whose last test passed."""
    from types import SimpleNamespace

    from backend.web.addons import settings as addon

    marked = []
    monkeypatch.setattr(addon._setup_steps, "mark", lambda step: marked.append(step))
    monkeypatch.setattr(
        addon.settings_store, "set_ticketing_sources", lambda rows: None
    )
    monkeypatch.setattr(addon, "_stamp_for_sync", lambda *a, **k: None)
    src = SimpleNamespace(
        to_dict=lambda: {"id": "jira-1", "provider": "jira", "api_token": "x"}
    )
    monkeypatch.setattr(
        addon.settings_store,
        "load_settings",
        lambda: SimpleNamespace(ticketing=SimpleNamespace(sources=[src])),
    )
    assert (
        addon._record_ticketing_test("jira-1", {"ok": False, "message": "HTTP 404"})
        is not None
    )
    assert marked == []
    assert (
        addon._record_ticketing_test("jira-1", {"ok": True, "message": "fine"})
        is not None
    )
    assert marked == ["tracker"]
