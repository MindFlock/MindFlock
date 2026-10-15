"""Connections addon: the dashboard of every outside thing MindFlock talks to.

Locks the row contract (``{id, name, kind, status, detail, action, test,
fix_command, docs, updated_at}`` + the legacy ``legacy_status`` projection),
each row's three states with its source stubbed, the kept ids, and that
``GET /api/connections`` is built from existing signals (doctor checks, the
settings store, the provider status, the device/peer/MCP/ntfy/dbclient/
extension readers) — no duplicated probes, no network on load.
"""

from __future__ import annotations

import time
import types

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.web import server
from backend.web.addons import connections

client = TestClient(server.app)

ROW_KEYS = {
    "id",
    "name",
    "kind",
    "status",
    "detail",
    "action",
    "test",
    "fix_command",
    "docs",
    "updated_at",
}


def _chk(status: str, detail: str = ""):
    """A stand-in for a doctor Check — the addon only reads .status/.detail."""
    return types.SimpleNamespace(status=status, detail=detail)


def _provider(name="claude", **kw) -> dict:
    """One ``/api/providers/status`` entry, installed + signed in by default."""
    base = dict(
        name=name,
        binary=name,
        installed=True,
        path=f"/usr/bin/{name}",
        authenticated=True,
        auth_detail="login state found",
        auth_probe=True,
        has_login=True,
        login_command=f"{name} login",
        install_hint=f"npm i -g {name}",
        launch_hint="",
        is_default=name == "claude",
    )
    base.update(kw)
    return base


def _stub_providers(monkeypatch, *rows, errors=()):
    rows = rows or (_provider(),)
    default = next((r["name"] for r in rows if r.get("is_default")), "claude")
    monkeypatch.setattr(
        connections, "_provider_statuses", lambda: (default, list(rows), list(errors))
    )


@pytest.fixture
def quiet(monkeypatch):
    """Every source stubbed to its calm default, so a test changes one."""
    _stub_providers(monkeypatch)
    monkeypatch.setattr(connections, "_github_token_source", lambda: "settings")
    monkeypatch.setattr(doctor, "check_gh", lambda: _chk("info", "not found"))
    monkeypatch.setattr(doctor, "check_git", lambda: _chk("ok", "git 2.43"))
    monkeypatch.setattr(doctor, "check_tailscale", lambda: _chk("ok", "present"))
    monkeypatch.setattr(connections, "_paired_devices", lambda: [])
    monkeypatch.setattr(connections, "_phone_last_seen", lambda: 0.0)
    monkeypatch.setattr(connections, "_peer_links", lambda: [])
    monkeypatch.setattr(connections, "_mcp_state", lambda: (False, {}, []))
    monkeypatch.setattr(connections, "_db_profiles", lambda: [])
    monkeypatch.setattr(connections, "_ntfy_state", lambda: {})
    monkeypatch.setattr(connections, "_extension_state", lambda: ([], []))
    monkeypatch.setattr(connections, "_GITHUB_TEST", {})
    return monkeypatch


# --------------------------------------------------------------------------- #
# Registration / manifest / payload
# --------------------------------------------------------------------------- #
def test_connections_registered_without_frontend_module():
    data = client.get("/api/addons").json()
    conn = next(a for a in data["addons"] if a["id"] == "connections")
    # No separate frontend/modal: the list renders inside Settings →
    # Connections (Connections.tsx) from /api/connections.
    assert conn["frontend"] == []


