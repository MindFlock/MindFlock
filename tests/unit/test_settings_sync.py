"""Settings sync across the user's own devices (:mod:`backend.web.core.settings_sync`).

Several devices in one process: each has its own settings dir (and template,
red-zone and provider stores), and "which device am I" is switched around
every call — the fake ``remote.get_json`` serves the OTHER device's real
:func:`export`, so merge/enable/sync_once run against the real code on both
ends. Fleet membership is faked at :mod:`backend.web.core.fleet` (its id, key
check) and :func:`remote.fleet_devices` (which members are visible).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web import server
from backend.web.core import remote, settings_hooks, settings_sync

TOKEN = "sync-token-abc123"
KEY = "fleet-key-0123456789"
FLEET = "f00dfeedf00dfeed"

#: The real filter, captured before any fixture swaps it for a fake.
_REAL_FLEET_DEVICES = remote.fleet_devices


def fleet_module(monkeypatch):
    """The fleet store (its membership answers are faked per test)."""
    from backend.web.core import fleet

    return fleet


class Devices:
    """The user's devices, simulated. ``members`` are in the fleet;
    ``offline`` ones aren't visible; ``outsiders`` are tailnet nodes that
    answer an export but are NOT members."""

    def __init__(self, tmp_path, monkeypatch, names=("laptop", "rig")):
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.names = list(names)
        self.members = set(names)
        self.offline: set = set()
        self.outsiders: dict = {}
        self.pulled: list = []  # (puller, pulled)
        self.posts: list = []  # (from, to, path)
        self.peers = {k: {} for k in names}
        self.deferred = {k: {} for k in names}
        self.warnings = {k: {} for k in names}
        self.unlanded = {k: {} for k in names}
        self.unresolved = {k: set() for k in names}
        for k in names:
            (tmp_path / k / "providers").mkdir(parents=True)
        self.me = self.names[0]
        fl = fleet_module(monkeypatch)
        monkeypatch.setattr(fl, "in_fleet", lambda: self.me in self.members)
        monkeypatch.setattr(
            fl,
            "fleet_id",
            lambda: FLEET if self.me in self.members else "",
        )
        monkeypatch.setattr(fl, "key_valid", lambda c: bool(c) and c == KEY)
        monkeypatch.setattr(
            remote, "self_identity", lambda: {"key": self.me, "host": self.me.title()}
        )
        monkeypatch.setattr(remote, "fleet_devices", self.fleet_devices)
        monkeypatch.setattr(remote, "get_json", self.get_json)
        monkeypatch.setattr(remote, "post_json", self.post_json)
        monkeypatch.setattr(settings_sync, "_LOOP", None)
        monkeypatch.setattr(settings_sync, "_soon", None)
        self.use(self.me)

    # --- the fake network ------------------------------------------------- #
    def fleet_devices(self):
        if self.me not in self.members:
            return []
        return [
            {
                "key": k,
                "host": k.title(),
                "base_url": "http://%s" % k,
                "reachable": True,
            }
            for k in self.names
            if k != self.me and k in self.members and k not in self.offline
        ]

    async def get_json(self, dev, path, timeout=3.0, *, auth=True, bearer=None):
        assert path == settings_sync.EXPORT_PATH
        self.pulled.append((self.me, dev["key"]))
        if dev["key"] in self.outsiders:
            return 200, self.outsiders[dev["key"]]
        if dev["key"] in self.offline:
            return 0, None
        with self.on(dev["key"]):
            try:
                return 200, settings_sync.export()
            except store.SettingsUnreadable as err:  # what the export route answers
                why = (
                    settings_sync.PAUSED
                    if str(err) == settings_sync.PAUSED
                    else settings_sync.UNREADABLE
                )
                return 503, {"error": why}

    async def post_json(self, dev, path, body, timeout=10.0, *, auth=True, bearer=None):
        self.posts.append((self.me, dev["key"], path))
        return 200, {"ok": True}

    # --- switching devices ------------------------------------------------ #
    def use(self, key):
        self.me = key
        d = self.tmp / key
        self.mp.setenv("MINDFLOCK_SETTINGS_FILE", str(d / "settings.json"))
        self.mp.setenv("MINDFLOCK_TEMPLATES_FILE", str(d / "templates.json"))
        self.mp.setenv("MINDFLOCK_RED_ZONES_FILE", str(d / "red_zones.json"))
        self.mp.setenv("MINDFLOCK_PROVIDERS_DIR", str(d / "providers"))
        self.mp.setattr(settings_sync, "_peers", self.peers[key])
        self.mp.setattr(settings_sync, "_deferred", self.deferred[key])
        self.mp.setattr(settings_sync, "_warnings", self.warnings[key])
        self.mp.setattr(settings_sync, "_unlanded", self.unlanded[key])
        self.mp.setattr(settings_sync, "_unresolved", self.unresolved[key])
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

    def sync(self, *keys):
        """One pass on each device in turn."""
        for k in keys:
            with self.on(k):
                asyncio.run(settings_sync.sync_once())

    def all_on(self, leader=None):
        """Sync on everywhere: ``leader`` (default the first) starts, the rest
        join from it."""
        leader = leader or self.names[0]
        with self.on(leader):
            asyncio.run(settings_sync.enable(""))
        for k in self.names:
            if k != leader:
                with self.on(k):
                    asyncio.run(settings_sync.enable(leader))


class Clock:
    """``time.time`` for every device, with a per-device skew."""

    def __init__(self, devices, start=1000.0):
        self.devices = devices
        self.t = start
        self.skew: dict = {}

    def __call__(self):
        return self.t + self.skew.get(self.devices.me, 0.0)

    def tick(self, dt=10.0):
        self.t += dt


@pytest.fixture
def devices(tmp_path, monkeypatch):
    yield Devices(tmp_path, monkeypatch)
    store.invalidate()


@pytest.fixture
def trio(tmp_path, monkeypatch):
    yield Devices(tmp_path, monkeypatch, names=("laptop", "rig", "mini"))
    store.invalidate()


def _clock(monkeypatch, devs):
    clock = Clock(devs)
    monkeypatch.setattr(settings_sync.time, "time", clock)
    return clock


@pytest.fixture
def clock(devices, monkeypatch):
    return _clock(monkeypatch, devices)


def run(coro):
    return asyncio.run(coro)


def _sources():
    store.invalidate()
    return [(s.id, s.project) for s in store.load_settings().ticketing.sources]


def _src(sid, **kw):
    return {"id": sid, "provider": "github", **kw}


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


def test_secret_and_keyed_paths_are_all_synced_paths():
    assert settings_sync.SECRET_PATHS <= set(settings_sync.paths())
    assert set(settings_sync.KEYED) <= set(settings_sync.paths())
    assert settings_sync.DEFER_PATHS <= set(settings_sync.paths())


def test_every_syncable_thing_has_a_human_label():
    """The pin picker lists them by label — a raw ``group.field`` there is a
    field someone forgot to name."""
    for path in settings_sync.paths():
        assert settings_sync.LABELS.get(path), path
    rows = settings_sync.syncable()
    stores = {r["path"] for r in rows if r["path"].startswith("store:")}
    assert {"store:templates", "store:red_zones", "store:providers"} <= stores
    for r in rows:
        assert r["label"] and r["group"] and r["label"] != r["path"]


# --------------------------------------------------------------------------- #
# only fleet members — the regression this rewrite exists for
# --------------------------------------------------------------------------- #
def _hostile_export():
    unit = "coding_cli.default_launch_args"
    return {
        "protocol": settings_sync.PROTOCOL,
        "fleet": FLEET,
        "device": "rogue",
        "enabled": True,
        "stamps": {unit: {"ts": 9e9, "by": "rogue", "h": "x"}},
        "values": {unit: {"claude": "--dangerously-evil"}},
        "withheld": [],
    }


def test_a_connected_tailnet_device_outside_the_fleet_is_never_pulled(
    devices, monkeypatch
):
    """A gate-off tailnet node counts as "connected" with no token at all;
    adopting its export would let it prefix every new session here."""
    rogue = {
        "key": "rogue",
        "host": "Rogue",
        "base_url": "http://rogue",
        "reachable": True,
        "remote_control": True,
    }
    monkeypatch.setattr(remote, "connected_devices", lambda: [rogue])
    devices.outsiders["rogue"] = _hostile_export()
    run(settings_sync.enable(""))
    run(settings_sync.sync_once())
    assert all(pulled != "rogue" for _me, pulled in devices.pulled)
    assert "evil" not in str(devices.get("coding_cli", "default_launch_args"))
    with pytest.raises(LookupError):
        run(settings_sync.enable("rogue"))
    assert "evil" not in str(devices.get("coding_cli", "default_launch_args"))


def test_the_real_peer_filter_drops_a_gate_off_non_member(devices, monkeypatch):
    """End to end through :func:`remote.fleet_devices`: a reachable device that
    even claims our fleet id in its hello is not a peer unless it's a member."""
    fl = fleet_module(monkeypatch)
    monkeypatch.setattr(fl, "is_member", lambda k: k == "rig")
    monkeypatch.setattr(
        fl, "member_device", lambda d: d.get("key") == "rig", raising=False
    )
    monkeypatch.setattr(remote, "fleet_devices", _REAL_FLEET_DEVICES)
    base = {"reachable": True, "remote_control": True, "fleet": FLEET}
    monkeypatch.setattr(
        remote,
        "_DEVICES",
        {
            "rogue": {**base, "key": "rogue", "host": "Rogue", "base_url": "http://r"},
            "rig": {**base, "key": "rig", "host": "Rig", "base_url": "http://g"},
        },
    )
    devices.outsiders["rogue"] = _hostile_export()
    run(settings_sync.enable(""))
    run(settings_sync.sync_once())
    assert ("laptop", "rig") in devices.pulled
    assert ("laptop", "rogue") not in devices.pulled
    assert "evil" not in str(devices.get("coding_cli", "default_launch_args"))


def test_merge_ignores_other_fleets_and_old_protocols(devices):
    run(settings_sync.enable(""))
    evil = _hostile_export()
    assert settings_sync.merge({**evil, "fleet": "somebody-else"}) == []
    assert settings_sync.merge({**evil, "protocol": 1}) == []
    assert settings_sync.merge({**evil, "fleet": ""}) == []
    devices.members.discard("laptop")  # left the fleet: nobody's export counts
    assert settings_sync.merge(evil) == []
    assert "evil" not in str(devices.get("coding_cli", "default_launch_args"))


