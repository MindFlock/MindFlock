"""The auth + discovery half of "Your devices" (spec §3).

* :mod:`backend.web.core.auth` — the fleet key is a credential everywhere a
  token is (bearer / cookie / ``?token=`` / websocket / ``presented_token``),
  ``own_token_valid`` is the check that excludes it, ``privileged()`` decides
  who may approve joins, and exactly three join routes are public.
* :mod:`backend.web.core.remote` — the hello advertises the fleet, discovery
  records it, ``_headers_for`` picks the right credential, ``fleet_devices``
  is the narrow peer list settings sync uses (the regression: a gate-off
  non-member must never be in it), the new ``post_json`` / ``refresh_device``
  / ``discover_now`` primitives, and the fake-tailnet status file.
* :mod:`backend.web.core.mobile_access` — the phone QR carries the fleet key.
* The new events + the ``device_join`` push rule.

The fleet STORE (:mod:`backend.web.core.fleet`) is faked throughout: these
units only consume its ``fleet_id / fleet_key / key_valid / is_member /
in_fleet`` surface, and pinning that surface here keeps this file independent
of the store's own persistence (covered by test_fleet.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import backend.web.core as core_pkg
from backend.web import server
from backend.web.core import auth, events, mobile_access, remote, tailnet_trust

TOKEN = "own-token-abc123"
FLEET_KEY = "fleet-key-XYZ_0123456789"
FLEET_ID = "0123456789abcdef"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# A fake fleet store, installed where the lazy ``from backend.web.core import
# fleet`` finds it (the package attribute wins over the submodule import).
# --------------------------------------------------------------------------- #
class _FakeFleet(types.ModuleType):
    def __init__(self):
        super().__init__("backend.web.core.fleet")
        self.id = ""
        self.key = ""
        self.members: set = set()
        self.dns: dict = {}
        self.behind: set = set()
        self.broken = False

    def _check(self):
        if self.broken:
            raise RuntimeError("fleet store exploded")

    def fleet_id(self) -> str:
        self._check()
        return self.id

    def fleet_key(self) -> str:
        self._check()
        return self.key

    def in_fleet(self) -> bool:
        self._check()
        return bool(self.id and self.key)

    def key_valid(self, candidate) -> bool:
        self._check()
        return bool(self.key) and candidate == self.key

    def is_member(self, key) -> bool:
        self._check()
        return key in self.members

    def member_device(self, dev) -> bool:
        # The real one also binds to the recorded MagicDNS name (dns here).
        self._check()
        key = (dev or {}).get("key")
        want = self.dns.get(key, "")
        return key in self.members and (not want or (dev or {}).get("dns") == want)

    def peer_on_other_epoch(self, key) -> bool:
        return key in self.behind

    def join(self, *members):
        self.id, self.key = FLEET_ID, FLEET_KEY
        self.members = {"mybox", *members}
        return self


@pytest.fixture
def fleet(monkeypatch):
    fake = _FakeFleet()
    monkeypatch.setattr(core_pkg, "fleet", fake, raising=False)
    monkeypatch.setitem(sys.modules, "backend.web.core.fleet", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv(remote.STATUS_FILE_ENV, raising=False)
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(
        remote,
        "_SELF",
        {"key": "mybox", "host": "MyBox", "dns": "", "ip": "", "os": "linux"},
    )
    monkeypatch.setattr(remote, "_TOKENS", {})
    monkeypatch.setattr(remote, "_persist_tokens", lambda: None)
    yield


@pytest.fixture
def authed(monkeypatch, fleet):
    """Gate on with a known own token; this device is in a fleet."""
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    fleet.join()
    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# token_valid / own_token_valid
# --------------------------------------------------------------------------- #
def test_token_valid_accepts_own_token_and_fleet_key(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    fleet.join()
    assert auth.token_valid(TOKEN) is True
    assert auth.token_valid(FLEET_KEY) is True
    assert auth.token_valid("nope") is False
    assert auth.token_valid("") is False and auth.token_valid(None) is False


def test_own_token_valid_excludes_the_fleet_key(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    fleet.join()
    assert auth.own_token_valid(TOKEN) is True
    assert auth.own_token_valid(FLEET_KEY) is False


def test_no_fleet_means_no_extra_credential(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    assert auth.token_valid(FLEET_KEY) is False
    # An empty key never matches an empty / missing candidate either.
    assert auth.token_valid("") is False


def test_a_broken_fleet_store_degrades_to_own_token_only(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    fleet.join().broken = True
    assert auth.token_valid(FLEET_KEY) is False  # never raises
    assert auth.token_valid(TOKEN) is True


def test_presented_token_accepts_the_fleet_key(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    fleet.join()
    bearer = {"headers": [(b"authorization", ("Bearer " + FLEET_KEY).encode())]}
    cookie = {
        "headers": [
            (
                b"cookie",
                ("%s=%s" % (auth.cookie_name_for(FLEET_KEY), FLEET_KEY)).encode(),
            )
        ]
    }
    assert auth.presented_token(bearer) is True
    assert auth.presented_token(cookie) is True
    assert auth.presented_token({"headers": []}) is False


# --------------------------------------------------------------------------- #
# The middleware takes the fleet key wherever it takes the token
# --------------------------------------------------------------------------- #
def test_fleet_key_bearer_passes_the_gate(authed):
    r = authed.get("/api/instances", headers={"Authorization": "Bearer " + FLEET_KEY})
    assert r.status_code == 200
    bad = authed.get("/api/instances", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_fleet_key_cookie_passes_the_gate(authed):
    authed.cookies.set(auth.cookie_name_for(FLEET_KEY), FLEET_KEY)
    assert authed.get("/api/instances").status_code == 200


def test_fleet_key_query_token_redirects_and_stores_it(authed):
    r = authed.get(
        "/?token=" + FLEET_KEY, headers={"accept": "text/html"}, follow_redirects=False
    )
    assert r.status_code == 302
    cookies = r.headers.get_list("set-cookie")
    # The own token's plain cookie AND the fleet key's keyed copy — the latter
    # is what signs the phone in on every other member.
    assert any(c.startswith(auth.COOKIE_NAME + "=") for c in cookies)
    assert any(c.startswith(auth.cookie_name_for(FLEET_KEY) + "=") for c in cookies)


def test_fleet_key_opens_the_websocket(authed):
    with authed.websocket_connect("/api/events?token=" + FLEET_KEY) as ws:
        assert ws.receive_json()["event"] == "hello"


def test_wrong_key_websocket_still_refused(authed):
    with pytest.raises(WebSocketDisconnect) as ei:
        with authed.websocket_connect("/api/events?token=not-the-key"):
            pass
    assert ei.value.code == 4401


# --------------------------------------------------------------------------- #
# privileged()
# --------------------------------------------------------------------------- #
def _scope(peer=("127.0.0.1", 50123), headers=(), client=None) -> dict:
    s = {"type": "http", "headers": list(headers), "mf_peer": peer}
    if client is not None:
        s["client"] = client
    return s


@pytest.fixture
def untrusted_tailnet(monkeypatch):
    """No Tailscale trust unless a test says so (and count the asks)."""
    calls = []

    async def _trusted(scope):
        calls.append(scope)
        return False

    monkeypatch.setattr(tailnet_trust, "request_trusted", _trusted)
    return calls


def test_privileged_loopback_without_forwarding(untrusted_tailnet):
    assert _run(auth.privileged(_scope(("127.0.0.1", 1)))) is True
    assert _run(auth.privileged(_scope(("::1", 1)))) is True
    assert _run(auth.privileged(_scope(("::ffff:127.0.0.1", 1)))) is True


@pytest.mark.parametrize("header", [b"x-forwarded-for", b"forwarded", b"x-real-ip"])
def test_privileged_loopback_with_forwarding_header_is_not_local(
    untrusted_tailnet, header
):
    """``tailscale serve`` delivers tailnet traffic from 127.0.0.1 too."""
    scope = _scope(("127.0.0.1", 1), headers=[(header, b"100.64.0.9")])
    assert _run(auth.privileged(scope)) is False
    assert untrusted_tailnet, "fell through to the tailnet trust check"


def test_privileged_tailnet_ip_needs_trust_or_token(monkeypatch, untrusted_tailnet):
    scope = _scope(("100.64.0.9", 1))
    assert _run(auth.privileged(scope)) is False

    async def _trusted(scope):
        return True

    monkeypatch.setattr(tailnet_trust, "request_trusted", _trusted)
    assert _run(auth.privileged(scope)) is True


def test_privileged_with_a_presented_credential(monkeypatch, fleet, untrusted_tailnet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    fleet.join()
    for cred in (TOKEN, FLEET_KEY):
        scope = _scope(
            ("100.64.0.9", 1), headers=[(b"authorization", b"Bearer " + cred.encode())]
        )
        assert _run(auth.privileged(scope)) is True, cred


def test_privileged_never_for_a_relayed_request(monkeypatch, untrusted_tailnet):
    """Even from loopback, even with the token, even with tailnet trust."""
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)

    async def _trusted(scope):
        return True

    monkeypatch.setattr(tailnet_trust, "request_trusted", _trusted)
    scope = _scope(
        ("127.0.0.1", 1),
        headers=[
            (b"x-mindflock-remote", b"otherbox"),
            (b"authorization", b"Bearer " + TOKEN.encode()),
        ],
    )
    assert _run(auth.privileged(scope)) is False


def test_privileged_reads_the_transport_peer_not_the_rewritten_client(
    untrusted_tailnet,
):
    # mf_peer (captured BEFORE the proxy-headers rewrite) is what counts.
    scope = _scope(("100.64.0.9", 1), client=("127.0.0.1", 1))
    assert _run(auth.privileged(scope)) is False
    # …with scope["client"] as the fallback when the capture isn't mounted.
    assert _run(
        auth.privileged({"type": "http", "headers": [], "client": ("127.0.0.1", 9)})
    )
    assert _run(auth.privileged({"type": "http", "headers": []})) is False
    # The TestClient's default peer is not loopback.
    assert _run(auth.privileged(_scope(("testclient", 50000)))) is False


def test_privileged_never_raises(monkeypatch):
    async def _boom(scope):
        raise RuntimeError("whois exploded")

    monkeypatch.setattr(tailnet_trust, "request_trusted", _boom)
    assert _run(auth.privileged(_scope(("100.64.0.9", 1)))) is False


def test_tailnet_trust_public_helpers():
    assert tailnet_trust.is_loopback("127.0.0.1") and tailnet_trust.is_loopback("::1")
    assert not tailnet_trust.is_loopback("100.64.0.1")
    assert not tailnet_trust.is_loopback("testclient")
    assert not tailnet_trust.is_loopback(None)
    assert tailnet_trust.has_forward_headers(
        {"headers": [(b"x-forwarded-for", b"1.2.3.4")]}
    )
    assert not tailnet_trust.has_forward_headers({"headers": [(b"host", b"x")]})
    assert not tailnet_trust.has_forward_headers({})


# --------------------------------------------------------------------------- #
# Public join routes: exact (method, path) only
# --------------------------------------------------------------------------- #
_POLL_ID = "0123456789abcdef"
_POLL = "/api/fleet/requests/" + _POLL_ID


@pytest.mark.parametrize(
    "method, path, public",
    [
        ("POST", "/api/fleet/redeem", True),
        ("POST", "/api/fleet/requests", True),
        ("GET", _POLL, True),
        # Wrong method on a public path.
        ("GET", "/api/fleet/redeem", False),
        ("GET", "/api/fleet/requests", False),
        ("POST", _POLL, False),
        ("DELETE", _POLL, False),
        # The privileged neighbours.
        ("POST", _POLL + "/approve", False),
        ("POST", _POLL + "/deny", False),
        ("GET", "/api/fleet", False),
        ("POST", "/api/fleet/join", False),
        ("POST", "/api/fleet/adopt", False),
        ("GET", "/api/fleet/roster", False),
        ("POST", "/api/fleet/invite", False),
        # Near misses on the poll id.
        ("GET", "/api/fleet/requests/" + _POLL_ID.upper(), False),
        ("GET", "/api/fleet/requests/0123456789abcde", False),
        ("GET", "/api/fleet/requests/0123456789abcdef0", False),
        ("GET", "/api/fleet/requests/0123456789abcdeg", False),
        ("GET", _POLL + "/", False),
        ("GET", _POLL + "\n", False),
        ("POST", "/api/fleet/redeem/", False),
        ("POST", "/api/fleet/requests/", False),
    ],
)
def test_public_fleet_route_matching(method, path, public):
    scope = {"type": "http", "method": method, "path": path}
    assert auth._public_fleet_route(scope) is public


def test_public_fleet_route_is_http_only():
    assert not auth._public_fleet_route(
        {"type": "websocket", "path": "/api/fleet/redeem", "method": "POST"}
    )


def _not_gated(r) -> bool:
    """Whatever the route itself answers (404 before the addon lands, its own
    400/403 after) — just not the middleware's refusal."""
    if r.status_code == 401:
        return False
    try:
        err = r.json().get("error")
    except ValueError:
        return True
    return err != "remote control is disabled on this device"


