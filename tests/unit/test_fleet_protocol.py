"""Your devices — regression tests for the fleet protocol + trust review.

Each test names the review finding it pins (``[n]`` = its index in the
confirmed list). They reuse :mod:`tests.unit.test_fleet`'s multi-device
harness (:class:`_World`): every "device" has its own settings dir, and the
device-to-device calls run the REAL fleet routes on the other end.

The harness shares the gossip bookkeeping (``fleet._STALE`` / ``_PEERS``)
between its devices — it is module state, one per process — so a test that
reads it after another device gossiped resets it first (:func:`_fresh`).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web.core import auth as web_auth
from backend.web.core import fleet, remote, settings_hooks, settings_sync
from tests.unit.test_fleet import (  # noqa: F401 — fixtures
    _bundle,
    _clean,
    _join_by_code,
    _wait_join,
    _World,
    events,
    world,
)


def _fresh():
    fleet._STALE.clear()
    fleet._PEERS.clear()


async def _three(w):
    await _join_by_code(w, "rig", "laptop")
    w.rediscover()
    await _join_by_code(w, "mini", "laptop")
    w.rediscover()


async def _gossip(w, key):
    with w.on(key):
        _fresh()
        await fleet.gossip_once()
        return fleet.stale_key()


# --------------------------------------------------------------------------- #
# [1] a removal reaches a member that was offline; staleness is by epoch
# --------------------------------------------------------------------------- #
async def test_removal_reaches_a_member_that_was_offline(world):
    await _three(world)
    world.down.add("mini")
    world.rediscover()
    with world.on("laptop"):
        old = fleet.fleet_key()
        out = await fleet.remove("rig")
        assert out["rekeyed"] == [] and out["missed"] == ["mini"]
        assert fleet.prev_key(1) == old
    world.down.discard("mini")
    world.rediscover()
    # mini looks first: laptop answers 401 with a NEWER epoch — but rig
    # (removed, still on the old key) took mini's key this pass, and a
    # member that accepts our key means "not stale" (round 2, [7]): no
    # rejoin banner on mini; laptop's own pass heals it.
    assert await _gossip(world, "mini") is False
    with world.on("mini"):
        assert fleet._PEERS["laptop"]["ahead"] is True
    # laptop's pass: mini answers 401 with an OLDER epoch -> laptop hands it
    # the new key under the old one; laptop is never told "you were removed".
    assert await _gossip(world, "laptop") is False
    with world.on("laptop"):
        assert fleet._PEERS["mini"]["error"] == ""
        new = fleet.fleet_key()
        # Everyone live is on the new epoch now: the old key is dropped.
        assert fleet.prev_key(1) == ""
    with world.on("mini"):
        assert fleet.fleet_key() == new
        assert not fleet.is_member("rig")  # the tombstone rode along
        assert not fleet.key_valid(old)
    # The removed rig's old key no longer opens mini.
    with world.on("rig"):
        st, body = await world.get_json(
            remote._DEVICES["mini"], "/api/fleet/roster", bearer=old
        )
    assert st == 401 and body["epoch"] == world.fleet_state("mini")["epoch"]
    assert await _gossip(world, "mini") is False


async def test_member_routes_401_with_id_and_epoch(world):
    with world.on("laptop"):
        fleet.create()
        fid = fleet.fleet_id()
        for method, path in (
            ("GET", "/api/fleet/roster"),
            ("POST", "/api/fleet/roster"),
            ("POST", "/api/fleet/rekey"),
            ("POST", "/api/fleet/rotate-token"),
        ):
            r = await world.http.request(
                method, path, json={}, headers={"Authorization": "Bearer nope"}
            )
            assert r.status_code == 401
            assert r.json() == {
                "error": "not one of this device's devices",
                "id": fid,
                "epoch": 1,
                "kfp": fleet.key_fp(fleet.fleet_key()),
            }


async def test_rejoin_through_a_device_on_an_older_key_heals_it(world):
    """The remover asked an out-of-date member to let it back in: it must
    not take that member's OLD key (and claim "joined" with two keys)."""
    await _three(world)
    world.down.add("mini")
    world.rediscover()
    with world.on("laptop"):
        await fleet.remove("rig")
    world.down.discard("mini")
    world.rediscover()
    with world.on("laptop"):
        out = await fleet.request_join("mini")
    with world.on("mini"):
        fleet.approve(out["id"])
    with world.on("laptop"):
        final = await _wait_join()
        assert final["state"] == "joined", final
        lk, le = fleet.fleet_key(), fleet.state()["epoch"]
    with world.on("mini"):
        assert fleet.fleet_key() == lk and fleet.state()["epoch"] == le
        assert not fleet.is_member("rig")


async def test_an_older_bundle_that_cant_be_healed_is_not_joined(world):
    await _three(world)
    world.down.add("mini")
    world.rediscover()
    with world.on("laptop"):
        await fleet.remove("rig")
    world.down.discard("mini")
    world.rediscover()
    with world.on("mini"):
        b = fleet.bundle()
        fleet.rekey()  # mini's key moved on too: b's key opens nothing now
    with world.on("laptop"):
        before = fleet.state()
        await fleet._finish_join(remote._DEVICES["mini"], b)
        st = fleet.join_status()
        assert st["state"] == "error" and "older key" in st["error"]
        assert fleet.state() == before