def test_sync_needs_a_fleet(devices):
    devices.members.discard("laptop")
    with pytest.raises(LookupError) as ei:
        run(settings_sync.enable(""))
    assert "join your other devices first" in str(ei.value)
    assert settings_sync.enabled() is False


def test_leaving_the_fleet_stops_pulling_even_with_sync_on(devices):
    run(settings_sync.enable(""))
    devices.members.discard("laptop")
    assert run(settings_sync.sync_once()) == []
    assert devices.pulled == []
    warn = settings_sync.status()["warnings"]
    assert warn and "isn't one of your devices" in warn[0]


# --------------------------------------------------------------------------- #
# joining — the older machine leads
# --------------------------------------------------------------------------- #
def test_join_from_another_device_adopts_its_shared_settings(devices):
    with devices.on("rig"):
        store.update_settings(
            ui={"accent": "teal"},
            github={"repos": ["me/app"], "token": "ghp_rig"},
            general={"auth_mode": "on", "serve_mode": "tailscale"},
        )
        run(settings_sync.enable(""))  # the rig leads
    store.update_settings(ui={"accent": "red"}, general={"serve_mode": "local"})
    out = run(settings_sync.enable("rig"))
    assert "ui.accent" in out["adopted"]
    assert devices.get("ui", "accent") == "teal"
    assert devices.get("github", "repos") == ["me/app"]
    assert devices.get("github", "token") == "ghp_rig"  # secrets always travel
    # machine-specific fields stay put
    assert devices.get("general", "serve_mode") == "local"
    assert devices.get("general", "auth_mode") == ""


def test_join_from_a_device_not_yet_syncing_lets_it_win_later(devices, clock):
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
    run(settings_sync.enable("rig"))  # rig isn't syncing yet
    assert devices.get("ui", "accent") == "teal"
    clock.tick()
    with devices.on("rig"):
        run(settings_sync.enable(""))
        store.update_settings(ui={"accent": "gold"})
    clock.tick()
    run(settings_sync.sync_once())
    assert devices.get("ui", "accent") == "gold"


def test_join_from_a_device_outside_the_fleet_is_refused(devices):
    devices.members.discard("rig")
    with pytest.raises(LookupError):
        run(settings_sync.enable("rig"))
    with pytest.raises(LookupError):
        run(settings_sync.enable("nowhere"))
    assert settings_sync.enabled() is False


def test_join_keeps_entries_only_the_joiner_has(devices, clock):
    """Joining is a union for lists: the leader's entries win where both have
    one (the same source: provider, project, workspace), an entry only the
    new device has spreads from it."""
    with devices.on("rig"):
        store.set_ticketing_sources([_src("a", project="p", label="rig's")])
        run(settings_sync.enable(""))
    store.set_ticketing_sources([_src("a", project="p", label="mine"), _src("z")])
    run(settings_sync.enable("rig"))
    store.invalidate()
    got = [(s.id, s.label) for s in store.load_settings().ticketing.sources]
    assert got == [("a", "rig's"), ("z", "")]
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert _sources() == [("a", "p"), ("z", "")]


def _id_project_token():
    store.invalidate()
    return sorted(
        (s.id, s.project, s.api_token) for s in store.load_settings().ticketing.sources
    )


def test_join_keeps_a_different_source_under_the_same_id_separate(devices, clock):
    """Ids are slugs each device seeds on its own (every device's first
    Shortcut source is ``sc``), and an id is its ticket slug prefix — so a
    different source under the same id is neither overwritten (it holds a
    token) nor renamed (its tickets would all be ingested again): it stays
    on the joining device, kept separate, and says so."""
    with devices.on("rig"):
        store.set_ticketing_sources(
            [{"id": "sc", "provider": "shortcut", "project": "work", "api_token": "w"}]
        )
        run(settings_sync.enable(""))
    store.set_ticketing_sources(
        [{"id": "sc", "provider": "shortcut", "project": "home", "api_token": "h"}]
    )
    run(settings_sync.enable("rig"))
    assert _id_project_token() == [("sc", "home", "h")]
    st = settings_sync.status()
    assert "ticketing.sources#sc" in st["pinned"]
    # Who it's kept separate from (Settings → Devices' Unpin confirm).
    assert st["separate"] == {"ticketing.sources#sc": "Rig"}
    assert any(
        "“sc” differs between Rig and this device — kept separate" in w
        for w in st["warnings"]
    )
    assert "ticketing.sources#sc" not in settings_sync.export()["values"]
    clock.tick()
    devices.sync("rig", "laptop")
    assert _id_project_token() == [("sc", "home", "h")]
    with devices.on("rig"):
        assert _id_project_token() == [("sc", "work", "w")]
    # Given another id here, it syncs — and the rig's "sc" comes in too.
    clock.tick()
    store.set_ticketing_sources(
        [{"id": "home", "provider": "shortcut", "project": "home", "api_token": "h"}]
    )
    settings_sync.local_change()
    assert "ticketing.sources#sc" not in settings_sync.status()["pinned"]
    clock.tick()
    devices.sync("laptop", "rig")
    assert _id_project_token() == [("home", "home", "h"), ("sc", "work", "w")]
    with devices.on("rig"):
        assert _id_project_token() == [("home", "home", "h"), ("sc", "work", "w")]


def test_join_tells_two_shortcut_workspaces_apart_by_token(devices, clock):
    """Shortcut has no project or base URL — the workspace is the token."""
    with devices.on("rig"):
        store.set_ticketing_sources(
            [{"id": "sc", "provider": "shortcut", "api_token": "WORK"}]
        )
        run(settings_sync.enable(""))
    store.set_ticketing_sources(
        [{"id": "sc", "provider": "shortcut", "api_token": "HOME"}]
    )
    run(settings_sync.enable("rig"))
    assert _id_project_token() == [("sc", "", "HOME")]
    assert "ticketing.sources#sc" in settings_sync.status()["pinned"]


def test_join_treats_a_tokenless_copy_of_the_same_source_as_the_same(devices, clock):
    with devices.on("rig"):
        store.set_ticketing_sources(
            [{"id": "sc", "provider": "shortcut", "api_token": "T", "label": "rig"}]
        )
        run(settings_sync.enable(""))
    store.set_ticketing_sources([{"id": "sc", "provider": "shortcut", "label": "me"}])
    run(settings_sync.enable("rig"))
    assert _id_project_token() == [("sc", "", "T")]
    assert settings_sync.status()["pinned"] == []


def test_a_leaders_delete_never_takes_the_joiners_source_under_that_id(devices, clock):
    with devices.on("rig"):
        store.set_ticketing_sources(
            [{"id": "sc", "provider": "shortcut", "project": "old", "api_token": "o"}]
        )
        run(settings_sync.enable(""))
        clock.tick()
        store.set_ticketing_sources(
            [{"id": "jira", "provider": "jira", "project": "OPS", "api_token": "j"}]
        )
        settings_sync.scan_local()  # a tombstone for ticketing.sources#sc
    store.set_ticketing_sources(
        [{"id": "sc", "provider": "shortcut", "project": "mine", "api_token": "MINE"}]
    )
    clock.tick()
    run(settings_sync.enable("rig"))
    assert _id_project_token() == [("jira", "OPS", "j"), ("sc", "mine", "MINE")]
    clock.tick()
    devices.sync("laptop")
    assert ("sc", "mine", "MINE") in _id_project_token()


# --------------------------------------------------------------------------- #
# two-way, last edit wins
# --------------------------------------------------------------------------- #
def test_an_edit_on_either_side_reaches_the_other(devices, clock):
    devices.all_on("rig")
    clock.tick()
    store.update_settings(ui={"accent": "red"})  # laptop edit
    with devices.on("rig"):
        run(settings_sync.sync_once())
        assert devices.get("ui", "accent") == "red"
        clock.tick()
        store.update_settings(github={"repos": ["me/other"]})  # rig edit
    run(settings_sync.sync_once())
    assert devices.get("github", "repos") == ["me/other"]
    assert devices.get("ui", "accent") == "red"


def test_the_later_edit_wins_a_conflict(devices, clock):
    devices.all_on("rig")
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    clock.tick()
    store.update_settings(ui={"accent": "red"})  # later
    run(settings_sync.sync_once())  # laptop pulls the older teal: ignored
    assert devices.get("ui", "accent") == "red"
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "red"


def test_a_fast_clock_cannot_freeze_a_field(devices, clock):
    """Hybrid stamps: a value adopted from a device whose clock runs 200 s
    fast must still lose to a LATER edit here, whatever this clock says."""
    devices.all_on()
    clock.skew["rig"] = 200.0
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    run(settings_sync.sync_once())
    assert devices.get("ui", "accent") == "teal"
    clock.tick()
    store.update_settings(ui={"accent": "red"})  # laptop: 1020 by its clock
    settings_sync.scan_local()
    assert settings_sync._load()["stamps"]["ui.accent"]["ts"] > 1210
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "red"


def test_clearing_a_field_syncs_as_a_clear(devices, clock):
    store.update_settings(ui={"accent": "red"})
    devices.all_on()
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "red"
    clock.tick()
    store.update_settings(ui={"accent": ""})
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == ""


def test_adopting_is_not_mistaken_for_a_local_edit(devices, clock):
    """After a pull, the next scan must not restamp the adopted value as this
    device's own (that would make every device 'win' in turn)."""
    devices.all_on("rig")
    clock.tick()
    with devices.on("rig"):
        store.update_settings(general={"tailnet_trusted_logins": ["Me@Example.com"]})
        store.set_ticketing_sources([_src("a")])
    run(settings_sync.sync_once())
    clock.tick()
    assert settings_sync.scan_local() == []


def test_off_means_no_pulls(devices):
    with devices.on("rig"):
        run(settings_sync.enable(""))
        store.update_settings(ui={"accent": "teal"})
    assert run(settings_sync.sync_once()) == []
    assert devices.get("ui", "accent") == ""


def test_adoption_runs_the_after_change_hooks(devices, clock, monkeypatch):
    seen = []
    monkeypatch.setattr(
        settings_hooks,
        "after_settings_change",
        lambda paths, source="", **_kw: seen.append((list(paths), source)),
    )
    devices.all_on()
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
    run(settings_sync.sync_once())
    assert seen[-1] == (["ui.accent"], "rig")