def test_endpoint_rows_follow_the_contract():
    body = client.get("/api/connections?refresh=1").json()
    conns = body["connections"]
    ids = {c["id"] for c in conns}
    # The old ids stay (tests and links pin them); the new rows join them.
    assert {"agent", "git", "github", "ticketing", "tailscale"} <= ids
    assert {"devices", "phone", "peers", "mcp", "ntfy"} <= ids
    for c in conns:
        assert set(c) >= ROW_KEYS, c["id"]
        assert c["status"] in connections.STATES
        assert c["kind"] in connections.KINDS
        assert c["connection"] == c["status"]
        assert c["legacy_status"] == connections.LEGACY_STATUS[c["status"]]
        if c["action"] is not None:
            assert set(c["action"]) >= {"label", "kind", "target"}
            assert c["action"]["kind"] in connections.ACTION_KINDS
        if c["test"] is not None:
            assert c["test"]["endpoint"].startswith("/api/")
            assert c["test"]["method"] == "POST"
            assert c["test_endpoint"] == c["test"]["endpoint"]
        assert c["updated_at"]
    # Rows arrive grouped: kinds never interleave.
    kinds = [c["kind"] for c in conns]
    order = [connections.KINDS.index(k) for k in kinds]
    assert order == sorted(order)
    s = body["summary"]
    assert s["total"] == len(conns)
    for st in connections.STATES:
        assert s[st] == sum(1 for c in conns if c["status"] == st)
    assert s["attention"] == s["needs_attention"]  # the old count, same meaning


def test_legacy_projection():
    assert connections.LEGACY_STATUS == {
        "connected": "connected",
        "needs_attention": "attention",
        "off": "not_connected",
    }


def test_one_broken_source_costs_only_its_own_row(quiet):
    def boom():
        raise RuntimeError("store unreadable")

    quiet.setattr(connections, "_devices_connection", boom)
    ids = {c["id"] for c in connections.build_connections()}
    assert "devices" not in ids and "agent" in ids and "phone" in ids


# --------------------------------------------------------------------------- #
# Agent CLIs — one row per provider
# --------------------------------------------------------------------------- #
def test_agent_one_row_per_provider_default_first_and_marked(quiet):
    _stub_providers(
        quiet,
        _provider("codex", is_default=False),
        _provider("claude", is_default=True),
    )
    rows = connections._agent_connections()
    assert [r["id"] for r in rows] == ["agent", "agent:codex"]
    assert rows[0]["default"] is True and rows[1]["default"] is False
    assert rows[0]["name"].lower().startswith("coding")  # the e2e journey pin
    assert rows[0]["required"] is True
    assert rows[0]["test"] == {
        "endpoint": "/api/connections/test/agent",
        "method": "POST",
        "body": {"provider": "claude"},
    }


def test_agent_signed_in_is_connected_with_no_fix(quiet):
    _stub_providers(quiet, _provider())
    r = connections._agent_connections()[0]
    assert r["status"] == "connected"
    assert r["fix"] == "" and r["fix_command"] == ""


def test_agent_with_nothing_to_sign_in_to_is_connected(quiet):
    # No login flow and nowhere it says credentials live: nothing to check.
    _stub_providers(
        quiet, _provider(authenticated=False, has_login=False, auth_probe=False)
    )
    r = connections._agent_connections()[0]
    assert r["status"] == "connected"
    assert "no sign-in needed" in r["detail"]


def test_api_key_agent_without_a_key_needs_attention_with_the_doctors_fix(quiet):
    """Aider: no login flow, but it names the API-key env vars it reads and
    none is set — not "no sign-in needed · Connected"."""
    _stub_providers(
        quiet,
        _provider("claude"),
        _provider(
            "aider",
            is_default=False,
            authenticated=False,
            auth_probe=True,
            has_login=False,
            login_command="",
        ),
    )
    r = next(r for r in connections._agent_connections() if r["id"] == "agent:aider")
    assert r["status"] == "needs_attention"
    assert "not signed in" in r["detail"]
    assert "no sign-in needed" not in r["detail"]
    # The doctor's own remedy: the API keys it reads, nothing to run.
    assert r["fix"].startswith("set one of the API keys `aider` reads (")
    assert r["fix_command"] == ""
    # No Sign in button: there is no login flow to open.
    assert r["action"]["label"] != "Sign in"
    assert r["action"] == {"label": "Set up", "kind": "screen", "target": "providers"}


def test_agent_installed_not_signed_in_needs_attention_with_sign_in(quiet):
    _stub_providers(
        quiet,
        _provider("claude"),
        _provider("codex", is_default=False, authenticated=False),
    )
    r = next(r for r in connections._agent_connections() if r["id"] == "agent:codex")
    assert r["status"] == "needs_attention"
    assert r["legacy_status"] == "attention"
    assert r["action"] == {
        "label": "Sign in",
        "kind": "dialog",
        "target": "agent-sign-in",
        "provider": "codex",
    }
    assert r["fix_command"] == "codex login"