async def test_push_pull_gossip_spreads_our_roster_too(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("rig"):
        fleet.add_member("mini", "Mini", by="rig")  # laptop doesn't know yet
    with world.on("rig"):
        await fleet.gossip_once()  # rig PUSHES while it pulls
    with world.on("laptop"):
        assert fleet.is_member("mini")


# --------------------------------------------------------------------------- #
# [9] [10] [21] [43] no ghosts: the admitting side never adds the joiner
# --------------------------------------------------------------------------- #
async def test_join_refused_up_front_when_already_in_another_group(world):
    with world.on("mini"):
        fleet.create()
    st, out = await _join_by_code(world, "rig", "mini")  # rig + mini
    assert st == 200 and out["state"] == "joined"
    world.rediscover()
    with world.on("laptop"):
        inv = fleet.create_invite()
        before = fleet.state()
    with world.on("rig"):
        st, out = await world.ui(
            "POST", "/api/fleet/join", {"device": "laptop", "code": inv["code"]}
        )
        assert st == 400 and "leave your current group first" in out["error"]
        st, out = await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        assert st == 400 and "leave your current group first" in out["error"]
    with world.on("laptop"):
        assert fleet.state() == before  # no ghost
        assert [i["code"] for i in fleet.invites()] == [inv["code"]]  # not used
        assert fleet.pending_requests() == []
    assert not [c for c in world.calls if c[1] == "laptop"]  # never contacted


async def test_join_with_a_device_already_in_my_group_is_already_joined(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    world.calls.clear()
    with world.on("rig"):
        st, out = await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
    assert st == 200 and out["state"] == "joined"
    # One look (does it take my key?) and nothing else.
    assert world.calls == [("rig", "laptop", "GET", "/api/fleet/roster", 200)]


async def test_a_failed_adopt_leaves_no_ghost_on_the_inviter(world, monkeypatch):
    """Even if the joiner refuses the bundle after the code was spent, the
    inviter's roster never listed it."""
    with world.on("laptop"):
        inv = fleet.create_invite()

    async def no_preflight(dev):
        return False

    monkeypatch.setattr(fleet, "_preflight", no_preflight)
    with world.on("rig"):
        fleet.create()
        fleet.add_member("solo", "Solo", by="rig")
        st, out = await world.ui(
            "POST", "/api/fleet/join", {"device": "laptop", "code": inv["code"]}
        )
        assert st == 400 and "leave that group first" in out["error"]
    with world.on("laptop"):
        assert not fleet.is_member("rig")
        assert "rig" not in fleet.state()["members"]


async def test_the_joiner_becomes_a_member_when_it_announces(world, events):
    await _join_by_code(world, "rig", "laptop")
    joined = [
        e["data"]["detail"]
        for e in events
        if e["event"] == "device.joined" and e["data"].get("via") is None
    ]
    assert joined == ["Rig joined your devices"]  # from laptop's merge
    with world.on("laptop"):
        assert fleet.is_member("rig")


async def test_tombstoned_after_approval_is_denied_not_handed_the_key(world):
    with world.on("laptop"):
        fleet.create()
    with world.on("rig"):
        out = await fleet.request_join("laptop")
    with world.on("laptop"):
        fleet.approve(out["id"])
        # The group removed rig (say a roster from another member) before
        # rig's poll collected the bundle.
        fleet.remove_member("rig")
        new_key = fleet.rekey()
    with world.on("rig"):
        final = await _wait_join()
        assert final["state"] == "denied", final
        assert not fleet.in_fleet() and fleet.fleet_key() != new_key
    with world.on("laptop"):
        assert not fleet.is_member("rig")


async def test_a_previously_removed_device_can_still_rejoin(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("laptop"):
        await fleet.remove("rig", rotate_tokens=False)
    with world.on("rig"):
        fleet.leave()
    st, out = await _join_by_code(world, "rig", "laptop")
    assert st == 200 and out["state"] == "joined", out
    with world.on("laptop"):
        assert fleet.is_member("rig")


async def test_cancel_withdraws_the_request_on_the_other_device(world):
    with world.on("rig"):
        st, out = await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
        assert st == 200 and out["state"] == "waiting"
        rid = out["id"]
        st, out = await world.ui("DELETE", "/api/fleet/request")
        assert out["state"] == "idle"
        for _ in range(50):  # the withdraw is a background task
            await asyncio.sleep(0.01)
            if any(c[3].endswith("/cancel") for c in world.calls):
                break
    with world.on("laptop"):
        assert fleet.pending_requests() == []
        st, _ = await world.ui("POST", "/api/fleet/requests/%s/approve" % rid)
        assert st == 404
        assert not fleet.is_member("rig")
    assert ("rig", "laptop", "POST", "/api/fleet/requests/%s/cancel" % rid, 200) in (
        world.calls
    )


def test_withdraw_request_needs_the_secret():
    out = fleet.open_request("rig", "Rig", "%064x" % 0)
    with pytest.raises(PermissionError):
        fleet.withdraw_request(out["id"], "wrong")
    with pytest.raises(KeyError):
        fleet.withdraw_request("0" * 16, "x")
    assert fleet.pending_requests()


async def test_cancel_while_joining_is_refused(world, monkeypatch):
    """[22] Once the bundle is in hand the join completes."""
    gate = asyncio.Event()
    orig = world.refresh_device

    async def slow_refresh(key):
        await gate.wait()
        return await orig(key)

    monkeypatch.setattr(remote, "refresh_device", slow_refresh)
    with world.on("rig"):
        await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
    with world.on("laptop"):
        [req] = fleet.pending_requests()
        await world.ui("POST", "/api/fleet/requests/%s/approve" % req["id"])
    with world.on("rig"):
        for _ in range(200):
            if fleet.join_status()["state"] == "joining" and fleet.in_fleet():
                break
            await asyncio.sleep(0.01)
        _, out = await world.ui("DELETE", "/api/fleet/request")
        assert out["state"] == "joining"
        gate.set()
        final = await _wait_join(("joined", "error"))
        assert final["state"] == "joined"
    assert ("rig", "enable", "laptop") in world.sync_log


# --------------------------------------------------------------------------- #
# [26] [29] a public request must come from the device it names
# --------------------------------------------------------------------------- #
def test_open_request_and_redeem_check_the_callers_tailnet_address():
    remote._DEVICES["rig"] = {
        "key": "rig",
        "ip": "100.64.0.5",
        "ips": ["100.64.0.5", "fd7a:115c:a1e0::5"],
    }
    h = "%064x" % 1
    with pytest.raises(PermissionError):
        fleet.open_request("rig", "Rig", h, ip="100.64.0.66")
    assert fleet.open_request("rig", "Rig", h, ip="fd7a:115c:a1e0::5")["id"]
    # Not a tailnet address (a local test, an unvouched proxy): can't check.
    remote._DEVICES["mini"] = {"key": "mini", "ip": "100.64.0.6", "ips": []}
    assert fleet.open_request("mini", "Mini", h, ip="127.0.0.1")["id"]
    code = fleet.create_invite()["code"]
    with pytest.raises(PermissionError, match="didn't come from rig"):
        fleet.redeem(code, "rig", "Rig", ip="100.64.0.66")
    assert fleet.invites()  # a mismatch doesn't spend the code
    assert "rig" in fleet.redeem(code, "rig", "Rig", ip="100.64.0.5")["members"]


def test_a_request_from_another_address_does_not_replace_a_pending_one():
    first = fleet.open_request("rig", "Rig", "%064x" % 1, ip="100.64.0.5")
    with pytest.raises(ValueError, match="already waiting"):
        fleet.open_request("rig", "Rig", "%064x" % 2, ip="100.64.0.66")
    assert [r["id"] for r in fleet.pending_requests()] == [first["id"]]
    # The same device asking again (same address) still replaces it.
    again = fleet.open_request("rig", "Rig", "%064x" % 3, ip="100.64.0.5")
    assert [r["id"] for r in fleet.pending_requests()] == [again["id"]]


async def test_requests_route_403s_an_impersonator(world):
    """The harness client is 100.64.0.9; discovery says rig is .5."""
    remote._DEVICES["rig"]["ip"] = "100.64.0.5"
    with world.on("laptop"):
        r = await world.http.post(
            "/api/fleet/requests",
            json={"device": "rig", "host": "Rig", "secret_hash": "a" * 64},
        )
        assert r.status_code == 403
        assert fleet.pending_requests() == []


# --------------------------------------------------------------------------- #
# [8] the key is bound to the member's full MagicDNS name
# --------------------------------------------------------------------------- #
async def test_rekey_never_goes_to_a_node_holding_a_members_label(world):
    await _three(world)
    with world.on("laptop"):
        doc = fleet.state()
        doc["members"]["mini"]["dns"] = "mini.tail1.ts.net"
        fleet._save(doc)
        remote._DEVICES["mini"]["dns"] = "mini.other-tailnet.ts.net"
        assert not fleet.member_device(remote._DEVICES["mini"])
        out = await fleet.remove("rig")
        assert out["missed"] == ["mini"]
    assert not [c for c in world.calls if c[1] == "mini" and c[3] == "/api/fleet/rekey"]


def test_member_dns_is_kept_and_filled_in():
    fleet.create()
    fid = fleet.fleet_id()
    fleet.add_member("rig", "Rig", by="laptop", dns="RIG.tail1.ts.net.")
    m = fleet.state()["members"]["rig"]
    assert m["dns"] == "rig.tail1.ts.net"
    # A name whose first label isn't the key is dropped.
    fleet.add_member("mini", "Mini", by="laptop", dns="evil.tail1.ts.net")
    assert fleet.state()["members"]["mini"]["dns"] == ""
    # The same add, now carrying the name: filled in by gossip.
    at = fleet.state()["members"]["mini"]["added_at"]
    fleet.merge_roster(
        {
            "id": fid,
            "epoch": 1,
            "members": {
                "mini": {"host": "Mini", "added_at": at, "dns": "mini.tail1.ts.net"}
            },
            "removed": {},
        }
    )
    assert fleet.state()["members"]["mini"]["dns"] == "mini.tail1.ts.net"
    assert fleet.member_device({"key": "mini", "dns": "mini.tail1.ts.net"})
    assert not fleet.member_device({"key": "mini", "dns": ""})


# --------------------------------------------------------------------------- #
# [18] [4] [11] admitting: before the adopt, seeded, automation_device
# --------------------------------------------------------------------------- #
async def test_add_paired_is_ready_before_the_adopt_arrives(world, monkeypatch):
    monkeypatch.setattr(fleet, "_spawn", lambda coro: coro.close())
    with world.on("laptop"):
        store.update_settings(general={"remote_control": "off"})
    remote.set_token("rig", world.own_tokens["rig"])
    seen = {}
    real = world.post_json

    async def spy(dev, path, body, timeout=10.0, *, auth=True, bearer=None):
        if path == "/api/fleet/adopt":
            with world.on("laptop"):
                seen["rc"] = store.load_settings().general.remote_control
            seen["sync"] = list(world.sync_log)
        return await real(dev, path, body, timeout, auth=auth, bearer=bearer)

    monkeypatch.setattr(remote, "post_json", spy)
    with world.on("laptop"):
        st, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
    assert st == 200, out
    assert seen == {"rc": "on", "sync": [("laptop", "seed", "")]}


async def test_admitting_inside_a_group_seeds_instead_of_leading(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    world.sync_on["laptop"] = False  # turned off here (or its join's enable failed)
    world.sync_log.clear()
    await _join_by_code(world, "mini", "laptop")
    assert ("laptop", "enable", "") not in world.sync_log
    assert ("laptop", "seed", "") in world.sync_log


async def test_admitting_picks_the_admitter_unless_the_joiner_runs_it(world):
    for k in ("laptop", "rig"):
        with world.on(k):
            assert store.load_settings().github.automation_device == ""
    await _join_by_code(world, "rig", "laptop")
    # rig runs nothing: the admitter keeps it — both sides decide the same.
    for k in ("laptop", "rig"):
        with world.on(k):
            assert store.load_settings().github.automation_device == "laptop"
    assert _runners(world) == ["laptop"]


async def test_status_and_hello_report_automation(world):
    await _join_by_code(world, "rig", "laptop")
    # Derived from the group's choice (github.automation_device), not from
    # what a hello last said.
    remote._DEVICES["rig"]["automation"] = True
    with world.on("laptop"):
        st = fleet.status(True)
        rows = {m["key"]: m["automation"] for m in st["members"]}
        assert rows == {"laptop": True, "rig": False}
        assert remote.hello_json()["automation"] is True
    with world.on("rig"):
        rows = {m["key"]: m["automation"] for m in fleet.status(True)["members"]}
        assert rows == {"laptop": True, "rig": False}


def test_discovery_stores_automation_and_ips():
    dev = remote._device_state("box")
    remote._apply_probe(dev, ("http://x", {"automation": True, "fleet": ""}), 1.0)
    assert dev["automation"] is True
    # An older MindFlock's hello doesn't say: unknown, not "doesn't run it".
    remote._apply_probe(dev, ("http://x", {"fleet": ""}), 2.0)
    assert dev["automation"] is None
    entry = remote._node_entry(
        {"DNSName": "box.tail.ts.net.", "TailscaleIPs": ["100.64.0.5", "fd7a::5"]}
    )
    assert entry["ips"] == ["100.64.0.5", "fd7a::5"] and entry["ip"] == "100.64.0.5"


# --------------------------------------------------------------------------- #
# [20] removal replaces access tokens; the fleet key can't read them
# --------------------------------------------------------------------------- #
async def test_remove_rotates_every_reachable_members_token(world):
    await _three(world)
    tokens = {}
    for k in ("laptop", "rig", "mini"):
        with world.on(k):
            tokens[k] = web_auth.get_token()
    with world.on("laptop"):
        st, out = await world.ui("POST", "/api/fleet/members/mini/remove")
    assert st == 200, out
    assert out["rotated"] == ["laptop", "rig"] and out["rotate_failed"] == []
    for k in ("laptop", "rig"):
        with world.on(k):
            assert web_auth.get_token() != tokens[k]
    with world.on("mini"):
        assert web_auth.get_token() == tokens["mini"]


async def test_remove_can_keep_tokens_and_reports_failures(world, monkeypatch):
    await _three(world)
    with world.on("laptop"):
        before = web_auth.get_token()
        st, out = await world.ui(
            "POST", "/api/fleet/members/mini/remove", {"rotate_tokens": False}
        )
        assert st == 200 and out["rotated"] == [] and out["rotate_failed"] == []
        assert web_auth.get_token() == before
    # An env-pinned token can't be rotated: reported, never a 500.
    with world.on("laptop"):
        fleet.add_member("mini", "Mini", by="laptop")
    world.rediscover()
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "pinned-token-0123456789abcdef")
    with world.on("laptop"):
        st, out = await world.ui("POST", "/api/fleet/members/mini/remove")
    assert st == 200
    assert out["rotated"] == [] and sorted(out["rotate_failed"]) == ["laptop", "rig"]


def test_auth_token_is_not_handed_to_a_fleet_key_caller(monkeypatch, tmp_path):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"general": {"onboarded": True}}))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(sf))
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "own-token-0123456789abcdef")
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    store.invalidate()
    from backend.web import server

    fleet.create()
    key = fleet.fleet_key()
    remote_ip = TestClient(server.app, client=("100.64.0.7", 5000))
    r = remote_ip.get(
        "/api/settings/auth-token", headers={"Authorization": "Bearer " + key}
    )
    assert r.status_code == 200
    assert r.json()["token"] is None and "devices' key" in r.json()["reason"]
    r = remote_ip.get(
        "/api/settings/auth-token",
        headers={"Authorization": "Bearer own-token-0123456789abcdef"},
    )
    assert r.json()["token"] == "own-token-0123456789abcdef"
    # This machine itself (the desktop app), unproxied: yes.
    local = TestClient(server.app, client=("127.0.0.1", 5000))
    r = local.get(
        "/api/settings/auth-token", headers={"Authorization": "Bearer " + key}
    )
    assert r.json()["token"] == "own-token-0123456789abcdef"