# --------------------------------------------------------------------------- #
# keyed lists: per entry
# --------------------------------------------------------------------------- #
def test_concurrent_edits_to_different_entries_both_survive(devices, clock):
    store.set_ticketing_sources([_src("a"), _src("b")])
    devices.all_on()
    clock.tick()
    store.set_ticketing_sources([_src("a", project="A2"), _src("b")])
    settings_sync.scan_local()
    with devices.on("rig"):
        store.set_ticketing_sources([_src("a"), _src("b", project="B2"), _src("c")])
        settings_sync.scan_local()
    clock.tick()
    devices.sync("laptop", "rig")
    want = [("a", "A2"), ("b", "B2"), ("c", "")]
    assert _sources() == want
    with devices.on("rig"):
        assert _sources() == want


def test_a_new_entry_keeps_local_order_and_lands_in_remote_order(devices, clock):
    store.set_ticketing_sources([_src("b"), _src("a")])
    devices.all_on()
    clock.tick()
    with devices.on("rig"):
        store.set_ticketing_sources([_src("x"), _src("b"), _src("y"), _src("a")])
    run(settings_sync.sync_once())
    assert [s for s, _p in _sources()] == ["b", "a", "x", "y"]


def test_a_delete_propagates_and_a_stale_copy_does_not_resurrect_it(trio, monkeypatch):
    clock = _clock(monkeypatch, trio)
    store.set_ticketing_sources([_src("a"), _src("b")])
    trio.all_on()
    trio.offline.add("mini")  # mini sleeps through the delete, still holding b
    clock.tick()
    store.set_ticketing_sources([_src("a")])  # laptop deletes b
    trio.sync("rig")
    with trio.on("rig"):
        assert _sources() == [("a", "")]
    trio.offline.discard("mini")
    clock.tick()
    trio.sync("laptop", "rig")  # both pull mini's stale b: the tombstone wins
    assert _sources() == [("a", "")]
    with trio.on("rig"):
        assert _sources() == [("a", "")]
    trio.sync("mini")
    with trio.on("mini"):
        assert _sources() == [("a", "")]
    # a deliberate re-add later is an edit like any other
    clock.tick()
    with trio.on("mini"):
        store.set_ticketing_sources([_src("a"), _src("b", project="back")])
    clock.tick()
    trio.sync("laptop")
    assert _sources() == [("a", ""), ("b", "back")]


def test_prompt_presets_are_one_entry_per_name_whatever_the_case(devices, clock):
    store.update_settings(prefs={"prompt_presets": [{"name": "Deploy", "prompt": "a"}]})
    devices.all_on()
    with devices.on("rig"):
        assert devices.get("prefs", "prompt_presets") == [
            {"name": "Deploy", "prompt": "a"}
        ]
        clock.tick()
        store.update_settings(
            prefs={"prompt_presets": [{"name": "DEPLOY", "prompt": "b"}]}
        )
    run(settings_sync.sync_once())
    assert devices.get("prefs", "prompt_presets") == [{"name": "DEPLOY", "prompt": "b"}]


# --------------------------------------------------------------------------- #
# canonical form: a local checkout travels as its origin URL
# --------------------------------------------------------------------------- #
def _git_repo(path, origin):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
    )
    return str(path)


def test_a_local_checkout_lands_as_this_machines_checkout(devices, clock, tmp_path):
    mine = _git_repo(tmp_path / "laptop-src" / "app", "git@github.com:me/app.git")
    theirs = _git_repo(tmp_path / "rig-src" / "app", "https://github.com/me/app.git")
    store.update_settings(repository={"url": mine})
    store.set_ticketing_sources([_src("a", repo_url=mine)])
    with devices.on("rig"):
        store.update_settings(general={"last_repo_path": theirs})
    run(settings_sync.enable(""))
    assert settings_sync.export()["values"]["repository.url"] == (
        "git@github.com:me/app.git"
    )
    with devices.on("rig"):
        run(settings_sync.enable("laptop"))
        assert devices.get("repository", "url") == theirs
        store.invalidate()
        assert store.load_settings().ticketing.sources[0].repo_url == theirs
    clock.tick()
    # no ping-pong: neither side reads the other's form as an edit
    assert settings_sync.scan_local() == []
    with devices.on("rig"):
        assert settings_sync.scan_local() == []
    devices.sync("laptop", "rig")
    assert devices.get("repository", "url") == mine


def test_a_machine_without_the_checkout_gets_the_url(devices, tmp_path):
    mine = _git_repo(tmp_path / "src" / "app", "git@github.com:me/app.git")
    store.update_settings(repository={"url": mine})
    devices.all_on()
    with devices.on("rig"):
        assert devices.get("repository", "url") == "git@github.com:me/app.git"


def test_localize_prefers_the_path_already_stored(tmp_path):
    a = _git_repo(tmp_path / "a", "git@github.com:me/app.git")
    assert settings_sync.localize_url("https://github.com/me/app", a) == a
    assert settings_sync.localize_url("https://github.com/me/other", a) == (
        "https://github.com/me/other"
    )
    assert settings_sync.canonical_url(str(tmp_path / "nowhere")) == str(
        tmp_path / "nowhere"
    )


# --------------------------------------------------------------------------- #
# deferral: an agent CLI that isn't installed here
# --------------------------------------------------------------------------- #
def test_an_agent_not_installed_here_is_held_back_until_it_is(
    devices, clock, monkeypatch
):
    installed = {"codex": False}
    monkeypatch.setattr(
        settings_hooks, "provider_installed", lambda n: installed.get(n, False)
    )
    devices.all_on()
    clock.tick()
    with devices.on("rig"):
        store.update_settings(
            coding_cli={"default_provider": "codex"}, ui={"accent": "teal"}
        )
    run(settings_sync.sync_once())
    assert devices.get("ui", "accent") == "teal"
    assert devices.get("coding_cli", "default_provider") == ""
    assert settings_sync.status()["deferred"] == [
        {
            "path": "coding_cli.default_provider",
            "value": "codex",
            "reason": "codex isn't installed on this device",
        }
    ]
    clock.tick()
    assert settings_sync.scan_local() == []  # the stamp wasn't taken either
    installed["codex"] = True
    clock.tick()
    run(settings_sync.sync_once())
    assert devices.get("coding_cli", "default_provider") == "codex"
    assert settings_sync.status()["deferred"] == []


def test_a_held_back_agent_never_spreads_this_devices_value(
    devices, clock, monkeypatch
):
    """Joining from a device whose default agent isn't installed here must
    not stamp this device's own (different) default as the newer one."""
    monkeypatch.setattr(settings_hooks, "provider_installed", lambda n: n != "codex")
    store.update_settings(coding_cli={"default_provider": "claude"})
    with devices.on("rig"):
        store.update_settings(coding_cli={"default_provider": "codex"})
        run(settings_sync.enable(""))
    out = run(settings_sync.enable("rig"))
    assert out["deferred"] == ["coding_cli.default_provider"]
    assert devices.get("coding_cli", "default_provider") == "claude"
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("coding_cli", "default_provider") == "codex"


# --------------------------------------------------------------------------- #
# pinning: keep different on this device
# --------------------------------------------------------------------------- #
def test_a_pinned_field_stays_different_until_unpinned(devices, clock):
    devices.all_on()
    clock.tick()
    assert settings_sync.set_pinned("ui.accent", True) == ["ui.accent"]
    store.update_settings(ui={"accent": "red"})
    out = settings_sync.export()
    assert "ui.accent" not in out["values"] and "ui.accent" not in out["stamps"]
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        run(settings_sync.sync_once())
        assert devices.get("ui", "accent") == "teal"  # laptop's red never came
    clock.tick()
    run(settings_sync.sync_once())
    assert devices.get("ui", "accent") == "red"  # rig's teal never landed
    assert settings_sync.status()["pinned"] == ["ui.accent"]
    settings_sync.set_pinned("ui.accent", False)
    clock.tick()
    run(settings_sync.sync_once())
    assert devices.get("ui", "accent") == "teal"  # the fleet's value wins
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "teal"


def test_pinning_something_that_doesnt_sync_is_refused(devices):
    with pytest.raises(ValueError):
        settings_sync.set_pinned("general.auth_token", True)
    with pytest.raises(ValueError):
        settings_sync.set_pinned("store:nope", True)
    assert settings_sync.set_pinned("store:templates", True) == ["store:templates"]


def test_pins_survive_turning_sync_off(devices):
    run(settings_sync.enable(""))
    settings_sync.set_pinned("prefs.keymap", True)
    settings_sync.disable()
    assert settings_sync.status()["pinned"] == ["prefs.keymap"]


# --------------------------------------------------------------------------- #
# status + per-device errors
# --------------------------------------------------------------------------- #
def test_status_shape(devices):
    run(settings_sync.enable(""))
    run(settings_sync.sync_once())
    st = settings_sync.status()
    assert st["enabled"] is True and st["in_fleet"] is True
    assert st["device"] == "laptop"
    assert [d["key"] for d in st["devices"]] == ["rig"]
    assert st["devices"][0]["last_sync"] is not None
    assert st["warnings"] == [] and st["pinned"] == [] and st["deferred"] == []
    assert {"path", "label", "group"} <= set(st["syncable"][0])


@pytest.mark.parametrize(
    "answer,needle",
    [
        ((401, None), "ask to rejoin"),
        ((404, None), "too old"),
        ((0, None), "unreachable"),
        ((200, {"protocol": 1, "enabled": True}), "update MindFlock on Rig"),
        (
            (200, {"protocol": 2, "fleet": "other", "enabled": True}),
            "different group",
        ),
    ],
)
def test_a_device_that_cant_be_synced_says_why(devices, monkeypatch, answer, needle):
    run(settings_sync.enable(""))

    async def fake(dev, path, timeout=3.0, *, auth=True, bearer=None):
        return answer

    monkeypatch.setattr(remote, "get_json", fake)
    assert run(settings_sync.sync_once()) == []
    assert needle in settings_sync.status()["devices"][0]["error"]


@pytest.mark.parametrize(
    "theirs,needle",
    [
        ({"id": FLEET, "epoch": 2}, "hasn't picked up the latest key"),
        ({"id": FLEET, "epoch": 4}, "this device missed a change"),
        ({"id": "another", "epoch": 2}, "ask to rejoin"),
        ({"error": "no"}, "ask to rejoin"),  # an older MindFlock's 401
    ],
)
def test_a_refused_key_says_which_side_is_behind(devices, monkeypatch, theirs, needle):
    # The 401 names the refusing device's key epoch: a device that is merely
    # behind (gossip hands it the new key) is not "removed — ask to rejoin".
    run(settings_sync.enable(""))
    fl = fleet_module(monkeypatch)
    monkeypatch.setattr(
        fl, "unauthorized_body", lambda: {"error": "x", "id": FLEET, "epoch": 3}
    )

    async def fake(dev, path, timeout=3.0, *, auth=True, bearer=None):
        return 401, theirs

    monkeypatch.setattr(remote, "get_json", fake)
    assert run(settings_sync.sync_once()) == []
    assert needle in settings_sync.status()["devices"][0]["error"]


