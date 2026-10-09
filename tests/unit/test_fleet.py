"""Your devices — the fleet roster, joins and gossip (:mod:`backend.web.core.fleet`
+ the routes in :mod:`backend.web.addons.fleet`).

The store and the state machines are tested directly. The join flows run
several "devices" in one process (:class:`_World`): each has its own settings
dir (so its own ``fleet.json``), "which device am I" is switched around every
call, and the fake ``remote.post_json`` / ``get_json`` deliver each
device-to-device request to the OTHER device's real fleet routes through an
in-process ASGI client — so redeem, requests, adopt, rekey and roster run the
real code on both ends. Nothing touches the network or the real ~/.mindflock.

The few tests at the bottom that need unit A2's auth changes
(``auth.privileged`` / public fleet paths in the middleware) skip until those
land.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat

import httpx
import pytest
from fastapi import FastAPI

from backend.config import settings as store
from backend.web.addons.fleet import FleetAddon
from backend.web.core import auth as web_auth
from backend.web.core import events as events_mod
from backend.web.core import fleet, remote, settings_hooks, settings_sync

DEVICES = ("laptop", "rig", "mini")
OTHER_ID = "fedcba9876543210"  # pragma: allowlist secret — a fleet id, not a key


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """Fresh fleet memory, no tailnet, auth off, one device ("laptop")."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(fleet, "_INVITES", [])
    monkeypatch.setattr(fleet, "_FAILS", {})
    monkeypatch.setattr(fleet, "_ALL_FAILS", [])
    monkeypatch.setattr(fleet, "_LOCKED_UNTIL", {})
    monkeypatch.setattr(fleet, "_LOCKOUTS", {})
    monkeypatch.setattr(fleet, "_JOIN_SECRET", [None])
    monkeypatch.setattr(fleet, "_SERVE", {"at": 0.0, "exposed": 0.0})
    monkeypatch.setattr(fleet, "_check_serve", lambda: False)  # no `tailscale`
    monkeypatch.setattr(fleet, "_REQUESTS", {})
    monkeypatch.setattr(fleet, "_JOIN", fleet._idle_join())
    monkeypatch.setattr(fleet, "_JOIN_TASK", [None])
    monkeypatch.setattr(fleet, "_HITS", {})
    monkeypatch.setattr(fleet, "_STALE", {})
    monkeypatch.setattr(fleet, "_PEERS", {})
    monkeypatch.setattr(fleet, "_READMITTED", {})
    monkeypatch.setattr(fleet, "_CACHE", {"sig": None, "doc": None})
    monkeypatch.setattr(fleet, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(remote, "_SELF", {})
    monkeypatch.setattr(remote, "_TOKENS", {})
    monkeypatch.setattr(remote, "_persist_tokens", lambda: None)
    monkeypatch.setattr(
        remote,
        "self_identity",
        lambda: {"key": "laptop", "host": "Laptop", "dns": "", "ip": "", "os": ""},
    )
    sync_calls = []

    async def fake_enable(start_from="", *, seed=False):
        sync_calls.append(("enable", start_from) if not seed else ("seed", start_from))
        return {"enabled": True}

    monkeypatch.setattr(settings_sync, "enable", fake_enable)
    monkeypatch.setattr(
        settings_sync, "disable", lambda: sync_calls.append(("disable", ""))
    )
    monkeypatch.setattr(settings_sync, "enabled", lambda: False)
    fleet._test_sync_calls = sync_calls  # type: ignore[attr-defined]
    yield
    store.invalidate()


@pytest.fixture
def events():
    got = []
    unsub = events_mod.BUS.subscribe(
        lambda env: (got.append(env) if env["event"].startswith("device.") else None)
    )
    yield got
    unsub()


def _clock(monkeypatch, start=1000.0):
    t = {"now": start}
    monkeypatch.setattr(fleet, "_now", lambda: t["now"])

    def tick(dt=1.0):
        t["now"] += dt
        return t["now"]

    return tick


def _sync_calls():
    return fleet._test_sync_calls  # type: ignore[attr-defined]


def _bundle(**over):
    b = {
        "id": "0123456789abcdef",
        "key": "k" * 43,
        "epoch": 1,
        "members": {"rig": {"host": "Rig", "added_at": 10.0, "added_by": "rig"}},
        "removed": {},
    }
    b.update(over)
    return b


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_empty_store_is_not_a_fleet_and_matches_no_key():
    assert not fleet.in_fleet()
    assert fleet.fleet_id() == "" and fleet.fleet_key() == ""
    assert fleet.state() == fleet._empty()
    assert not fleet.key_valid("")
    assert not fleet.key_valid(None)
    assert not fleet.key_valid("anything")
    assert fleet.live_members() == {}


def test_create_is_idempotent_and_private(tmp_path):
    doc = fleet.create()
    assert fleet.in_fleet()
    assert len(doc["id"]) == 16 and int(doc["id"], 16) >= 0
    assert doc["epoch"] == 1
    assert list(doc["members"]) == ["laptop"]
    assert doc["members"]["laptop"]["added_by"] == "laptop"
    assert fleet.create() == doc  # idempotent: same id, same key
    path = fleet._path()
    assert path.name == "fleet.json"
    assert path.parent == store.settings_path().parent
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert fleet.key_valid(doc["key"])
    assert not fleet.key_valid(doc["key"] + "x")
    assert not fleet.key_valid("")


def test_state_is_a_copy():
    fleet.create()
    s = fleet.state()
    s["members"]["evil"] = {"host": "x", "added_at": 1.0, "added_by": "x"}
    assert "evil" not in fleet.state()["members"]


def test_corrupt_file_reads_as_no_fleet():
    path = fleet._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert not fleet.in_fleet()
    path.write_text(json.dumps({"id": "abc", "key": "", "members": "nope"}))
    assert fleet.state() == fleet._empty()


def test_external_write_is_seen():
    """The cache is keyed by mtime/size — a hand edit (or another process)
    is read on the next call."""
    fleet.create()
    doc = json.loads(fleet._path().read_text())
    doc["members"]["rig"] = {"host": "Rig", "added_at": 5.0, "added_by": "x"}
    fleet._path().write_text(json.dumps(doc, indent=4))
    assert fleet.is_member("rig")


def test_tombstone_and_readd(monkeypatch):
    tick = _clock(monkeypatch)
    fleet.create()
    fleet.add_member("rig", "Rig", by="laptop")
    assert fleet.is_member("rig")
    tick()
    fleet.remove_member("rig")
    assert not fleet.is_member("rig")
    assert "rig" not in fleet.live_members()
    assert "rig" in fleet.state()["members"]  # the entry stays, tombstoned
    # A re-add in the SAME instant as the removal still takes.
    fleet.add_member("rig", "Rig", by="laptop")
    assert fleet.is_member("rig")
    assert (
        fleet.state()["members"]["rig"]["added_at"]
        > fleet.state()["removed"]["rig"]["at"]
    )
    assert fleet.state()["removed"]["rig"]["by"] == "laptop"  # who removed it


def test_remove_with_a_clock_behind_the_add_still_removes(monkeypatch):
    tick = _clock(monkeypatch, start=1000.0)
    fleet.create()
    fleet.merge_roster(
        {
            "id": fleet.fleet_id(),
            "epoch": 1,
            "members": {"rig": {"host": "Rig", "added_at": 1200.0, "added_by": "x"}},
            "removed": {},
        }
    )
    tick()
    fleet.remove_member("rig")  # our clock (1001) is behind the adder's (1200)
    assert not fleet.is_member("rig")


def test_add_member_rejects_bad_names():
    fleet.create()
    for bad in ("", "Rig", "-rig", "a" * 64, "rig/../x", "rig box"):
        with pytest.raises(ValueError):
            fleet.add_member(bad, "x", by="laptop")


def test_bundle_has_key_roster_does_not():
    fleet.create()
    assert set(fleet.bundle()) == {"id", "key", "epoch", "members", "removed"}
    assert set(fleet.roster()) == {"id", "epoch", "members", "removed"}
    assert fleet.bundle()["key"] == fleet.fleet_key()


def test_merge_roster_unions_by_max_timestamp():
    fleet.create()
    fid = fleet.fleet_id()
    fleet.add_member("rig", "Rig", by="laptop")
    rig_at = fleet.state()["members"]["rig"]["added_at"]
    remote_roster = {
        "id": fid,
        "epoch": 1,
        "members": {
            "rig": {"host": "OLD", "added_at": rig_at - 100, "added_by": "x"},
            "mini": {"host": "Mini", "added_at": 50.0, "added_by": "rig"},
        },
        "removed": {"ghost": 7.0},
    }
    assert fleet.merge_roster(remote_roster) is True
    s = fleet.state()
    assert s["members"]["rig"]["host"] == "Rig"  # ours was newer
    assert s["members"]["mini"]["host"] == "Mini"
    # An older build's bare-timestamp tombstone reads as one with no "by".
    assert s["removed"] == {"ghost": {"at": 7.0, "by": ""}}
    assert fleet.merge_roster(remote_roster) is False  # nothing new
    # A newer tombstone wins over an older add.
    remote_roster["removed"] = {"mini": 60.0}
    assert fleet.merge_roster(remote_roster) is True
    assert not fleet.is_member("mini")


def test_merge_roster_ignores_another_fleet_and_garbage():
    fleet.create()
    before = fleet.state()
    other = {
        "id": "ffffffffffffffff",
        "members": {"mini": {"host": "M", "added_at": 1.0, "added_by": "x"}},
        "removed": {},
    }
    assert fleet.merge_roster(other) is False
    assert fleet.merge_roster("nope") is False  # type: ignore[arg-type]
    assert (
        fleet.merge_roster(
            {"id": before["id"], "members": {"BAD NAME": {}}, "removed": {"x": "y"}}
        )
        is False
    )
    assert fleet.state() == before


def test_merge_roster_outside_a_fleet_does_nothing():
    assert fleet.merge_roster(_bundle()) is False
    assert not fleet.in_fleet()


def test_merge_roster_that_removes_this_device_leaves(events):
    fleet.create()
    fid = fleet.fleet_id()
    at = fleet.state()["members"]["laptop"]["added_at"]
    assert fleet.merge_roster(
        {"id": fid, "epoch": 1, "members": {}, "removed": {"laptop": at + 1}}
    )
    assert not fleet.in_fleet()
    assert ("disable", "") in _sync_calls()
    assert [e["event"] for e in events] == ["device.removed"]
    assert events[0]["data"]["device"] == "laptop"


def test_rekey_and_apply_rekey():
    fleet.create()
    old = fleet.fleet_key()
    new = fleet.rekey()
    assert new != old and fleet.fleet_key() == new
    assert fleet.state()["epoch"] == 2
    assert not fleet.key_valid(old)

    fid = fleet.fleet_id()
    body = {"id": fid, "epoch": 2, "key": "n" * 43, "members": {}, "removed": {}}
    assert fleet.apply_rekey(body) is False  # not newer
    assert fleet.apply_rekey(dict(body, epoch=3, id="ffffffffffffffff")) is False
    assert fleet.apply_rekey(dict(body, epoch=3, key="short")) is False
    assert fleet.apply_rekey(dict(body, epoch="x")) is False
    body.update(
        epoch=3,
        members={"rig": {"host": "Rig", "added_at": 9e9, "added_by": "laptop"}},
    )
    assert fleet.apply_rekey(body) is True
    assert fleet.fleet_key() == "n" * 43 and fleet.state()["epoch"] == 3
    assert fleet.is_member("rig")  # the roster rode along


def test_rekey_outside_a_fleet_raises():
    with pytest.raises(ValueError):
        fleet.rekey()


def test_adopt_bundle_validates():
    for bad in (
        "x",
        _bundle(id="nothex"),
        _bundle(key=""),
        _bundle(key="has spaces in it!!!!!"),
        _bundle(epoch=0),
        _bundle(epoch="x"),
        _bundle(members=[]),
    ):
        with pytest.raises(ValueError):
            fleet.adopt_bundle(bad)
    assert not fleet.in_fleet()


def test_adopt_bundle_joins_and_adds_self():
    fleet.adopt_bundle(_bundle())
    assert fleet.fleet_id() == "0123456789abcdef"
    assert fleet.key_valid("k" * 43)
    assert set(fleet.live_members()) == {"rig", "laptop"}


def test_adopt_bundle_replaces_a_fleet_of_one():
    fleet.create()
    fleet.adopt_bundle(_bundle())
    assert fleet.fleet_id() == "0123456789abcdef"


def test_adopt_bundle_refuses_to_orphan_another_fleet():
    fleet.create()
    fleet.add_member("mini", "Mini", by="laptop")
    with pytest.raises(ValueError, match="already one of 2 devices — leave that group"):
        fleet.adopt_bundle(_bundle())
    assert fleet.fleet_id() != "0123456789abcdef"


def test_adopt_bundle_same_fleet_older_key_is_refused_and_changes_nothing():
    fleet.adopt_bundle(_bundle(epoch=3, key="a" * 43))
    before = fleet.state()
    with pytest.raises(fleet.OlderBundle, match="older key"):
        fleet.adopt_bundle(
            _bundle(
                epoch=2,
                key="b" * 43,
                members={"mini": {"host": "Mini", "added_at": 5.0, "added_by": "rig"}},
            )
        )
    assert fleet.state() == before
    assert not fleet.is_member("mini")


def test_adopt_bundle_same_fleet_same_epoch_unions():
    fleet.adopt_bundle(_bundle(epoch=3, key="a" * 43))
    fleet.adopt_bundle(
        _bundle(
            epoch=3,
            key="a" * 43,
            members={"mini": {"host": "Mini", "added_at": 5.0, "added_by": "rig"}},
        )
    )
    assert fleet.fleet_key() == "a" * 43 and fleet.state()["epoch"] == 3
    assert {"rig", "mini", "laptop"} <= set(fleet.live_members())


def test_adopt_bundle_newer_epoch_replaces_the_roster(monkeypatch):
    """Finding [0]: whatever this device merged while it held the OLD key
    (here: the removed rig re-adding itself under that key) must not survive
    a rejoin onto the new key — and the old key is kept for healing others."""
    tick = _clock(monkeypatch, start=1000.0)
    fid = "0123456789abcdef"
    m = lambda k, t: {"host": k.title(), "added_at": t, "added_by": "laptop"}  # noqa
    fleet.adopt_bundle(
        _bundle(
            epoch=1,
            key="A" * 43,
            members={"rig": m("rig", 10.0), "mini": m("mini", 10.0)},
        )
    )
    tick(100)
    # rig, removed elsewhere at t=1050, re-adds itself here under K1 (honest clock).
    assert fleet.merge_roster(
        {"id": fid, "epoch": 1, "members": {"rig": m("rig", 1100.0)}, "removed": {}}
    )
    assert fleet.is_member("rig")
    fleet.adopt_bundle(
        _bundle(
            epoch=2,
            key="B" * 43,
            members={
                "rig": m("rig", 10.0),
                "mini": m("mini", 10.0),
                "laptop": m("laptop", 10.0),
            },
            removed={"rig": 1050.0},
        )
    )
    assert fleet.fleet_key() == "B" * 43
    assert not fleet.is_member("rig")
    assert fleet.prev_key(1) == "A" * 43


def test_adopt_bundle_that_tombstones_self_is_refused():
    with pytest.raises(ValueError, match="removed from that group"):
        fleet.adopt_bundle(
            _bundle(
                members={
                    "rig": {"host": "Rig", "added_at": 1.0, "added_by": "rig"},
                    "laptop": {"host": "Laptop", "added_at": 1.0, "added_by": "rig"},
                },
                removed={"laptop": 2.0},
            )
        )
    assert not fleet.in_fleet()


def test_merge_roster_only_on_the_same_epoch():
    fleet.create()
    fid = fleet.fleet_id()
    rig = {"rig": {"host": "Rig", "added_at": 5.0, "added_by": "x"}}
    for ep in (None, 0, 2, "x"):
        body = {"id": fid, "members": rig, "removed": {}}
        if ep is not None:
            body["epoch"] = ep
        assert fleet.merge_roster(body) is False
    assert not fleet.is_member("rig")
    assert fleet.merge_roster({"id": fid, "epoch": 1, "members": rig, "removed": {}})
    assert fleet.is_member("rig")


def test_clean_drops_infinite_and_clamps_future_timestamps(monkeypatch):
    _clock(monkeypatch, start=1000.0)
    fleet.create()
    fid = fleet.fleet_id()
    body = json.loads(
        '{"id": "%s", "epoch": 1, "members": {"rig": {"host": "R", "added_at":'
        ' Infinity}, "mini": {"host": "M", "added_at": 1e12}}, "removed":'
        ' {"laptop": NaN, "ghost": Infinity, "old": 1e12}}' % fid
    )
    fleet.merge_roster(body)
    s = fleet.state()
    assert "rig" not in s["members"]
    assert s["members"]["mini"]["added_at"] == 1000.0 + fleet._MAX_SKEW
    assert s["removed"] == {"old": {"at": 1000.0 + fleet._MAX_SKEW, "by": ""}}
    assert fleet.is_member("laptop")


def test_leave_resets_everything():
    fleet.create()
    fleet.create_invite()
    fleet.leave()
    assert fleet.state() == fleet._empty()
    assert fleet.invites() == []


# --------------------------------------------------------------------------- #
# codes
# --------------------------------------------------------------------------- #
def test_normalize_code():
    assert fleet.normalize_code("abcd-efgh") == "ABCDEFGH"
    assert fleet.normalize_code(" ab cd  ef-gh ") == "ABCDEFGH"
    assert fleet.normalize_code("iLoU") == "110V"
    assert fleet.normalize_code("") == ""
    assert fleet.normalize_code(None) == ""  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text, want",
    [
        ("mindflock devices join laptop ABCD-EFGH", ("laptop", "ABCDEFGH")),
        ("  MindFlock Devices JOIN Laptop abcd-efgh ", ("laptop", "ABCDEFGH")),
        ("laptop ABCD-EFGH", ("laptop", "ABCDEFGH")),
        ("laptop abcd efgh", ("laptop", "ABCDEFGH")),
        ("ABCD-EFGH", ("", "ABCDEFGH")),
        ("ABCD EFGH", ("", "ABCDEFGH")),
        ("abcdefgh", ("", "ABCDEFGH")),
        ("", ("", "")),
    ],
)
def test_parse_join_string(text, want):
    assert fleet.parse_join_string(text) == want