# --------------------------------------------------------------------------- #
# [23] membership passes the remote-control gate; [17] disconnect a member
# --------------------------------------------------------------------------- #
def test_fleet_key_passes_the_remote_control_gate(monkeypatch, tmp_path):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"general": {"onboarded": True, "remote_control": "off"}}))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(sf))
    store.invalidate()
    from backend.web import server

    fleet.create()
    key = fleet.fleet_key()
    c = TestClient(server.app, client=("100.64.0.7", 5000))
    member = {"Authorization": "Bearer " + key, "X-MindFlock-Remote": "rig"}
    r = c.get("/api/fleet/roster", headers=member)
    assert r.status_code == 200 and r.json()["id"] == fleet.fleet_id()
    r = c.get("/api/settings/sync/export", headers=member)
    assert r.status_code != 403
    # A paired non-member is still governed by the toggle …
    other = {"Authorization": "Bearer something-else", "X-MindFlock-Remote": "rig"}
    r = c.get("/api/instances", headers=other)
    assert r.status_code == 403
    assert r.json()["error"] == "remote control is disabled on this device"
    # … except on a member route, which answers for itself — the fleet-aware
    # 401, so a member on an OLD key learns this device's epoch (round 2 [5]).
    r = c.get("/api/fleet/roster", headers=other)
    assert r.status_code == 401 and r.json()["epoch"] == 1