# --- the agent card: one next action, the account in words (round 4) ------
def test_default_agent_not_installed_offers_the_one_click_install(quiet):
    _stub_providers(quiet, _provider(installed=False, authenticated=False))
    r = connections._agent_connections()[0]
    assert r["status"] == "needs_attention"
    assert r["action"] == {
        "label": "Install",
        "kind": "dialog",
        "target": "agent-install",
        "provider": "claude",
    }
    # The copy line stays on the row as the fallback when the plan lacks it.
    assert r["fix_command"] == "npm i -g claude"


def test_other_agent_not_installed_keeps_the_copy_command(quiet):
    _stub_providers(
        quiet,
        _provider("claude"),
        _provider("codex", is_default=False, installed=False),
    )
    r = next(r for r in connections._agent_connections() if r["id"] == "agent:codex")
    assert r["action"]["kind"] == "copy"


def test_connected_default_leads_to_change_agent_not_a_second_card(quiet):
    _stub_providers(quiet, _provider("claude"), _provider("codex", is_default=False))
    rows = {r["id"]: r for r in connections._agent_connections()}
    assert rows["agent"]["action"] == {
        "label": "Change agent",
        "kind": "screen",
        "target": "coding",
    }
    # Another agent's card has Test and nothing else to press.
    assert rows["agent:codex"]["action"] is None