# --------------------------------------------------------------------------- #
# nudges
# --------------------------------------------------------------------------- #
def test_nudge_peers_tells_every_fleet_device(trio):
    trio.offline.add("mini")
    assert run(settings_sync.nudge_peers()) == ["rig"]
    assert trio.posts == [("laptop", "rig", settings_sync.NUDGE_PATH)]


def test_a_local_change_stamps_and_nudges(devices):
    async def go():
        run_ok = settings_sync.local_change()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return run_ok

    run(settings_sync.enable(""))
    store.update_settings(ui={"accent": "teal"})
    assert run(go()) == ["ui.accent"]
    assert ("laptop", "rig", settings_sync.NUDGE_PATH) in devices.posts
    devices.posts.clear()
    assert run(go()) == []  # nothing new: nobody is bothered
    assert devices.posts == []


def test_a_nudge_pulls_once_after_a_short_delay(devices, monkeypatch):
    monkeypatch.setattr(settings_sync, "NUDGE_DELAY", 0.01)
    calls = []

    async def fake_sync_once():
        calls.append(1)
        return []

    run(settings_sync.enable(""))
    monkeypatch.setattr(settings_sync, "sync_once", fake_sync_once)

    async def go():
        first = settings_sync.nudged()
        second = settings_sync.nudged()  # same burst
        await asyncio.sleep(0.05)
        return first, second

    assert run(go()) == (True, False)
    assert calls == [1]


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(devices, monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    return TestClient(server.app)


def test_export_route_needs_a_credential(devices, client, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    store.update_settings(github={"token": "ghp_secret"})
    run(settings_sync.enable(""))
    r = client.get("/api/settings/sync/export")
    assert r.status_code == 401 and {"error", "id", "epoch"} <= set(r.json())
    assert "ghp_secret" not in r.text
    for cred in (TOKEN, KEY):
        r = client.get(
            "/api/settings/sync/export", headers={"Authorization": "Bearer " + cred}
        )
        assert r.status_code == 200, cred
        body = r.json()
        assert body["protocol"] == 2 and body["fleet"] == FLEET
        assert body["values"]["github.token"] == "ghp_secret"
        assert body["withheld"] == []


def test_settings_save_stamps_immediately(devices, client):
    run(settings_sync.enable(""))
    before = settings_sync._load()["stamps"]["ui.accent"]["h"]
    client.post("/api/settings", json={"ui": {"accent": "teal"}})
    assert settings_sync._load()["stamps"]["ui.accent"]["h"] != before


def test_sync_toggle_route_and_remote_refusal(devices, client, monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    r = client.post(
        "/api/settings/sync",
        json={"enabled": True},
        headers={"x-mindflock-remote": "rig"},
    )
    assert r.status_code == 403
    r = client.post("/api/settings/sync", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.get("/api/settings/sync").json()["devices"][0]["key"] == "rig"
    r = client.post("/api/settings/sync", json={"enabled": True, "from": "ghost"})
    assert r.status_code == 409
    r = client.post("/api/settings/sync", json={"enabled": False})
    assert r.json()["enabled"] is False


def test_sync_route_without_a_fleet_says_join_first(devices, client):
    devices.members.discard("laptop")
    r = client.post("/api/settings/sync", json={"enabled": True})
    assert r.status_code == 409
    assert "join your other devices first" in r.json()["error"]


def test_sync_now_route(devices, client, clock, monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    devices.all_on()
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
    r = client.post("/api/settings/sync/now", headers={"x-mindflock-remote": "rig"})
    assert r.status_code == 403
    r = client.post("/api/settings/sync/now")
    assert r.status_code == 200 and r.json()["adopted"] == ["ui.accent"]
    assert devices.get("ui", "accent") == "teal"


def test_pin_route(devices, client, monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    r = client.post(
        "/api/settings/sync/pin",
        json={"path": "ui.accent", "pinned": True},
        headers={"x-mindflock-remote": "rig"},
    )
    assert r.status_code == 403
    r = client.post("/api/settings/sync/pin", json={"path": "nope", "pinned": True})
    assert r.status_code == 400
    r = client.post(
        "/api/settings/sync/pin", json={"path": "ui.accent", "pinned": True}
    )
    assert r.status_code == 200 and r.json()["pinned"] == ["ui.accent"]
    r = client.post(
        "/api/settings/sync/pin", json={"path": "ui.accent", "pinned": False}
    )
    assert r.json()["pinned"] == []


def test_nudge_route_takes_only_the_fleet_key(devices, client, monkeypatch):
    run(settings_sync.enable(""))
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    scheduled = []
    monkeypatch.setattr(settings_sync, "nudged", lambda: scheduled.append(1) or True)
    r = client.post("/api/settings/sync/nudge")
    assert r.status_code == 401
    # Non-secret, but enough for the caller to tell which side is behind.
    assert {"error", "id", "epoch"} <= set(r.json())
    r = client.post(
        "/api/settings/sync/nudge", headers={"Authorization": "Bearer " + TOKEN}
    )
    assert r.status_code == 401  # this device's token is not a member's key
    r = client.post(
        "/api/settings/sync/nudge", headers={"Authorization": "Bearer " + KEY}
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "scheduled": True}
    assert scheduled == [1]


# --------------------------------------------------------------------------- #
# never from a broken settings.json
# --------------------------------------------------------------------------- #
def test_a_corrupt_settings_file_pauses_sync_instead_of_deleting_everywhere(
    devices, clock, tmp_path
):
    """Read as empty, a truncated file is "every source deleted, the token
    cleared" — stamped now and spread to every device. And an adoption would
    save over the file the user could still fix."""
    store.update_settings(github={"token": "ghp_x"})
    store.set_ticketing_sources([_src("a"), _src("b")])
    devices.all_on()
    p = tmp_path / "laptop" / "settings.json"
    broken = p.read_text()[:-5]
    p.write_text(broken)
    store.invalidate()
    clock.tick()
    assert settings_sync.scan_local() == []
    with pytest.raises(store.SettingsUnreadable):
        settings_sync.export()
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})  # a newer edit elsewhere
    devices.sync("rig", "laptop")
    with devices.on("rig"):
        assert [s for s, _ in _sources()] == ["a", "b"]
        assert devices.get("github", "token") == "ghp_x"
        assert "can't be read" in settings_sync.status()["devices"][0]["error"]
    assert p.read_text() == broken  # never saved over
    st = settings_sync.status()
    assert st["error"] == settings_sync.UNREADABLE
    # Said once (its own banner), not again in the warnings list.
    assert not any(settings_sync.UNREADABLE in w for w in st["warnings"])
    with pytest.raises(LookupError):
        run(settings_sync.enable("rig"))
    assert p.read_text() == broken


def test_merge_refuses_while_the_file_is_unreadable(devices, clock, tmp_path):
    devices.all_on()
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        body = settings_sync.export()
    p = tmp_path / "laptop" / "settings.json"
    p.write_text("{not json")
    store.invalidate()
    assert settings_sync.merge(body) == []
    assert p.read_text() == "{not json"


def test_export_route_answers_503_for_a_broken_file(
    devices, client, tmp_path, monkeypatch
):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)  # no token minted (a save)
    run(settings_sync.enable(""))
    (tmp_path / "laptop" / "settings.json").write_text("[1, 2")
    store.invalidate()
    r = client.get(
        "/api/settings/sync/export", headers={"Authorization": "Bearer " + KEY}
    )
    assert r.status_code == 503
    assert r.json()["error"] == settings_sync.UNREADABLE


def test_a_missing_settings_file_is_just_empty(devices):
    run(settings_sync.enable(""))
    assert settings_sync.readable() is True
    assert settings_sync.export()["values"]["ui.accent"] is None


# --------------------------------------------------------------------------- #
# joining never wipes what only the joiner has set
# --------------------------------------------------------------------------- #
def test_joining_an_unconfigured_device_keeps_the_joiners_settings(devices, clock):
    """The configured laptop asks to join the fresh rig: the rig turns sync on
    (after_admit), the laptop starts from it. The rig's unset fields must not
    clear the laptop's — the laptop's values stay and spread to the rig."""
    store.update_settings(
        github={"token": "ghp_laptop", "repos": ["me/app"]},
        notifications={"ntfy_topic": "t", "ntfy_token": "tk"},
        ui={"accent": "red"},
    )
    store.set_ticketing_sources([_src("a", project="p")])
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})  # the one thing it has
        run(settings_sync.enable("", seed=True))
    clock.tick()
    out = run(settings_sync.enable("rig"))
    assert "github.token" not in out["adopted"]
    assert devices.get("github", "token") == "ghp_laptop"
    assert devices.get("github", "repos") == ["me/app"]
    assert devices.get("notifications", "ntfy_token") == "tk"
    assert devices.get("ui", "accent") == "teal"  # set there: the leader's wins
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_laptop"
        assert devices.get("notifications", "ntfy_topic") == "t"
        assert _sources() == [("a", "p")]
        assert devices.get("ui", "accent") == "teal"


def test_joining_a_device_that_leads_from_here_keeps_the_joiners_settings(
    devices, clock
):
    """Same, when the leader turned sync on itself (stamped now): an unset
    field there still never outranks one set on the joiner."""
    store.update_settings(github={"token": "ghp_laptop"})
    with devices.on("rig"):
        run(settings_sync.enable(""))
    clock.tick()
    run(settings_sync.enable("rig"))
    assert devices.get("github", "token") == "ghp_laptop"
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_laptop"


# --------------------------------------------------------------------------- #
# a seeded enable (admitting a device) never overrides the fleet
# --------------------------------------------------------------------------- #
def test_a_seeded_enable_never_reverts_what_the_fleet_edited(trio, monkeypatch):
    """A member with sync off admits a computer: turning its sync back on as
    a side effect must not spread its stale values over newer edits (a
    rotated token, a deleted source) made elsewhere meanwhile."""
    clock = _clock(monkeypatch, trio)
    store.update_settings(github={"token": "ghp_old"})
    store.set_ticketing_sources([_src("a"), _src("b")])
    trio.all_on()
    with trio.on("rig"):
        settings_sync.disable()
    clock.tick()
    store.update_settings(github={"token": "ghp_NEW"})
    store.set_ticketing_sources([_src("a")])
    trio.sync("mini")
    clock.tick()
    with trio.on("rig"):
        run(settings_sync.enable("", seed=True))  # what after_admit does
    clock.tick()
    trio.sync("laptop", "mini", "rig")
    assert trio.get("github", "token") == "ghp_NEW"
    assert _sources() == [("a", "")]
    with trio.on("rig"):
        assert trio.get("github", "token") == "ghp_NEW"
        assert _sources() == [("a", "")]


def test_a_seeded_device_still_spreads_what_nobody_else_has(devices, clock):
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        store.set_ticketing_sources([_src("r")])
        run(settings_sync.enable("", seed=True))
    run(settings_sync.enable("rig"))
    assert devices.get("ui", "accent") == "teal"
    assert _sources() == [("r", "")]


def test_a_seeded_enable_on_a_syncing_device_changes_nothing(devices, clock):
    store.update_settings(ui={"accent": "red"})
    run(settings_sync.enable(""))
    before = settings_sync._load()["stamps"]
    clock.tick()
    run(settings_sync.enable("", seed=True))
    assert settings_sync._load()["stamps"] == before


def test_enable_does_its_heavy_work_off_the_event_loop(devices, monkeypatch):
    import threading

    seen = []
    real = settings_sync._snapshot

    def spy():
        seen.append(threading.current_thread() is threading.main_thread())
        return real()

    monkeypatch.setattr(settings_sync, "_snapshot", spy)
    run(settings_sync.enable(""))
    assert seen and not any(seen)


# --------------------------------------------------------------------------- #
# a checkout whose origin git can't tell right now
# --------------------------------------------------------------------------- #
def _git_times_out_for(monkeypatch, path):
    real = subprocess.run

    def flaky(cmd, *a, **kw):
        if cmd[:3] == ["git", "-C", path]:
            raise subprocess.TimeoutExpired(cmd, 3)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(settings_sync.subprocess, "run", flaky)
    settings_sync._ORIGINS.clear()


def test_a_git_hiccup_never_ships_this_machines_path(
    devices, clock, tmp_path, monkeypatch
):
    mine = _git_repo(tmp_path / "laptop-src" / "app", "git@github.com:me/app.git")
    theirs = _git_repo(tmp_path / "rig-src" / "app", "https://github.com/me/app.git")
    store.update_settings(repository={"url": mine})
    with devices.on("rig"):
        store.update_settings(general={"last_repo_path": theirs})
    devices.all_on()
    clock.tick(100)  # origin cache expired
    _git_times_out_for(monkeypatch, mine)
    assert settings_sync.scan_local() == []  # the remembered origin answers
    assert settings_sync.export()["values"]["repository.url"] == (
        "git@github.com:me/app.git"
    )
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("repository", "url") == theirs


def test_an_unknown_origin_sits_the_pass_out(devices, clock, tmp_path, monkeypatch):
    """No origin remembered and git can't answer: not exported, not stamped,
    not adopted over — and said so."""
    mine = _git_repo(tmp_path / "laptop-src" / "app", "git@github.com:me/app.git")
    run(settings_sync.enable(""))
    settings_sync._CANON.clear()
    _git_times_out_for(monkeypatch, mine)
    clock.tick()
    store.update_settings(repository={"url": mine})
    assert settings_sync.scan_local() == []
    out = settings_sync.export()
    assert "repository.url" not in out["values"]
    assert "repository.url" not in out["stamps"]
    assert any(
        "Not shared right now" in w and "Repository" in w
        for w in settings_sync.status()["warnings"]
    )
    with devices.on("rig"):
        run(settings_sync.enable("laptop"))
        assert devices.get("repository", "url") == ""
        clock.tick()
        store.update_settings(repository={"url": "https://github.com/me/other"})
    clock.tick()
    devices.sync("laptop")
    assert devices.get("repository", "url") == mine  # never adopted over
    settings_sync._ORIGINS.clear()


def test_an_incoming_path_that_doesnt_exist_here_never_replaces_ours(tmp_path):
    here = str(tmp_path)
    assert settings_sync.localize_url("/nowhere/on/this/box", here) == here
    assert settings_sync.localize_url(here, "") == here
    assert settings_sync.localize_url("/nowhere/on/this/box", "") == (
        "/nowhere/on/this/box"
    )


# --------------------------------------------------------------------------- #
# stamps from the far future
# --------------------------------------------------------------------------- #
def test_a_stamp_that_cant_be_a_real_time_is_never_adopted(devices, clock):
    """Not a finite number, negative, or years ahead: skipped (clamped, it
    would outrank every later edit here), and the device is named."""
    devices.all_on()
    rogue = {
        "protocol": settings_sync.PROTOCOL,
        "fleet": FLEET,
        "device": "rig",
        "enabled": True,
        "stamps": {
            "ui.accent": {"ts": 1e18, "by": "rig", "h": "x"},
            "ui.scroll_speed": {"ts": float("inf"), "by": "rig", "h": "x"},
            "repository.base_branch": {"ts": float("nan"), "by": "rig", "h": "x"},
            "repository.live_branch": {"ts": -5, "by": "rig", "h": "x"},
        },
        "values": {
            "ui.accent": "teal",
            "ui.scroll_speed": 3,
            "repository.base_branch": "x",
            "repository.live_branch": "y",
        },
        "withheld": [],
    }
    assert settings_sync.merge(rogue) == []
    assert devices.get("ui", "accent") == ""
    assert any(
        "Rig's clock is far ahead — its changes are ignored" in w
        for w in settings_sync.status()["warnings"]
    )


def test_a_fast_clock_is_taken_and_a_later_edit_here_still_wins(devices, clock):
    """The rig's clock runs an hour fast. Holding its change back (round 2)
    only postponed the comparison: once the hold lapsed — the clock still
    wrong, or fixed — its old stamp beat an edit made here AFTER it, and the
    later edit was reverted out of nowhere. Now its change is taken at once
    (with a note about its clock) and an edit here after seeing it is
    stamped later still, so it wins and stays. No warning about rig's clock:
    the lead it reads is carried forward by every later edit (HLC), so it
    would name healthy devices too."""
    devices.all_on()
    clock.skew["rig"] = 3600.0
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    clock.tick()
    devices.sync("laptop")
    assert devices.get("ui", "accent") == "teal"  # nothing waits for a clock
    assert not any("clock" in w for w in settings_sync.status()["warnings"])
    clock.tick(5)
    store.update_settings(ui={"accent": "red"})  # the later edit, here
    settings_sync.local_change()
    clock.tick(5)
    devices.sync("laptop", "rig")
    assert devices.get("ui", "accent") == "red"
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "red"
    # 55 minutes on, rig's clock still fast: nothing is reverted.
    clock.tick(3300)
    devices.sync("laptop", "rig")
    assert devices.get("ui", "accent") == "red"
    # NTP fixes rig's clock; nobody edits: still nothing is reverted.
    clock.skew["rig"] = 0.0
    clock.tick(60)
    devices.sync("laptop", "rig")
    assert devices.get("ui", "accent") == "red"
    # rig's next edit lands.
    clock.tick(4000)
    with devices.on("rig"):
        store.update_settings(ui={"accent": "gold"})
        settings_sync.local_change()
    clock.tick()
    devices.sync("laptop")
    assert devices.get("ui", "accent") == "gold"
    assert not any("clock" in w for w in settings_sync.status()["warnings"])


def test_a_lead_carried_by_a_later_edit_never_blames_its_editor(trio, monkeypatch):
    """Rig runs an hour fast; the laptop takes its change, then edits on top
    — stamped rig's time + 1 ms, by the laptop (HLC). Mini pulls the laptop
    with rig asleep: the laptop's clock is fine, so nothing may name it (the
    "clock looks N min ahead" note did, for an hour). A stamp that can't be
    real is still skipped and named."""
    clock = _clock(monkeypatch, trio)
    trio.all_on()
    clock.skew["rig"] = 3600.0
    clock.tick()
    with trio.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    clock.tick()
    trio.sync("laptop")
    clock.tick(5)
    store.update_settings(ui={"accent": "red"})
    settings_sync.local_change(["ui.accent"])
    clock.tick(5)
    trio.offline.add("rig")
    with trio.on("mini"):
        trio.sync("mini")
        assert trio.get("ui", "accent") == "red"
        assert not any("clock" in w for w in settings_sync.status()["warnings"])


def test_a_stamp_too_big_for_a_float_is_skipped(devices, clock, tmp_path):
    """A 400-digit JSON integer parses to an int that float() can't hold
    (OverflowError, not ValueError): skipped like any junk stamp — never an
    exception that aborts the pull or the load."""
    import json

    devices.all_on()
    body = {
        "protocol": settings_sync.PROTOCOL,
        "fleet": FLEET,
        "device": "rig",
        "enabled": True,
        "stamps": {"ui.accent": {"ts": 10**400, "by": "rig", "h": "x"}},
        "values": {"ui.accent": "teal"},
        "withheld": [],
    }
    assert settings_sync.merge(json.loads(json.dumps(body))) == []
    assert devices.get("ui", "accent") == ""
    p = tmp_path / "laptop" / "settings_sync.json"
    data = json.loads(p.read_text())
    data["stamps"]["ui.accent"] = {"ts": 10**400, "by": "laptop", "h": "x"}
    p.write_text(json.dumps(data))
    assert "ui.accent" not in settings_sync._load()["stamps"]


def test_a_slow_clock_here_still_takes_the_others_changes(devices, clock):
    """This device's clock is 10 min behind (WSL after sleep): every other
    device's stamp looks ahead. It used to skip them all and blame the
    healthy devices; now they're taken, and a few minutes ahead says
    nothing."""
    devices.all_on()
    clock.skew["laptop"] = -600.0
    clock.tick()
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        settings_sync.scan_local()
    devices.sync("laptop")
    assert devices.get("ui", "accent") == "teal"
    assert not any("clock" in w for w in settings_sync.status()["warnings"])


def test_a_join_takes_the_leaders_value_from_a_fast_clock(devices, clock):
    """Joining takes the leader's values; a fast clock there no longer makes
    the leader's value wait (and an edit here afterwards still wins)."""
    clock.skew["rig"] = 3600.0
    with devices.on("rig"):
        store.update_settings(ui={"accent": "teal"})
        run(settings_sync.enable(""))
    store.update_settings(ui={"accent": "red"})
    run(settings_sync.enable("rig"))
    assert devices.get("ui", "accent") == "teal"
    clock.tick()
    store.update_settings(ui={"accent": "red"})
    settings_sync.local_change()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == "red"


def test_stored_stamps_that_cant_be_real_are_clamped_on_load(devices, clock, tmp_path):
    import json

    run(settings_sync.enable(""))
    p = tmp_path / "laptop" / "settings_sync.json"
    data = json.loads(p.read_text())
    data["stamps"]["ui.accent"]["ts"] = 1e18
    data["stamps"]["ui.scroll_speed"]["ts"] = -5
    data["stamps"]["repository.base_branch"]["ts"] = 4000.0
    p.write_text(json.dumps(data))
    stamps = settings_sync._load()["stamps"]
    assert stamps["ui.accent"]["ts"] <= 1000 + settings_sync._MAX_AHEAD
    assert "ui.scroll_speed" not in stamps
    assert stamps["repository.base_branch"]["ts"] == 4000.0  # a fast clock: kept


# --------------------------------------------------------------------------- #
# ticket sources without an id
# --------------------------------------------------------------------------- #
def test_a_source_without_an_id_stays_here_and_is_never_renamed(devices, clock):
    """Its id is its ticket slug prefix (the pipeline falls back to ``sc``
    for Shortcut): making one up for sync changed every slug, so every
    ticket it had brought in was ingested again. It stays unsynced, said."""
    import json

    from backend.ticket_ingestion.config import TicketProviderConfig, _assign_source_ids

    path = store.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"ticketing": {"sources": [{"provider": "shortcut", "api_token": "t"}]}}
    path.write_text(json.dumps(doc))
    store.invalidate()
    devices.all_on()
    clock.tick()
    assert settings_sync.scan_local() == []
    assert json.loads(path.read_text()) == doc  # never written back
    store.invalidate()
    cfgs = [
        TicketProviderConfig(provider=s.provider, api_token=s.api_token, id=s.id)
        for s in store.load_settings().ticketing.sources
    ]
    _assign_source_ids(cfgs)
    assert [c.id for c in cfgs] == ["sc"]  # the pipeline's slug prefix holds
    assert not any(
        u.startswith("ticketing.sources") for u in settings_sync.export()["values"]
    )
    assert any(
        "A ticket source without an id isn't synced" in w
        for w in settings_sync.status()["warnings"]
    )
    with devices.on("rig"):
        assert _sources() == []


