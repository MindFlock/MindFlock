"""Settings sync across the user's devices (:mod:`backend.web.core.settings_sync`).

Two devices in one process: each has its own settings dir, and "which device
am I" is switched around every call — the fake ``remote.get_json`` serves the
OTHER device's real :func:`export`, so merge/enable/sync_once run against the
real code on both ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web import server
from backend.web.core import remote, settings_sync

TOKEN = "sync-token-abc123"


class _Fleet:
    def __init__(self, tmp_path, monkeypatch):
        self.mp = monkeypatch
        self.files = {k: tmp_path / k / "settings.json" for k in ("laptop", "rig")}
        for f in self.files.values():
            f.parent.mkdir(parents=True)
        self.me = "laptop"
        self.secrets_ok = True
        monkeypatch.setattr(
            remote, "self_identity", lambda: {"key": self.me, "host": self.me}
        )
        monkeypatch.setattr(
            remote,
            "connected_devices",
            lambda: [{"key": k, "host": k.title()} for k in self.files if k != self.me],
        )

        async def fake_get_json(dev, path, timeout=3.0):
            assert path == "/api/settings/sync/export"
            with self.on(dev["key"]):
                return 200, settings_sync.export(self.secrets_ok)

        monkeypatch.setattr(remote, "get_json", fake_get_json)
        self.use("laptop")

    def use(self, key):
        self.me = key
        self.mp.setenv("MINDFLOCK_SETTINGS_FILE", str(self.files[key]))
        store.invalidate()

    @contextlib.contextmanager
    def on(self, key):
        prev = self.me
        self.use(key)
        try:
            yield
        finally:
            self.use(prev)

    def get(self, group, field):
        store.invalidate()
        return getattr(getattr(store.load_settings(), group), field)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    settings_sync._peers.clear()
    yield _Fleet(tmp_path, monkeypatch)
    store.invalidate()


def _clock(monkeypatch, start=1000.0):
    t = {"now": start}
    monkeypatch.setattr(settings_sync.time, "time", lambda: t["now"])

    def tick(dt=10.0):
        t["now"] += dt

    return tick


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_every_settings_field_is_classified():
    """A new settings field must be put in SYNCED or LOCAL on purpose — an
    unclassified one would silently never sync (or, worse, sync a path)."""
    for group in dataclasses.fields(store.Settings):
        sub = getattr(store.Settings(), group.name)
        if not dataclasses.is_dataclass(sub):
            continue
        for f in dataclasses.fields(sub):
            synced = f.name in settings_sync.SYNCED.get(group.name, ())
            local = f.name in settings_sync.LOCAL.get(group.name, ())
            assert synced != local, "%s.%s must be in exactly one of SYNCED / LOCAL" % (
                group.name,
                f.name,
            )


def test_secret_paths_are_all_synced_paths():
    assert settings_sync.SECRET_PATHS <= set(settings_sync.paths())


# --------------------------------------------------------------------------- #
# joining — the older machine leads
# --------------------------------------------------------------------------- #
def test_join_from_another_device_adopts_its_shared_settings(fleet):
    with fleet.on("rig"):
        store.update_settings(
            ui={"accent": "teal"},
            github={"repos": ["me/app"], "token": "ghp_rig"},
            general={"auth_mode": "on", "serve_mode": "tailscale"},
        )
        asyncio.run(settings_sync.enable(""))  # the rig leads
    store.update_settings(ui={"accent": "red"}, general={"serve_mode": "local"})
    out = asyncio.run(settings_sync.enable("rig"))
    assert "ui.accent" in out["adopted"]
    assert fleet.get("ui", "accent") == "teal"
    assert fleet.get("github", "repos") == ["me/app"]
    assert fleet.get("github", "token") == "ghp_rig"
    # machine-specific fields stay put
    assert fleet.get("general", "serve_mode") == "local"
    assert fleet.get("general", "auth_mode") == ""


def test_join_from_a_device_not_yet_syncing_lets_it_win_later(fleet, monkeypatch):
    tick = _clock(monkeypatch)
    with fleet.on("rig"):
        store.update_settings(ui={"accent": "teal"})
    asyncio.run(settings_sync.enable("rig"))  # rig isn't syncing yet
    assert fleet.get("ui", "accent") == "teal"
    tick()
    with fleet.on("rig"):
        asyncio.run(settings_sync.enable(""))
        store.update_settings(ui={"accent": "gold"})
    tick()
    asyncio.run(settings_sync.sync_once())
    assert fleet.get("ui", "accent") == "gold"


def test_join_from_an_unconnected_device_is_refused(fleet):
    with pytest.raises(LookupError):
        asyncio.run(settings_sync.enable("nowhere"))
    assert settings_sync.enabled() is False


# --------------------------------------------------------------------------- #
# two-way, last edit wins
# --------------------------------------------------------------------------- #
def _both_on(fleet):
    with fleet.on("rig"):
        asyncio.run(settings_sync.enable(""))
    asyncio.run(settings_sync.enable("rig"))


def test_an_edit_on_either_side_reaches_the_other(fleet, monkeypatch):
    tick = _clock(monkeypatch)
    _both_on(fleet)
    tick()
    store.update_settings(ui={"accent": "red"})  # laptop edit
    with fleet.on("rig"):
        asyncio.run(settings_sync.sync_once())
        assert fleet.get("ui", "accent") == "red"
        tick()
        store.update_settings(github={"repos": ["me/other"]})  # rig edit
    asyncio.run(settings_sync.sync_once())
    assert fleet.get("github", "repos") == ["me/other"]
    assert fleet.get("ui", "accent") == "red"


def test_the_later_edit_wins_a_conflict(fleet, monkeypatch):
    tick = _clock(monkeypatch)
    _both_on(fleet)
    tick()
    with fleet.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    tick()
    store.update_settings(ui={"accent": "red"})  # later
    asyncio.run(settings_sync.sync_once())  # laptop pulls the older teal: ignored
    assert fleet.get("ui", "accent") == "red"
    with fleet.on("rig"):
        asyncio.run(settings_sync.sync_once())
        assert fleet.get("ui", "accent") == "red"


def test_clearing_a_field_syncs_as_a_clear(fleet, monkeypatch):
    tick = _clock(monkeypatch)
    store.update_settings(ui={"accent": "red"})
    with fleet.on("rig"):
        asyncio.run(settings_sync.enable("laptop"))
    asyncio.run(settings_sync.enable(""))
    with fleet.on("rig"):
        asyncio.run(settings_sync.enable("laptop"))
        assert fleet.get("ui", "accent") == "red"
    tick()
    store.update_settings(ui={"accent": ""})
    with fleet.on("rig"):
        asyncio.run(settings_sync.sync_once())
        assert fleet.get("ui", "accent") == ""


def test_adopting_is_not_mistaken_for_a_local_edit(fleet, monkeypatch):
    """After a pull, the next scan must not restamp the adopted value as this
    device's own (that would make every device 'win' in turn)."""
    tick = _clock(monkeypatch)
    _both_on(fleet)
    tick()
    with fleet.on("rig"):
        store.update_settings(general={"tailnet_trusted_logins": ["Me@Example.com"]})
    asyncio.run(settings_sync.sync_once())
    tick()
    assert settings_sync.scan_local() == []