# --------------------------------------------------------------------------- #
# invites
# --------------------------------------------------------------------------- #
def test_create_invite_shape_and_starts_a_fleet():
    inv = fleet.create_invite()
    assert fleet.in_fleet()
    code = inv["code"]
    assert len(code) == 9 and code[4] == "-"
    assert all(c in fleet.ALPHABET for c in code.replace("-", ""))
    assert inv["device"] == "laptop"
    assert inv["command"] == "mindflock devices join laptop %s" % code
    assert inv["expires_at"] > fleet._now() + fleet.INVITE_TTL - 5


def test_at_most_three_live_invites_oldest_dropped():
    codes = [fleet.create_invite()["code"] for _ in range(4)]
    assert [i["code"] for i in fleet.invites()] == codes[1:]
    with pytest.raises(PermissionError):
        fleet.redeem(codes[0], "rig", "Rig")


def test_redeem_is_single_use_and_hands_out_a_bundle_with_the_joiner(events):
    """Findings [10]/[43]: the joiner is on the BUNDLE's roster, not on ours —
    it puts itself on ours once it holds the key (its announce), so a join
    that fails on its side leaves no ghost member here."""
    code = fleet.create_invite()["code"]
    b = fleet.redeem(code, "rig", "Rig", dns="rig.tail1234.ts.net")
    assert b["key"] == fleet.fleet_key() and b["id"] == fleet.fleet_id()
    assert b["members"]["rig"]["added_by"] == "laptop"
    assert b["members"]["rig"]["dns"] == "rig.tail1234.ts.net"
    assert not fleet.is_member("rig")
    assert "rig" not in fleet.state()["members"]
    assert events == []  # "joined" comes when the joiner confirms
    with pytest.raises(PermissionError, match="that code is wrong or expired"):
        fleet.redeem(code, "mini", "Mini")
    # The joiner's announce (same key epoch) is what makes it a member here.
    assert fleet.merge_roster({k: b[k] for k in ("id", "epoch", "members", "removed")})
    assert fleet.is_member("rig")
    assert [e["event"] for e in events] == ["device.joined"]
    assert events[0]["data"]["device"] == "rig"
    assert events[0]["data"]["detail"] == "Rig joined your devices"


