"""Contract tests for the account-attach validation endpoints (C5):
POST /api/settings/test/{shortcut,github,agent}. Network/CLI probes are
monkeypatched — no real Shortcut/GitHub traffic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import doctor, providers
from backend.config import settings as S
from backend.doctor import Check
from backend.web.addons import settings as settings_addon


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    # A developer machine may carry real tokens — isolate the resolution chain.
    monkeypatch.delenv("SHORTCUT_API_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    S.invalidate()
    providers.rebuild_registry()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()
    providers.rebuild_registry()


class TestShortcutTest:
    def test_no_token_configured(self, client):
        r = client.post("/api/settings/test/shortcut", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "token" in body["error"]

    def test_request_token_validates_and_returns_member(self, client, monkeypatch):
        seen = {}

        async def fake(token):
            seen["token"] = token
            return {"id": "uuid-1", "name": "Ethan", "mention_name": "ethan"}, ""

        monkeypatch.setattr(settings_addon, "_fetch_shortcut_member", fake)
        body = client.post(
            "/api/settings/test/shortcut", json={"api_token": "sc_tok"}
        ).json()
        assert body == {
            "ok": True,
            "member_id": "uuid-1",
            "name": "Ethan",
            "mention_name": "ethan",
        }  # exact shape: never echoes the token
        assert seen["token"] == "sc_tok"

    def test_mask_sentinel_falls_back_to_stored_token(self, client, monkeypatch):
        from backend.config import settings as S

        S.set_ticketing_sources(
            [{"id": "sc", "provider": "shortcut", "api_token": "sc_stored"}]
        )
        seen = {}

        async def fake(token):
            seen["token"] = token
            return {"id": "m-9", "profile": {"name": "P"}}, ""

        monkeypatch.setattr(settings_addon, "_fetch_shortcut_member", fake)
        body = client.post(
            "/api/settings/test/shortcut", json={"api_token": "•••set"}
        ).json()
        assert body["ok"] is True and body["member_id"] == "m-9"
        assert seen["token"] == "sc_stored"

    def test_rejected_token_reports_error(self, client, monkeypatch):
        async def fake(token):
            return None, "Shortcut rejected the token (HTTP 401)"

        monkeypatch.setattr(settings_addon, "_fetch_shortcut_member", fake)
        body = client.post(
            "/api/settings/test/shortcut", json={"api_token": "bad"}
        ).json()
        assert body["ok"] is False and "401" in body["error"]


class TestGithubTest:
    @pytest.fixture()
    def gh(self, monkeypatch):
        def set_status(installed=True, authenticated=False, detail="d"):
            monkeypatch.setattr(
                settings_addon,
                "_gh_cli_status",
                lambda: (installed, authenticated, detail),
            )

        set_status()
        return set_status

    def test_settings_token_wins(self, client, gh):
        client.post("/api/settings", json={"github": {"token": "ghp_secret"}})
        body = client.post("/api/settings/test/github").json()
        assert body["ok"] is True and body["token_source"] == "settings"
        assert "ghp_secret" not in str(body)

    def test_env_token_source_reported(self, client, gh, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghp_env")
        body = client.post("/api/settings/test/github").json()
        assert body["token_source"] == "env:GH_TOKEN"
        assert "ghp_env" not in str(body)

    def test_gh_cli_fallback(self, client, gh):
        gh(installed=True, authenticated=True)
        body = client.post("/api/settings/test/github").json()
        assert body["ok"] is True
        assert body["token_source"] == "gh-cli"
        assert body["gh_authenticated"] is True

    def test_nothing_available(self, client, gh):
        body = client.post("/api/settings/test/github").json()
        assert body["ok"] is False
        assert body["token_source"] == "none"
        assert body["gh_installed"] is True and body["gh_authenticated"] is False


class TestAgentTest:
    def test_ok_when_cli_and_auth_pass(self, client, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "check_agent_cli",
            lambda: Check("agent-cli", "agent CLI (claude)", "ok", "/usr/bin/claude"),
        )
        monkeypatch.setattr(
            doctor,
            "check_agent_auth",
            lambda: Check("agent-auth", "agent auth (claude)", "ok", "login found"),
        )
        body = client.post("/api/settings/test/agent").json()
        assert body["ok"] is True
        assert body["cli"]["status"] == "ok"
        assert body["auth"]["detail"] == "login found"

    def test_auth_warn_means_not_ready(self, client, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "check_agent_cli",
            lambda: Check("agent-cli", "agent CLI (claude)", "ok", "/usr/bin/claude"),
        )
        monkeypatch.setattr(
            doctor,
            "check_agent_auth",
            lambda: Check(
                "agent-auth",
                "agent auth (claude)",
                "warn",
                "no sign of a login",
                "run `claude` once to log in",
            ),
        )
        body = client.post("/api/settings/test/agent").json()
        assert body["ok"] is False
        assert "run `claude` once" in body["auth"]["fix"]

    def test_info_auth_probe_counts_as_ok(self, client, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "check_agent_cli",
            lambda: Check("agent-cli", "agent CLI (aider)", "ok", "/usr/bin/aider"),
        )
        monkeypatch.setattr(
            doctor,
            "check_agent_auth",
            lambda: Check("agent-auth", "agent auth (aider)", "info", "no probe"),
        )
        assert client.post("/api/settings/test/agent").json()["ok"] is True