def test_off_means_no_pulls(fleet):
    with fleet.on("rig"):
        asyncio.run(settings_sync.enable(""))
        store.update_settings(ui={"accent": "teal"})
    assert asyncio.run(settings_sync.sync_once()) == []
    assert fleet.get("ui", "accent") == ""


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def test_secrets_are_withheld_without_a_token(fleet):
    store.update_settings(github={"token": "ghp_secret", "repos": ["me/app"]})
    asyncio.run(settings_sync.enable(""))
    out = settings_sync.export(include_secrets=False)
    assert "github.token" not in out["values"]
    assert "ticketing.sources" not in out["values"]
    assert set(out["withheld"]) == set(settings_sync.SECRET_PATHS)
    assert out["values"]["github.repos"] == ["me/app"]


def test_a_withheld_secret_is_left_alone_on_the_puller(fleet, monkeypatch):
    tick = _clock(monkeypatch)
    _both_on(fleet)
    store.update_settings(github={"token": "ghp_laptop"})
    tick()
    with fleet.on("rig"):
        store.update_settings(github={"token": "ghp_rig"})
    fleet.secrets_ok = False
    asyncio.run(settings_sync.sync_once())
    assert fleet.get("github", "token") == "ghp_laptop"
    assert settings_sync.status()["devices"][0]["withheld"]


def test_export_route_gives_secrets_only_to_a_token_holder(fleet, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")  # gate OFF: anyone reaches the route
    store.update_settings(github={"token": "ghp_secret"})
    asyncio.run(settings_sync.enable(""))
    c = TestClient(server.app)
    anon = c.get("/api/settings/sync/export").json()
    assert "github.token" not in anon["values"]
    authed = c.get(
        "/api/settings/sync/export", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert authed["values"]["github.token"] == "ghp_secret"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
def test_settings_save_stamps_immediately(fleet, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    asyncio.run(settings_sync.enable(""))
    before = settings_sync._load()["stamps"]["ui.accent"]["h"]
    TestClient(server.app).post("/api/settings", json={"ui": {"accent": "teal"}})
    assert settings_sync._load()["stamps"]["ui.accent"]["h"] != before


def test_sync_toggle_route_and_remote_refusal(fleet, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    c = TestClient(server.app)
    r = c.post(
        "/api/settings/sync",
        json={"enabled": True},
        headers={"x-mindflock-remote": "rig"},
    )
    assert r.status_code == 403
    r = c.post("/api/settings/sync", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert c.get("/api/settings/sync").json()["devices"][0]["key"] == "rig"
    r = c.post("/api/settings/sync", json={"enabled": True, "from": "ghost"})
    assert r.status_code == 409
    r = c.post("/api/settings/sync", json={"enabled": False})
    assert r.json()["enabled"] is False
