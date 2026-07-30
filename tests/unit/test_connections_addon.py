"""Connections addon: the aggregated outside-service status view.

Locks the contract that the addon self-registers on the generic slot path and
that GET /api/connections derives its status cheaply from the existing doctor
checks + settings store (no duplicated probes, no network on load).
"""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

from backend import doctor
from backend.web import server
from backend.web.addons import connections

client = TestClient(server.app)


def _chk(status: str, detail: str = ""):
    """A stand-in for a doctor Check — the addon only reads .status/.detail."""
    return types.SimpleNamespace(status=status, detail=detail)


# --------------------------------------------------------------------------- #
# Registration / manifest
# --------------------------------------------------------------------------- #
def test_connections_registered_without_frontend_module():
    data = client.get("/api/addons").json()
    conn = next(a for a in data["addons"] if a["id"] == "connections")
    # No separate frontend/modal anymore: the list is rendered inline in the
    # Settings → Connections screen (app.js) from /api/connections.
    assert conn["frontend"] == []


def test_connections_endpoint_shape():
    body = client.get("/api/connections").json()
    conns = body["connections"]
    ids = {c["id"] for c in conns}
    assert ids == {"agent", "git", "github", "ticketing", "tailscale"}
    # Every card carries the fields the frontend renders.
    for c in conns:
        assert set(c) >= {
            "id",
            "name",
            "purpose",
            "required",
            "status",
            "detail",
            "settings_screen",
        }
        assert c["status"] in ("connected", "attention", "not_connected")
    # Summary counts are internally consistent.
    s = body["summary"]
    assert s["total"] == len(conns)
    assert s["connected"] == sum(1 for c in conns if c["status"] == "connected")
    assert s["attention"] == sum(1 for c in conns if c["status"] == "attention")


# --------------------------------------------------------------------------- #
# Status derivation (monkeypatched signals — no real subprocess/network)
# --------------------------------------------------------------------------- #
def test_agent_required_missing_is_attention(monkeypatch):
    monkeypatch.setattr(
        doctor, "check_agent_cli", lambda: _chk("fail", "not found on PATH")
    )
    monkeypatch.setattr(
        doctor, "check_agent_auth", lambda: _chk("warn", "cannot probe")
    )
    c = connections._agent_connection()
    assert c["required"] is True
    assert c["status"] == "attention"
    assert "PATH" in c["detail"]


def test_agent_ok_is_connected(monkeypatch):
    monkeypatch.setattr(
        doctor, "check_agent_cli", lambda: _chk("ok", "/usr/bin/claude")
    )
    monkeypatch.setattr(
        doctor, "check_agent_auth", lambda: _chk("ok", "login state found")
    )
    assert connections._agent_connection()["status"] == "connected"


def test_github_token_source_makes_it_connected(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    monkeypatch.setattr(doctor, "check_gh", lambda: _chk("warn", "not authenticated"))
    c = connections._github_connection()
    assert c["status"] == "connected"
    assert "GH_TOKEN" in c["detail"]


def test_github_no_token_no_cli_is_not_connected(monkeypatch):
    # gh absent now surfaces as `info` (optional dep), and with no token that's
    # the calm gray "GitHub off" state — never attention-seeking.
    monkeypatch.setattr(connections, "_github_token_source", lambda: "")
    monkeypatch.setattr(
        doctor, "check_gh", lambda: _chk("info", "not found (optional)")
    )
    assert connections._github_connection()["status"] == "not_connected"


def test_github_no_credential_leads_with_the_token_not_a_gh_install(monkeypatch):
    # The bug this replaces: the tile's only offered remedy was `brew install gh`,
    # so a contributor with SSH remotes and no interest in the CLI was told the
    # single fix was to install it. A token is the better answer and must lead.
    monkeypatch.setattr(connections, "_github_token_source", lambda: "")
    monkeypatch.setattr(
        doctor,
        "check_gh",
        lambda: types.SimpleNamespace(
            status="info",
            detail="not found (optional)",
            fix="brew install gh",
            docs="https://cli.github.com",
        ),
    )
    c = connections._github_connection()
    assert c["fix"] == connections._NO_CREDENTIAL_FIX
    assert (
        c["fix"]
        == "add a GitHub token in Settings → PR review, or install the GitHub CLI"
    )
    assert c["fix_command"] == ""  # Configure/$GH_TOKEN, not a shell command
    assert "brew install gh" not in c["fix"]
    assert c["docs"] == "https://cli.github.com"  # still reachable as a secondary


def test_github_no_credential_detail_is_honest_about_pushing(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "")
    monkeypatch.setattr(doctor, "check_gh", lambda: _chk("info", "not found"))
    detail = connections._github_connection()["detail"]
    # Names exactly what is lost...
    assert "PR create/merge and PR review are off" in detail
    # ...and refuses to imply the user cannot push. Pushing is plain git over
    # whatever remote (SSH or HTTPS) they configured; gh is never in that path.
    assert "pushing still works" in detail


def test_github_purpose_does_not_claim_pushing_needs_this_connection(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "settings")
    monkeypatch.setattr(doctor, "check_gh", lambda: _chk("info", "not found"))
    purpose = connections._github_connection()["purpose"]
    assert "Push" not in purpose and "push" not in purpose


def test_git_missing_is_calm_not_connected(monkeypatch):
    # Git is optional: absent git is the calm gray state (never "attention"),
    # and Configure points at Doctor (a system dep, no in-app credential).
    monkeypatch.setattr(
        doctor, "check_git", lambda: _chk("info", "not found (optional)")
    )
    c = connections._git_connection()
    assert c["required"] is False
    assert c["status"] == "not_connected"
    assert c["settings_screen"] == "doctor"


def test_git_present_is_connected(monkeypatch):
    monkeypatch.setattr(doctor, "check_git", lambda: _chk("ok", "git version 2.43.0"))
    assert connections._git_connection()["status"] == "connected"


def _ticketing_ns(**tk):
    """A settings namespace whose ticketing group has a sources list.

    With no provider kwarg the sources list is empty (unconfigured); otherwise a
    single source is synthesized from the kwargs."""
    base = dict(
        id="",
        provider="",
        api_token="",
        base_url="",
        email="",
        member_id="",
        project="",
        label="",
        workflow_state_id=None,
    )
    base.update(tk)
    sources = [types.SimpleNamespace(**base)] if base["provider"] else []
    return types.SimpleNamespace(
        ticketing=types.SimpleNamespace(sources=sources),
        github=types.SimpleNamespace(repo="", enabled=None),
    )


def test_ticketing_configured_is_connected(monkeypatch):
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: _ticketing_ns(provider="shortcut", api_token="tok-123"),
    )
    c = connections._ticketing_connections()[0]
    assert c["status"] == "connected"
    assert c["test_endpoint"] == "/api/settings/test/ticketing"
    assert c["provider"] == "shortcut"