def test_disconnect_on_a_member_is_409(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    store.invalidate()
    from backend.web import server

    fleet.create()
    fleet.add_member("rig", "Rig", by="laptop")
    remote._DEVICES["rig"] = {"key": "rig", "host": "Rig", "dns": ""}
    remote._TOKENS["rig"] = "pasted-token-0123456789"
    c = TestClient(server.app)
    r = c.post("/api/devices/rig/disconnect")
    assert r.status_code == 409
    assert r.json()["error"] == (
        "Rig is one of your devices — remove it in Settings → Devices"
    )
    assert remote.token_for("rig") == "pasted-token-0123456789"


# --------------------------------------------------------------------------- #
# [25] the proxy never forwards dot segments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "/api/instances/rig::x/../../fleet/rekey",
        "/api/instances/rig::x/./send",
        "/api/instances/rig::../fleet/roster",
        "/api/instances/rig::x/%2e%2e/%2e%2e/settings/sync/export",
        "/api/instances/rig::x/%2fapi",
        "/api/instances/rig::x/..\\..\\fleet",
        "/api/devices/rig/fwd/api/../fleet/rekey",
    ],
)
def test_proxy_rejects_dot_segments(path):
    sent = []

    async def inner(scope, receive, send):  # pragma: no cover — must not run
        raise AssertionError("passed through")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": path, "headers": []}
    asyncio.run(remote.RemoteProxyMiddleware(inner)(scope, receive, send))
    assert sent[0]["status"] == 400


def test_proxy_paths_without_dots_still_pass_the_check():
    for p in (
        "/api/instances/rig::my.session/send",
        "/api/instances/rig::x/diff",
        "/api/devices/rig/fwd/api/config",
    ):
        assert not remote._unsafe_proxy_path(p)


# --------------------------------------------------------------------------- #
# [40] gate_warning when tailscale serve fronts a local-mode server
# --------------------------------------------------------------------------- #
async def test_gate_warning_when_serve_exposes_a_local_server(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    assert fleet.status(True)["gate_warning"] is False
    monkeypatch.setattr(fleet, "_check_serve", lambda: True)
    await fleet.refresh_exposure()
    assert fleet.status(True)["gate_warning"] is True
    # Cached: the next look doesn't shell out again.
    monkeypatch.setattr(fleet, "_check_serve", lambda: 1 / 0)
    await fleet.refresh_exposure()
    assert fleet.status(True)["gate_warning"] is True
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    assert fleet.status(True)["gate_warning"] is False


# =========================================================================== #
# Round 2 — "[n]" below is the index in the round-2 confirmed list.
# =========================================================================== #
async def _remove_rig_while_mini_is_away(w):
    """laptop removes rig while mini is offline; mini comes back on K1."""
    await _three(w)
    w.down.add("mini")
    w.rediscover()
    with w.on("laptop"):
        k1 = fleet.fleet_key()
        out = await fleet.remove("rig")
        assert out["missed"] == ["mini"]
        k2 = fleet.fleet_key()
    w.down.discard("mini")
    w.rediscover()
    return k1, k2


# --------------------------------------------------------------------------- #
# [0] [7] rekeys and rejoins never resurrect a removed device, never jump
# --------------------------------------------------------------------------- #
async def test_a_delivered_rekey_replaces_a_roster_the_removed_device_wrote(world):
    k1, k2 = await _remove_rig_while_mini_is_away(world)
    # rig (removed, still holding K1) writes itself back onto mini's roster
    # with a fresh added_at before laptop's gossip reaches mini.
    with world.on("rig"):
        r = fleet.roster()
        r["members"]["rig"]["added_at"] = time.time() + 5
        st, _ = await world.post_json(
            remote._DEVICES["mini"], "/api/fleet/roster", r, bearer=k1
        )
        assert st == 200
    await _gossip(world, "laptop")  # mini takes K2 — and laptop's roster
    with world.on("mini"):
        assert fleet.fleet_key() == k2
        assert not fleet.is_member("rig")
    await _gossip(world, "mini")  # mini never hands rig the new key
    with world.on("rig"):
        assert fleet.fleet_key() != k2
    with world.on("laptop"):
        assert not fleet.is_member("rig")


async def test_a_rekey_is_only_ever_the_next_epoch(world):
    k1, k2 = await _remove_rig_while_mini_is_away(world)
    with world.on("rig"):
        body = {
            "id": fleet.fleet_id(),
            "epoch": 99,
            "key": "A" * 43,
            "members": fleet.roster()["members"],
            "removed": {"laptop": time.time()},
        }
        st, resp = await world.post_json(
            remote._DEVICES["mini"], "/api/fleet/rekey", body, bearer=k1
        )
    assert st == 200 and resp == {"ok": False}
    with world.on("mini"):
        assert fleet.fleet_key() == k1 and fleet.state()["epoch"] == 1
    # The remover is not told it is the stale one; it heals mini instead.
    assert await _gossip(world, "laptop") is False
    with world.on("mini"):
        assert fleet.fleet_key() == k2 and not fleet.is_member("rig")


async def test_a_member_two_key_changes_behind_is_walked_up_one_at_a_time(world):
    await _three(world)
    world.down.add("mini")
    world.rediscover()
    with world.on("laptop"):
        await fleet.rotate_key()
        await fleet.rotate_key()
        k3 = fleet.fleet_key()
        assert fleet.state()["epoch"] == 3
    world.down.discard("mini")
    world.rediscover()
    assert await _gossip(world, "laptop") is False
    with world.on("mini"):
        assert fleet.fleet_key() == k3 and fleet.state()["epoch"] == 3
    rekeys = [c for c in world.calls if c[1] == "mini" and c[3] == "/api/fleet/rekey"]
    assert [c[4] for c in rekeys[-2:]] == [200, 200]


def test_gossip_never_brings_back_a_device_known_removed_here():
    fleet.create()
    fid = fleet.fleet_id()
    fleet.add_member("rig", "Rig", by="laptop")
    fleet.remove_member("rig")
    later = {"rig": {"host": "Rig", "added_at": time.time() + 60, "added_by": "x"}}
    fleet.merge_roster({"id": fid, "epoch": 1, "members": later, "removed": {}})
    assert not fleet.is_member("rig")
    # Re-admitting it HERE (this device's own admit path) is what lets it in.
    b = fleet.bundle_for("rig", "Rig")
    fleet.merge_roster(
        {"id": fid, "epoch": 1, "members": b["members"], "removed": b["removed"]}
    )
    assert fleet.is_member("rig")
    assert fleet.state()["admits"] == {}  # used up


def test_tombstones_survive_a_newer_bundle_and_a_rekey():
    fleet.create()
    fid = fleet.fleet_id()
    fleet.add_member("rig", "Rig", by="laptop")
    fleet.remove_member("rig")
    fresh = {
        "laptop": {"host": "Laptop", "added_at": time.time(), "added_by": "laptop"},
        "rig": {"host": "Rig", "added_at": time.time() + 60, "added_by": "rig"},
    }
    body = {"id": fid, "epoch": 2, "key": "B" * 43, "members": fresh, "removed": {}}
    assert fleet.apply_rekey(body) is True
    assert fleet.fleet_key() == "B" * 43 and not fleet.is_member("rig")
    assert fleet.state()["removed"]["rig"]["by"] == "laptop"
    fleet.adopt_bundle(dict(body, epoch=3, key="C" * 43))  # a rejoin
    assert fleet.fleet_key() == "C" * 43 and not fleet.is_member("rig")
    assert fleet.is_member("laptop")


def test_a_rekey_that_removes_this_device_makes_it_leave(events):
    fleet.create()
    fid = fleet.fleet_id()
    body = {
        "id": fid,
        "epoch": 2,
        "key": "B" * 43,
        "members": {},
        "removed": {"laptop": {"at": time.time() + 1, "by": "rig"}},
    }
    assert fleet.apply_rekey(body) is True
    assert not fleet.in_fleet()
    gone = [e for e in events if e["event"] == "device.removed"]
    assert gone and gone[0]["data"]["by"] == "rig"


async def test_status_says_who_removed_a_device(world):
    await _three(world)
    with world.on("laptop"):
        await fleet.remove("rig")
    with world.on("mini"):
        st = fleet.status(True)
    [row] = st["removed"]
    assert row["key"] == "rig" and row["removed_by"] == "laptop"
    assert row["removed_by_host"] == "Laptop" and row["removed_at"] > 0


# --------------------------------------------------------------------------- #
# [5] a gate-ON member answers member routes with the fleet-aware 401
# --------------------------------------------------------------------------- #
def _gated_server(monkeypatch, tmp_path, rc="on"):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"general": {"onboarded": True, "remote_control": rc}}))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(sf))
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "own-token-0123456789abcdef")
    store.invalidate()
    from backend.web import server

    fleet.create()
    return TestClient(server.app, client=("100.64.0.7", 5000))


