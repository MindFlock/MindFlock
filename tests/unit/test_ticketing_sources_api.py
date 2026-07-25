"""Contract tests for the ticketing-sources CRUD endpoints (multi-source)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S

_MASK = "•••set"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    S.invalidate()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()


def test_empty_sources(client):
    assert client.get("/api/settings/ticketing/sources").json()["sources"] == []


def test_put_multiple_sources_including_same_provider(client):
    body = {
        "sources": [
            {
                "id": "sc",
                "provider": "shortcut",
                "api_token": "sc_tok",
                "member_id": "m1",
            },
            {
                "id": "jira",
                "provider": "jira",
                "base_url": "https://a",
                "email": "a@a",
                "api_token": "t1",
            },
            {
                "id": "jira-2",
                "provider": "jira",
                "base_url": "https://b",
                "email": "b@b",
                "api_token": "t2",
            },
        ]
    }
    r = client.put("/api/settings/ticketing/sources", json=body).json()
    got = {s["id"]: s for s in r["sources"]}
    assert set(got) == {"sc", "jira", "jira-2"}
    # Secrets masked on read, never echoed.
    assert got["jira"]["api_token"] == _MASK
    assert got["jira-2"]["api_token"] == _MASK
    assert got["jira"]["base_url"] == "https://a"
    assert got["jira-2"]["base_url"] == "https://b"


def test_blank_secret_keeps_stored_token(client):
    client.put(
        "/api/settings/ticketing/sources",
        json={
            "sources": [
                {
                    "id": "jira",
                    "provider": "jira",
                    "base_url": "https://a",
                    "email": "a@a",
                    "api_token": "secret1",
                },
            ]
        },
    )
    # Re-save with a masked/blank token (as the UI does) and a changed field.
    client.put(
        "/api/settings/ticketing/sources",
        json={
            "sources": [
                {
                    "id": "jira",
                    "provider": "jira",
                    "base_url": "https://a2",
                    "email": "a@a",
                    "api_token": _MASK,
                },
            ]
        },
    )
    # The stored token must survive (verified via the resolved pipeline config).
    # A global repo is required to resolve the config (no config.toml in CI).
    S.update_settings(repository={"url": "git@github.com:org/repo.git"})
    S.invalidate()
    from backend.ticket_ingestion.config import load_config

    cfg = load_config()  # layered: reads the settings store
    src = cfg.ticketing_sources[0]
    assert src.api_token == "secret1"
    assert src.base_url == "https://a2"


def test_blank_provider_entries_dropped(client):
    r = client.put(
        "/api/settings/ticketing/sources",
        json={
            "sources": [
                {"id": "x", "provider": ""},
                {
                    "id": "jira",
                    "provider": "jira",
                    "base_url": "https://a",
                    "email": "a@a",
                    "api_token": "t",
                },
            ]
        },
    ).json()
    assert [s["id"] for s in r["sources"]] == ["jira"]


def test_provider_catalog_served(client):
    provs = client.get("/api/settings/providers/ticketing").json()["providers"]
    assert {p["id"] for p in provs} == {
        "shortcut",
        "jira",
        "linear",
        "github_issues",
        "asana",
    }