# --------------------------------------------------------------------------- #
# a write that can't land here
# --------------------------------------------------------------------------- #
def test_a_write_that_fails_is_reported_and_not_retried_every_pass(
    devices, clock, monkeypatch
):
    entries = {"laptop": {}, "rig": {}}
    attempts = []

    def write(key, value):
        attempts.append((devices.me, key))
        if devices.me == "rig" and value == "bad":
            raise ValueError("refused here")
        entries[devices.me][key] = value

    def delete(key):
        entries[devices.me].pop(key, None)

    settings_sync._stores()
    settings_sync.register_store(
        "fake",
        lambda: dict(entries[devices.me]),
        write,
        delete,
        label="Fake things",
    )
    try:
        devices.all_on()
        clock.tick()
        entries["laptop"]["k"] = "bad"
        settings_sync.scan_local()
        for _ in range(3):
            clock.tick()
            devices.sync("rig")
        assert attempts == [("rig", "k")]
        with devices.on("rig"):
            warns = settings_sync.status()["warnings"]
            assert any("Fake things “k” couldn't be applied here" in w for w in warns)
            assert "store:fake#k" not in settings_sync._load()["stamps"]
        # nothing was deleted elsewhere because it never landed here
        clock.tick()
        devices.sync("laptop")
        assert entries["laptop"] == {"k": "bad"}
        # a newer version is tried again
        clock.tick()
        entries["laptop"]["k"] = "good"
        settings_sync.scan_local()
        clock.tick()
        devices.sync("rig")
        assert entries["rig"] == {"k": "good"}
        with devices.on("rig"):
            assert not any("Fake" in w for w in settings_sync.status()["warnings"])
    finally:
        settings_sync.unregister_store("fake")