@pytest.mark.parametrize("rc", ["on", "off"])
def test_gate_on_member_routes_say_their_epoch(monkeypatch, tmp_path, rc):
    c = _gated_server(monkeypatch, tmp_path, rc)
    newer = {
        "Authorization": "Bearer newer-key-AAAAAAAAAAAAAAAAAAAA",
        "X-MindFlock-Remote": "laptop",
    }
    for method, path in (
        ("GET", "/api/fleet/roster"),
        ("POST", "/api/fleet/roster"),
        ("POST", "/api/fleet/rekey"),
        ("POST", "/api/fleet/rotate-token"),
        ("POST", "/api/settings/sync/nudge"),
        ("GET", "/api/settings/sync/export"),
    ):
        r = c.request(method, path, json={}, headers=newer)
        assert r.status_code == 401, (path, r.text)
        assert r.json()["epoch"] == 1 and r.json()["id"] == fleet.fleet_id(), path
        assert r.json()["kfp"] == fleet.key_fp(fleet.fleet_key())
    # The current key still works through both gates.
    ok = {"Authorization": "Bearer " + fleet.fleet_key(), "X-MindFlock-Remote": "l"}
    assert c.get("/api/fleet/roster", headers=ok).status_code == 200
    # Everything else is still behind the gate.
    assert c.get("/api/instances", headers=newer).status_code in (401, 403)


def test_a_relayed_own_token_on_a_member_route_obeys_the_toggle(monkeypatch, tmp_path):
    c = _gated_server(monkeypatch, tmp_path, "off")
    hdr = {
        "Authorization": "Bearer own-token-0123456789abcdef",
        "X-MindFlock-Remote": "paired",
    }
    r = c.get("/api/settings/sync/export", headers=hdr)
    assert r.status_code == 403
    assert r.json()["error"] == "remote control is disabled on this device"


def test_a_member_whose_gate_hides_its_epoch_is_tried_with_kept_keys(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    store.invalidate()
    monkeypatch.setattr(
        remote,
        "self_identity",
        lambda: {"key": "laptop", "host": "Laptop", "dns": "", "ip": ""},
    )
    _fresh()
    fleet.create()
    fleet.add_member("mini", "Mini", by="laptop")
    fleet.add_member("rig", "Rig", by="laptop")
    old = fleet.fleet_key()
    fleet.remove_member("rig")
    fleet.rekey()
    mini = {"key": "mini", "host": "Mini", "reachable": True, "fleet": fleet.fleet_id()}
    monkeypatch.setattr(remote, "fleet_devices", lambda: [mini])
    calls = []

    async def post_json(dev, path, body, timeout=10.0, *, auth=True, bearer=None):
        calls.append((path, bearer))
        if bearer == old and path == "/api/fleet/rekey":
            return 200, {"ok": True}
        return 401, {"error": "unauthorized"}  # an older build's gate

    monkeypatch.setattr(remote, "post_json", post_json)
    asyncio.run(fleet.gossip_once())
    assert ("/api/fleet/rekey", old) in calls
    assert fleet.stale_key() is False
    assert fleet._PEERS["mini"]["epoch"] == 2


# --------------------------------------------------------------------------- #
# [8] same epoch, different keys: a conflict with exactly one loser
# --------------------------------------------------------------------------- #
async def test_two_removals_apart_are_a_key_conflict_one_side_rejoins(world):
    await _three(world)
    world.down.add("mini")
    world.rediscover()
    with world.on("laptop"):
        await fleet.remove("rig")
    world.down.discard("mini")
    world.down.add("laptop")
    world.rediscover()
    with world.on("mini"):
        await fleet.remove("rig")
    world.down.discard("laptop")
    world.rediscover()
    stale = {}
    for k, other in (("laptop", "mini"), ("mini", "laptop")):
        stale[k] = await _gossip(world, k)
        with world.on(k):
            peer = fleet._PEERS[other]
            assert peer["conflict"] is True
            assert peer["error"] == (
                "has a different key for your devices — rejoin one from the other"
            )
            assert fleet.peer_on_other_epoch(other)  # no fleet key sent to it
            assert fleet.prev_key(1)  # not pruned because of it
            assert fleet.status(True)["members"][
                [m["key"] for m in fleet.status(True)["members"]].index(other)
            ]["key_conflict"]
    assert sorted(stale.values()) == [False, True]
    loser = [k for k, v in stale.items() if v][0]
    winner = "mini" if loser == "laptop" else "laptop"
    with world.on(loser):
        lfp = fleet.key_fp(fleet.fleet_key())
    with world.on(winner):
        assert lfp < fleet.key_fp(fleet.fleet_key())
    # The loser rejoins the winner (a code made there): one key again, and
    # rig stays removed on both.
    await _join_by_code(world, loser, winner)
    with world.on(loser):
        lk = fleet.fleet_key()
        assert not fleet.is_member("rig")
    with world.on(winner):
        assert fleet.fleet_key() == lk


# --------------------------------------------------------------------------- #
# [1] [6] Rotate token replaces the devices' key; own token only to its owner
# --------------------------------------------------------------------------- #
async def test_rotate_key_reaches_members_and_kills_the_old_key(world):
    await _three(world)
    with world.on("laptop"):
        old = fleet.fleet_key()
        out = await fleet.rotate_key()
        new = fleet.fleet_key()
    assert out["rekeyed"] == ["mini", "rig"] and out["missed"] == []
    for k in ("rig", "mini"):
        with world.on(k):
            assert fleet.fleet_key() == new and not fleet.key_valid(old)


async def test_rotate_key_replaces_every_reached_members_own_token(world):
    """Round 3 [3]: a lost phone holds the members' OWN tokens too (the
    shared-link QR carries the paired ones), so rotating only the shared key
    left it signed in on them."""
    await _three(world)
    world.down = {"mini"}
    world.rediscover()
    tokens = {}
    for k in ("laptop", "rig", "mini"):
        with world.on(k):
            tokens[k] = web_auth.get_token()
    with world.on("laptop"):
        out = await fleet.rotate_key()
    assert out == {
        "rekeyed": ["rig"],
        "missed": ["mini"],
        "rotated": ["rig"],
        "rotate_failed": ["mini"],  # rotated there by hand
    }
    with world.on("rig"):
        assert web_auth.get_token() != tokens["rig"]
    for k in ("laptop", "mini"):  # laptop's own: the route's business
        with world.on(k):
            assert web_auth.get_token() == tokens[k]


def _phone_server(monkeypatch, tmp_path):
    sf = tmp_path / "settings.json"
    sf.write_text(
        json.dumps(
            {
                "general": {
                    "onboarded": True,
                    "auth_mode": "on",
                    "auth_token": "own-token-0123456789abcdef",
                }
            }
        )
    )
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(sf))
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    store.invalidate()
    from backend.web import server

    return server


def test_rotate_token_route_signs_qr_phones_out_of_the_group(monkeypatch, tmp_path):
    server = _phone_server(monkeypatch, tmp_path)
    fleet.create()
    key = fleet.fleet_key()
    c = TestClient(server.app, client=("100.64.0.7", 5000))
    own = {"Authorization": "Bearer own-token-0123456789abcdef"}
    r = c.post("/api/settings/auth-token/rotate", headers=own)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["in_fleet"] is True and body["token"] and "scan the QR" in body["note"]
    assert not web_auth.token_valid(key)  # the phone's fleet-key cookie is dead
    assert fleet.state()["epoch"] == 2
    assert body["rotated"] == [] and body["rotate_failed"] == []
    assert "Not reached" not in body["note"]