def test_redeem_forgives_case_dashes_and_lookalikes(monkeypatch):
    monkeypatch.setattr(fleet.secrets, "choice", lambda alphabet: "1")
    code = fleet.create_invite()["code"]
    assert code == "1111-1111"
    assert "rig" in fleet.redeem("il1L i-LlI", "rig", "Rig")["members"]


def test_redeem_rejects_bad_device_name():
    code = fleet.create_invite()["code"]
    with pytest.raises(ValueError):
        fleet.redeem(code, "Not A Name", "x")


def test_invite_expires(monkeypatch):
    tick = _clock(monkeypatch)
    code = fleet.create_invite()["code"]
    tick(fleet.INVITE_TTL + 1)
    assert fleet.invites() == []
    with pytest.raises(PermissionError):
        fleet.redeem(code, "rig", "Rig")


def test_cancel_invites():
    code = fleet.create_invite()["code"]
    fleet.cancel_invites()
    with pytest.raises(PermissionError):
        fleet.redeem(code, "rig", "Rig")


def test_wrong_codes_lock_out_only_that_caller(monkeypatch):
    """Finding [27]: one caller's guesses lock THAT caller out; they never
    cost the owner's real joiner its code."""
    tick = _clock(monkeypatch)
    real = fleet.create_invite()["code"]
    evil = "100.64.0.66"
    for _ in range(fleet._FAIL_LIMIT - 1):
        with pytest.raises(PermissionError) as exc:
            fleet.redeem("ZZZZ-ZZZZ", "evil", "Evil", ip=evil)
        assert not isinstance(exc.value, fleet.TooManyAttempts)
        tick()
    with pytest.raises(PermissionError):
        fleet.redeem("ZZZZ-ZZZZ", "evil", "Evil", ip=evil)  # the 5th: locked
    with pytest.raises(fleet.TooManyAttempts):
        fleet.redeem(real, "evil", "Evil", ip=evil)  # even a right code
    assert len(fleet.invites()) == 1  # the real code survives
    assert "rig" in fleet.redeem(real, "rig", "Rig", ip="100.64.0.7")["members"]
    tick(fleet._LOCKOUT + 1)
    with pytest.raises(PermissionError) as exc:
        fleet.redeem("ZZZZ-ZZZZ", "evil", "Evil", ip=evil)  # lockout over
    assert not isinstance(exc.value, fleet.TooManyAttempts)