# --------------------------------------------------------------------------- #
# a v1 state file
# --------------------------------------------------------------------------- #
def test_a_v1_state_file_loads_cleanly(devices, tmp_path):
    """v1 (whole-field stamps, any connected device, no fleet, no "v"): read
    as a fresh state — sync off, no stamps — and with no fleet yet status
    reads off with a hint instead of crashing."""
    import json

    store.update_settings(github={"token": "ghp_x"}, ui={"accent": "teal"})
    store.set_ticketing_sources([_src("a"), _src("b")])
    srcs = store.load_settings().to_dict()["ticketing"]["sources"]
    st = tmp_path / "laptop" / "settings_sync.json"
    st.write_text(
        json.dumps(
            {
                "enabled": True,
                "joined_from": "laptop",
                "stamps": {
                    "ticketing.sources": {
                        "ts": 500.0,
                        "by": "laptop",
                        "h": settings_sync._hash(srcs),
                    },
                    "github.token": {
                        "ts": 500.0,
                        "by": "laptop",
                        "h": settings_sync._hash("ghp_x"),
                    },
                    "ui.accent": {
                        "ts": 500.0,
                        "by": "laptop",
                        "h": settings_sync._hash("teal"),
                    },
                },
            }
        )
    )
    devices.members.discard("laptop")
    data = settings_sync._load()
    assert data["stamps"] == {} and data["enabled"] is False
    assert data["joined_from"] == "" and data["pinned"] == []
    status = settings_sync.status()
    assert status["enabled"] is False
    assert any("isn't one of your devices" in w for w in status["warnings"])
    assert run(settings_sync.sync_once()) == []
    # joining the others later just works
    devices.members.add("laptop")
    with devices.on("rig"):
        store.set_ticketing_sources([_src("r")])
        run(settings_sync.enable("laptop"))
        assert [s for s, _ in _sources()] == ["r", "a", "b"]


# --------------------------------------------------------------------------- #
# round 3: upgrading from v1
# --------------------------------------------------------------------------- #
_V1_SIMPLE = [p for p in settings_sync._simple_paths() if not p.startswith("prefs.")]


def _v1_enable(tmp_path, me, ts):
    """Exactly what v1's enable("") wrote: every shared field stamped at a
    real time, unset ones included — and no "v" marker."""
    import json

    store.invalidate()
    doc = store.load_settings().to_dict()
    stamps = {}
    for p in _V1_SIMPLE:
        g, _, f = p.partition(".")
        v = (doc.get(g) or {}).get(f)
        stamps[p] = {"ts": ts, "by": me, "h": settings_sync._hash(v)}
    (tmp_path / me / "settings_sync.json").write_text(
        json.dumps({"enabled": True, "joined_from": me, "stamps": stamps})
    )


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    yield Devices(tmp_path, monkeypatch, names=("laptop", "mac"))
    store.invalidate()


def _admit_here():
    """What fleet.after_admit does to sync on the admitting device."""
    if not settings_sync.enabled():
        run(settings_sync.enable("", seed=True))


def test_a_v1_laptop_never_wipes_what_a_joining_device_has(
    upgrade, tmp_path, monkeypatch
):
    """The laptop ran v1 sync (every field stamped, unset ones too). Kept,
    those stamps made every field it never set a "deliberate default" that
    cleared the mac's own ntfy topic, budget, launch flags and the phone's
    trusted sign-in when the mac joined it."""
    clock = _clock(monkeypatch, upgrade)
    store.update_settings(github={"token": "ghp_laptop", "repos": ["o/r"]})
    _v1_enable(tmp_path, "laptop", 500.0)
    with upgrade.on("mac"):
        store.update_settings(
            notifications={"ntfy_enabled": True, "ntfy_topic": "mac-topic"},
            general={
                "session_budget_usd": 7.5,
                "tailnet_trusted_logins": ["owner@example.com"],
            },
            coding_cli={"default_launch_args": {"claude": "--model opus"}},
        )
    clock.tick(100)
    _admit_here()
    st = settings_sync._load()
    assert st["stamps"]["notifications.ntfy_topic"]["ts"] <= settings_sync._SEED_TS
    clock.tick(5)
    with upgrade.on("mac"):
        run(settings_sync.enable("laptop"))
        assert upgrade.get("notifications", "ntfy_topic") == "mac-topic"
        assert upgrade.get("general", "session_budget_usd") == 7.5
        assert upgrade.get("general", "tailnet_trusted_logins") == ["owner@example.com"]
        assert upgrade.get("coding_cli", "default_launch_args") == {
            "claude": "--model opus"
        }
        assert upgrade.get("github", "token") == "ghp_laptop"


def test_a_v1_laptops_saves_never_wipe_the_joiners_prefs(
    upgrade, tmp_path, monkeypatch
):
    clock = _clock(monkeypatch, upgrade)
    _v1_enable(tmp_path, "laptop", 500.0)
    clock.tick(100)
    store.update_settings(ui={"scroll_speed": 3})  # a Settings save, pre-join
    settings_sync.local_change()
    with upgrade.on("mac"):
        store.update_settings(prefs={"theme": "dark", "keymap": {"keys": {"x": "y"}}})
    clock.tick(100)
    _admit_here()
    with upgrade.on("mac"):
        run(settings_sync.enable("laptop"))
        assert upgrade.get("prefs", "theme") == "dark"
        assert upgrade.get("prefs", "keymap") == {"keys": {"x": "y"}}


def test_the_state_file_carries_its_version(devices, tmp_path):
    import json

    run(settings_sync.enable(""))
    data = json.loads((tmp_path / "laptop" / "settings_sync.json").read_text())
    assert data["v"] == settings_sync.STATE_VERSION == 2
    assert "v1" not in data
    assert settings_sync.enabled() is True  # a v2 file is read as it is


def _group_of(monkeypatch, *keys):
    """fleet.live_members() for a group of ``keys`` (the Devices harness
    fakes membership, not the roster)."""
    monkeypatch.setattr(
        fleet_module(monkeypatch), "live_members", lambda: {k: {} for k in keys}
    )


