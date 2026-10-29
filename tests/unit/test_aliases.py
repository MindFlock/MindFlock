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


def test_post_delta_announces_the_rename():
    # Every open page re-reads the map on this: the owner's other browsers
    # and a guest's see the new name at once.
    seen = []
    unsub = server._events.BUS.subscribe(
        lambda e: seen.append(e) if e["event"] == "session.renamed" else None
    )
    try:
        client.post("/api/aliases", json={"title": "s1", "alias": "renamed"})
        client.post("/api/aliases", json={"title": "s1", "alias": ""})
        # The boot-time seed is not a rename.
        client.post("/api/aliases", json={"aliases": {"other": "o"}})
    finally:
        unsub()
    assert [(e["session"], e["new"], e["data"]) for e in seen] == [
        ("s1", "renamed", {"name": "renamed"}),
        ("s1", "", {"name": ""}),
    ]


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
    assert "/api/aliases" in server.app.openapi()["paths"]


# --------------------------------------------------------------------------- #
# A rename is the session's name everywhere, so names are unique
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(self, title, branch=""):
        self.Title = title
        self.Branch = branch


@pytest.fixture
def two_sessions(monkeypatch):
    insts = {
        "alpha": _Inst("alpha"),
        "sc-12345": _Inst("sc-12345", "feature/sc-12345/dark-mode"),
    }
    # setattr, never setitem: the real engine may hold the owner's sessions.
    monkeypatch.setattr(server.ENGINE, "instances", insts)
    return insts


def test_owner_of_matches_titles_renames_and_labels():
    branches = {"alpha": "", "sc-12345": "feature/sc-12345/dark-mode"}
    aliases.set_alias("alpha", "Login fix")
    assert aliases.owner_of("Login fix", branches) == "alpha"
    assert aliases.owner_of("alpha", branches) == "alpha"
    # The label the rail shows an unrenamed ticket session is its name too.
    assert aliases.owner_of("(tix) dark-mode/sc-12345", branches) == "sc-12345"
    # A session never collides with itself, and the match is exact.
    assert aliases.owner_of("Login fix", branches, exclude="alpha") == ""
    assert aliases.owner_of("login fix", branches) == ""
    assert aliases.owner_of("", branches) == ""


def test_rename_to_another_sessions_name_is_409(two_sessions):
    client.post("/api/aliases", json={"title": "alpha", "alias": "Login fix"})
    r = client.post("/api/aliases", json={"title": "sc-12345", "alias": "Login fix"})
    assert r.status_code == 409
    assert r.json()["session"] == "alpha"
    assert aliases.label_for("sc-12345") == "sc-12345"
    # Another session's raw title is taken just the same.
    assert (
        client.post(
            "/api/aliases", json={"title": "sc-12345", "alias": "alpha"}
        ).status_code
        == 409
    )
    # Renaming a session to its own current name is not a collision.
    assert (
        client.post(
            "/api/aliases", json={"title": "alpha", "alias": "Login fix"}
        ).status_code
        == 200
    )


def test_a_closed_sessions_old_rename_does_not_hold_the_name(two_sessions):
    aliases.set_alias("long-gone", "Login fix")  # not in ENGINE.instances
    r = client.post("/api/aliases", json={"title": "alpha", "alias": "Login fix"})
    assert r.status_code == 200


def test_new_session_may_not_take_a_renamed_sessions_name(two_sessions):
    aliases.set_alias("alpha", "Login fix")
    r = client.post("/api/instances", json={"title": "Login fix", "program": "bash"})
    assert r.status_code == 409
    assert "already named" in r.json()["error"]


def test_untitled_numbering_skips_a_renamed_name(two_sessions):
    aliases.set_alias("alpha", "untitled")
    assert server._free_untitled() == "untitled-2"


def test_instances_listing_carries_the_name(two_sessions):
    aliases.set_alias("alpha", "Login fix")
    rows = [
        {"title": "alpha"},
        {"title": "sc-12345", "branch": "feature/sc-12345/dark-mode"},
        {"title": "rig::alpha", "device": "rig", "name": "theirs"},
    ]
    aliases.overlay(rows)
    assert rows[0]["name"] == "Login fix"
    assert rows[1]["name"] == "(tix) dark-mode/sc-12345"
    # Another device's row keeps the name its own server gave it.
    assert rows[2]["name"] == "theirs"


def test_phone_renders_the_name_not_the_title():
    js = client.get("/mobile.js").text
    assert "name.textContent = inst.name || title;" in js
    assert "function nameOf(title)" in js


def test_a_template_run_named_pr_something_is_not_a_pr_session():
    """Round 5: a template called "pr-bot" starts "pr-bot-oct14" — plain
    title, never "(pr) bot-oct14". Only pipeline shapes split."""
    from backend.web.core import aliases

    assert aliases.session_label("pr-bot-oct14", "") == "pr-bot-oct14"
    assert aliases.session_label("pr-bot", "") == "pr-bot"
    assert aliases.session_label("pr-widgets-318", "") == "(pr) widgets-318"
    assert aliases.session_label("issue-widgets-42", "") == "(iss) widgets-42"
    assert aliases.session_label("pr-318-c-9001", "") == "(pr) 318-c-9001"
