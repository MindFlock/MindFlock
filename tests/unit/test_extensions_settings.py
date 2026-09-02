"""The ``extensions.disabled`` settings group: store + API round-trips.

The Extensions screen persists exactly ONE thing — the opt-out list — through
the generic field-merge (``update_settings`` / ``POST /api/settings``): a list
value replaces the stored list wholesale, so unlike ticketing's
list-of-records no dedicated setter is needed. These tests pin that down, plus
the group's emit-on-deviation shape and its effect on the ``/api/addons``
``enabled`` flag.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    yield
    S.invalidate()


class TestStoreRoundTrip:
    def test_update_settings_replaces_the_list(self, store):
        S.update_settings(extensions={"disabled": ["a", "b"]})
        assert S.load_settings().extensions.disabled == ["a", "b"]
        # Wholesale replacement, not a merge.
        S.update_settings(extensions={"disabled": ["c"]})
        assert S.load_settings().extensions.disabled == ["c"]

    def test_empty_list_clears_the_group(self, store):
        S.update_settings(extensions={"disabled": ["a"]})
        S.update_settings(extensions={"disabled": []})
        assert S.load_settings().extensions.disabled == []
        # Emit-on-deviation: the default state writes no group at all.
        assert "extensions" not in S.load_settings().to_dict()

    def test_survives_an_unrelated_group_save(self, store):
        S.update_settings(extensions={"disabled": ["a"]})
        S.update_settings(repository={"url": "git@x:me/r.git"})
        assert S.load_settings().extensions.disabled == ["a"]

    def test_from_dict_is_tolerant(self):
        # Comma string (what a text input sends) parses like every list field…
        s = S.Settings.from_dict({"extensions": {"disabled": "a, b"}})
        assert s.extensions.disabled == ["a", "b"]
        # …and garbage yields the empty default rather than raising.
        assert (
            S.Settings.from_dict({"extensions": {"disabled": 7}}).extensions.disabled
            == []
        )
        assert S.Settings.from_dict({"extensions": "nope"}).extensions.disabled == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()


class TestSettingsApiRoundTrip:
    def test_post_persists_and_echoes(self, client):
        r = client.post(
            "/api/settings", json={"extensions": {"disabled": ["dbclient"]}}
        )
        assert r.status_code == 200
        assert r.json()["settings"]["extensions"]["disabled"] == ["dbclient"]
        s = client.get("/api/settings").json()["settings"]
        assert s["extensions"]["disabled"] == ["dbclient"]
        assert S.load_settings().extensions.disabled == ["dbclient"]

    def test_post_replaces_then_clears(self, client):
        client.post("/api/settings", json={"extensions": {"disabled": ["a", "b"]}})
        client.post("/api/settings", json={"extensions": {"disabled": ["b"]}})
        s = client.get("/api/settings").json()["settings"]
        assert s["extensions"]["disabled"] == ["b"]
        client.post("/api/settings", json={"extensions": {"disabled": []}})
        s = client.get("/api/settings").json()["settings"]
        assert s.get("extensions", {}).get("disabled", []) == []
        assert S.load_settings().extensions.disabled == []

    def test_flips_the_addons_manifest_enabled_flag(self, client):
        # The consumer of this group: manifest() reads the store fresh, so a
        # toggle lands on the very next /api/addons fetch without a restart.
        client.post("/api/settings", json={"extensions": {"disabled": ["notify"]}})
        addons = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
        assert addons["notify"]["enabled"] is False
        assert addons["mindflock"]["enabled"] is True
        client.post("/api/settings", json={"extensions": {"disabled": []}})
        addons = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
        assert addons["notify"]["enabled"] is True