def test_rotate_token_route_names_the_members_to_rotate_by_hand(monkeypatch, tmp_path):
    server = _phone_server(monkeypatch, tmp_path)
    fleet.create()
    fleet.add_member("mini", "Mini", by="laptop")  # offline: not discovered
    c = TestClient(server.app, client=("100.64.0.7", 5000))
    own = {"Authorization": "Bearer own-token-0123456789abcdef"}
    r = c.post("/api/settings/auth-token/rotate", headers=own)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missed"] == ["mini"] and body["rotate_failed"] == ["mini"]
    assert "Not reached: Mini — rotate the token on it too" in body["note"]


def test_mobile_and_rotate_hand_no_own_token_to_a_fleet_key_caller(
    monkeypatch, tmp_path
):
    server = _phone_server(monkeypatch, tmp_path)
    from backend.web.core import shared_link

    fleet.create()
    key = fleet.fleet_key()
    monkeypatch.setattr(remote, "paired_tokens", lambda: {"mac": "MACTOKEN-xyz"})
    monkeypatch.setattr(shared_link, "advertised_url", lambda: "https://f.ts.net/m")
    c = TestClient(server.app, client=("100.64.0.7", 5000))
    for hdr in (
        {"Authorization": "Bearer " + key},
        {"Authorization": "Bearer " + key, "X-MindFlock-Remote": "rig"},
    ):
        r = c.get("/api/mobile", headers=hdr)
        assert r.status_code == 200
        assert r.json()["token"] is None
        assert "token=" not in (r.json()["qr_target"] or "")
    # The owner (own token) still gets the QR with every token in it.
    r = c.get(
        "/api/mobile", headers={"Authorization": "Bearer own-token-0123456789abcdef"}
    )
    assert r.json()["token"] == "own-token-0123456789abcdef"
    assert "MACTOKEN-xyz" in r.json()["qr_target"]
    # A rotate asked with the devices' key rotates, but hands back no token.
    r = c.post(
        "/api/settings/auth-token/rotate", headers={"Authorization": "Bearer " + key}
    )
    assert r.status_code == 200 and r.json()["token"] is None
    assert "set-cookie" not in r.headers
    assert not web_auth.own_token_valid("own-token-0123456789abcdef")


# --------------------------------------------------------------------------- #
# [17] one caller is only ever locked out; burning invites takes 3 addresses
# --------------------------------------------------------------------------- #
def test_one_caller_never_burns_the_invites(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(fleet, "_now", lambda: t[0])
    monkeypatch.setattr(fleet, "INVITE_TTL", 1e9)  # outlives the guessing
    fleet.create_invite()
    spans = []
    for _ in range(60):
        try:
            fleet.redeem("WRONGCOD", "nobody", "x", ip="100.64.0.66")
        except fleet.TooManyAttempts:
            spans.append(fleet._LOCKED_UNTIL["100.64.0.66"] - t[0])
            t[0] = fleet._LOCKED_UNTIL["100.64.0.66"] + 1
        except PermissionError:
            t[0] += 1
    assert fleet._INVITES, "one address burned everyone's code"
    # 60 s, doubling each repeat lockout.
    assert spans[:3] == [
        pytest.approx(60, abs=2),
        pytest.approx(120, abs=2),
        pytest.approx(240, abs=2),
    ]
    # Two addresses taking turns don't either.
    fleet._LOCKED_UNTIL.clear()
    fleet._FAILS.clear()
    fleet._ALL_FAILS.clear()
    for i in range(200):
        try:
            fleet.redeem("WRONGCOD", "nobody", "x", ip="100.64.2.%d" % (i % 2))
        except PermissionError:
            pass
        t[0] += 30
    assert fleet._INVITES
    # Guessing from several addresses (at least three) still burns them.
    fleet._LOCKED_UNTIL.clear()
    fleet._FAILS.clear()
    fleet._ALL_FAILS.clear()
    for i in range(20):
        try:
            fleet.redeem("WRONGCOD", "nobody", "x", ip="100.64.1.%d" % (i % 5))
        except PermissionError:
            pass
        t[0] += 1
    assert not fleet._INVITES


# --------------------------------------------------------------------------- #
# [18] public join routes answer only tailnet callers (or this machine)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "client, ok",
    [
        (("100.64.0.9", 4321), True),  # a tailnet address
        (("127.0.0.1", 4321), True),  # this machine, unproxied
        (("192.168.1.50", 4321), False),  # a LAN neighbour
    ],
)
def test_public_join_routes_answer_only_the_tailnet(monkeypatch, client, ok):
    from backend.web import server

    c = TestClient(server.app, client=client)
    body = {"device": "rig", "host": "Rig", "secret_hash": "a" * 64}
    r = c.post("/api/fleet/requests", json=body)
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert r.status_code == 403 and fleet.pending_requests() == []
        assert c.post("/api/fleet/redeem", json={}).status_code == 403
        assert c.get("/api/fleet/requests/" + "0" * 16).status_code == 403
    # Behind an unvouched proxy hop (loopback + a forwarding header): no.
    r = TestClient(server.app, client=("127.0.0.1", 4321)).post(
        "/api/fleet/requests", json=body, headers={"X-Forwarded-For": "192.168.1.50"}
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# [19] after a removal the remover keeps sending the members the key
# --------------------------------------------------------------------------- #
async def test_after_remove_the_fleet_key_still_goes_to_the_rekeyed(world):
    await _three(world)
    await _gossip(world, "laptop")  # _PEERS[*].epoch == 1
    with world.on("laptop"):
        out = await fleet.remove("rig")
        assert out["rekeyed"] == ["mini"]
        assert not fleet.peer_on_other_epoch("mini")
        assert remote._fleet_key_for("mini") == fleet.fleet_key()
        assert fleet.TAILNET_ADVICE == out["advice"]


# --------------------------------------------------------------------------- #
# [2] [9] / round 3 [0] [4] [5] ONE device runs PR review + issue handling:
# github.automation_device, synced, decided the same on both sides of a join
# --------------------------------------------------------------------------- #
def _runners(w):
    """The group members whose automation_here() says yes."""
    out = []
    for k in ("laptop", "rig", "mini"):
        with w.on(k):
            if fleet.in_fleet() and settings_hooks.automation_here():
                out.append(k)
    return out


def _runs_pr_review(w, *keys):
    for k in keys:
        with w.on(k):
            store.update_settings(github={"repos": ["o/r"], "token": "t"})


@pytest.fixture
def synced_choice(world, monkeypatch):
    """Settings sync's "the device joined through leads": the joiner takes
    its github.automation_device (the harness fakes sync otherwise)."""
    real = settings_sync.enable

    async def enable(start_from="", *, seed=False):
        out = await real(start_from, seed=seed)
        if start_from:
            with world.on(start_from):
                chosen = store.load_settings().github.automation_device
            if chosen:
                store.update_settings(github={"automation_device": chosen})
        return out

    monkeypatch.setattr(settings_sync, "enable", enable)
    return world


async def test_the_joiner_that_runs_pr_review_keeps_it(world):
    _runs_pr_review(world, "rig")
    with world.on("rig"):
        assert fleet.runs_automation() is True
    await _join_by_code(world, "rig", "laptop")
    for k in ("laptop", "rig"):
        with world.on(k):
            assert store.load_settings().github.automation_device == "rig"
    assert _runners(world) == ["rig"]


@pytest.mark.parametrize("through", ["laptop", "rig"])
async def test_three_devices_that_all_run_it_end_with_one_runner(
    synced_choice, through
):
    """[5]: v1 sync made all three identical, so each says it runs PR review
    on its own — the third join (through either member) must not add a
    second runner."""
    world = synced_choice
    _runs_pr_review(world, "laptop", "rig", "mini")
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    await _join_by_code(world, "mini", through)
    assert _runners(world) == ["rig"]
    for k in ("laptop", "rig", "mini"):
        with world.on(k):
            assert store.load_settings().github.automation_device == "rig"


@pytest.mark.parametrize("through", ["mini", "rig"])
async def test_mac_and_rig_first_then_the_laptop_that_runs_it(synced_choice, through):
    """[0]: the mac (mini) admits the rig first — neither runs anything, so
    the admitter is chosen — then the laptop that runs PR review joins: still
    exactly one runner."""
    world = synced_choice
    _runs_pr_review(world, "laptop")
    await _join_by_code(world, "rig", "mini")
    world.rediscover()
    await _join_by_code(world, "laptop", through)
    assert _runners(world) == ["mini"]


async def test_ask_to_join_and_one_click_add_follow_the_automation_too(world):
    with world.on("mini"):
        store.update_settings(github={"issue_repos": ["o/r"], "issues_enabled": True})
    with world.on("laptop"):
        fleet.create()
    with world.on("mini"):
        out = await fleet.request_join("laptop")
    with world.on("laptop"):
        st, _ = await world.ui("POST", "/api/fleet/requests/%s/approve" % out["id"])
        assert st == 200
    with world.on("mini"):
        assert (await _wait_join())["state"] == "joined"
    for k in ("laptop", "mini"):
        with world.on(k):
            assert store.load_settings().github.automation_device == "mini"
    # One click: the group already chose (mini) — rig, added while running
    # PR review itself, doesn't make a second runner.
    _runs_pr_review(world, "rig")
    remote.set_token("rig", world.own_tokens["rig"])
    world.rediscover()
    with world.on("laptop"):
        st, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
    assert st == 200 and out["direction"] == "theirs_take_mine"
    with world.on("laptop"):
        assert store.load_settings().github.automation_device == "mini"


async def test_one_click_add_of_the_device_that_runs_it(world, monkeypatch):
    follow_ups = []
    monkeypatch.setattr(fleet, "_spawn", follow_ups.append)
    _runs_pr_review(world, "rig")
    remote.set_token("rig", world.own_tokens["rig"])
    with world.on("laptop"):
        st, out = await world.ui("POST", "/api/fleet/add-paired", {"device": "rig"})
    assert st == 200, out
    with world.on("laptop"):
        assert store.load_settings().github.automation_device == "rig"
    with world.on("rig"):  # the adopted side settles after its settings pull
        for coro in follow_ups:
            await coro
        assert store.load_settings().github.automation_device == "rig"


async def test_removing_the_device_that_runs_it_moves_it_to_the_remover(
    synced_choice,
):
    """[4]: the removed device is usually lost or offline — it can't hand
    PR review over itself."""
    world = synced_choice
    _runs_pr_review(world, "rig")
    await _three(world)
    assert _runners(world) == ["rig"]
    world.down = {"rig"}
    world.rediscover()
    with world.on("mini"):
        out = await fleet.remove("rig", rotate_tokens=False)
        assert out["rekeyed"] == ["laptop"]
        assert store.load_settings().github.automation_device == "mini"
    with world.on("laptop"):  # what the save's nudge makes laptop pull
        store.update_settings(github={"automation_device": "mini"})
    # (rig, offline, never heard — it is cut off; see TAILNET_ADVICE.)
    assert [k for k in _runners(world) if k != "rig"] == ["mini"]


async def test_the_last_device_left_after_a_removal_runs_it(world):
    _runs_pr_review(world, "rig")
    await _join_by_code(world, "rig", "laptop")
    assert _runners(world) == ["rig"]
    world.down = {"rig"}
    world.rediscover()
    with world.on("laptop"):
        await fleet.remove("rig", rotate_tokens=False)
        assert settings_hooks.automation_here() is True


async def test_leaving_hands_it_to_the_lowest_keyed_member_first(world, monkeypatch):
    await _three(world)
    with world.on("laptop"):
        assert store.load_settings().github.automation_device == "laptop"
    nudged = []

    async def nudge_peers():
        nudged.append((world.me, fleet.in_fleet()))
        return []

    monkeypatch.setattr(settings_sync, "nudge_peers", nudge_peers)
    with world.on("laptop"):
        await fleet.leave_fleet()
        assert store.load_settings().github.automation_device == "mini"
        assert settings_hooks.automation_here() is True  # alone: runs its own
    assert nudged == [("laptop", True)]  # told while still one of them
    # The others' fallback (laptop gone) picks the same one; laptop, alone
    # now, isn't a member of anything.
    assert _runners(world) == ["mini"]
    with world.on("rig"):
        assert settings_hooks.automation_here() is False


async def test_leaving_or_being_removed_leaves_the_group_cleanly(world):
    await _three(world)
    with world.on("rig"):
        await fleet.leave_fleet()
        assert not fleet.in_fleet() and settings_hooks.automation_here() is True
    with world.on("mini"):  # a roster that removed mini reaches it
        r = fleet.roster()
        r["removed"]["mini"] = {"at": time.time() + 1, "by": "laptop"}
        fleet.merge_roster(r)
        assert not fleet.in_fleet() and settings_hooks.automation_here() is True


# --------------------------------------------------------------------------- #
# A settings.json that can't be read never fails a join
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self):
        self.lines = []

    def Printf(self, fmt, *args):  # noqa: N802 — mirrors backend.log
        self.lines.append(fmt.replace("%v", "%s") % args)


