"""UI preferences that follow the person (settings ``prefs`` + ``/api/prefs``),
and the terminal scroll speed's synced copy (``ui.scroll_speed``)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web import server
from backend.web.core import settings_sync
from tests.unit.test_settings_sync import Devices

FIELDS = [f.name for f in dataclasses.fields(store.PrefsSettings)]


# --------------------------------------------------------------------------- #
# the settings group
# --------------------------------------------------------------------------- #
def test_an_untouched_group_writes_nothing():
    assert store.PrefsSettings().to_dict() == {}
    assert "prefs" not in store.Settings().to_dict()


def test_every_prefs_field_syncs():
    assert sorted(settings_sync.SYNCED["prefs"]) == sorted(FIELDS)
    assert settings_sync.KEYED["prefs.prompt_presets"] == "name"


def test_round_trip_keeps_every_field():
    p = store.PrefsSettings(
        keymap={"keys": {"palette": "Ctrl+K"}, "chords": {}},
        prompt_presets=[{"name": "Ship", "prompt": "ship it"}],
        theme="light",
        diff_mode="split",
        diff_base="main",
        hidden_bars=[],
        bar_order=["prompts", "outbox"],
        reduce_motion=True,
        break_on=False,
        break_every=50,
        idle_flock=True,
        idle_after=120,
        hints=False,
    )
    d = store.Settings(prefs=p).to_dict()["prefs"]
    assert d["hidden_bars"] == []  # "none hidden" is a choice, not the default
    assert d["break_on"] is False and d["hints"] is False
    assert store.Settings.from_dict({"prefs": d}).prefs == p


def test_coercion_is_tolerant():
    p = store.PrefsSettings.from_dict(
        {
            "keymap": "not a dict",
            "prompt_presets": [
                {"name": "A", "prompt": "1"},
                {"name": "a", "prompt": "dupe"},  # a preset IS its name
                {"prompt": "nameless"},
                "junk",
            ],
            "theme": None,
            "hidden_bars": "not a list",
            "bar_order": "x, y",
            "reduce_motion": "yes",
            "break_every": "abc",
            "idle_after": "30",
        }
    )
    assert p.keymap == {}
    assert p.prompt_presets == [{"name": "A", "prompt": "1"}]
    assert p.theme == "" and p.hidden_bars is None
    assert p.bar_order == ["x", "y"]
    assert p.reduce_motion is True
    assert p.break_every is None and p.idle_after == 30


def test_scroll_speed_keeps_its_fraction():
    ui = store.UiSettings.from_dict({"scroll_speed": 0.3333})
    assert ui.scroll_speed == 0.3333
    assert store.UiSettings.from_dict({"scroll_speed": 2.0}).scroll_speed == 2
    assert isinstance(
        store.UiSettings.from_dict({"scroll_speed": "3"}).scroll_speed, int
    )
    assert store.UiSettings.from_dict({"scroll_speed": "nan"}).scroll_speed is None


# --------------------------------------------------------------------------- #
# /api/prefs
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    return TestClient(server.app)


def test_get_prefs_has_every_key_even_when_unset(client):
    body = client.get("/api/prefs").json()
    assert sorted(body) == sorted(FIELDS)
    assert body["keymap"] == {} and body["prompt_presets"] == []
    assert body["hidden_bars"] is None and body["theme"] == ""


def test_post_prefs_is_a_partial_update(client):
    r = client.post(
        "/api/prefs",
        json={"theme": "dark", "keymap": {"keys": {"a": "b"}}, "bogus": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] == "dark" and body["keymap"] == {"keys": {"a": "b"}}
    assert "bogus" not in body
    body = client.post("/api/prefs", json={"hints": False}).json()
    assert body["theme"] == "dark" and body["hints"] is False
    body = client.post("/api/prefs", json={"theme": None, "keymap": None}).json()
    assert body["theme"] == "" and body["keymap"] == {}
    store.invalidate()
    assert store.load_settings().prefs.hints is False


def test_a_prefs_save_is_stamped_for_sync(tmp_path, monkeypatch, client):
    devices = Devices(tmp_path, monkeypatch)
    asyncio.run(settings_sync.enable(""))
    client.post("/api/prefs", json={"prompt_presets": [{"name": "Go", "prompt": "x"}]})
    assert "prefs.prompt_presets#go" in settings_sync._load()["stamps"]
    with devices.on("rig"):
        asyncio.run(settings_sync.enable("laptop"))
        assert devices.get("prefs", "prompt_presets") == [{"name": "Go", "prompt": "x"}]


def test_prefs_sync_between_devices(tmp_path, monkeypatch):
    devices = Devices(tmp_path, monkeypatch)
    devices.all_on()
    store.update_settings(prefs={"keymap": {"keys": {"x": "Alt+X"}}, "theme": "light"})
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("prefs", "keymap") == {"keys": {"x": "Alt+X"}}
        assert devices.get("prefs", "theme") == "light"


# --------------------------------------------------------------------------- #
# the scroll speed travels as a setting
# --------------------------------------------------------------------------- #
def test_scroll_speed_route_also_writes_the_synced_setting(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "apply_scroll_speed", lambda speed: None)
    from backend.web.core import terminal

    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    r = client.post("/api/scroll-speed", json={"speed": 0.5})
    assert r.json() == {"speed": 0.6667}
    store.invalidate()
    assert store.load_settings().ui.scroll_speed == 0.6667
    assert terminal.load_scroll_speed() == 0.6667


# --------------------------------------------------------------------------- #
# other browsers on this server hear about a prefs save
# --------------------------------------------------------------------------- #
def test_a_prefs_save_tells_every_open_browser(client):
    """The desktop app and a browser tab keep separate localStorage: without
    an event the stale one's next whole-list save drops this write (and sync
    spreads the drop as a delete)."""
    from backend.web.core import events

    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    try:
        client.post(
            "/api/prefs",
            json={"prompt_presets": [{"name": "X", "prompt": "x"}], "theme": "dark"},
        )
        client.post("/api/prefs", json={"bogus": 1})  # nothing saved: no event
    finally:
        unsubscribe()
    synced = [e for e in seen if e["event"] == "settings.synced"]
    assert [e["data"] for e in synced] == [
        {"paths": ["prefs.prompt_presets", "prefs.theme"], "from": ""}
    ]


def test_moving_the_automation_reconciles_the_pipeline(client, monkeypatch):
    """github.automation_device gates both GitHub halves, so moving them to
    this device (Run here in Settings → Your devices) must reach the
    pipeline the way the on/off switches do."""
    import threading

    from backend.web.core import events, fleet, remote, settings_hooks

    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.05)
    monkeypatch.setattr(fleet, "in_fleet", lambda: True)
    monkeypatch.setattr(
        fleet, "live_members", lambda: {"laptop": {"host": "L"}, "mac": {"host": "M"}}
    )
    monkeypatch.setattr(remote, "self_identity", lambda: {"key": "mac", "host": "M"})
    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    try:
        client.post("/api/settings", json={"github": {"automation_device": "mac"}})
        client.post("/api/settings", json={"github": {"automation_device": "mac"}})
        threading.Event().wait(0.3)  # the debounced reconcile fires once
    finally:
        unsubscribe()
    toggles = [e for e in seen if e["event"].endswith("settings.github_toggled")]
    assert len(toggles) == 1  # moved once; the same choice again is no flip
    assert toggles[0]["data"]["changed"] == ["automation"]
    store.invalidate()
    assert store.load_settings().github.automation_device == "mac"


def test_map_tour_is_a_synced_bool(tmp_path):
    """The Map tab's tour, once seen on one device, stays seen on the others:
    it is a prefs field (settings sync carries it), not a per-browser flag."""
    from backend.config.settings import PrefsSettings
    from backend.web.core import settings_sync

    assert PrefsSettings.from_dict({"map_tour": True}).to_dict() == {"map_tour": True}
    assert PrefsSettings.from_dict({"map_tour": "nope"}).map_tour is None
    assert "map_tour" in settings_sync.SYNCED["prefs"]


def test_welcome_tour_is_a_synced_bool():
    """The welcome tour auto-opens once per person, not once per browser: seen
    on one device, it stays seen on the others."""
    from backend.config.settings import PrefsSettings
    from backend.web.core import settings_sync

    assert PrefsSettings.from_dict({"welcome_tour": True}).to_dict() == {
        "welcome_tour": True
    }
    assert PrefsSettings.from_dict({"welcome_tour": False}).to_dict() == {
        "welcome_tour": False
    }
    assert PrefsSettings.from_dict({"welcome_tour": "nope"}).welcome_tour is None
    assert "welcome_tour" not in PrefsSettings.from_dict({}).to_dict()
    assert "welcome_tour" in settings_sync.SYNCED["prefs"]
    assert "prefs.welcome_tour" in settings_sync.LABELS


def test_map_tour_false_survives_and_unset_is_omitted():
    from backend.config.settings import PrefsSettings

    assert PrefsSettings.from_dict({"map_tour": False}).to_dict() == {"map_tour": False}
    assert "map_tour" not in PrefsSettings.from_dict({}).to_dict()


def test_map_tour_over_the_prefs_route(client):
    assert client.get("/api/prefs").json()["map_tour"] is None
    assert client.post("/api/prefs", json={"map_tour": True}).json()["map_tour"] is True
    store.invalidate()
    assert store.load_settings().prefs.map_tour is True
    assert client.post("/api/prefs", json={"map_tour": None}).json()["map_tour"] is None
    store.invalidate()
    assert store.load_settings().prefs.map_tour is None


def test_map_tour_seen_on_one_device_is_seen_on_the_others(tmp_path, monkeypatch):
    devices = Devices(tmp_path, monkeypatch)
    devices.all_on()
    store.update_settings(prefs={"map_tour": True})
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("prefs", "map_tour") is True


def test_settings_advanced_is_a_synced_bool():
    """Settings' "Show advanced settings" follows the person to every device:
    a prefs field settings sync carries, unset (basic) until they flip it."""
    from backend.config.settings import PrefsSettings
    from backend.web.core import settings_sync

    on = PrefsSettings.from_dict({"settings_advanced": True})
    assert on.to_dict() == {"settings_advanced": True}
    off = PrefsSettings.from_dict({"settings_advanced": False})
    assert off.to_dict() == {"settings_advanced": False}
    assert PrefsSettings.from_dict({"settings_advanced": "x"}).settings_advanced is None
    assert "settings_advanced" not in PrefsSettings.from_dict({}).to_dict()
    assert "settings_advanced" in settings_sync.SYNCED["prefs"]


def test_settings_advanced_over_the_prefs_route(client):
    assert client.get("/api/prefs").json()["settings_advanced"] is None
    got = client.post("/api/prefs", json={"settings_advanced": True}).json()
    assert got["settings_advanced"] is True
    store.invalidate()
    assert store.load_settings().prefs.settings_advanced is True


# --------------------------------------------------------------------------- #
# pane_header (Customize → Pane header)
# --------------------------------------------------------------------------- #
def test_pane_header_is_a_synced_object_with_two_lists():
    choice = {"order": ["agent", "map", "copy"], "hidden": ["copy"]}
    p = store.PrefsSettings.from_dict({"pane_header": choice})
    assert p.pane_header == choice
    assert p.to_dict() == {"pane_header": choice}
    assert "pane_header" in settings_sync.SYNCED["prefs"]
    assert settings_sync.LABELS["prefs.pane_header"] == "Pane header items"


def test_pane_header_unset_or_malformed_is_never_customized():
    assert "pane_header" not in store.PrefsSettings.from_dict({}).to_dict()
    for bad in ("x", [], {"order": ["a"]}, {"order": "a", "hidden": []}, None):
        assert (
            store.PrefsSettings.from_dict({"pane_header": bad}).pane_header is None
        ), bad
    # Unknown ids (a newer build's) are kept, blanks dropped — the frontend
    # ignores ids it doesn't know (lib/paneHeader.resolvePaneHeader).
    p = store.PrefsSettings.from_dict(
        {"pane_header": {"order": ["agent", " ", "zap"], "hidden": []}}
    )
    assert p.pane_header == {"order": ["agent", "zap"], "hidden": []}


def test_pane_header_over_the_prefs_route(client):
    assert client.get("/api/prefs").json()["pane_header"] is None
    choice = {"order": ["agent", "shell"], "hidden": ["shell"]}
    assert (
        client.post("/api/prefs", json={"pane_header": choice}).json()["pane_header"]
        == choice
    )
    store.invalidate()
    assert store.load_settings().prefs.pane_header == choice
    assert (
        client.post("/api/prefs", json={"pane_header": None}).json()["pane_header"]
        is None
    )
    store.invalidate()
    assert store.load_settings().prefs.pane_header is None


def test_pane_header_chosen_on_one_device_shows_on_the_others(tmp_path, monkeypatch):
    devices = Devices(tmp_path, monkeypatch)
    devices.all_on()
    choice = {"order": ["agent", "map"], "hidden": []}
    store.update_settings(prefs={"pane_header": choice})
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("prefs", "pane_header") == choice


def test_tours_seen_is_a_synced_string_list():
    """Round 5: the per-menu coach tours are marked seen on EVERY device
    (the first-run flags ban), so the list rides prefs sync like bar_order."""
    from backend.config.settings import PrefsSettings
    from backend.web.core import settings_sync

    assert "tours_seen" in settings_sync.SYNCED["prefs"]
    assert PrefsSettings.from_dict({}).to_dict().get("tours_seen") is None
    assert PrefsSettings.from_dict({"tours_seen": ["new", "intake"]}).to_dict() == {
        "tours_seen": ["new", "intake"]
    }