def test_guessing_from_many_callers_burns_every_invite(monkeypatch):
    tick = _clock(monkeypatch)
    real = fleet.create_invite()["code"]
    for i in range(fleet._GLOBAL_FAIL_LIMIT):
        with pytest.raises(PermissionError) as exc:
            fleet.redeem("ZZZZ-ZZZZ", "evil", "Evil", ip="100.64.1.%d" % i)
        assert not isinstance(exc.value, fleet.TooManyAttempts)
        tick()
    assert fleet.invites() == []
    with pytest.raises(PermissionError):
        fleet.redeem(real, "rig", "Rig", ip="100.64.0.7")  # burned
    assert "rig" in fleet.redeem(fleet.create_invite()["code"], "rig", "Rig")["members"]


def test_wrong_codes_spread_over_more_than_the_window_never_lock(monkeypatch):
    tick = _clock(monkeypatch)
    fleet.create_invite()
    for _ in range(fleet._FAIL_LIMIT * 2):
        with pytest.raises(PermissionError) as exc:
            fleet.redeem("ZZZZ-ZZZZ", "evil", "Evil")
        assert not isinstance(exc.value, fleet.TooManyAttempts)
        tick(fleet._FAIL_WINDOW / (fleet._FAIL_LIMIT - 1) + 1)
        fleet.create_invite()
    assert fleet.invites()


# --------------------------------------------------------------------------- #
# incoming requests
# --------------------------------------------------------------------------- #
def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def test_open_request_shape_and_event(events):
    out = fleet.open_request("rig", "Rig", _hash("s3cret"))
    assert len(out["id"]) == 16
    code = out["code"]
    assert len(code) == 7 and code[3] == " " and code.replace(" ", "").isdigit()
    [ev] = events
    assert ev["event"] == "device.join_requested"
    assert ev["data"] == {
        "device": "rig",
        "host": "Rig",
        "code": code,
        "id": out["id"],
        "detail": "Rig · code %s" % code,
    }
    [row] = fleet.pending_requests()
    assert row["id"] == out["id"] and row["device"] == "rig" and row["code"] == code
    assert "secret_hash" not in row


def test_open_request_validates():
    with pytest.raises(ValueError):
        fleet.open_request("Not Valid", "x", _hash("s"))
    with pytest.raises(ValueError):
        fleet.open_request("rig", "x", "abc")
    with pytest.raises(ValueError):
        fleet.open_request("rig", "x", _hash("s").upper())


def test_rerequest_replaces_and_at_most_five_pending(monkeypatch):
    tick = _clock(monkeypatch)
    first = fleet.open_request("rig", "Rig", _hash("a"))
    second = fleet.open_request("rig", "Rig", _hash("b"))
    assert [r["id"] for r in fleet.pending_requests()] == [second["id"]]
    with pytest.raises(KeyError):
        fleet.request_state(first["id"], "a")
    for i in range(6):
        tick()
        fleet.open_request("dev%d" % i, "Dev", _hash("x"))
    devices = [r["device"] for r in fleet.pending_requests()]
    assert len(devices) == fleet.MAX_REQUESTS
    assert devices == ["dev1", "dev2", "dev3", "dev4", "dev5"]  # oldest dropped


def test_request_state_checks_the_secret_and_serves_the_bundle_once(events):
    out = fleet.open_request("rig", "Rig", _hash("s3cret"))
    with pytest.raises(PermissionError):
        fleet.request_state(out["id"], "wrong")
    with pytest.raises(PermissionError):
        fleet.request_state(out["id"], "")
    assert fleet.request_state(out["id"], "s3cret") == {"state": "pending"}
    res = fleet.approve(out["id"])
    assert res == {"ok": True, "device": "rig", "host": "Rig", "runs_automation": False}
    assert fleet.in_fleet()  # approve started the fleet …
    assert not fleet.is_member("rig")  # … but rig adds itself, once it has the key
    assert fleet.pending_requests() == []
    got = fleet.request_state(out["id"], "s3cret")
    assert got["state"] == "approved"
    assert got["bundle"]["key"] == fleet.fleet_key()
    assert "rig" in got["bundle"]["members"]
    with pytest.raises(KeyError):
        fleet.request_state(out["id"], "s3cret")  # served once, then gone
    assert [e["event"] for e in events] == ["device.join_requested"]


def test_approve_or_deny_unknown_raises():
    with pytest.raises(KeyError):
        fleet.approve("0000000000000000")
    with pytest.raises(KeyError):
        fleet.deny("0000000000000000")


def test_deny(events):
    out = fleet.open_request("rig", "Rig", _hash("s"))
    assert fleet.deny(out["id"])["ok"]
    assert fleet.request_state(out["id"], "s") == {"state": "denied"}
    assert fleet.request_state(out["id"], "s") == {"state": "denied"}  # stable
    with pytest.raises(KeyError):
        fleet.approve(out["id"])  # can't approve a denied one
    assert not fleet.in_fleet()


def test_request_expires(monkeypatch):
    tick = _clock(monkeypatch)
    out = fleet.open_request("rig", "Rig", _hash("s"))
    tick(fleet.REQUEST_TTL + 1)
    assert fleet.pending_requests() == []
    assert fleet.request_state(out["id"], "s") == {"state": "expired"}
    with pytest.raises(KeyError):
        fleet.approve(out["id"])
    tick(fleet.REQUEST_TTL)
    with pytest.raises(KeyError):
        fleet.request_state(out["id"], "s")  # finally dropped


def test_allow_public_rate_limits_per_ip(monkeypatch):
    tick = _clock(monkeypatch)
    for _ in range(fleet.PUBLIC_LIMIT):
        assert fleet.allow_public("100.64.0.2")
    assert not fleet.allow_public("100.64.0.2")
    assert fleet.allow_public("100.64.0.3")  # another caller is unaffected
    tick(fleet.PUBLIC_WINDOW + 1)
    assert fleet.allow_public("100.64.0.2")