def test_signed_in_claude_names_its_account(quiet, tmp_path, monkeypatch):
    (tmp_path / ".claude.json").write_text(
        '{"oauthAccount": {"emailAddress": "me@example.com"}}', encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    r = connections._agent_connections()[0]
    assert r["account"] == "me@example.com"
    assert (
        connections.agent_test_line("claude", "ok", "ok", "")
        == "Signed in as me@example.com"
    )


def test_agent_test_line_is_plain_words_never_a_path():
    # Only Claude Code's own config names an account; the rest read "Signed in".
    line = connections.agent_test_line
    assert line("codex", "ok", "ok", "login state found in /x/auth.json") == "Signed in"
    assert (
        line("codex", "ok", "ok", "OPENAI_API_KEY is set")
        == "Signed in with an API key"
    )
    assert line("claude", "fail", "warn", "") == "Claude Code isn't installed"
    assert line("codex", "ok", "warn", "") == "Installed, not signed in yet"
    assert "can't check" in line("goose", "ok", "info", "")


def test_agent_test_route_answers_in_words(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_agent_cli_for",
        lambda name: types.SimpleNamespace(
            status="ok", detail="/usr/bin/codex", to_dict=lambda: {"status": "ok"}
        ),
    )
    monkeypatch.setattr(
        doctor,
        "check_agent_auth_for",
        lambda name: types.SimpleNamespace(
            status="ok",
            detail="login state found in /home/me/.codex/auth.json",
            to_dict=lambda: {"status": "ok"},
        ),
    )
    d = client.post("/api/connections/test/agent", json={"provider": "codex"}).json()
    assert d["ok"] is True
    assert d["detail"] == "Signed in"
    assert "auth.json" not in d["detail"]


def test_agent_sign_in_carries_the_default_account_profile(quiet):
    _stub_providers(quiet, _provider(authenticated=False))
    quiet.setattr(connections, "_sign_in_profile", lambda name: "work")
    r = connections._agent_connections()[0]
    assert r["action"]["profile"] == "work"


def test_agent_not_installed_is_off_with_the_install_command(quiet):
    _stub_providers(
        quiet,
        _provider("claude"),
        _provider("goose", is_default=False, installed=False),
    )
    r = next(r for r in connections._agent_connections() if r["id"] == "agent:goose")
    assert r["status"] == "off"
    assert r["fix_command"] == "npm i -g goose"
    assert r["action"]["kind"] == "copy" and r["action"]["target"] == "npm i -g goose"


def test_default_agent_missing_needs_attention_not_off(quiet):
    # Every new session runs the default: it missing is not a calm gray.
    _stub_providers(quiet, _provider(installed=False))
    r = connections._agent_connections()[0]
    assert r["id"] == "agent" and r["status"] == "needs_attention"


def test_a_custom_provider_that_failed_to_load_is_a_row(quiet):
    _stub_providers(quiet, _provider(), errors=[{"file": "kiro.toml", "error": "bad"}])
    r = connections._agent_connections()[-1]
    assert r["id"] == "agent-config:kiro.toml"
    assert r["status"] == "needs_attention" and "bad" in r["detail"]


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def test_github_token_source_makes_it_connected(quiet):
    quiet.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    quiet.setattr(doctor, "check_gh", lambda: _chk("warn", "not authenticated"))
    c = connections._github_connection()
    assert c["status"] == "connected"
    assert "GH_TOKEN" in c["detail"]
    assert c["token_source"] == "env:GH_TOKEN"
    assert c["action"] == {
        "label": "Manage",
        "kind": "dialog",
        "target": "connect-github",
    }
    assert c["test"] == {"endpoint": "/api/settings/test/github", "method": "POST"}


def test_github_names_the_account_from_the_last_test(quiet):
    connections.note_github_test(
        {"ok": True, "login": "octo", "token_source": "settings", "detail": "ok"}
    )
    c = connections._github_connection()
    assert c["status"] == "connected"
    assert c["account"] == "octo" and "@octo" in c["detail"]


def test_github_token_whose_check_failed_needs_attention(quiet):
    connections.note_github_test(
        {
            "ok": False,
            "token_source": "settings",
            "detail": "@octo via settings · org/r: no such repo, or this token cannot see it",
        }
    )
    c = connections._github_connection()
    assert c["status"] == "needs_attention"
    assert "cannot see it" in c["detail"]
    assert c["action"]["label"] == "Connect"


def test_github_failed_test_of_another_credential_does_not_count(quiet):
    connections.note_github_test({"ok": False, "token_source": "env:GH_TOKEN"})
    assert connections._github_connection()["status"] == "connected"


def test_github_no_token_no_cli_is_off(quiet):
    # gh absent surfaces as `info` (optional dep), and with no token that's
    # the calm gray "off" state — never attention-seeking.
    quiet.setattr(connections, "_github_token_source", lambda: "")
    c = connections._github_connection()
    assert c["status"] == "off" and c["legacy_status"] == "not_connected"


def test_github_no_credential_leads_with_the_token_not_a_gh_install(quiet):
    # The bug this replaces: the tile's only offered remedy was `brew install gh`,
    # so a contributor with SSH remotes and no interest in the CLI was told the
    # single fix was to install it. A token is the better answer and must lead.
    quiet.setattr(connections, "_github_token_source", lambda: "")
    quiet.setattr(
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
        c["fix"] == "add a GitHub token in Settings → Connections → GitHub → Connect, "
        "or install the GitHub CLI"
    )
    assert c["fix_command"] == ""  # Connect/$GH_TOKEN, not a shell command
    assert "brew install gh" not in c["fix"]
    # Docs is MindFlock's own Connect GitHub help (UX r5), not gh's homepage.
    assert c["docs"] == connections.GITHUB_HELP_URL


def test_github_no_credential_detail_is_honest_about_pushing(quiet):
    quiet.setattr(connections, "_github_token_source", lambda: "")
    detail = connections._github_connection()["detail"]
    # Names exactly what is lost...
    assert "PR create/merge and PR review are off" in detail
    # ...and refuses to imply the user cannot push. Pushing is plain git over
    # whatever remote (SSH or HTTPS) they configured; gh is never in that path.
    assert "pushing still works" in detail


def test_github_purpose_does_not_claim_pushing_needs_this_connection(quiet):
    purpose = connections._github_connection()["purpose"]
    assert "Push" not in purpose and "push" not in purpose


def test_github_gh_unauthenticated_is_off_but_offers_gh_auth_login(quiet):
    quiet.setattr(connections, "_github_token_source", lambda: "")
    gh = types.SimpleNamespace(
        status="warn",
        detail="not logged in",
        fix="run `gh auth login`",
        docs="https://x",
    )
    quiet.setattr(doctor, "check_gh", lambda: gh)
    c = connections._github_connection()
    assert c["status"] == "off"
    assert c["fix_command"] == "gh auth login"  # extracted from the backtick span
    assert c["fix"] == "run gh auth login"  # hint, backticks stripped
    assert c["docs"] == connections.GITHUB_HELP_URL


def test_settings_github_test_route_records_its_verdict(quiet, monkeypatch):
    from backend.web.core import github_auth

    monkeypatch.setattr(
        github_auth,
        "test_connection",
        lambda: {"ok": False, "token_source": "settings", "login": "", "detail": "401"},
    )
    client.post("/api/settings/test/github")
    assert connections._GITHUB_TEST["ok"] is False
    assert connections._github_connection()["status"] == "needs_attention"


# --------------------------------------------------------------------------- #
# Tracker sources
# --------------------------------------------------------------------------- #
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
        last_test={},
    )
    base.update(tk)
    sources = [types.SimpleNamespace(**base)] if base["provider"] else []
    return types.SimpleNamespace(
        ticketing=types.SimpleNamespace(sources=sources),
        github=types.SimpleNamespace(repo="", enabled=None),
    )


