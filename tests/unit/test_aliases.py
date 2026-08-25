"""Session display aliases (tab renames) synced to the server.

Renames live in the browser (localStorage ``mf_aliases``); the server mirror
(:mod:`backend.web.core.aliases`) exists so server-originated notification text
(the ntfy channel) names sessions the way the sidebar does instead of by raw
title. Covers the store, the /api/aliases routes, the ntfy ``_fill`` lookup,
and the frontend wiring.
"""

import pytest
from starlette.testclient import TestClient

from backend.web import server
from backend.web.addons import notify as notify_addon
from backend.web.core import aliases

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated_alias_store(tmp_path, monkeypatch):
    """Fresh on-disk store per test; never the user's real ~/.mindflock."""
    monkeypatch.setattr(aliases, "_path", lambda: str(tmp_path / "aliases.json"))
    monkeypatch.setattr(aliases, "_ALIASES", None)
    yield
    monkeypatch.setattr(aliases, "_ALIASES", None)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_label_falls_back_to_raw_title():
    assert aliases.label_for("shortcut-21129") == "shortcut-21129"
    assert aliases.label_for("") == ""


def test_set_alias_roundtrips_and_persists():
    aliases.set_alias("shortcut-21129", "(tix) rebuild-scans")
    assert aliases.label_for("shortcut-21129") == "(tix) rebuild-scans"
    # Survives a "restart" (the in-memory cache dropped, reloaded from disk).
    aliases._ALIASES = None
    assert aliases.label_for("shortcut-21129") == "(tix) rebuild-scans"


def test_clearing_alias_restores_raw_title():
    aliases.set_alias("t", "renamed")
    aliases.set_alias("t", "")
    assert aliases.label_for("t") == "t"


def test_merge_sets_but_never_deletes():
    aliases.set_alias("kept", "kept-label")
    aliases.merge({"new": "new-label", "kept": ""})  # empty value = no-op
    assert aliases.label_for("new") == "new-label"
    assert aliases.label_for("kept") == "kept-label"


def test_drop_forgets_deleted_session():
    aliases.set_alias("gone", "label")
    aliases.drop("gone")
    assert aliases.label_for("gone") == "gone"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_post_delta_and_get():
    r = client.post("/api/aliases", json={"title": "s1", "alias": "renamed"})
    assert r.status_code == 200
    assert r.json()["aliases"] == {"s1": "renamed"}
    assert client.get("/api/aliases").json()["aliases"] == {"s1": "renamed"}
    # Empty alias clears.
    client.post("/api/aliases", json={"title": "s1", "alias": ""})
    assert client.get("/api/aliases").json()["aliases"] == {}


def test_post_bulk_seed_merges_only():
    client.post("/api/aliases", json={"title": "mine", "alias": "mine-label"})
    r = client.post("/api/aliases", json={"aliases": {"other": "other-label"}})
    assert r.json()["aliases"] == {"mine": "mine-label", "other": "other-label"}


def test_post_without_title_is_400():
    assert client.post("/api/aliases", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# The ntfy channel formats through the alias
# --------------------------------------------------------------------------- #
def test_ntfy_fill_uses_alias_with_raw_fallback():
    aliases.set_alias("shortcut-21129", "(tix) rebuild-scans")
    env = {"session": "shortcut-21129"}
    assert notify_addon._fill("{session} is idle", env) == "(tix) rebuild-scans is idle"
    assert notify_addon._fill("{session} is idle", {"session": "plain"}) == (
        "plain is idle"
    )
    assert notify_addon._fill("{session} is idle", {}) == "session is idle"


# --------------------------------------------------------------------------- #
# ...and by the label the rail shows when nobody renamed it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title,branch,expected",
    [
        # The three examples the TypeScript original carries in its doc comment.
        ("sc-12345", "feature/sc-12345/add-dark-mode", "(tix) add-dark-mode/sc-12345"),
        ("pr-app-42", "fix/login-crash", "(pr) login-crash/app-42"),
        ("issue-app-77", "feature/issue-app-77/cant-open", "(iss) cant-open/app-77"),
        # A hand-made session has nothing to reformat...
        ("my-refactor", "my-refactor", "my-refactor"),
        # ...and neither has a ticket session with no branch to read a name from.
        ("sc-12345", "", "sc-12345"),
        # A branch that just restates the slug adds nothing worth the width.
        ("pr-app-42", "feature/pr-app-42/app-42", "(pr) app-42"),
    ],
)
def test_session_label_is_the_port_the_sidebar_renders(title, branch, expected):
    """A PORT, and it has to stay one: `frontend/src/lib/sessionLabel.ts` is the
    original, and a push that names a window differently from the rail describes
    a session the reader cannot find."""
    assert aliases.session_label(title, branch) == expected


def test_display_name_prefers_the_rename_over_the_label():
    aliases.set_alias("sc-12345", "dark mode")
    assert (
        aliases.display_name("sc-12345", "feature/sc-12345/add-dark-mode")
        == "dark mode"
    )


def test_ntfy_fill_names_an_unrenamed_ticket_session_the_way_the_rail_does(
    monkeypatch,
):
    """The half that used to be missing. A ticket session is titled by its
    provider slug — "shortcut-21431" — while every surface in the app calls it
    "(tix) social-scan-noise/shortcut-21431"."""
    monkeypatch.setattr(
        notify_addon._events,
        "sessions_snapshot",
        lambda: [
            {
                "title": "shortcut-21431",
                "branch": "feature/shortcut-21431/social-scan-noise",
            }
        ],
    )
    assert notify_addon._fill("{session} is idle", {"session": "shortcut-21431"}) == (
        "(tix) social-scan-noise/shortcut-21431 is idle"
    )


def test_ntfy_fill_falls_back_to_the_title_for_a_session_it_cannot_see(monkeypatch):
    """A session the poll has not reached yet, or one on a device this node only
    proxies: naming it by its title is what happened before this looked at all."""
    monkeypatch.setattr(notify_addon._events, "sessions_snapshot", lambda: [])
    assert notify_addon._fill("{session} is idle", {"session": "shortcut-21431"}) == (
        "shortcut-21431 is idle"
    )


def test_ntfy_fill_survives_an_unreadable_snapshot(monkeypatch):
    """Naming must never break a push."""

    def boom():
        raise RuntimeError("no snapshot")

    monkeypatch.setattr(notify_addon._events, "sessions_snapshot", boom)
    assert notify_addon._fill("{session} is idle", {"session": "plain"}) == (
        "plain is idle"
    )


# --------------------------------------------------------------------------- #
# Frontend wiring
# --------------------------------------------------------------------------- #
def test_browser_notify_channel_reads_renames():
    notify = client.get("/addons/notify.js").text
    assert "mf_aliases" in notify  # the SPA's rename store
    assert "sessionLabel" in notify


def test_browser_notify_channel_asks_the_app_what_the_window_is_called():
    """The desktop notification named its session by the raw title off the
    envelope while the window it was about showed a rename or a pipeline label.
    It now asks the SPA's own resolver (`window.mindflock.displayName`), so the
    two cannot drift — the alias read stays as the fallback for a page where the
    bundle has not published the bridge yet."""
    notify = client.get("/addons/notify.js").text
    assert "mindflock.displayName" in notify or "displayName" in notify


def test_spa_syncs_renames_to_server():
    js = client.get("/app.js").text
    assert "/api/aliases" in js  # setAlias delta + boot-time bulk seed