def test_public_join_routes_skip_the_gate(authed):
    assert _not_gated(authed.post("/api/fleet/redeem", json={}))
    assert _not_gated(authed.post("/api/fleet/requests", json={}))
    assert _not_gated(authed.get(_POLL))


def test_privileged_neighbours_stay_gated(authed):
    assert authed.post(_POLL + "/approve").status_code == 401
    assert authed.get("/api/fleet").status_code == 401
    assert authed.get("/api/fleet/requests/nothex").status_code == 401


def test_public_join_routes_skip_the_remote_control_gate(monkeypatch, fleet):
    """A device that isn't a member yet still carries the remote marker (every
    outbound call does) — remote control being off here must not stop a join."""
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: False)
    c = TestClient(server.app)
    hdr = {"X-MindFlock-Remote": "newbox"}
    assert _not_gated(c.post("/api/fleet/redeem", json={}, headers=hdr))
    assert _not_gated(c.post("/api/fleet/requests", json={}, headers=hdr))
    assert _not_gated(c.get(_POLL, headers=hdr))
    # Anything else is still refused.
    r = c.post(_POLL + "/approve", headers=hdr)
    assert r.status_code == 403
    assert r.json()["error"] == "remote control is disabled on this device"


# --------------------------------------------------------------------------- #
# remote: hello + discovery
# --------------------------------------------------------------------------- #
def test_hello_advertises_the_fleet(fleet):
    hello = remote.hello_json()
    assert hello["fleet"] == "" and hello["fleet_proto"] == 1
    fleet.join()
    assert remote.hello_json()["fleet"] == FLEET_ID
    # Never the key.
    assert FLEET_KEY not in json.dumps(remote.hello_json())