def test_an_unmarked_file_in_a_group_turns_sync_back_on_seeded(
    upgrade, tmp_path, monkeypatch
):
    """A device that ran an earlier build with sync on (no "v" marker) and
    is already in a group: no join is coming to turn sync back on, so the
    migration does it at once, seeded — instead of stopping silently. What
    the other device has wins; what only this one has spreads."""
    import json

    clock = _clock(monkeypatch, upgrade)
    _group_of(monkeypatch, "laptop", "mac")
    with upgrade.on("mac"):
        store.update_settings(
            prefs={"theme": "dark"}, notifications={"ntfy_topic": "mac-topic"}
        )
        run(settings_sync.enable(""))
    store.update_settings(github={"token": "ghp_laptop"}, prefs={"theme": "light"})
    _v1_enable(tmp_path, "laptop", 500.0)
    clock.tick(100)
    st = settings_sync.status()
    assert st["enabled"] is True
    assert not any("isn't one of your devices" in w for w in st["warnings"])
    disk = json.loads((tmp_path / "laptop" / "settings_sync.json").read_text())
    assert disk["v"] == settings_sync.STATE_VERSION and disk["enabled"] is True
    assert "v1" not in disk
    assert all(x["ts"] <= settings_sync._SEED_TS for x in disk["stamps"].values())
    assert disk["stamps"]["notifications.ntfy_topic"]["ts"] == settings_sync._UNSET_TS
    clock.tick()
    upgrade.sync("laptop", "mac", "laptop")
    assert upgrade.get("prefs", "theme") == "dark"
    assert upgrade.get("notifications", "ntfy_topic") == "mac-topic"
    with upgrade.on("mac"):
        assert upgrade.get("github", "token") == "ghp_laptop"
        assert upgrade.get("notifications", "ntfy_topic") == "mac-topic"
    assert settings_sync._load()["enabled"] is True  # migrated once, stays on


def test_an_unmarked_file_off_or_alone_stays_off(upgrade, tmp_path, monkeypatch):
    """Sync was off in it, or this device's group is just itself: nothing
    turns on."""
    import json

    p = tmp_path / "laptop" / "settings_sync.json"
    _group_of(monkeypatch, "laptop", "mac")
    p.write_text(json.dumps({"enabled": False, "stamps": {}}))
    assert settings_sync._load()["enabled"] is False
    _group_of(monkeypatch, "laptop")
    _v1_enable(tmp_path, "laptop", 500.0)
    assert settings_sync._load()["enabled"] is False
    assert "v" not in json.loads(p.read_text())  # nothing written


def test_an_unmarked_file_waits_for_a_readable_settings_file(
    upgrade, tmp_path, monkeypatch
):
    import json

    _group_of(monkeypatch, "laptop", "mac")
    store.update_settings(github={"token": "ghp_laptop"})
    _v1_enable(tmp_path, "laptop", 500.0)
    sj = tmp_path / "laptop" / "settings.json"
    good = sj.read_text()
    _corrupt(sj)
    assert settings_sync._load()["enabled"] is False  # can't stamp: not yet
    assert "v" not in json.loads(
        (tmp_path / "laptop" / "settings_sync.json").read_text()
    )
    sj.write_text(good)
    store.invalidate()
    assert settings_sync._load()["enabled"] is True


def test_two_seeded_devices_never_clear_a_value_by_key_order(
    upgrade, tmp_path, monkeypatch
):
    """Both devices migrate (or seed) at once: every stamp at the seed time.
    A tie between "dark" on the laptop and "not set" on the mac used to go to
    the higher key — the mac — and cleared the laptop's theme. Not set is
    stamped below any seeded value."""
    clock = _clock(monkeypatch, upgrade)
    _group_of(monkeypatch, "laptop", "mac")
    store.update_settings(prefs={"theme": "dark"})
    run(settings_sync.enable("", seed=True))
    with upgrade.on("mac"):
        store.update_settings(github={"token": "ghp_mac"})
        run(settings_sync.enable("", seed=True))
    clock.tick()
    upgrade.sync("laptop", "mac", "laptop")
    assert upgrade.get("prefs", "theme") == "dark"
    assert upgrade.get("github", "token") == "ghp_mac"
    with upgrade.on("mac"):
        assert upgrade.get("prefs", "theme") == "dark"
        assert upgrade.get("github", "token") == "ghp_mac"


def test_a_new_unset_field_is_stamped_older_than_any_edit(devices, clock, tmp_path):
    """A field this device's stamps don't know yet (an upgrade added it) and
    that isn't set here: stamped "now", it outranked a value set on another
    device and cleared it there."""
    import json

    devices.all_on()
    p = tmp_path / "laptop" / "settings_sync.json"
    data = json.loads(p.read_text())
    data["stamps"].pop("prefs.theme")  # this device never knew the field
    p.write_text(json.dumps(data))
    clock.tick()
    with devices.on("rig"):
        store.update_settings(prefs={"theme": "dark"})
        settings_sync.local_change()
    clock.tick()
    assert "prefs.theme" in settings_sync.scan_local()
    assert (
        settings_sync._load()["stamps"]["prefs.theme"]["ts"] == settings_sync._UNSET_TS
    )
    devices.sync("laptop", "rig")
    assert devices.get("prefs", "theme") == "dark"
    with devices.on("rig"):
        assert devices.get("prefs", "theme") == "dark"


# --------------------------------------------------------------------------- #
# round 2: no writer saves over a broken file
# --------------------------------------------------------------------------- #
def _corrupt(path):
    broken = path.read_text()[:-5]
    path.write_text(broken)
    store.invalidate()
    return broken


def test_no_writer_saves_defaults_over_a_broken_file(devices, tmp_path):
    store.update_settings(github={"token": "ghp_x"})
    broken = _corrupt(tmp_path / "laptop" / "settings.json")
    with pytest.raises(store.SettingsUnreadable):
        store.update_settings(prefs={"hidden_bars": ["x"]})
    with pytest.raises(store.SettingsUnreadable):
        store.set_ticketing_sources([_src("a")])
    with pytest.raises(store.SettingsUnreadable):
        store.set_auth_profiles([{"id": "p", "kind": "api_key"}])
    assert (tmp_path / "laptop" / "settings.json").read_text() == broken


def test_a_side_write_on_a_paused_broken_file_never_spreads_deletes(
    devices, clock, tmp_path
):
    """The pause held, then anything else wrote (admitting a device picks
    github.automation_device; a browser flushes a pref): the lenient read
    saved defaults over the file, and the next pass spread "every source
    deleted, the token cleared" to every device."""
    store.update_settings(github={"token": "ghp_real", "repos": ["o/r"]})
    store.set_ticketing_sources(
        [{"id": "sc", "provider": "shortcut", "api_token": "T"}]
    )
    devices.all_on()
    clock.tick()
    broken = _corrupt(tmp_path / "laptop" / "settings.json")
    with pytest.raises(store.SettingsUnreadable):  # a side write: refused
        store.update_settings(github={"automation_device": "laptop"})
    assert settings_sync.local_change() == []
    clock.tick()
    devices.sync("laptop", "rig")
    assert (tmp_path / "laptop" / "settings.json").read_text() == broken
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_real"
        assert [s for s, _ in _sources()] == ["sc"]


def test_prefs_routes_refuse_a_broken_file(devices, client, clock, tmp_path):
    store.update_settings(
        github={"token": "ghp_x"}, prefs={"theme": "light", "hidden_bars": ["a"]}
    )
    store.set_ticketing_sources([_src("a"), _src("b")])
    devices.all_on()
    clock.tick()
    p = tmp_path / "laptop" / "settings.json"
    broken = _corrupt(p)
    r = client.post("/api/prefs", json={"hidden_bars": ["x"]})
    assert r.status_code == 409
    assert r.json()["error"] == store.UNREADABLE_HINT
    # GET never serves defaults for it: a seeded browser would apply them
    # over its own (maybe only intact) copy.
    assert client.get("/api/prefs").status_code == 409
    assert client.post("/api/settings", json={"ui": {"accent": "x"}}).status_code == (
        409
    )
    r = client.put("/api/settings/ticketing/sources", json={"sources": [_src("z")]})
    assert r.status_code == 409
    assert p.read_text() == broken
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert [s for s, _ in _sources()] == ["a", "b"]
        assert devices.get("github", "token") == "ghp_x"


# --------------------------------------------------------------------------- #
# round 2: a scan that reads like a reset file pauses
# --------------------------------------------------------------------------- #
def _reset_laptop(tmp_path):
    (tmp_path / "laptop" / "settings.json").write_text("{}")
    store.invalidate()


def _configured(devices):
    """A device with 8 settings set (the pause's floor)."""
    store.update_settings(
        github={"token": "ghp_x", "repos": ["o/r"], "issue_repos": ["o/i"]},
        ui={"accent": "red"},
        notifications={"ntfy_topic": "t0p1c"},
        repository={"base_branch": "dev"},
    )
    store.set_ticketing_sources([_src("a"), _src("b")])
    devices.all_on()


def test_a_scan_that_clears_most_of_this_device_pauses(devices, clock, tmp_path):
    _configured(devices)
    clock.tick()
    _reset_laptop(tmp_path)  # valid JSON, but everything is gone
    assert settings_sync.scan_local() == []
    st = settings_sync.status()
    assert st["paused"] == settings_sync.PAUSED
    assert st["choices"] == ["theirs", "mine"]
    with pytest.raises(store.SettingsUnreadable):
        settings_sync.export()  # nobody pulls (or joins from) a reset device
    clock.tick()
    devices.sync("rig", "laptop")
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_x"
        assert [s for s, _ in _sources()] == ["a", "b"]
        assert settings_sync.status()["devices"][0]["error"] == (
            "Laptop paused sync — its settings look reset; answer it in "
            "Settings → Devices there"
        )
    assert devices.get("github", "token") == ""  # nothing adopted while paused


def test_resume_theirs_takes_the_fleets_settings_back(devices, clock, tmp_path):
    _configured(devices)
    clock.tick()
    _reset_laptop(tmp_path)
    settings_sync.scan_local()
    out = run(settings_sync.resume("theirs"))
    assert "github.token" in out["adopted"]
    assert devices.get("github", "token") == "ghp_x"
    assert devices.get("ui", "accent") == "red"
    assert [s for s, _ in _sources()] == ["a", "b"]
    assert settings_sync.status()["paused"] == ""
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert [s for s, _ in _sources()] == ["a", "b"]


def test_resume_mine_spreads_the_reset(devices, clock, tmp_path):
    _configured(devices)
    clock.tick()
    _reset_laptop(tmp_path)
    settings_sync.scan_local()
    run(settings_sync.resume("mine"))
    assert settings_sync.status()["paused"] == ""
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == ""
        assert _sources() == []
    with pytest.raises(ValueError):
        run(settings_sync.resume("both"))


def test_a_pause_is_announced_once(devices, clock, tmp_path):
    from backend.web.core import events

    _configured(devices)
    clock.tick()
    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    try:
        _reset_laptop(tmp_path)
        settings_sync.scan_local()
        settings_sync.scan_local()  # still paused: not announced again
    finally:
        unsubscribe()
    paused = [e for e in seen if e["event"] == "settings.sync_paused"]
    assert len(paused) == 1
    assert paused[0]["data"]["cleared"] == 8 and paused[0]["data"]["held"] == 8
    assert "Settings → Devices" in paused[0]["data"]["detail"]