def test_ticketing_with_a_token_but_no_test_needs_attention(monkeypatch):
    # A token alone used to read as connected. Connected means TESTED now: a
    # source nobody has seen work is a source that needs attention, and the
    # row says what to do about it.
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: _ticketing_ns(
            id="sc", provider="shortcut", api_token="tok-123", member_id="m"
        ),
    )
    c = connections._ticketing_connections()[0]
    assert c["status"] == "needs_attention" and c["legacy_status"] == "attention"
    assert c["connection"] == "needs_attention"
    assert "run Test" in c["message"]
    assert c["kind"] == "tracker"
    assert c["test"] == {
        "endpoint": "/api/settings/test/ticketing",
        "method": "POST",
        "body": {"id": "sc", "record": True},
    }
    assert c["action"]["target"] == "intake" and c["action"]["tab"] == "tickets"
    assert c["action"]["source_id"] == "sc"
    assert c["provider"] == "shortcut"


def test_ticketing_tested_today_is_connected(monkeypatch):
    from datetime import datetime, timezone

    at = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc).isoformat()
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: _ticketing_ns(
            provider="shortcut",
            api_token="tok-123",
            member_id="m",
            last_test={"ok": True, "at": at, "message": "Connected — ethan"},
        ),
    )
    c = connections._ticketing_connections()[0]
    assert c["status"] == "connected"
    assert c["message"] == "Connected — ethan"
    assert c["tested_at"] == at and c["updated_at"] == at
    assert c["fix"] == ""


def test_ticketing_unconfigured_is_off(monkeypatch):
    monkeypatch.setattr(
        connections.settings_store, "load_settings", lambda: _ticketing_ns()
    )
    c = connections._ticketing_connections()[0]
    assert c["id"] == "ticketing" and c["status"] == "off"
    assert c["legacy_status"] == "not_connected"
    assert c["test"] is None  # nothing to test yet
    assert c["action"]["target"] == "intake"


def test_ticketing_github_issues_needs_scope(monkeypatch):
    # GitHub Issues can borrow the GitHub token, but still needs owner/repo.
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: _ticketing_ns(provider="github_issues", project=""),
    )
    assert connections._ticketing_connections()[0]["status"] == "needs_attention"


# --------------------------------------------------------------------------- #
# Your devices · Your phone · People
# --------------------------------------------------------------------------- #
def test_devices_three_states(quiet):
    quiet.setattr(connections, "_paired_devices", lambda: [])
    r = connections._devices_connection()
    assert (r["id"], r["kind"], r["status"]) == ("devices", "devices", "off")
    assert r["action"] == {
        "label": "Add a device",
        "kind": "screen",
        "target": "devices",
    }

    quiet.setattr(
        connections,
        "_paired_devices",
        lambda: [
            {"key": "a", "name": "mac-mini", "reachable": True},
            {"key": "b", "name": "ml-rig", "reachable": False},
        ],
    )
    r = connections._devices_connection()
    assert r["status"] == "connected"
    assert "mac-mini" in r["detail"] and "ml-rig" in r["detail"]

    quiet.setattr(
        connections,
        "_paired_devices",
        lambda: [{"key": "b", "name": "ml-rig", "reachable": False}],
    )
    r = connections._devices_connection()
    assert r["status"] == "needs_attention" and "ml-rig" in r["detail"]