def test_hello_survives_a_broken_fleet_store(fleet):
    fleet.join().broken = True
    hello = remote.hello_json()
    assert hello["fleet"] == "" and hello["fleet_proto"] == 1


def test_hello_route_serves_the_fleet_fields(fleet):
    fleet.join()
    body = TestClient(server.app).get("/api/remote/hello").json()
    assert body["fleet"] == FLEET_ID and body["fleet_proto"] == 1


def _peer(key="otherbox", ip="100.1.2.3", dns="otherbox.tail.ts.net"):
    return {
        "key": key,
        "host": key.title(),
        "dns": dns,
        "ip": ip,
        "os": "linux",
        "online": True,
    }


def test_discovery_records_fleet_and_proto(monkeypatch):
    monkeypatch.setattr(
        remote, "tailscale_nodes", lambda: (None, [_peer(), _peer("old")])
    )

    async def _probe(peer):
        if peer["key"] == "old":  # a MindFlock from before fleets
            return "http://%s:8765" % peer["ip"], {"app": "mindflock"}
        return "http://%s:8765" % peer["ip"], {
            "app": "mindflock",
            "fleet": FLEET_ID,
            "fleet_proto": 1,
        }

    monkeypatch.setattr(remote, "_probe_peer", _probe)
    _run(remote._discover_once())
    dev = remote._DEVICES["otherbox"]
    assert dev["fleet"] == FLEET_ID and dev["fleet_proto"] == 1
    assert dev["dns"] == "otherbox.tail.ts.net"
    old = remote._DEVICES["old"]
    assert old["fleet"] == "" and old["fleet_proto"] == 0


