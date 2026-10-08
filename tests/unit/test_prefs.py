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


def test_switching_run_here_reconciles_the_pipeline(client):
    """github.run_here gates both GitHub halves, so flipping it in Settings
    must reach the pipeline the way the on/off switches do."""
    from backend.web.core import events

    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    try:
        client.post("/api/settings", json={"github": {"run_here": False}})
        client.post("/api/settings", json={"github": {"run_here": False}})  # same
    finally:
        unsubscribe()
    toggles = [e for e in seen if e["event"].endswith("settings.github_toggled")]
    assert len(toggles) == 1
    store.invalidate()
    assert store.load_settings().github.run_here is False