def test_ticketing_unconfigured_is_not_connected(monkeypatch):
    monkeypatch.setattr(
        connections.settings_store, "load_settings", lambda: _ticketing_ns()
    )
    assert connections._ticketing_connections()[0]["status"] == "not_connected"


def test_ticketing_github_issues_needs_scope(monkeypatch):
    # GitHub Issues can borrow the GitHub token, but still needs owner/repo.
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: _ticketing_ns(provider="github_issues", project=""),
    )
    assert connections._ticketing_connections()[0]["status"] == "attention"


def test_github_attention_exposes_copyable_fix(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "")
    gh = types.SimpleNamespace(
        status="warn",
        detail="not logged in",
        fix="run `gh auth login`",
        docs="https://x",
    )
    monkeypatch.setattr(doctor, "check_gh", lambda: gh)
    c = connections._github_connection()
    assert c["status"] == "attention"
    assert c["fix_command"] == "gh auth login"  # extracted from the backtick span
    assert c["fix"] == "run gh auth login"  # hint, backticks stripped
    assert c["docs"] == "https://x"


def test_connected_service_exposes_no_fix(monkeypatch):
    monkeypatch.setattr(doctor, "check_agent_cli", lambda: _chk("ok", "/bin/claude"))
    monkeypatch.setattr(doctor, "check_agent_auth", lambda: _chk("ok", "signed in"))
    c = connections._agent_connection()
    assert c["fix"] == "" and c["fix_command"] == ""


def test_endpoint_caches_within_ttl(monkeypatch):
    calls = {"n": 0}
    real = connections.build_connections

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(connections, "build_connections", counting)
    connections._cache["payload"] = None  # start from a cold cache
    connections._cache["at"] = 0.0

    client.get("/api/connections")
    client.get("/api/connections")
    assert calls["n"] == 1  # second call served from the ~4s cache

    client.get("/api/connections?refresh=1")
    assert calls["n"] == 2  # refresh=1 bypasses the cache (manual Refresh)


def test_fix_command_extraction():
    assert connections._fix_command("run `gh auth login`") == "gh auth login"
    assert connections._fix_command("sudo apt install tmux") == "sudo apt install tmux"
    assert connections._fix_command("see the docs") == ""  # not runnable, no span
    assert connections._fix_command("") == ""


def test_required_attention_sorts_first(monkeypatch):
    # Agent (required) missing must sort ahead of everything else.
    monkeypatch.setattr(doctor, "check_agent_cli", lambda: _chk("fail", "gone"))
    monkeypatch.setattr(doctor, "check_agent_auth", lambda: _chk("warn", "n/a"))
    monkeypatch.setattr(connections, "_github_token_source", lambda: "settings")
    monkeypatch.setattr(doctor, "check_gh", lambda: _chk("ok", "authed"))
    monkeypatch.setattr(
        connections.settings_store, "load_settings", lambda: _ticketing_ns()
    )
    monkeypatch.setattr(doctor, "check_tailscale", lambda: _chk("ok", "present"))
    ordered = connections.build_connections()
    assert ordered[0]["id"] == "agent"  # required + attention wins the top slot