def test_phone_connected_within_seven_days_else_off(quiet):
    quiet.setattr(connections, "_phone_last_seen", lambda: time.time() - 2 * 86400)
    r = connections._phone_connection()
    assert r["id"] == "phone" and r["status"] == "connected"
    assert "2 days ago" in r["detail"]
    assert r["action"]["target"] == "mobile"

    quiet.setattr(connections, "_phone_last_seen", lambda: time.time() - 8 * 86400)
    assert connections._phone_connection()["status"] == "off"
    quiet.setattr(connections, "_phone_last_seen", lambda: 0.0)
    r = connections._phone_connection()
    assert r["status"] == "off" and "scan the QR" in r["detail"]


def test_phone_load_from_m_counts(monkeypatch):
    monkeypatch.setitem(connections._PHONE, "at", 0.0)
    connections.note_phone_load()
    assert time.time() - connections._phone_last_seen() < 5


def test_phone_falls_back_to_the_setup_step(monkeypatch):
    from backend.web.core import setup_steps

    monkeypatch.setitem(connections._PHONE, "at", 0.0)
    at = connections._iso(time.time() - 3600)
    monkeypatch.setattr(
        setup_steps, "state", lambda: {"phone": {"done": True, "at": at}}
    )
    assert abs(connections._phone_last_seen() - (time.time() - 3600)) < 5


def test_peers_three_states(quiet):
    r = connections._peers_connection()
    assert (r["id"], r["status"]) == ("peers", "off")
    assert r["action"]["target"] == "peer"
    quiet.setattr(
        connections,
        "_peer_links",
        lambda: [
            {"name": "dana", "connected": True},
            {"name": "lee", "connected": False},
        ],
    )
    r = connections._peers_connection()
    assert r["status"] == "connected" and "lee" in r["detail"]
    quiet.setattr(
        connections, "_peer_links", lambda: [{"name": "lee", "connected": False}]
    )
    r = connections._peers_connection()
    assert r["status"] == "needs_attention" and "lee" in r["detail"]


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def test_mcp_off_when_the_setting_is_off(quiet):
    r = connections._mcp_connection()
    assert (r["id"], r["kind"], r["status"]) == ("mcp", "mcp", "off")
    assert r["action"] == {
        "label": "Manage",
        "kind": "screen",
        "target": "orchestration",
    }


def test_mcp_connected_when_the_last_launch_attached(quiet):
    quiet.setattr(
        connections,
        "_mcp_state",
        lambda: (
            True,
            {"session": "mf_a", "attached": True, "at": time.time()},
            ["claude"],
        ),
    )
    quiet.setattr(connections, "_launch_provider", lambda name: ("a", "claude"))
    r = connections._mcp_connection()
    assert r["status"] == "connected" and "claude in a" in r["detail"]


def test_mcp_needs_attention_naming_the_provider(quiet):
    quiet.setattr(
        connections,
        "_mcp_state",
        lambda: (
            True,
            {"session": "mf_b", "attached": False, "at": time.time()},
            ["claude"],
        ),
    )
    quiet.setattr(connections, "_launch_provider", lambda name: ("b", "aider"))
    r = connections._mcp_connection()
    assert r["status"] == "needs_attention"
    assert "aider" in r["detail"] and "no MCP attach support" in r["detail"]
    assert r["provider"] == "aider"


def test_mcp_records_the_last_launch(monkeypatch):
    from backend.providers import mcp_attach

    monkeypatch.setattr(mcp_attach, "_LAUNCHED", {})
    monkeypatch.setattr(mcp_attach, "_LAST_LAUNCH", {})
    mcp_attach.note_launch("mf_x", False)
    mcp_attach.note_launch("mf_y", True)
    last = mcp_attach.last_launch()
    assert last["session"] == "mf_y" and last["attached"] is True and last["at"]