def test_enable_remote_control_logs_an_unreadable_settings_file(monkeypatch, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("{ not json")
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(f))
    store.invalidate()
    rec = _Recorder()
    monkeypatch.setattr(fleet.log, "ErrorLog", rec)
    fleet._enable_remote_control()  # must not raise
    assert f.read_text() == "{ not json", "the broken file was replaced"
    assert any("remote control" in line for line in rec.lines), rec.lines


async def test_a_joiner_with_an_unreadable_settings_file_still_joins(
    world, monkeypatch
):
    rec = _Recorder()
    monkeypatch.setattr(fleet.log, "ErrorLog", rec)
    world.files["rig"].write_text("{ not json")
    with world.on("rig"):
        store.invalidate()
    status, out = await _join_by_code(world, "rig", "laptop")
    assert status == 200, out
    assert out["state"] == "joined", out
    with world.on("rig"):
        assert fleet.in_fleet() and fleet.is_member("laptop")
    assert world.files["rig"].read_text() == "{ not json"
    assert any("remote control" in line for line in rec.lines), rec.lines


# --------------------------------------------------------------------------- #
# round 3 [1] a rejoin that brings back a device removed here replaces the key
# --------------------------------------------------------------------------- #
async def test_conflict_rejoin_replaces_the_key_the_removed_device_holds(
    world, monkeypatch
):
    import secrets as _secrets

    await _three(world)
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    # mini is away: laptop rotates the key; rig takes it.
    world.down = {"mini"}
    world.rediscover()
    with world.on("laptop"):
        assert (await fleet.rotate_key())["rekeyed"] == ["rig"]
        kl = fleet.fleet_key()
    # laptop is away: on mini the owner removes rig, and mini's new key is
    # the conflict's LOSER (the lower fingerprint).
    while True:
        cand = _secrets.token_urlsafe(32)
        if fleet.key_fp(cand) < fleet.key_fp(kl):
            break
    real = fleet.secrets.token_urlsafe
    monkeypatch.setattr(fleet.secrets, "token_urlsafe", lambda n=32: cand)
    world.down = {"laptop"}
    world.rediscover()
    with world.on("mini"):
        await fleet.remove("rig", rotate_tokens=False)
    monkeypatch.setattr(fleet.secrets, "token_urlsafe", real)
    world.down = set()
    world.rediscover()
    await _gossip(world, "laptop")
    assert await _gossip(world, "mini") is True  # asked to rejoin laptop
    with world.on("laptop"):
        laptop_token = web_auth.get_token()
    st, out = await _join_by_code(world, "mini", "laptop")
    assert out["state"] == "joined", out
    world.rediscover()
    for _ in range(2):
        for k in ("laptop", "mini"):
            await _gossip(world, k)
    with world.on("laptop"):
        assert not fleet.is_member("rig")
        key_now = fleet.fleet_key()
        # The members' own tokens go too: rig may have read laptop's.
        assert web_auth.get_token() != laptop_token
    with world.on("mini"):
        assert not fleet.is_member("rig") and fleet.fleet_key() == key_now
    with world.on("rig"):
        assert fleet.fleet_key() != key_now  # cut off