def test_discovery_tolerates_a_garbage_fleet_proto(monkeypatch):
    monkeypatch.setattr(remote, "tailscale_nodes", lambda: (None, [_peer()]))

    async def _probe(peer):
        return "http://x:1", {"app": "mindflock", "fleet_proto": "two", "fleet": None}

    monkeypatch.setattr(remote, "_probe_peer", _probe)
    _run(remote._discover_once())
    dev = remote._DEVICES["otherbox"]
    assert dev["fleet_proto"] == 0 and dev["fleet"] == ""


def test_discover_now_runs_one_sweep(monkeypatch):
    calls = []

    async def _once():
        calls.append(1)

    monkeypatch.setattr(remote, "_discover_once", _once)
    _run(remote.discover_now())
    assert calls == [1]


def test_refresh_route_sweeps_and_returns_devices(monkeypatch):
    calls = []

    async def _now():
        calls.append(1)
        remote._DEVICES["otherbox"] = _dev(last_seen=1.0)

    monkeypatch.setattr(remote, "discover_now", _now)
    r = TestClient(server.app).post("/api/devices/refresh")
    assert r.status_code == 200 and calls == [1]
    assert [d["device"] for d in r.json()["devices"]] == ["otherbox"]


def test_refresh_device_reprobes_with_stored_address(monkeypatch):
    remote._DEVICES["otherbox"] = _dev(fleet="", dns="otherbox.tail.ts.net")
    seen = []

    async def _probe(peer):
        seen.append(peer)
        return "https://otherbox.tail.ts.net", {
            "app": "mindflock",
            "fleet": FLEET_ID,
            "fleet_proto": 1,
            "remote_control": True,
        }

    monkeypatch.setattr(remote, "_probe_peer", _probe)
    snap = _run(remote.refresh_device("otherbox"))
    assert seen == [{"ip": "100.1.2.3", "dns": "otherbox.tail.ts.net"}]
    assert (
        snap["fleet"] == FLEET_ID and snap["base_url"] == "https://otherbox.tail.ts.net"
    )
    # A snapshot, not the live state.
    snap["fleet"] = "mutated"
    assert remote._DEVICES["otherbox"]["fleet"] == FLEET_ID


