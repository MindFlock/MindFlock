"""The Sky's routes (``backend/web/routes/sky.py``): the landing strip's
data and the sticky notes, over a TestClient with the flights store in tmp."""

import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import landing as L
from backend.web.core import sky_notes as N

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    L.forget()
    yield
    L.forget()


def test_landing_route_answers_the_core(monkeypatch, tmp_path):
    seen = {}

    async def fake(instances, flights=None, now=None):
        seen["instances"] = instances
        return {
            "repos": [
                {
                    "root": "/r",
                    "name": "r",
                    "branch": "main",
                    "head": "abc",
                    "checks": "green",
                    "last_merge_at": 1.0,
                    "landed": [],
                }
            ],
            "at": 2.0,
        }

    monkeypatch.setattr(L, "landing", fake)
    monkeypatch.setattr(
        server.ENGINE, "instances", {"sess": SimpleNamespace(Path="/r")}
    )
    r = client.get("/api/sky/landing")
    assert r.status_code == 200
    assert r.json()["repos"][0]["branch"] == "main"
    assert "sess" in seen["instances"]


def test_landing_route_with_no_sessions():
    r = client.get("/api/sky/landing")
    assert r.status_code == 200
    assert r.json()["repos"] == []


def test_notes_roundtrip(tmp_path):
    assert client.get("/api/sky/notes").json() == {"notes": []}
    body = {
        "notes": [
            {
                "id": "n1",
                "text": "hello",
                "x": 10.6,
                "y": -4,
                "w": 50,
                "h": 5000,
                "line": "fix-login",
                "updated": 7,
            },
            {"text": "no id", "x": "junk"},
            "junk",
            {"id": "n1", "text": "dup id"},
        ]
    }
    r = client.put("/api/sky/notes", json=body)
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert len(notes) == 3
    assert notes[0] == {
        "id": "n1",
        "text": "hello",
        "x": 11,
        "y": -4,
        "w": 120,
        "h": 800,
        "line": "fix-login",
        "updated": 7.0,
    }
    assert (
        notes[1]["id"].startswith("n_")
        and notes[1]["x"] == 0
        and notes[1]["w"] == 200
        and notes[1]["h"] == 120
    )
    assert notes[2]["id"] != "n1"
    # Stored under the flights dir, and read back the same.
    path = os.path.join(str(tmp_path / "flights"), N.NOTES_FILE)
    assert json.load(open(path))["notes"] == notes
    assert client.get("/api/sky/notes").json()["notes"] == notes
    # Replaced whole.
    assert client.put("/api/sky/notes", json={"notes": []}).json() == {"notes": []}
    assert client.get("/api/sky/notes").json() == {"notes": []}


def test_notes_refuse_a_non_list():
    r = client.put("/api/sky/notes", json={"notes": {"id": "x"}})
    assert r.status_code == 400 and r.json()["error"] == "bad_notes"
    assert client.put("/api/sky/notes", json={}).status_code == 400


def test_note_text_is_capped():
    long = "x" * (N.MAX_TEXT + 50)
    r = client.put("/api/sky/notes", json={"notes": [{"id": "a", "text": long}]})
    assert len(r.json()["notes"][0]["text"]) == N.MAX_TEXT