# --------------------------------------------------------------------------- #
# several devices in one process
# --------------------------------------------------------------------------- #
class _World:
    """``laptop``, ``rig`` and ``mini``, each with its own settings dir.

    ``remote._DEVICES`` is shared (every device "sees" every other; the code
    always skips its own key). Device-to-device calls go to the target's REAL
    routes; the browser's calls go to the current device's routes."""

    def __init__(self, tmp_path, monkeypatch):
        self.mp = monkeypatch
        self.files = {k: tmp_path / k / "settings.json" for k in DEVICES}
        for f in self.files.values():
            f.parent.mkdir(parents=True)
        self.me = "laptop"
        self.down: set = set()
        self.own_tokens = {k: "own-token-%s-0123456789" % k for k in DEVICES}
        self.calls: list = []
        self.privileged_ok = True
        remote._DEVICES.update(
            {
                k: {
                    "key": k,
                    "host": k.title(),
                    "base_url": "http://%s:8765" % k,
                    "reachable": True,
                    "remote_control": True,
                    "auth": True,
                    "version": "9.9.9",
                    "fleet": "",
                    "fleet_proto": 1,
                    "last_seen": 1.0,
                    "error": "",
                }
                for k in DEVICES
            }
        )
        monkeypatch.setattr(
            remote,
            "self_identity",
            lambda: {"key": self.me, "host": self.me.title(), "dns": "", "ip": ""},
        )
        monkeypatch.setattr(remote, "post_json", self.post_json)
        monkeypatch.setattr(remote, "get_json", self.get_json)
        monkeypatch.setattr(remote, "refresh_device", self.refresh_device)
        monkeypatch.setattr(remote, "fleet_devices", self.fleet_devices)
        monkeypatch.setattr(web_auth, "privileged", self.privileged)
        monkeypatch.setattr(
            web_auth,
            "own_token_valid",
            lambda c: bool(c) and c == self.own_tokens[self.me],
        )
        self.sync_on = {k: False for k in DEVICES}
        self.sync_log: list = []

        async def enable(start_from="", *, seed=False):
            what = "seed" if seed else "enable"
            self.sync_log.append((self.me, what, start_from))
            self.sync_on[self.me] = True
            return {"enabled": True}

        def disable():
            self.sync_log.append((self.me, "disable", ""))
            self.sync_on[self.me] = False

        monkeypatch.setattr(settings_sync, "enable", enable)
        monkeypatch.setattr(settings_sync, "disable", disable)
        monkeypatch.setattr(settings_sync, "enabled", lambda: self.sync_on[self.me])
        app = FastAPI()
        app.include_router(FleetAddon().router)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("100.64.0.9", 4321)),
            base_url="http://test",
        )
        self.use("laptop")

    # -- device switching ------------------------------------------------- #
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

    def rediscover(self):
        """What each device's hello says about its fleet now."""
        for k in DEVICES:
            with self.on(k):
                remote._DEVICES[k]["fleet"] = fleet.fleet_id()
                remote._DEVICES[k]["automation"] = settings_hooks.automation_here()
            remote._DEVICES[k]["reachable"] = k not in self.down

    # -- fakes for unit A2's remote API ----------------------------------- #
    async def privileged(self, scope):
        if not self.privileged_ok:
            return False
        return not any(k == b"x-mindflock-remote" for k, _ in scope.get("headers", []))

    async def refresh_device(self, key):
        if key in self.down:
            remote._DEVICES[key]["reachable"] = False
            return dict(remote._DEVICES[key])
        with self.on(key):
            fid = fleet.fleet_id()
            auto = settings_hooks.automation_here()
        remote._DEVICES[key].update(fleet=fid, reachable=True, automation=auto)
        return dict(remote._DEVICES[key])

    def fleet_devices(self):
        fid = fleet.fleet_id()
        return [
            dict(d)
            for d in remote._DEVICES.values()
            if d["reachable"]
            and fleet.is_member(d["key"])
            and fid
            and d["fleet"] == fid
        ]

    def _headers(self, dev, auth, bearer):
        h = {remote.REMOTE_HEADER: self.me}
        if bearer:
            h["Authorization"] = "Bearer " + bearer
        elif auth and fleet.is_member(dev["key"]):
            h["Authorization"] = "Bearer " + fleet.fleet_key()
        elif auth and remote.token_for(dev["key"]):
            h["Authorization"] = "Bearer " + remote.token_for(dev["key"])
        return h

    async def _send(self, method, dev, path, body, auth, bearer):
        if dev["key"] in self.down:
            self.calls.append((self.me, dev["key"], method, path, 0))
            return 0, None
        headers = self._headers(dev, auth, bearer)
        src = self.me
        with self.on(dev["key"]):
            r = await self.http.request(method, path, json=body, headers=headers)
        self.calls.append((src, dev["key"], method, path, r.status_code))
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    async def post_json(self, dev, path, body, timeout=10.0, *, auth=True, bearer=None):
        return await self._send("POST", dev, path, body, auth, bearer)

    async def get_json(self, dev, path, timeout=3.0, *, auth=True, bearer=None):
        return await self._send("GET", dev, path, None, auth, bearer)

    # -- the browser on the current device -------------------------------- #
    async def ui(self, method, path, body=None):
        r = await self.http.request(method, path, json=body)
        return r.status_code, r.json()

    def fleet_state(self, key):
        with self.on(key):
            return fleet.state()


@pytest.fixture
async def world(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch)
    yield w
    fleet.cancel_join()
    await w.http.aclose()


async def _join_by_code(w, joiner, host_device):
    with w.on(host_device):
        status, inv = await w.ui("POST", "/api/fleet/invite")
        assert status == 200
    with w.on(joiner):
        status, out = await w.ui(
            "POST", "/api/fleet/join", {"device": host_device, "code": inv["code"]}
        )
    return status, out


async def test_join_with_code_end_to_end(world, events):
    status, out = await _join_by_code(world, "rig", "laptop")
    assert status == 200, out
    assert out["state"] == "joined" and out["error"] == ""
    lap, rig = world.fleet_state("laptop"), world.fleet_state("rig")
    assert lap["id"] == rig["id"] and lap["key"] == rig["key"] and lap["id"]
    with world.on("laptop"):
        assert fleet.is_member("rig")
    with world.on("rig"):
        assert fleet.is_member("laptop") and fleet.is_member("rig")
        # Joining IS the permission: remote control is on now.
        assert store.load_settings().general.remote_control == "on"
    # The device joined leads settings sync.
    assert ("rig", "enable", "laptop") in world.sync_log
    # The redeem went out without credentials (the joiner has none yet).
    redeem = [c for c in world.calls if c[3] == "/api/fleet/redeem"]
    assert redeem == [("rig", "laptop", "POST", "/api/fleet/redeem", 200)]
    joined = [e for e in events if e["event"] == "device.joined"]
    assert {e["data"]["device"] for e in joined} == {"rig"}


async def test_join_with_pasted_command(world):
    with world.on("laptop"):
        _, inv = await world.ui("POST", "/api/fleet/invite")
    with world.on("rig"):
        status, out = await world.ui(
            "POST", "/api/fleet/join", {"text": inv["command"]}
        )
    assert status == 200 and out["state"] == "joined"