def test_refresh_device_miss_marks_unreachable_and_unknown_is_none(monkeypatch):
    remote._DEVICES["otherbox"] = _dev()

    async def _miss(peer):
        return None

    monkeypatch.setattr(remote, "_probe_peer", _miss)
    snap = _run(remote.refresh_device("otherbox"))
    assert snap["reachable"] is False
    assert _run(remote.refresh_device("nobody")) is None


def test_candidate_bases_skip_an_empty_ip():
    assert remote._candidate_bases({"ip": "", "dns": "a.ts.net"}) == [
        "https://a.ts.net"
    ]


# --------------------------------------------------------------------------- #
# Fake tailnet file (the e2e / sandbox hook)
# --------------------------------------------------------------------------- #
def test_status_file_replaces_the_tailscale_cli(monkeypatch, tmp_path):
    doc = {
        "Self": {
            "HostName": "Alpha",
            "DNSName": "alpha.tail.ts.net.",
            "OS": "linux",
            "Online": True,
            "TailscaleIPs": ["127.0.0.2"],
        },
        "Peer": {
            "b": {
                "HostName": "Beta",
                "DNSName": "beta.tail.ts.net.",
                "OS": "linux",
                "Online": True,
                "TailscaleIPs": ["127.0.0.3"],
            },
            "p": {
                "HostName": "Phone",
                "DNSName": "phone.tail.ts.net.",
                "OS": "iOS",
                "Online": True,
                "TailscaleIPs": ["127.0.0.4"],
            },
        },
    }
    path = tmp_path / "ts.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setenv(remote.STATUS_FILE_ENV, str(path))

    def _no_cli(*a, **kw):
        raise AssertionError("must not shell out to tailscale")

    import shutil
    import subprocess

    monkeypatch.setattr(subprocess, "run", _no_cli)
    monkeypatch.setattr(shutil, "which", lambda _: None)  # no CLI at all
    self_entry, peers = remote.tailscale_nodes()
    assert self_entry["key"] == "alpha" and self_entry["ip"] == "127.0.0.2"
    assert [p["key"] for p in peers] == ["beta"]  # the phone is filtered


def test_status_file_unreadable_means_no_tailnet(monkeypatch, tmp_path):
    monkeypatch.setenv(remote.STATUS_FILE_ENV, str(tmp_path / "missing.json"))
    assert remote.tailscale_nodes() == (None, [])
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    monkeypatch.setenv(remote.STATUS_FILE_ENV, str(bad))
    assert remote.tailscale_nodes() == (None, [])


