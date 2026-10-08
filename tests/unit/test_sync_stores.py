"""The stores settings sync carries beside settings.json
(:mod:`backend.web.core.sync_stores`): session templates, red zones and custom
agent providers — between simulated devices, through the real store APIs."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess

import pytest

from backend import providers
from backend.config import red_zones
from backend.web import server
from backend.web.addons import templates
from backend.web.core import red_zone_monitor, settings_sync, sync_stores
from tests.unit.test_settings_sync import Devices, _clock

REPO = "github.com/me/app"


@pytest.fixture
def devices(tmp_path, monkeypatch):
    yield Devices(tmp_path, monkeypatch)


@pytest.fixture
def clock(devices, monkeypatch):
    return _clock(monkeypatch, devices)


def _git_repo(path, origin):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
    )
    return str(path)


def test_the_built_in_stores_are_registered():
    assert {"templates", "red_zones", "providers"} <= set(settings_sync._stores())


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def test_templates_sync_with_the_repo_mapped_to_each_checkout(devices, clock, tmp_path):
    from backend.config import settings as store

    mine = _git_repo(tmp_path / "a" / "app", "git@github.com:me/app.git")
    theirs = _git_repo(tmp_path / "b" / "app", "https://github.com/me/app")
    templates.save_template({"name": "Fix CI", "repo_path": mine, "prompt": "go"})
    with devices.on("rig"):
        store.update_settings(general={"last_repo_path": theirs})
    devices.all_on()
    assert settings_sync.export()["values"]["store:templates#fix ci"]["repo_path"] == (
        "git@github.com:me/app.git"
    )
    with devices.on("rig"):
        (tpl,) = templates.list_templates()
        assert tpl["name"] == "Fix CI" and tpl["prompt"] == "go"
        assert tpl["repo_path"] == theirs
        clock.tick()
        assert settings_sync.scan_local() == []  # its own path isn't an edit
    # an edit and a delete travel back
    clock.tick()
    with devices.on("rig"):
        templates.save_template({"name": "fix ci", "repo_path": theirs, "prompt": "v2"})
        templates.save_template({"name": "Other", "prompt": "o"})
    devices.sync("laptop")
    got = {t["name"].lower(): t for t in templates.list_templates()}
    assert got["fix ci"]["prompt"] == "v2" and got["fix ci"]["repo_path"] == mine
    assert "other" in got
    clock.tick()
    templates.delete_template("Other")
    devices.sync("rig")
    with devices.on("rig"):
        assert [t["name"] for t in templates.list_templates()] == ["fix ci"]


def test_an_unreadable_store_is_not_read_as_everything_deleted(devices, clock):
    templates.save_template({"name": "Keep", "prompt": "k"})
    devices.all_on()
    with open(os.environ["MINDFLOCK_TEMPLATES_FILE"], "w") as f:
        f.write("{not json")
    clock.tick()
    assert settings_sync.scan_local() == []
    st = settings_sync._load()["stamps"]["store:templates#keep"]
    assert not st.get("deleted")
    devices.sync("rig")
    with devices.on("rig"):
        assert [t["name"] for t in templates.list_templates()] == ["Keep"]


def test_a_template_under_the_wrong_key_is_refused():
    with pytest.raises(ValueError):
        sync_stores.templates_write("a", {"name": "b"})
    with pytest.raises(ValueError):
        sync_stores.templates_write("a", "not a template")


# --------------------------------------------------------------------------- #
# red zones
# --------------------------------------------------------------------------- #
@pytest.fixture
def route_writes(monkeypatch):
    """Count store writes bracketed by route_write (the tamper detector's
    "this was us" marker)."""
    seen = []
    real = red_zone_monitor.route_write

    @contextlib.contextmanager
    def counting():
        seen.append(1)
        with real():
            yield

    monkeypatch.setattr(red_zone_monitor, "route_write", counting)
    return seen


def test_red_zones_sync_by_pattern_not_id(devices, clock, monkeypatch, route_writes):
    resynced = []
    monkeypatch.setattr(red_zone_monitor, "resync", lambda t, r: resynced.append(r))
    monkeypatch.setattr(server, "_live_repo_roots", lambda rid: [("/wt", "/wt")])
    red_zones.add_zone("repo", REPO, "secrets/", name="Secrets", label="me/app")
    red_zones.add_zone("repo", REPO, "/infra", note="ops only", label="me/app")
    red_zones.set_plan_first(REPO, True, label="me/app")
    red_zones.set_companions(REPO, ["dist/"], label="me/app")
    # never synced: a folder on this machine, and per-checkout state
    red_zones.add_zone("repo", "path:/home/me/scratch", "x/")
    red_zones.add_zone("worktree", "/tmp/wt-laptop", "y/", repo_id=REPO)
    devices.all_on()
    with devices.on("rig"):
        repos = red_zones.all_repos()
        assert set(repos) == {REPO}
        r = repos[REPO]
        assert sorted(z["pattern"] for z in r["zones"]) == ["/infra", "secrets/"]
        assert r["plan_first"] is True and r["companions"] == ["dist/"]
        assert r["label"] == "me/app"
        assert {z["name"] for z in r["zones"]} == {"Secrets", ""}
        assert red_zones._load()["worktrees"] == {}
        clock.tick()
        assert settings_sync.scan_local() == []  # new ids aren't an edit
    assert route_writes and REPO in resynced
    # removing one zone travels
    clock.tick()
    infra = next(
        z for z in red_zones.all_repos()[REPO]["zones"] if "infra" in z["pattern"]
    )
    red_zones.remove_zone(infra["id"])
    devices.sync("rig")
    with devices.on("rig"):
        assert [z["name"] for z in red_zones.all_repos()[REPO]["zones"]] == ["Secrets"]
    # emptying the repo entry is a delete
    clock.tick()
    for z in red_zones.all_repos()[REPO]["zones"]:
        red_zones.remove_zone(z["id"])
    red_zones.set_plan_first(REPO, False)
    red_zones.set_companions(REPO, [])
    assert REPO not in sync_stores.red_zones_list()
    devices.sync("rig")
    with devices.on("rig"):
        assert REPO not in sync_stores.red_zones_list()
        assert red_zones.all_repos().get(REPO, {}).get("zones", []) == []


def test_a_renamed_zone_is_replaced_not_duplicated(devices, clock):
    red_zones.add_zone("repo", REPO, "secrets/", name="Old")
    devices.all_on()
    clock.tick()
    zid = red_zones.all_repos()[REPO]["zones"][0]["id"]
    red_zones.remove_zone(zid)
    red_zones.add_zone("repo", REPO, "secrets/", name="New")
    devices.sync("rig")
    with devices.on("rig"):
        assert [z["name"] for z in red_zones.all_repos()[REPO]["zones"]] == ["New"]


def test_a_red_zone_that_conflicts_here_is_reported_once_not_reapplied(
    devices, clock, monkeypatch
):
    """The zone is a GREEN zone in a worktree here, so the entry can't land:
    that is said (Devices screen warning), not re-attempted — with a guard
    resync and a settings.synced event — on every 30 s pass."""
    monkeypatch.setattr(red_zone_monitor, "resync", lambda t, r: None)
    monkeypatch.setattr(server, "_live_repo_roots", lambda rid: [])
    calls = []
    monkeypatch.setattr(
        settings_sync,
        "_after_change",
        lambda units, source, before=None: calls.append(list(units)),
    )
    writes = []
    real_write = sync_stores.red_zones_write

    def counting(key, value):
        writes.append(key)
        return real_write(key, value)

    settings_sync._stores()["red_zones"].write_fn = counting
    try:
        devices.all_on()
        with devices.on("rig"):
            red_zones.add_zone(
                "worktree", "/tmp/wt-rig", "secrets/", repo_id=REPO, kind="green"
            )
        clock.tick()
        red_zones.add_zone("repo", REPO, "secrets/", name="S", label="me/app")
        for _ in range(3):
            clock.tick()
            devices.sync("rig")
    finally:
        settings_sync._stores()["red_zones"].write_fn = real_write
    assert writes == [REPO]
    assert not [c for c in calls if any("red_zones" in u for u in c)]
    with devices.on("rig"):
        warns = settings_sync.status()["warnings"]
        assert any("secrets/ is a green zone in a worktree here" in w for w in warns)
        # never stamped, so never tombstoned (deleted elsewhere) either
        assert "store:red_zones#" + REPO not in settings_sync._load()["stamps"]
    clock.tick()
    devices.sync("laptop")
    assert red_zones.all_repos()[REPO]["zones"]


def test_a_path_repo_id_is_never_written():
    with pytest.raises(ValueError):
        sync_stores.red_zones_write("path:/x", {"zones": []})


# --------------------------------------------------------------------------- #
# custom providers
# --------------------------------------------------------------------------- #
TOML = '[provider]\nname = "mycli"\nprogram = "mycli"\n'


def test_custom_providers_sync_as_their_toml(devices, clock):
    try:
        (devices.tmp / "laptop" / "providers" / "mycli.toml").write_text(TOML)
        devices.all_on()
        with devices.on("rig"):
            f = devices.tmp / "rig" / "providers" / "mycli.toml"
            assert f.read_text() == TOML
            assert stat.S_IMODE(f.stat().st_mode) == 0o600
            assert providers.get("mycli") is not None  # registry rebuilt here
        clock.tick()
        (devices.tmp / "laptop" / "providers" / "mycli.toml").unlink()
        devices.sync("rig")
        with devices.on("rig"):
            assert not (devices.tmp / "rig" / "providers" / "mycli.toml").exists()
            assert providers.get("mycli") is None
    finally:
        for k in ("laptop", "rig"):
            p = devices.tmp / k / "providers" / "mycli.toml"
            if p.exists():
                p.unlink()
        providers.rebuild_registry()


def test_a_broken_or_misnamed_provider_is_refused(devices):
    with pytest.raises(Exception):
        sync_stores.providers_write("mycli", "this is = = not toml")
    with pytest.raises(ValueError):
        sync_stores.providers_write("../evil", TOML)
    with pytest.raises(ValueError):
        sync_stores.providers_delete("../evil")
    assert sync_stores.providers_list() == {}


def test_the_provider_crud_routes_stamp_for_sync(devices, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    asyncio.run(settings_sync.enable(""))
    try:
        c = TestClient(server.app)
        r = c.post("/api/providers", json={"name": "crudcli", "program": "crudcli"})
        assert r.status_code == 200
        assert "store:providers#crudcli" in settings_sync._load()["stamps"]
        c.delete("/api/providers/crudcli")
        assert settings_sync._load()["stamps"]["store:providers#crudcli"]["deleted"]
    finally:
        providers.rebuild_registry()