# --------------------------------------------------------------------------- #
# Databases
# --------------------------------------------------------------------------- #
def test_databases_off_when_none(quiet):
    rows = connections._db_connections()
    assert [(r["id"], r["status"]) for r in rows] == [("databases", "off")]
    assert rows[0]["action"]["target"] == "db-explorer"


def test_databases_one_row_per_profile_with_its_last_test(quiet):
    quiet.setattr(
        connections,
        "_db_profiles",
        lambda: [
            {
                "id": "a",
                "name": "Prod",
                "engine": "postgres",
                "host": "db",
                "last_test": {"ok": True, "server": "PG 16", "at": 1.0},
            },
            {
                "id": "b",
                "name": "Dev",
                "engine": "sqlite",
                "last_test": {"ok": False, "error": "no such file", "at": 2.0},
            },
            {"id": "c", "name": "New", "engine": "mysql", "last_test": {}},
        ],
    )
    rows = {r["id"]: r for r in connections._db_connections()}
    assert (
        rows["database:a"]["status"] == "connected"
        and "PG 16" in rows["database:a"]["detail"]
    )
    assert rows["database:b"]["status"] == "needs_attention"
    assert "no such file" in rows["database:b"]["detail"]
    assert rows["database:c"]["status"] == "needs_attention"
    assert rows["database:a"]["test"]["endpoint"] == "/api/dbclient/connections/a/test"


def test_dbclient_listing_helper_never_carries_a_password(tmp_path, monkeypatch):
    from backend.web.addons.dbclient import store

    monkeypatch.setenv("MINDFLOCK_DBCLIENT_FILE", str(tmp_path / "dbclient.json"))
    pw = "s3cret"  # pragma: allowlist secret
    store.save_profiles([{"id": "p", "name": "P", "engine": "sqlite", "password": pw}])
    store.note_test("p", {"ok": True, "server": "3.45"})
    (row,) = store.profile_summaries()
    assert row["last_test"]["ok"] is True and row["last_test"]["server"] == "3.45"
    assert pw not in str(row)
    # The verdict survives a restart (a fresh read of the side file)...
    monkeypatch.setitem(store._TESTS_LOADED, "path", "")
    assert store.profile_summaries()[0]["last_test"]["ok"] is True
    # ...and an edit makes the profile untested again.
    store.upsert_profile(store._complete({"id": "p", "name": "P2", "engine": "sqlite"}))
    assert store.profile_summaries()[0]["last_test"] == {}


# --------------------------------------------------------------------------- #
# ntfy
# --------------------------------------------------------------------------- #
def test_ntfy_three_states(quiet):
    r = connections._ntfy_connection()
    assert (r["id"], r["kind"], r["status"]) == ("ntfy", "notifications", "off")
    assert r["test"] is None and r["action"]["target"] == "notifications"

    on = {"configured": True, "enabled": True, "active": True, "server": "ntfy.sh"}
    quiet.setattr(
        connections, "_ntfy_state", lambda: {**on, "last": {"ok": True, "ts": 5.0}}
    )
    r = connections._ntfy_connection()
    assert r["status"] == "connected"
    assert r["test"] == {"endpoint": "/api/notify/ntfy/test", "method": "POST"}

    quiet.setattr(
        connections,
        "_ntfy_state",
        lambda: {**on, "last": {"ok": False, "error": "403"}},
    )
    r = connections._ntfy_connection()
    assert r["status"] == "needs_attention" and "403" in r["detail"]

    quiet.setattr(connections, "_ntfy_state", lambda: {**on, "enabled": False})
    assert connections._ntfy_connection()["status"] == "off"


def test_notify_status_getter_never_leaks_the_topic(monkeypatch):
    from backend.web.addons import notify
    from backend.web.core import ntfy

    cfg = types.SimpleNamespace(
        enabled=True, configured=True, active=True, host="ntfy.sh", topic="secret-topic"
    )
    monkeypatch.setattr(ntfy, "load", lambda: cfg)
    monkeypatch.setattr(
        ntfy, "last_result", lambda: {"ok": True, "ts": 1.0, "error": ""}
    )
    st = notify.ntfy_status()
    assert st["active"] is True and st["last"]["ok"] is True
    assert "secret-topic" not in str(st)