# --------------------------------------------------------------------------- #
# remote: credentials, connected, fleet_devices, devices_json
# --------------------------------------------------------------------------- #
def _dev(**over) -> dict:
    dev = {
        "key": "otherbox",
        "host": "OtherBox",
        "os": "linux",
        "ip": "100.1.2.3",
        "dns": "",
        "base_url": "http://100.1.2.3:8765",
        "reachable": True,
        "remote_control": True,
        "auth": True,
        "version": "0.7.3",
        "shared_link": "",
        "fleet": FLEET_ID,
        "fleet_proto": 1,
        "last_seen": 0.0,
        "instances": [],
        "instances_ok": False,
        "error": "",
    }
    dev.update(over)
    return dev


def test_headers_for_prefers_the_fleet_key_for_members(fleet):
    fleet.join("otherbox")
    remote._DEVICES["otherbox"] = _dev()
    remote._TOKENS["otherbox"] = "pasted-token"
    h = remote._headers_for("otherbox")
    assert h[remote.REMOTE_HEADER] == "mybox"
    assert h["Authorization"] == "Bearer " + FLEET_KEY


def test_headers_for_sends_the_fleet_key_only_to_the_member_itself(fleet):
    """Findings [8]/[17]: the key goes only to the discovered device under the
    member's recorded MagicDNS name whose hello names this fleet — never to a
    node that merely holds the same first label, nor to a member whose hello
    says it isn't in this fleet (a ghost) or that is on another key epoch.
    Remote control then falls back to the pasted token."""
    fleet.join("otherbox")
    fleet.dns["otherbox"] = "otherbox.tail1.ts.net"
    remote._TOKENS["otherbox"] = "pasted-token"
    # Same label, another tailnet's node.
    remote._DEVICES["otherbox"] = _dev(dns="otherbox.evil.ts.net")
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer pasted-token"
    assert remote.fleet_devices() == []
    # The member itself.
    remote._DEVICES["otherbox"] = _dev(dns="otherbox.tail1.ts.net")
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer " + FLEET_KEY
    assert [d["key"] for d in remote.fleet_devices()] == ["otherbox"]
    # Its hello names no / another fleet: a ghost — the pasted token.
    remote._DEVICES["otherbox"]["fleet"] = ""
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer pasted-token"
    remote._DEVICES["otherbox"]["fleet"] = FLEET_ID
    # Gossip found it on another key epoch: its pasted token still works.
    fleet.behind.add("otherbox")
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer pasted-token"
    # An explicit bearer (the fleet's own routes) always wins.
    assert (
        remote._headers_for("otherbox", bearer=FLEET_KEY)["Authorization"]
        == "Bearer " + FLEET_KEY
    )


def test_headers_for_falls_back_to_the_pasted_token(fleet):
    fleet.join()  # otherbox is NOT a member
    remote._TOKENS["otherbox"] = "pasted-token"
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer pasted-token"
    # Neither: the marker alone.
    assert "Authorization" not in remote._headers_for("stranger")


def test_headers_for_explicit_bearer_and_no_auth(fleet):
    fleet.join("otherbox")
    remote._TOKENS["otherbox"] = "pasted-token"
    assert (
        remote._headers_for("otherbox", bearer="old-key")["Authorization"]
        == "Bearer old-key"
    )
    # auth=False: a public join route gets no credential at all…
    bare = remote._headers_for("otherbox", auth=False)
    assert "Authorization" not in bare and bare[remote.REMOTE_HEADER] == "mybox"
    # …unless one is passed explicitly.
    assert (
        remote._headers_for("otherbox", auth=False, bearer="x")["Authorization"]
        == "Bearer x"
    )


def test_headers_for_a_broken_store_uses_the_pasted_token(fleet):
    fleet.join("otherbox").broken = True
    remote._TOKENS["otherbox"] = "pasted-token"
    assert remote._headers_for("otherbox")["Authorization"] == "Bearer pasted-token"


def test_a_member_is_connected_without_a_pasted_token(fleet):
    remote._DEVICES["otherbox"] = _dev(auth=True)
    assert remote.connected_devices() == []
    fleet.join("otherbox")
    fleet.dns["otherbox"] = "otherbox.tail1.ts.net"
    assert remote.connected_devices() == []  # not under the member's name
    remote._DEVICES["otherbox"]["dns"] = "otherbox.tail1.ts.net"
    assert [d["key"] for d in remote.connected_devices()] == ["otherbox"]
    # Remote control off there still means not drivable.
    remote._DEVICES["otherbox"]["remote_control"] = False
    assert remote.connected_devices() == []