_BULK_CLEAR = {
    "github": {"repos": [], "issue_repos": []},
    "ui": {"accent": ""},
    "notifications": {"ntfy_topic": ""},
}


def test_a_bulk_clear_saved_through_a_route_never_pauses(devices, client, clock):
    """Saved on a screen, clearing most of what's set is the person's own
    edit: what the route wrote never counts toward a pause."""
    _configured(devices)
    clock.tick()
    r = client.post("/api/settings", json=_BULK_CLEAR)
    assert r.status_code == 200
    r = client.put("/api/settings/ticketing/sources", json={"sources": []})
    assert r.status_code == 200
    assert settings_sync.status()["paused"] == ""
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("ui", "accent") == ""
        assert _sources() == []
        assert devices.get("github", "token") == "ghp_x"  # not touched


def test_the_same_bulk_clear_unattributed_pauses(devices, clock):
    """The control for the test above: the very same clears, found by a
    scan that no route explains, pause."""
    _configured(devices)
    clock.tick()
    store.update_settings(**_BULK_CLEAR)
    assert settings_sync.local_change() == []  # names nothing it saved
    assert settings_sync.status()["paused"] == settings_sync.PAUSED


def test_a_route_explains_only_what_it_saved(devices, clock, tmp_path):
    """settings.json replaced with {} — then one unrelated save on a screen
    before the background scan sees it. Only the accent is the person's
    edit; the rest of the reset is unexplained and pauses sync, so the
    token and the sources stay on the other devices."""
    _configured(devices)
    clock.tick()
    _reset_laptop(tmp_path)
    store.update_settings(ui={"accent": "blue"})
    assert settings_sync.local_change(["ui.accent"]) == []
    assert settings_sync.status()["paused"] == settings_sync.PAUSED
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_x"
        assert [s for s, _ in _sources()] == ["a", "b"]


def test_deleting_a_broken_file_then_a_pref_retry_still_pauses(
    devices, client, clock, tmp_path
):
    """settings.json won't parse; the person deletes it ("fix or delete
    it") and the browser's kept-dirty pref retries POST /api/prefs before
    the background scan runs. The pref is theirs; every token and source
    gone with the file is not — sync pauses instead of wiping the fleet."""
    _configured(devices)
    clock.tick()
    p = tmp_path / "laptop" / "settings.json"
    _corrupt(p)
    assert settings_sync.scan_local() == []
    p.unlink()
    store.invalidate()
    assert client.post("/api/prefs", json={"theme": "light"}).status_code == 200
    assert settings_sync.status()["paused"] == settings_sync.PAUSED
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == "ghp_x"
        assert [s for s, _ in _sources()] == ["a", "b"]


def test_attribution_covers_only_units_under_the_saved_bases():
    under = settings_sync._attributed
    saved = frozenset({"github", "prefs.theme", "ticketing.sources", "store:providers"})
    assert under("github.token", saved)
    assert under("prefs.theme", saved)
    assert under("ticketing.sources#a", saved)
    assert under("store:providers#cli", saved)
    assert not under("githubx.token", saved)
    assert not under("prefs.themes", saved)
    assert not under("prefs.prompt_presets#p", saved)
    assert not under("store:templates#t", saved)
    assert not under("ui.accent", frozenset())


def test_a_lightly_configured_device_never_pauses(devices, clock):
    """Below 8 settings set, a bulk delete found by the background scan (a
    templates file edited by hand…) is just an edit."""
    store.update_settings(github={"token": "ghp_x"}, ui={"accent": "red"})
    store.set_ticketing_sources([_src("a"), _src("b"), _src("c"), _src("d")])
    devices.all_on()
    clock.tick()
    store.set_ticketing_sources([_src("a")])
    assert len(settings_sync.scan_local()) == 3
    assert settings_sync.status()["paused"] == ""


def test_a_small_delete_is_just_an_edit(devices, clock):
    _configured(devices)
    clock.tick()
    store.set_ticketing_sources([_src("a")])
    assert settings_sync.scan_local() == ["ticketing.sources#b"]
    assert settings_sync.status()["paused"] == ""


def test_resume_route(devices, client, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    _configured(devices)
    clock.tick()
    _reset_laptop(tmp_path)
    settings_sync.scan_local()
    r = client.post(
        "/api/settings/sync/resume",
        json={"keep": "theirs"},
        headers={"x-mindflock-remote": "rig"},
    )
    assert r.status_code == 403
    assert client.post("/api/settings/sync/resume", json={}).status_code == 400
    r = client.post("/api/settings/sync/resume", json={"keep": "theirs"})
    assert r.status_code == 200 and r.json()["paused"] == ""
    assert devices.get("github", "token") == "ghp_x"


# --------------------------------------------------------------------------- #
# round 2: joining honours a deliberate clear on the leader
# --------------------------------------------------------------------------- #
def test_joining_takes_a_value_the_leader_set_back_on_purpose(devices, clock):
    """The fleet cleared a revoked token and turned phone notifications off
    (that's the default): a device joining with the old values must not
    bring them back to every device."""
    with devices.on("rig"):
        store.update_settings(
            notifications={"ntfy_enabled": True}, github={"token": "ghp_revoked"}
        )
        run(settings_sync.enable(""))
        clock.tick()
        store.update_settings(
            github={"token": ""}, notifications={"ntfy_enabled": False}
        )
        settings_sync.scan_local()
    clock.tick()
    store.update_settings(
        github={"token": "ghp_revoked"}, notifications={"ntfy_enabled": True}
    )
    run(settings_sync.enable("rig"))
    assert devices.get("github", "token") == ""
    assert devices.get("notifications", "ntfy_enabled") is False
    clock.tick()
    devices.sync("rig")
    with devices.on("rig"):
        assert devices.get("github", "token") == ""
        assert devices.get("notifications", "ntfy_enabled") is False


# --------------------------------------------------------------------------- #
# round 2: a write that didn't land keeps this device's own stamp
# --------------------------------------------------------------------------- #
def test_a_failed_write_never_serves_the_old_value_as_the_new_version(
    trio, monkeypatch
):
    """The rig couldn't write the laptop's v2 (a red-zone conflict). Under
    the laptop's stamp its old v1 read as that version: the mini, pulling
    the rig first, took v1 and never took the laptop's v2."""
    clock = _clock(monkeypatch, trio)
    data = {"laptop": {}, "rig": {}, "mini": {}}
    tries = []

    def write(key, value):
        if trio.me == "rig" and value == "v2":
            tries.append(key)
            raise ValueError("conflicts with a zone here")
        data[trio.me][key] = value

    settings_sync.register_store(
        "toy",
        lambda: dict(data[trio.me]),
        write,
        lambda key: data[trio.me].pop(key, None),
        label="Toy",
    )
    try:
        for k in data:
            data[k]["x"] = "v1"
        trio.all_on()
        clock.tick()
        data["laptop"]["x"] = "v2"
        with trio.on("laptop"):
            settings_sync.scan_local()
            laptop_stamp = settings_sync._load()["stamps"]["store:toy#x"]
        clock.tick()
        trio.sync("rig")
        with trio.on("rig"):
            st = settings_sync._load()["stamps"]["store:toy#x"]
            assert st["ts"] < laptop_stamp["ts"]  # its own, older stamp
            assert settings_sync._unlanded["store:toy#x"] == (
                laptop_stamp["ts"],
                "laptop",
            )
        trio.offline.add("laptop")
        clock.tick()
        trio.sync("mini")
        trio.offline.discard("laptop")
        clock.tick()
        trio.sync("mini")
        assert data["mini"]["x"] == "v2"
        for _ in range(2):  # the same version isn't retried every pass
            clock.tick()
            trio.sync("rig")
        assert tries == ["x"]
    finally:
        settings_sync.unregister_store("toy")


# --------------------------------------------------------------------------- #
# round 2: origin caches
# --------------------------------------------------------------------------- #
def _git_always_times_out(monkeypatch, calls):
    def slow(cmd, *a, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 3)

    monkeypatch.setattr(settings_sync.subprocess, "run", slow)
    monkeypatch.setattr(settings_sync, "_ORIGINS", {})
    monkeypatch.setattr(settings_sync, "_CANON", {})


def test_a_git_timeout_is_cached_too(tmp_path, monkeypatch):
    """Under memory pressure git times out (3 s): asked again on every
    canonical() call, one pass cost N x 3 s while holding the sync lock."""
    calls = []
    _git_always_times_out(monkeypatch, calls)
    (tmp_path / ".git").mkdir()
    for _ in range(5):
        assert settings_sync._origin_of(str(tmp_path)) is None
    assert len(calls) == 1
    settings_sync._remember_origin(str(tmp_path), "git@x:o/r.git")
    assert settings_sync._origin_of(str(tmp_path)) == "git@x:o/r.git"
    assert len(calls) == 1  # the remembered origin answers a cached failure


def test_probing_checkouts_asks_git_once_per_checkout(tmp_path, monkeypatch):
    calls = []
    _git_always_times_out(monkeypatch, calls)
    paths = []
    for i in range(20):
        (tmp_path / ("r%d" % i) / ".git").mkdir(parents=True)
        paths.append(str(tmp_path / ("r%d" % i)))
    monkeypatch.setattr(settings_sync, "known_checkouts", lambda: paths)
    url = "https://github.com/x/y.git"
    for _ in range(3):
        assert settings_sync.localize_url(url, "") == url
    assert len(calls) == 20


def test_the_remembered_origins_are_capped_where_they_are_kept(devices, monkeypatch):
    import json

    monkeypatch.setattr(settings_sync, "_CANON", {})
    run(settings_sync.enable(""))
    for i in range(600):
        settings_sync._remember_origin("/w/%d" % i, "git@x:o/r%d.git" % i)
        if i == 0:
            settings_sync._save(settings_sync._load())
        if i % 50 == 0:
            assert settings_sync._canon_get("/w/0")  # used: stays
            settings_sync._save(settings_sync._load())
    settings_sync._save(settings_sync._load())
    p = store.settings_path().parent / "settings_sync.json"
    canon = json.loads(p.read_text())["canon"]
    assert len(canon) == settings_sync._CANON_MAX
    assert "/w/599" in canon and "/w/1" not in canon
    settings_sync._CANON.clear()
    settings_sync._load()  # a fresh process reads what was kept
    assert len(settings_sync._CANON) == settings_sync._CANON_MAX