def test_adopt_bundle_reports_and_retombstones_exposed_devices(monkeypatch):
    fleet.create()
    fleet.add_member("rig", "Rig", by="laptop")
    fleet.remove_member("rig")
    b = fleet.bundle()
    b["members"]["rig"] = dict(b["members"]["rig"], added_at=time.time() + 60)
    del b["removed"]["rig"]  # the admitter never heard of the removal
    assert fleet.adopt_bundle(b) == ["rig"]
    doc = fleet.state()
    assert not fleet.is_member("rig")
    assert doc["removed"]["rig"]["at"] > b["members"]["rig"]["added_at"]
    assert doc["removed"]["rig"]["by"] == "laptop"
    # A plain rejoin (nothing removed here is live there) reports nothing.
    assert fleet.adopt_bundle(fleet.bundle()) == []


# --------------------------------------------------------------------------- #
# round 3 [2] [8] a leave isn't a removal: a later admit anywhere revives it
# --------------------------------------------------------------------------- #
async def test_leave_then_rejoin_is_readmitted_on_the_third_device(world):
    await _three(world)
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    with world.on("laptop"):
        await fleet.leave_fleet()
    world.rediscover()
    with world.on("rig"):
        st = fleet.status(True)
        assert [(r["key"], r["left"]) for r in st["removed"]] == [("laptop", True)]
    st, out = await _join_by_code(world, "laptop", "mini")
    assert st == 200 and out["state"] == "joined", out
    world.rediscover()
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    for k in ("rig", "mini"):
        with world.on(k):
            assert fleet.is_member("laptop")
            assert fleet.status(True)["readmitted_elsewhere"] == []


async def test_a_readmitted_device_gets_another_members_next_key(world):
    await _three(world)
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    with world.on("laptop"):
        await fleet.leave_fleet()
    world.rediscover()
    for k in ("rig", "mini"):
        await _gossip(world, k)
    st, out = await _join_by_code(world, "laptop", "mini")
    assert out["state"] == "joined", out
    world.rediscover()
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    with world.on("rig"):
        out = await fleet.rotate_key()
        rig_key = fleet.fleet_key()
    assert out["rekeyed"] == ["laptop", "mini"]
    for k in ("laptop", "mini"):
        with world.on(k):
            assert fleet.fleet_key() == rig_key
    with world.on("mini"):
        assert fleet.is_member("laptop")


async def test_a_removal_stays_until_allowed_here(world):
    """A removal by another device is sticky: when another member lets the
    device back in, this one lists it (readmitted_elsewhere) and the person
    allows it here — gossip alone never does."""
    await _three(world)
    with world.on("laptop"):
        await fleet.remove("rig", rotate_tokens=False)
        st = fleet.status(True)
        assert [(r["key"], r["left"]) for r in st["removed"]] == [("rig", False)]
    world.rediscover()
    # mini lets rig back in (a fresh join through mini).
    st, out = await _join_by_code(world, "rig", "mini")
    assert out["state"] == "joined", out
    world.rediscover()
    for _ in range(2):
        for k in ("rig", "mini", "laptop"):
            await _gossip(world, k)
    with world.on("mini"):
        assert fleet.is_member("rig")
    with world.on("laptop"):
        assert not fleet.is_member("rig")
        assert fleet.status(True)["readmitted_elsewhere"] == [
            {"key": "rig", "host": "Rig", "by": "mini", "by_host": "Mini"}
        ]
        world.privileged_ok = False
        st, _ = await world.ui("POST", "/api/fleet/members/rig/allow")
        assert st == 403
        world.privileged_ok = True
        st, _ = await world.ui("POST", "/api/fleet/members/mini/allow")
        assert st == 404  # not a device removed here
        st, out = await world.ui("POST", "/api/fleet/members/rig/allow")
        assert st == 200 and out == {"ok": True, "device": "rig", "host": "Rig"}
        assert fleet.is_member("rig")
        assert fleet.status(True)["readmitted_elsewhere"] == []
    for k in ("rig", "mini", "laptop"):
        await _gossip(world, k)
    with world.on("laptop"):
        assert fleet.is_member("rig")  # the others' tombstones are older


# --------------------------------------------------------------------------- #
# round 4 F: a later leave never unsticks a removal by another device
# --------------------------------------------------------------------------- #
async def test_a_later_leave_never_unsticks_a_removal(world):
    """rig removes laptop while mini is away (old epoch) and laptop offline;
    laptop comes back and leaves — its own tombstone reaches mini first, then
    mini takes rig's rekey. The removal (sticky, by rig) must survive on
    every member: before the fix the later self-tombstone replaced it, so it
    was relabelled "left" and a plain re-join would bring laptop back."""
    await _three(world)
    for k in ("laptop", "rig", "mini"):
        await _gossip(world, k)
    world.down = {"mini", "laptop"}
    world.rediscover()
    with world.on("rig"):
        await fleet.remove("laptop", rotate_tokens=False)
        before = [
            (r["key"], r["left"], r["removed_by"])
            for r in fleet.status(True)["removed"]
        ]
    assert before == [("laptop", False, "rig")]
    world.down = {"rig"}
    world.rediscover()
    with world.on("laptop"):
        await fleet.leave_fleet()
    world.down = set()
    world.rediscover()
    for _ in range(3):
        for k in ("mini", "rig"):
            await _gossip(world, k)
    for k in ("rig", "mini"):
        with world.on(k):
            after = [
                (r["key"], r["left"], r["removed_by"])
                for r in fleet.status(True)["removed"]
            ]
            assert after == before, (k, after)
            assert "laptop" in fleet._dead_here(fleet.state())


def test_tombstone_merge_removal_outranks_self_in_either_order():
    """The merge itself: a removal by another device beats the device's own
    leave whichever is later and whichever side holds it (both orders give
    the same tombstone), and it takes the later ``at``; two of a kind keep
    the later one."""
    removal = {"at": 10.0, "by": "rig"}
    leave = {"at": 20.0, "by": "laptop"}
    for mine, theirs in ((removal, leave), (leave, removal)):
        doc = {"removed": {"laptop": dict(mine)}}
        assert fleet._union_removed(doc, {"laptop": dict(theirs)}) is True
        assert doc["removed"]["laptop"] == {"at": 20.0, "by": "rig"}
    # an older leave than the removal: unchanged, still the removal
    doc = {"removed": {"laptop": {"at": 30.0, "by": "rig"}}}
    assert fleet._union_removed(doc, {"laptop": dict(leave)}) is False
    assert doc["removed"]["laptop"] == {"at": 30.0, "by": "rig"}
    # self over self, removal over removal: the later wins, as before
    doc = {"removed": {"laptop": {"at": 5.0, "by": "laptop"}}}
    assert fleet._union_removed(doc, {"laptop": dict(leave)}) is True
    assert doc["removed"]["laptop"] == leave
    doc = {"removed": {"laptop": dict(removal)}}
    assert fleet._union_removed(doc, {"laptop": {"at": 15.0, "by": "mini"}})
    assert doc["removed"]["laptop"] == {"at": 15.0, "by": "mini"}
    # no tombstone here: a leave is taken as it is
    doc = {"removed": {}}
    assert fleet._union_removed(doc, {"laptop": dict(leave)}) is True
    assert doc["removed"]["laptop"] == leave