def test_fleet_devices_filter(fleet):
    fleet.join("member", "otherfleet", "offline", "norc")
    remote._DEVICES.update(
        {
            "member": _dev(key="member"),
            # On my roster but its hello names another fleet (it left, or
            # joined someone else): never trusted with my settings.
            "otherfleet": _dev(key="otherfleet", fleet="ffffffffffffffff"),
            "offline": _dev(key="offline", reachable=False),
            # Remote control off is NOT a filter — its 403 surfaces as an error.
            "norc": _dev(key="norc", remote_control=False),
            # Claims my fleet id but isn't on my roster.
            "impostor": _dev(key="impostor"),
        }
    )
    assert sorted(d["key"] for d in remote.fleet_devices()) == ["member", "norc"]
    # Copies.
    remote.fleet_devices()[0]["fleet"] = "x"
    assert remote._DEVICES["member"]["fleet"] == FLEET_ID


def test_gate_off_non_member_is_connected_but_never_a_fleet_device(fleet):
    """The regression the fleet closes: a gate-off tailnet node counts as
    "connected" with no credential — settings sync must not see it."""
    fleet.join()
    remote._DEVICES["gateoff"] = _dev(key="gateoff", auth=False, fleet="")
    assert [d["key"] for d in remote.connected_devices()] == ["gateoff"]
    assert remote.fleet_devices() == []
    # Even when it lies about being in my fleet.
    remote._DEVICES["gateoff"]["fleet"] = FLEET_ID
    assert remote.fleet_devices() == []


def test_fleet_devices_empty_outside_a_fleet(fleet):
    fleet.members = {"otherbox"}  # roster without an id: not in a fleet
    remote._DEVICES["otherbox"] = _dev(fleet="")
    assert remote.fleet_devices() == []


def test_devices_json_rows_carry_fleet_fields(fleet):
    fleet.join("member")
    remote._DEVICES.update(
        {
            "member": _dev(key="member"),
            "joinable": _dev(key="joinable", fleet="", fleet_proto=1),
            "elsewhere": _dev(key="elsewhere", fleet="ffffffffffffffff"),
            # Pre-fleet device injected the old way (no fleet keys at all).
            "old": {
                k: v
                for k, v in _dev(key="old").items()
                if k not in ("fleet", "fleet_proto")
            },
        }
    )
    rows = {d["device"]: d for d in remote.devices_json()["devices"]}
    assert rows["member"]["member"] is True
    assert rows["member"]["same_fleet"] is True and rows["member"]["in_fleet"] is True
    # A member needs no pasted token.
    assert rows["member"]["needs_token"] is False
    assert rows["joinable"] == dict(
        rows["joinable"], member=False, in_fleet=False, same_fleet=False, fleet_proto=1
    )
    assert rows["joinable"]["needs_token"] is True
    assert (
        rows["elsewhere"]["in_fleet"] is True
        and rows["elsewhere"]["same_fleet"] is False
    )
    assert rows["old"]["fleet_proto"] == 0 and rows["old"]["in_fleet"] is False


# --------------------------------------------------------------------------- #
# remote: get_json / post_json on the wire
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, payload=None, raw=None):
        self.status = status
        self._payload = payload
        self._raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if self._raw is not None:
            raise ValueError("not JSON")
        return self._payload


class _Session:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.reply()

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.reply()


def _install(monkeypatch, reply):
    sess = _Session(reply)

    async def _http():
        return sess

    monkeypatch.setattr(remote, "_http_session", _http)
    return sess


def test_post_json_sends_json_with_the_fleet_key(monkeypatch, fleet):
    fleet.join("otherbox")
    remote._DEVICES["otherbox"] = _dev()
    sess = _install(monkeypatch, lambda: _Resp(200, {"ok": True}))
    status, body = _run(remote.post_json(_dev(), "/api/fleet/roster", {"a": 1}))
    assert (status, body) == (200, {"ok": True})
    method, url, kw = sess.calls[0]
    assert (method, url) == ("POST", "http://100.1.2.3:8765/api/fleet/roster")
    assert kw["json"] == {"a": 1}
    assert kw["headers"]["Authorization"] == "Bearer " + FLEET_KEY


