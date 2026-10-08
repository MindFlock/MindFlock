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

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web.core import auth as web_auth
from backend.web.core import fleet, remote
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
    # mini looks first: laptop answers 401 with a NEWER epoch -> mini is the
    # one behind (and rig, still on the old key, can't outvote that).
    assert await _gossip(world, "mini") is True
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
# [18] [4] [11] admitting: before the adopt, seeded, run_here
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


async def test_run_here_admitter_keeps_it_joiner_hands_it_over(world):
    for k in ("laptop", "rig"):
        with world.on(k):
            assert store.load_settings().github.run_here is None
    await _join_by_code(world, "rig", "laptop")
    with world.on("laptop"):
        assert store.load_settings().github.run_here is True
    with world.on("rig"):
        assert store.load_settings().github.run_here is False
    # A choice already made is never overridden.
    with world.on("mini"):
        store.update_settings(github={"run_here": True})
    await _join_by_code(world, "mini", "laptop")
    with world.on("mini"):
        assert store.load_settings().github.run_here is True


async def test_status_and_hello_report_automation(world):
    await _join_by_code(world, "rig", "laptop")
    remote._DEVICES["rig"]["automation"] = False
    with world.on("laptop"):
        st = fleet.status(True)
        rows = {m["key"]: m["automation"] for m in st["members"]}
        assert rows == {"laptop": True, "rig": False}
        assert remote.hello_json()["automation"] is True
    # A member too old to say is reported as unknown (the UI's hint and the
    # CLI's warning leave it out) rather than as "doesn't run it".
    remote._DEVICES["rig"]["automation"] = None
    with world.on("laptop"):
        rows = {m["key"]: m["automation"] for m in fleet.status(True)["members"]}
        assert rows == {"laptop": True, "rig": None}


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
    # A paired non-member is still governed by the toggle.
    other = {"Authorization": "Bearer something-else", "X-MindFlock-Remote": "rig"}
    for path in ("/api/fleet/roster", "/api/instances"):
        r = c.get(path, headers=other)
        assert r.status_code == 403
        assert r.json()["error"] == "remote control is disabled on this device"


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
