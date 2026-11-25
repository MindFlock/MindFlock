"""Favorite sessions (:mod:`backend.web.core.favorites`): star, list open and
closed, follow a rename, never auto-closed."""

from __future__ import annotations

import json

import pytest

from backend.web.core import favorites as fav


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FAVORITES_FILE", str(tmp_path / "favorites.json"))
    fav._CACHE["sig"], fav._CACHE["data"] = None, None
    yield
    fav._CACHE["sig"], fav._CACHE["data"] = None, None


def test_star_unstar_and_persist(tmp_path):
    assert fav.is_favorite("api") is False
    assert fav.set_favorite("api", True, now=10.0) is True
    assert fav.is_favorite("api")
    with open(tmp_path / "favorites.json") as f:
        assert json.load(f) == {"favorites": {"api": {"at": 10.0}}}
    # Starring again keeps the first time.
    fav.set_favorite("api", True, now=99.0)
    assert fav.favorites()["api"]["at"] == 10.0
    assert fav.set_favorite("api", False) is False
    assert fav.favorites() == {}


def test_blank_titles_and_a_broken_file_read_as_none(tmp_path):
    assert fav.set_favorite("  ", True) is False
    with open(tmp_path / "favorites.json", "w") as f:
        f.write("{not json")
    fav._CACHE["sig"] = None
    assert fav.favorites() == {}
    assert fav.is_favorite("api") is False


def test_a_rename_moves_the_star_and_forget_drops_it():
    fav.set_favorite("old", True)
    fav.rename("old", "new")
    assert not fav.is_favorite("old") and fav.is_favorite("new")
    fav.forget("new")
    assert fav.favorites() == {}


def test_listing_says_open_closed_or_gone_with_the_newest_closed_entry():
    for t, at in (("live", 1.0), ("shut", 2.0), ("wiped", 3.0), ("lost", 4.0)):
        fav.set_favorite(t, True, now=at)
    closed = [
        {
            "id": "e1",
            "title": "shut",
            "branch": "old",
            "folder": "/a",
            "closed_at": "x",
        },
        {"id": "e2", "title": "shut", "branch": "b", "folder": "/b", "closed_at": "y"},
        {"id": "e3", "title": "wiped", "branch": "w", "folder": "", "wiped": True},
        {"id": "e4", "title": "someone-else"},
    ]
    rows = fav.listing({"live": {"branch": "main", "folder": "/live"}}, closed)
    by = {r["title"]: r for r in rows}
    assert [r["title"] for r in rows] == ["live", "shut", "wiped", "lost"]
    assert by["live"]["state"] == "open" and by["live"]["folder"] == "/live"
    assert by["shut"]["state"] == "closed" and by["shut"]["entry_id"] == "e2"
    assert by["shut"]["branch"] == "b"
    assert by["wiped"]["state"] == "gone" and by["wiped"]["wiped"] is True
    assert by["lost"]["state"] == "gone" and by["lost"]["entry_id"] == ""


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    return TestClient(server.app), server


def test_api_stars_a_live_or_closed_session_and_lists_it(client, monkeypatch):
    c, server = client
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "e9", "title": "closed-one", "branch": "b", "folder": "/f"}],
    )
    r = c.post("/api/favorites", json={"title": "closed-one"})
    assert r.status_code == 200 and r.json() == {
        "title": "closed-one",
        "favorite": True,
    }
    rows = c.get("/api/favorites").json()
    assert rows[0]["title"] == "closed-one" and rows[0]["state"] == "closed"
    assert rows[0]["entry_id"] == "e9"
    r = c.post("/api/favorites", json={"title": "closed-one", "on": False})
    assert r.json()["favorite"] is False
    assert c.get("/api/favorites").json() == []


def test_api_refuses_a_blank_or_unknown_title_but_unstars_anything(client, monkeypatch):
    c, server = client
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    assert c.post("/api/favorites", json={}).status_code == 400
    assert c.post("/api/favorites", json={"title": "never-seen-x9"}).status_code == 404
    fav.set_favorite("never-seen-x9", True)
    r = c.post("/api/favorites", json={"title": "never-seen-x9", "on": False})
    assert r.status_code == 200 and r.json()["favorite"] is False


# --------------------------------------------------------------------------- #
# auto-close leaves a favorite open
# --------------------------------------------------------------------------- #
def test_auto_close_keeps_a_favorite(monkeypatch):
    from backend.web.core import auto_close as ac
    from backend.web.core import sleep, team_runs
    from tests.unit import test_auto_close as t

    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 0)
    monkeypatch.setattr(team_runs, "owner_of_title", lambda title, exclude_run="": None)
    monkeypatch.setattr(ac, "_route_running", lambda title, parent, created=None: False)
    monkeypatch.setattr(ac, "work_accounted", lambda *a: (True, ac.W_MERGED))
    srv, w = t._world()
    assert t._judge(srv, w)[:2] == (ac.CLOSE, ac.W_MERGED)
    fav.set_favorite("w", True)
    assert t._judge(srv, w)[:2] == (ac.KEEP, ac.R_FAVORITE)


def test_the_row_says_favorite():
    from backend.web.core import snapshot

    class _I:
        Title = "api"

    assert snapshot._favorite_row(_I()) is False
    fav.set_favorite("api", True)
    assert snapshot._favorite_row(_I()) is True


def test_a_rename_through_the_registry_moves_the_star():
    from backend.web.core import rename_stores

    fav.set_favorite("before", True)
    rename_stores._favorites("before", "after")
    assert fav.is_favorite("after") and not fav.is_favorite("before")