async def test_join_announces_to_the_other_members(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    status, out = await _join_by_code(world, "mini", "laptop")
    assert out["state"] == "joined"
    # mini told rig about itself straight away (no waiting for gossip).
    assert ("mini", "rig", "POST", "/api/fleet/roster", 200) in world.calls
    with world.on("rig"):
        assert fleet.is_member("mini")


async def test_join_with_wrong_code(world):
    with world.on("laptop"):
        await world.ui("POST", "/api/fleet/invite")
    with world.on("rig"):
        status, out = await world.ui(
            "POST", "/api/fleet/join", {"device": "laptop", "code": "ZZZZ-ZZZZ"}
        )
        assert status == 400
        assert out["state"] == "error"
        assert out["error"] == "that code is wrong or expired"
        assert not fleet.in_fleet()


async def test_join_needs_a_new_enough_peer(world):
    remote._DEVICES["laptop"]["fleet_proto"] = 0
    world.mp.setattr(
        remote,
        "refresh_device",
        lambda key: _const(remote._DEVICES[key]),
        raising=False,
    )
    with world.on("rig"):
        status, out = await world.ui(
            "POST", "/api/fleet/join", {"device": "laptop", "code": "ABCD-EFGH"}
        )
    assert status == 400 and out["error"] == "update MindFlock on Laptop first"


async def test_join_unreachable_peer(world):
    world.down.add("laptop")
    remote._DEVICES["laptop"]["reachable"] = False
    with world.on("rig"):
        status, out = await world.ui(
            "POST", "/api/fleet/join", {"device": "laptop", "code": "ABCD-EFGH"}
        )
    assert status == 400 and "isn't reachable" in out["error"]


async def test_join_route_validates(world):
    with world.on("rig"):
        assert (await world.ui("POST", "/api/fleet/join", {"code": "x"}))[0] == 400
        assert (await world.ui("POST", "/api/fleet/join", {"device": "laptop"}))[
            0
        ] == 400
        assert (await world.ui("POST", "/api/fleet/join", {"device": "rig", "code": "AAAA-AAAA"}))[0] == 400  # fmt: skip


async def _const(v):
    return dict(v)


async def _wait_join(states=("joined", "denied", "expired", "error")):
    for _ in range(200):
        if fleet.join_status()["state"] in states:
            return fleet.join_status()
        await asyncio.sleep(0.01)
    raise AssertionError("join never settled: %r" % fleet.join_status())


async def test_request_and_approve_end_to_end(world, events):
    with world.on("rig"):
        status, out = await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        assert status == 200, out
        assert out["state"] == "waiting" and len(out["code"]) == 7
        code = out["code"]
    with world.on("laptop"):
        _, st = await world.ui("GET", "/api/fleet")
        [req] = st["requests"]
        assert req["device"] == "rig" and req["code"] == code  # same on both screens
        status, res = await world.ui(
            "POST", "/api/fleet/requests/%s/approve" % req["id"]
        )
        assert status == 200 and res["device"] == "rig"
    with world.on("rig"):
        final = await _wait_join()
        assert final["state"] == "joined", final
        _, polled = await world.ui("GET", "/api/fleet/request")
        assert polled["state"] == "joined"
    assert world.fleet_state("rig")["key"] == world.fleet_state("laptop")["key"]
    asked = [e for e in events if e["event"] == "device.join_requested"]
    assert asked and asked[0]["data"]["detail"] == "Rig · code %s" % code
    # Polling used the public route without credentials.
    polls = [c for c in world.calls if c[3].startswith("/api/fleet/requests/")]
    assert polls and all(c[0] == "rig" for c in polls)


@pytest.mark.parametrize("how", ["code", "approve"])
async def test_admitting_turns_remote_control_and_sync_on_here(world, how):
    """The newcomer's first acts are a relayed settings pull and roster gossip
    FROM the admitting device — refused there while remote control is off,
    and empty while its sync is off. Admitting turns both on, before the
    joiner gets the bundle."""
    with world.on("laptop"):
        store.update_settings(general={"remote_control": "off"})
    if how == "code":
        status, out = await _join_by_code(world, "rig", "laptop")
        assert status == 200 and out["state"] == "joined", out
    else:
        with world.on("rig"):
            await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        with world.on("laptop"):
            [req] = fleet.pending_requests()
            status, res = await world.ui(
                "POST", "/api/fleet/requests/%s/approve" % req["id"]
            )
            assert status == 200 and res["sync_error"] == "", res
        with world.on("rig"):
            assert (await _wait_join())["state"] == "joined"
    with world.on("laptop"):
        assert store.load_settings().general.remote_control == "on"
    # Laptop's own sync came on (its values lead) BEFORE rig pulled from it.
    log = world.sync_log
    # Seeded (finding [4]/[11]): its values only fill gaps elsewhere.
    assert ("laptop", "seed", "") in log and ("rig", "enable", "laptop") in log
    assert ("laptop", "enable", "") not in log
    assert log.index(("laptop", "seed", "")) < log.index(("rig", "enable", "laptop"))


async def test_admitting_keeps_a_sync_that_is_already_on(world):
    world.sync_on["laptop"] = True
    status, _ = await _join_by_code(world, "rig", "laptop")
    assert status == 200
    assert ("laptop", "enable", "") not in world.sync_log
    assert ("laptop", "seed", "") not in world.sync_log


async def test_after_admit_reports_a_sync_error_and_never_raises(monkeypatch):
    async def boom(start_from="", *, seed=False):
        raise LookupError("join your other devices first")

    monkeypatch.setattr(settings_sync, "enable", boom)
    monkeypatch.setattr(
        remote, "remote_control_enabled", lambda: (_ for _ in ()).throw(OSError())
    )
    assert await fleet.after_admit() == "join your other devices first"


async def test_request_denied(world):
    with world.on("rig"):
        await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
    with world.on("laptop"):
        [req] = fleet.pending_requests()
        status, _ = await world.ui("POST", "/api/fleet/requests/%s/deny" % req["id"])
        assert status == 200
        assert not fleet.in_fleet()
    with world.on("rig"):
        final = await _wait_join()
        assert final["state"] == "denied" and "said no" in final["error"]
        assert not fleet.in_fleet()


async def test_request_cancelled(world):
    with world.on("rig"):
        await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        status, out = await world.ui("DELETE", "/api/fleet/request")
        assert status == 200 and out["state"] == "idle"
        await asyncio.sleep(0.05)
        assert fleet.join_status()["state"] == "idle"


async def test_only_one_join_at_a_time(world, monkeypatch):
    monkeypatch.setattr(fleet, "POLL_INTERVAL", 30.0)
    with world.on("rig"):
        await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        status, out = await world.ui("POST", "/api/fleet/request", {"device": "mini"})
        assert status == 400 and "already joining" in out["error"]


async def test_lost_request_reads_expired(world):
    with world.on("rig"):
        await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
    with world.on("laptop"):
        fleet._REQUESTS.clear()  # laptop restarted
    with world.on("rig"):
        final = await _wait_join()
        assert final["state"] == "expired" and "ask again" in final["error"]


async def test_add_paired(world, monkeypatch):
    # The receiving side's follow-up is a background task; in one process the
    # "current device" would have moved on by the time it ran, so collect it
    # and run it as rig.
    spawned = []
    monkeypatch.setattr(fleet, "_spawn", spawned.append)
    remote.set_token("rig", world.own_tokens["rig"])
    with world.on("laptop"):
        status, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
    assert status == 200, out
    assert out["ok"] and out["device"] == "rig"
    lap, rig = world.fleet_state("laptop"), world.fleet_state("rig")
    assert lap["id"] == rig["id"] and lap["key"] == rig["key"]
    with world.on("rig"):
        assert fleet.is_member("laptop") and fleet.is_member("rig")
        assert store.load_settings().general.remote_control == "on"
    assert ("laptop", "seed", "") in world.sync_log  # this device's sync on
    adopt = [c for c in world.calls if c[3] == "/api/fleet/adopt"]
    assert adopt == [("laptop", "rig", "POST", "/api/fleet/adopt", 200)]
    [follow_up] = spawned
    with world.on("rig"):
        await follow_up
    assert ("rig", "enable", "laptop") in world.sync_log
    assert remote._DEVICES["laptop"]["fleet"] == lap["id"]  # re-probed


async def test_add_paired_with_a_bad_token_rolls_back(world):
    remote.set_token("rig", "not-rigs-token-at-all")
    with world.on("laptop"):
        status, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
        assert status == 502 and "own access token" in out["error"]
        assert not fleet.is_member("rig")
    assert not world.fleet_state("rig")["id"]


async def test_add_paired_needs_a_token(world):
    with world.on("laptop"):
        status, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
    assert status == 400 and "paste its access token" in out["error"]


async def test_adopt_refuses_the_fleet_key_and_no_token(world):
    """A member can't drag another device into a group with the fleet key —
    only that device's own token authorizes adopt."""
    await _join_by_code(world, "rig", "laptop")
    with world.on("laptop"):
        key = fleet.fleet_key()
        other = _bundle(id=OTHER_ID)
        st, _ = await world.post_json(
            remote._DEVICES["rig"], "/api/fleet/adopt", {"bundle": other, "from": {"key": "laptop"}}, bearer=key  # fmt: skip
        )
        assert st == 401
        st, _ = await world.post_json(
            remote._DEVICES["rig"], "/api/fleet/adopt", {"bundle": other, "from": {"key": "laptop"}}, auth=False  # fmt: skip
        )
        assert st == 401
    assert world.fleet_state("rig")["id"] != OTHER_ID


async def test_adopt_into_a_different_fleet_with_members_is_409(world):
    await _join_by_code(world, "rig", "laptop")  # rig + laptop
    with world.on("mini"):
        fleet.create()
        fleet.add_member("solo", "Solo", by="mini")
    remote.set_token("rig", world.own_tokens["rig"])
    with world.on("mini"):
        st, body = await world.post_json(
            remote._DEVICES["rig"],
            "/api/fleet/adopt",
            {"bundle": fleet.bundle(), "from": {"key": "mini", "host": "Mini"}},
            bearer=world.own_tokens["rig"],
        )
    assert st == 409 and "leave that group first" in body["error"]


async def _three(world):
    await _join_by_code(world, "rig", "laptop")
    await _join_by_code(world, "mini", "laptop")
    world.rediscover()


async def test_remove_rotates_the_key(world, events):
    await _three(world)
    with world.on("laptop"):
        old = fleet.fleet_key()
        status, out = await world.ui("POST", "/api/fleet/members/mini/remove")
        assert status == 200, out
        assert out["rekeyed"] == ["rig"] and out["missed"] == []
        assert not fleet.is_member("mini")
        new = fleet.fleet_key()
        assert new != old
    assert world.fleet_state("rig")["key"] == new
    assert world.fleet_state("rig")["epoch"] == world.fleet_state("laptop")["epoch"]
    with world.on("rig"):
        assert not fleet.is_member("mini")
        assert not fleet.key_valid(old)
    # The removed device was never sent the new key.
    assert not [c for c in world.calls if c[1] == "mini" and c[3] == "/api/fleet/rekey"]
    assert any(
        e["event"] == "device.removed" and e["data"]["device"] == "mini" for e in events
    )


async def test_remove_reports_members_it_could_not_reach(world):
    await _three(world)
    world.down.add("rig")
    world.rediscover()
    with world.on("laptop"):
        status, out = await world.ui("POST", "/api/fleet/members/mini/remove")
    assert out["rekeyed"] == [] and out["missed"] == ["rig"]


async def test_remove_unknown_member_is_404(world):
    with world.on("laptop"):
        fleet.create()
        status, _ = await world.ui("POST", "/api/fleet/members/nobody/remove")
    assert status == 404


async def test_remove_self_leaves(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("rig"):
        status, out = await world.ui("POST", "/api/fleet/members/rig/remove")
        # A leave, said so (the CLI words it as one): no key change, no
        # token replaced.
        assert status == 200 and out == {"ok": True, "left": True}
        assert not fleet.in_fleet()


async def test_leave_tells_the_others(world, events):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("rig"):
        status, out = await world.ui("POST", "/api/fleet/leave")
        assert status == 200 and out == {"ok": True}
        assert not fleet.in_fleet()
    assert ("rig", "disable", "") in world.sync_log
    with world.on("laptop"):
        assert not fleet.is_member("rig")  # its tombstone arrived
        assert fleet.in_fleet()  # laptop is still a group (of one)
    assert any(
        e["event"] == "device.removed" and e["data"]["device"] == "rig" for e in events
    )


async def test_leave_outside_a_fleet_is_a_noop(world):
    with world.on("rig"):
        status, out = await world.ui("POST", "/api/fleet/leave")
    assert status == 200 and out == {"ok": True}


# --------------------------------------------------------------------------- #
# gossip
# --------------------------------------------------------------------------- #
async def test_gossip_spreads_the_roster(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("laptop"):
        fleet.add_member("mini", "Mini", by="laptop")  # rig doesn't know yet
    with world.on("rig"):
        assert not fleet.is_member("mini")
        assert await fleet.gossip_once() is True
        assert fleet.is_member("mini")
        assert await fleet.gossip_once() is False
        assert not fleet.stale_key()


async def test_gossip_marks_a_stale_key(world):
    """rig was offline when laptop removed it: every member it can still see
    rejects its key, which the UI turns into "ask to rejoin"."""
    await _three(world)
    world.down.add("rig")
    with world.on("laptop"):
        out = await fleet.remove("rig")
        assert out["missed"] == [] and out["rekeyed"] == ["mini"]
    world.down.clear()
    world.rediscover()
    with world.on("rig"):
        assert fleet.in_fleet()  # it never heard
        await fleet.gossip_once()
        assert fleet.stale_key()
        _, st = await world.ui("GET", "/api/fleet")
        assert st["stale_key"] is True
        errs = {m["key"]: m["error"] for m in st["members"]}
        # The 401 named a NEWER epoch: this device is the one behind.
        assert errs["laptop"] == "has a newer key — this device missed a change"


async def test_gossip_outside_a_fleet_does_nothing(world):
    with world.on("rig"):
        assert await fleet.gossip_once() is False
    assert world.calls == []


async def test_fleet_loop_survives_errors(monkeypatch):
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("x")

    monkeypatch.setattr(fleet, "gossip_once", boom)
    monkeypatch.setattr(fleet, "INTERVAL", 0.0)
    with pytest.raises(asyncio.CancelledError):
        await fleet.fleet_loop()
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# routes: who may call what
# --------------------------------------------------------------------------- #
_BROWSER_ROUTES = [
    ("POST", "/api/fleet/invite", None),
    ("DELETE", "/api/fleet/invite", None),
    ("POST", "/api/fleet/join", {"device": "laptop", "code": "ABCD-EFGH"}),
    ("POST", "/api/fleet/request", {"device": "laptop"}),
    ("GET", "/api/fleet/request", None),
    ("DELETE", "/api/fleet/request", None),
    ("POST", "/api/fleet/requests/0123456789abcdef/approve", None),
    ("POST", "/api/fleet/requests/0123456789abcdef/deny", None),
    ("POST", "/api/fleet/add-paired", {"device": "laptop"}),
    ("POST", "/api/fleet/members/laptop/remove", None),
    ("POST", "/api/fleet/leave", None),
]


@pytest.mark.parametrize("method, path, body", _BROWSER_ROUTES)
async def test_browser_routes_refuse_unprivileged_callers(world, method, path, body):
    world.privileged_ok = False
    with world.on("rig"):
        fleet.create()
        status, out = await world.ui(method, path, body)
    assert status == 403 and out == {"error": "open this on the device itself"}


@pytest.mark.parametrize("method, path, body", _BROWSER_ROUTES)
async def test_browser_routes_refuse_a_relayed_request(world, method, path, body):
    """Another MindFlock (the remote header) is never privileged — a member
    drives its peers' sessions, not their membership."""
    with world.on("rig"):
        r = await world.http.request(
            method, path, json=body, headers={remote.REMOTE_HEADER: "laptop"}
        )
    assert r.status_code == 403


async def test_status_hides_codes_from_unprivileged_callers(world):
    with world.on("laptop"):
        fleet.create_invite()
        fleet.open_request("mini", "Mini", _hash("s"))
        _, full = await world.ui("GET", "/api/fleet")
        assert len(full["invites"]) == 1 and len(full["requests"]) == 1
        world.privileged_ok = False
        status, limited = await world.ui("GET", "/api/fleet")
    assert status == 200
    assert limited["invites"] == [] and limited["requests"] == []
    assert limited["join"]["code"] == ""
    assert limited["members"] == full["members"]


async def test_status_payload(world, monkeypatch):
    remote.set_token("mini", "minis-token")
    remote._DEVICES["mini"]["fleet"] = "ffffffffffffffff"
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    remote._DEVICES["mini"]["fleet"] = "ffffffffffffffff"
    with world.on("laptop"):
        _, st = await world.ui("GET", "/api/fleet")
    assert set(st) == {
        "in_fleet",
        "id",
        "epoch",
        "self",
        "members",
        "invites",
        "requests",
        "join",
        "stale_key",
        "gate_warning",
        "candidates",
        "removed",
        "readmitted_elsewhere",
    }
    assert st["in_fleet"] is True and st["self"] == {"key": "laptop", "host": "Laptop"}
    members = {m["key"]: m for m in st["members"]}
    assert set(members) == {"laptop", "rig"}
    assert members["laptop"]["self"] is True and members["laptop"]["reachable"] is True
    assert members["rig"] == {
        "key": "rig",
        "host": "Rig",
        "added_at": members["rig"]["added_at"],
        "self": False,
        "reachable": True,
        "version": "9.9.9",
        "same_fleet": True,
        "error": "",
        "automation": False,
        "key_conflict": False,
    }
    [cand] = st["candidates"]
    assert cand == {
        "device": "mini",
        "host": "Mini",
        "version": "9.9.9",
        "fleet_proto": 1,
        "reachable": True,
        "member": False,
        "in_fleet": True,
        "same_fleet": False,
        "has_token": True,
    }
    assert st["invites"] == []  # the join used the only one
    with world.on("laptop"):
        fleet.create_invite()
        _, st = await world.ui("GET", "/api/fleet")
    assert st["invites"][0]["command"].startswith("mindflock devices join laptop ")
    assert st["gate_warning"] is True  # gate off, not local-only
    monkeypatch.setenv("CS_WEB_MODE", "local")
    with world.on("laptop"):
        assert (await world.ui("GET", "/api/fleet"))[1]["gate_warning"] is False


def test_gate_warning_off_when_the_gate_is_on(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert fleet.status(True)["gate_warning"] is False


async def test_member_routes_need_the_fleet_key(world):
    with world.on("laptop"):
        fleet.create()
        key = fleet.fleet_key()
        for method, path in (
            ("GET", "/api/fleet/roster"),
            ("POST", "/api/fleet/roster"),
            ("POST", "/api/fleet/rekey"),
        ):
            r = await world.http.request(method, path, json={})
            assert r.status_code == 401, path
            r = await world.http.request(
                method, path, json={}, headers={"Authorization": "Bearer " + world.own_tokens["laptop"]}  # fmt: skip
            )
            assert r.status_code == 401, path  # the device token is not the key
        r = await world.http.get(
            "/api/fleet/roster", headers={"Authorization": "Bearer " + key}
        )
        assert r.status_code == 200
        assert "key" not in r.json() and r.json()["id"] == fleet.fleet_id()


async def test_member_routes_outside_a_fleet_401(world):
    with world.on("rig"):
        r = await world.http.get(
            "/api/fleet/roster", headers={"Authorization": "Bearer "}
        )
    assert r.status_code == 401


async def test_public_routes_validate(world):
    with world.on("laptop"):
        bad = [
            ("/api/fleet/redeem", {"code": "ABCD-EFGH", "device": "Bad Name"}),
            ("/api/fleet/redeem", {"code": "A" * 65, "device": "rig"}),
            ("/api/fleet/redeem", {"code": "", "device": "rig"}),
            ("/api/fleet/redeem", {"code": "ABCD-EFGH", "device": "rig", "host": "h" * 256}),  # fmt: skip
            ("/api/fleet/requests", {"device": "rig", "secret_hash": "abc"}),
            ("/api/fleet/requests", {"device": "../x", "secret_hash": _hash("s")}),
            ("/api/fleet/requests", {"device": "rig", "secret_hash": 5}),
        ]
        for path, body in bad:
            r = await world.http.post(path, json=body)
            assert r.status_code == 400, (path, body)
        r = await world.http.post("/api/fleet/redeem", content=b"not json")
        assert r.status_code == 400
        r = await world.http.get("/api/fleet/requests/NOT-AN-ID?secret=x")
        assert r.status_code == 404
        r = await world.http.get("/api/fleet/requests/0123456789abcdef?secret=x")
        assert r.status_code == 404
        r = await world.http.post(
            "/api/fleet/redeem", json={"code": "ZZZZ-ZZZZ", "device": "rig"}
        )
        assert (
            r.status_code == 403
            and r.json()["error"] == "that code is wrong or expired"
        )
        out = (
            await world.http.post(
                "/api/fleet/requests", json={"device": "rig", "secret_hash": _hash("s")}
            )
        ).json()
        r = await world.http.get("/api/fleet/requests/%s?secret=nope" % out["id"])
        assert r.status_code == 403
        r = await world.http.get("/api/fleet/requests/%s?secret=s" % out["id"])
        assert r.status_code == 200 and r.json() == {"state": "pending"}


async def test_public_routes_are_rate_limited(world):
    with world.on("laptop"):
        codes = []
        for i in range(fleet.PUBLIC_LIMIT + 1):
            path = "/api/fleet/redeem" if i % 2 else "/api/fleet/requests"
            r = await world.http.post(path, json={"device": "Bad Name"})
            codes.append(r.status_code)
        assert codes[-1] == 429 and set(codes[:-1]) == {400}
        # The joiner's poll is its own bucket: a request-to-join polls every
        # 2 s (30 a minute) and must not be cut off.
        for _ in range(30):
            r = await world.http.get("/api/fleet/requests/0123456789abcdef?secret=x")
            assert r.status_code == 404
        codes = []
        for _ in range(fleet.POLL_LIMIT):
            r = await world.http.get("/api/fleet/requests/0123456789abcdef?secret=x")
            codes.append(r.status_code)
        assert codes[-1] == 429


def test_allow_public_buckets_are_separate(monkeypatch):
    _clock(monkeypatch)
    for _ in range(fleet.PUBLIC_LIMIT):
        assert fleet.allow_public("1.2.3.4")
    assert not fleet.allow_public("1.2.3.4")
    assert fleet.allow_public("1.2.3.4", "poll")


async def test_redeem_lockout_is_429(world):
    with world.on("laptop"):
        fleet.create_invite()
        for _ in range(fleet._FAIL_LIMIT):
            await world.http.post(
                "/api/fleet/redeem", json={"code": "ZZZZ-ZZZZ", "device": "rig"}
            )
        r = await world.http.post(
            "/api/fleet/redeem", json={"code": "ZZZZ-ZZZZ", "device": "rig"}
        )
    assert r.status_code == 429


# --------------------------------------------------------------------------- #
# the real server (needs unit A2's auth changes)
# --------------------------------------------------------------------------- #
_A2 = hasattr(web_auth, "privileged") and hasattr(web_auth, "own_token_valid")
needs_a2 = pytest.mark.skipif(
    not _A2, reason="auth.privileged / own_token_valid not landed"
)


def test_addon_is_registered():
    from fastapi.testclient import TestClient

    from backend.web import server

    r = TestClient(server.app).get("/api/fleet/roster")
    assert r.status_code == 401 and r.json() == {
        "error": "not one of this device's devices",
        "id": "",
        "epoch": 0,
        "kfp": "",
    }
    assert "fleet" in [a.id for a in server.ADDONS]


@pytest.fixture
def gated(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "gate-token-0123456789")
    return lambda **kw: TestClient(server.app, **kw)


@needs_a2
def test_public_fleet_paths_are_exact(gated):
    c = gated(client=("100.64.0.9", 4321))  # a tailnet caller
    r = c.post("/api/fleet/requests", json={"device": "rig", "secret_hash": _hash("s")})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert c.get("/api/fleet/requests/%s?secret=s" % rid).status_code == 200
    assert c.post("/api/fleet/redeem", json={"code": "ZZZZ-ZZZZ", "device": "rig"}).status_code == 403  # fmt: skip
    # Everything else under /api/fleet stays behind the gate.
    for method, path in (
        ("POST", "/api/fleet/requests/%s/approve" % rid),
        ("POST", "/api/fleet/requests/%s/deny" % rid),
        ("GET", "/api/fleet"),
        ("POST", "/api/fleet/invite"),
        ("GET", "/api/fleet/requests"),
        ("GET", "/api/fleet/requests/%s/approve" % rid),
        ("GET", "/api/fleet/redeem"),
    ):
        assert c.request(method, path).status_code in (401, 405), path
    assert fleet.pending_requests()  # nobody approved it


@needs_a2
def test_fleet_key_is_privileged_and_remote_header_is_not(gated):
    fleet.create()
    key = fleet.fleet_key()
    c = gated()
    r = c.post("/api/fleet/invite", headers={"Authorization": "Bearer " + key})
    assert r.status_code == 200 and r.json()["code"]
    r = c.get("/api/fleet", headers={"Authorization": "Bearer " + key})
    assert r.status_code == 200 and r.json()["invites"]


@needs_a2
def test_loopback_is_privileged_without_a_token(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.post("/api/fleet/invite").status_code == 200
    # ... but not when it was forwarded (tailscale serve also arrives on loopback)
    r = c.post("/api/fleet/invite", headers={"X-Forwarded-For": "100.64.0.7"})
    assert r.status_code == 403
    # ... and never when relayed by another MindFlock
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    r = c.post("/api/fleet/invite", headers={remote.REMOTE_HEADER: "rig"})
    assert r.status_code == 403
    # A non-loopback caller with the gate off is NOT privileged.
    c2 = TestClient(server.app, client=("100.64.0.7", 50000))
    assert c2.post("/api/fleet/invite").status_code == 403