# --------------------------------------------------------------------------- #
# Extensions
# --------------------------------------------------------------------------- #
def test_extensions_loaded_and_failed_rows(quiet):
    ext = types.SimpleNamespace(
        id="dbclient", label="Database Client", origin="builtin"
    )
    fail = {
        "id": "broken",
        "file": "/x/broken/extension.py",
        "error": "SyntaxError",
        "at": 1.0,
    }
    quiet.setattr(connections, "_extension_state", lambda: ([ext], [fail]))
    rows = {r["id"]: r for r in connections._extension_connections()}
    assert rows["extension:dbclient"]["status"] == "connected"
    bad = rows["extension-failed:broken"]
    assert bad["status"] == "needs_attention"
    assert "/x/broken/extension.py" in bad["detail"] and "SyntaxError" in bad["detail"]
    assert "restart" in bad["fix"]
    assert bad["action"]["target"] == "extensions"


def test_a_failed_extension_load_is_recorded_and_served(tmp_path, monkeypatch):
    from backend.web import addons

    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "extension.py").write_text("raise RuntimeError('nope')\n")
    monkeypatch.setenv("MINDFLOCK_EXTENSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(addons, "_LOAD_FAILURES", [])
    assert addons.discover_extensions(ctx=None) == []
    (f,) = addons.load_failures()
    assert f["id"] == "broken" and "nope" in f["error"]
    assert f["file"].endswith("broken/extension.py")
    # GET /api/addons carries them (Settings → Extensions lists them).
    assert client.get("/api/addons").json()["failed"] == addons.load_failures()


# --------------------------------------------------------------------------- #
# System dependencies (git, tailscale) — kept ids
# --------------------------------------------------------------------------- #
def test_git_missing_is_calm_off(monkeypatch):
    # Git is optional: absent git is the calm gray state (never attention),
    # and its action points at Doctor (a system dep, no in-app credential).
    monkeypatch.setattr(
        doctor, "check_git", lambda: _chk("info", "not found (optional)")
    )
    c = connections._git_connection()
    assert c["required"] is False
    assert c["status"] == "off" and c["legacy_status"] == "not_connected"
    assert c["settings_screen"] == "doctor"
    assert c["action"] == {"label": "Open Doctor", "kind": "screen", "target": "doctor"}


def test_git_present_is_connected(monkeypatch):
    monkeypatch.setattr(doctor, "check_git", lambda: _chk("ok", "git version 2.43.0"))
    assert connections._git_connection()["status"] == "connected"


def test_system_dep_with_an_install_command_offers_copy(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_tailscale",
        lambda: types.SimpleNamespace(
            status="info", detail="not installed", fix="`brew install tailscale`"
        ),
    )
    c = connections._tailscale_connection()
    assert c["fix_command"] == "brew install tailscale"
    assert c["action"]["kind"] == "copy"


# --------------------------------------------------------------------------- #
# Cache / helpers / order
# --------------------------------------------------------------------------- #
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


def test_rows_are_ordered_by_kind_agents_first(quiet):
    _stub_providers(quiet, _provider(installed=False))
    ordered = connections.build_connections()
    assert ordered[0]["id"] == "agent"
    assert [c["kind"] for c in ordered][-1] == "system"


def test_github_docs_is_mindflocks_own_help_not_the_gh_homepage(quiet):
    # UX r5 settings #16: "Docs" on the GitHub card went to cli.github.com.
    quiet.setattr(connections, "_github_token_source", lambda: "")
    c = connections._github_connection()
    assert c["docs"] == connections.GITHUB_HELP_URL
    assert c["docs"].endswith("docs/web-ui.md#connect-github")
    assert "cli.github.com" not in c["docs"]
    # The anchor it links to exists.
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "web-ui.md").read_text()
    assert "\n### Connect GitHub\n" in doc