def test_post_json_parses_error_bodies(monkeypatch, fleet):
    _install(
        monkeypatch, lambda: _Resp(403, {"error": "that code is wrong or expired"})
    )
    status, body = _run(remote.post_json(_dev(), "/api/fleet/redeem", {}, auth=False))
    assert status == 403 and body == {"error": "that code is wrong or expired"}
    _install(monkeypatch, lambda: _Resp(502, raw=b"<html>"))
    assert _run(remote.post_json(_dev(), "/x", {})) == (502, None)


def test_post_json_auth_false_and_explicit_bearer(monkeypatch, fleet):
    fleet.join("otherbox")
    sess = _install(monkeypatch, lambda: _Resp(200, {}))
    _run(remote.post_json(_dev(), "/api/fleet/redeem", {}, auth=False))
    assert "Authorization" not in sess.calls[-1][2]["headers"]
    _run(remote.post_json(_dev(), "/api/fleet/adopt", {}, bearer="their-token"))
    assert sess.calls[-1][2]["headers"]["Authorization"] == "Bearer their-token"


def test_post_json_unreachable(monkeypatch, fleet):
    def _boom():
        raise OSError("connection refused")

    _install(monkeypatch, _boom)
    assert _run(remote.post_json(_dev(), "/x", {})) == (0, None)
    assert _run(remote.post_json(_dev(base_url=""), "/x", {})) == (0, None)


def test_get_json_new_kwargs(monkeypatch, fleet):
    fleet.join("otherbox")
    remote._DEVICES["otherbox"] = _dev()
    sess = _install(monkeypatch, lambda: _Resp(200, {"state": "pending"}))
    status, body = _run(
        remote.get_json(_dev(), "/api/fleet/requests/x?secret=s", auth=False)
    )
    assert (status, body) == (200, {"state": "pending"})
    assert "Authorization" not in sess.calls[-1][2]["headers"]
    _run(remote.get_json(_dev(), "/api/fleet/roster"))
    assert sess.calls[-1][2]["headers"]["Authorization"] == "Bearer " + FLEET_KEY
    # Non-200 keeps its old shape: status only.
    _install(monkeypatch, lambda: _Resp(401, {"error": "x"}))
    assert _run(remote.get_json(_dev(), "/api/fleet/roster")) == (401, None)


# --------------------------------------------------------------------------- #
# mobile QR
# --------------------------------------------------------------------------- #
def test_signin_tokens_carry_the_fleet_key(monkeypatch, fleet):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    assert mobile_access._signin_tokens(False) == [TOKEN]
    fleet.join()
    assert mobile_access._signin_tokens(False) == [TOKEN, FLEET_KEY]
    remote._TOKENS["otherbox"] = "paired-token"
    remote._TOKENS["dup"] = FLEET_KEY  # never twice
    assert mobile_access._signin_tokens(True) == [TOKEN, FLEET_KEY, "paired-token"]


def test_signin_tokens_gate_off_or_broken_store(monkeypatch, fleet):
    fleet.join()
    assert mobile_access._signin_tokens(False) == []  # gate off: no tokens at all
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    fleet.broken = True
    assert mobile_access._signin_tokens(False) == [TOKEN]


# --------------------------------------------------------------------------- #
# events + the push rule
# --------------------------------------------------------------------------- #
def test_device_events_are_in_the_vocabulary():
    for name in (
        "device.join_requested",
        "device.joined",
        "device.removed",
        "settings.synced",
    ):
        assert name in events.EVENT_NAMES


def test_device_join_rule_shape_and_fill():
    from backend.web.addons import notify as notify_addon

    rule = next(r for r in notify_addon.NOTIFY_RULES if r["id"] == "device_join")
    assert rule == {
        "id": "device_join",
        "label": "A device asks to join your devices",
        "event": "device.join_requested",
        "old": None,
        "new": None,
        "title": "Device wants to join",
        "body": "{detail}",
        "default_enabled": True,
        "priority": 4,
        "tags": ["computer"],
    }
    env = {
        "event": "device.join_requested",
        "session": "",
        "old": None,
        "new": None,
        "data": {"detail": "Beta · code 123 456"},
    }
    assert notify_addon._matches(rule, env)
    assert notify_addon._fill(rule["body"], env) == "Beta · code 123 456"
