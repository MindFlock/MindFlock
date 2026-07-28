"""Session Templates addon: CRUD store + manifest.

Locks that the addon self-registers on the generic slot path and that its
JSON store round-trips (upsert-by-name, delete), with launching left to the
existing POST /api/instances (not re-implemented here).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import templates

client = TestClient(server.app)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Point the template store at an isolated tmp file for the whole request."""
    path = tmp_path / "session_templates.json"
    monkeypatch.setenv("MINDFLOCK_TEMPLATES_FILE", str(path))
    return path


# --------------------------------------------------------------------------- #
# Registration / manifest
# --------------------------------------------------------------------------- #
def test_templates_registered_on_generic_slot_path():
    data = client.get("/api/addons").json()
    tpl = next(a for a in data["addons"] if a["id"] == "templates")
    fe = tpl["frontend"][0]
    # No sidebar bar: templates are surfaced from the + New dialog. slots.js
    # still imports the module (it keys on `module`, not `where`).
    assert fe["where"] == "dialog"
    assert fe["module"] == "/addons/templates.js"
    assert fe["builtin_ui"] is False


# --------------------------------------------------------------------------- #
# CRUD over the store
# --------------------------------------------------------------------------- #
def test_create_list_update_delete(store):
    assert client.get("/api/templates").json()["templates"] == []

    # create
    r = client.post(
        "/api/templates",
        json={
            "name": "fix-tests",
            "program": "claude",
            "prompt": "fix the failing tests",
            "provisioned": True,
            "workspace_strategy": "clone",
        },
    )
    assert r.status_code == 200
    saved = r.json()["template"]
    assert saved["name"] == "fix-tests" and saved["provisioned"] is True
    assert saved["workspace_strategy"] == "clone"

    listed = client.get("/api/templates").json()["templates"]
    assert len(listed) == 1 and listed[0]["prompt"] == "fix the failing tests"

    # upsert by name (case-insensitive) replaces, does not duplicate
    r = client.post(
        "/api/templates", json={"name": "Fix-Tests", "prompt": "new prompt"}
    )
    assert r.status_code == 200
    listed = r.json()["templates"]
    assert len(listed) == 1
    assert listed[0]["prompt"] == "new prompt"
    assert listed[0]["provisioned"] is False  # a fresh body resets unspecified fields

    # delete
    r = client.delete("/api/templates/fix-tests")
    assert r.json()["deleted"] is True
    assert client.get("/api/templates").json()["templates"] == []
    # deleting a missing one is a false, not an error
    assert client.delete("/api/templates/nope").json()["deleted"] is False


def test_validation(store):
    assert client.post("/api/templates", json={"name": "  "}).status_code == 400
    assert (
        client.post(
            "/api/templates", json={"name": "x", "workspace_strategy": "bogus"}
        ).status_code
        == 400
    )


def test_field_size_backstops(store):
    assert client.post("/api/templates", json={"name": "x" * 101}).status_code == 400
    assert (
        client.post(
            "/api/templates", json={"name": "ok", "prompt": "p" * 20001}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/templates", json={"name": "ok", "program": "p" * 1001}
        ).status_code
        == 400
    )
    # a normal, sensibly-sized template still saves
    assert (
        client.post(
            "/api/templates", json={"name": "ok", "prompt": "do the thing"}
        ).status_code
        == 200
    )


def test_clean_drops_unknown_and_coerces(store):
    saved = templates.save_template(
        {"name": " t1 ", "program": " claude ", "junk": "ignored", "provisioned": 1}
    )
    assert saved["name"] == "t1"  # trimmed
    assert saved["program"] == "claude"  # trimmed
    assert "junk" not in saved  # unknown dropped
    assert saved["provisioned"] is True  # coerced to bool
    assert saved["workspace_strategy"] == "worktree"  # invalid/missing -> default
