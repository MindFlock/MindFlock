"""Tailnet multi-device control (:mod:`backend.web.core.remote`).

Covers the title namespacing, the tailscale peer filter (mobile devices never
count), the public hello endpoint, the devices endpoint, the merged instances
snapshot, the remote-control permission gate, and the proxy middleware's
routing decisions (local titles untouched, unknown devices 502).

No network: device state is injected straight into ``remote._DEVICES`` and the
tailscale CLI is monkeypatched away.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.web import server
from backend.web.core import remote


@pytest.fixture(autouse=True)
def _clean_remote_state(monkeypatch):
    """Isolate each test: no devices, no tokens, no tailscale, auth off."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(remote, "_SELF", {})
    monkeypatch.setattr(remote, "_TOKENS", {})
    monkeypatch.setattr(remote, "_persist_tokens", lambda: None)
    yield


def _fake_device(**over) -> dict:
    dev = {
        "key": "otherbox",
        "host": "OtherBox",
        "os": "windows",
        "ip": "100.1.2.3",
        "base_url": "http://100.1.2.3:8765",
        "reachable": True,
        "remote_control": True,
        "auth": False,
        "version": "0.1.2",
        "last_seen": 0.0,
        "instances": [],
        "instances_ok": False,
        "error": "",
    }
    dev.update(over)
    return dev


# --------------------------------------------------------------------------- #
# title namespacing
# --------------------------------------------------------------------------- #
def test_title_namespace_roundtrip():
    ns = remote.join_title("otherbox", "myrepo")
    assert ns == "otherbox::myrepo"
    assert remote.is_remote_title(ns)
    assert remote.split_title(ns) == ("otherbox", "myrepo")


def test_local_title_is_not_remote():
    assert not remote.is_remote_title("myrepo")
    assert remote.split_title("myrepo") == ("", "myrepo")


def test_title_containing_separator_splits_on_first():
    dev, bare = remote.split_title("otherbox::weird::title")
    assert (dev, bare) == ("otherbox", "weird::title")


# --------------------------------------------------------------------------- #
# tailscale peer filter — phones/tablets never count as devices
# --------------------------------------------------------------------------- #
_TS_STATUS = {
    "Self": {
        "HostName": "MyBox",
        "DNSName": "mybox.tail1234.ts.net.",
        "OS": "linux",
        "Online": True,
        "TailscaleIPs": ["100.0.0.1"],
    },
    "Peer": {
        "a": {
            "HostName": "Desktop",
            "DNSName": "desktop.tail1234.ts.net.",
            "OS": "windows",
            "Online": True,
            "TailscaleIPs": ["100.0.0.2"],
        },
        "b": {
            "HostName": "iPhone",
            "DNSName": "iphone.tail1234.ts.net.",
            "OS": "iOS",
            "Online": True,
            "TailscaleIPs": ["100.0.0.3"],
        },
        "c": {
            "HostName": "Pixel",
            "DNSName": "pixel.tail1234.ts.net.",
            "OS": "android",
            "Online": True,
            "TailscaleIPs": ["100.0.0.4"],
        },
        "d": {
            "HostName": "Laptop",
            "DNSName": "laptop.tail1234.ts.net.",
            "OS": "macOS",
            "Online": False,
            "TailscaleIPs": ["100.0.0.5"],
        },
    },
}


def test_peer_filter_excludes_mobile_and_offline(monkeypatch):
    import json as _json
    import shutil as _shutil
    import subprocess as _sub

    class _CP:
        returncode = 0
        stdout = _json.dumps(_TS_STATUS).encode()

    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(_sub, "run", lambda *a, **kw: _CP())
    self_entry, peers = remote.tailscale_nodes()
    assert self_entry["key"] == "mybox"
    assert [p["key"] for p in peers] == ["desktop"]  # no iOS, no android, no offline


# --------------------------------------------------------------------------- #
# hello + devices endpoints
# --------------------------------------------------------------------------- #
def test_hello_is_public_and_identifies_the_app(monkeypatch):
    # Public even with auth ON — discovery must work before pairing.
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "sekrit")
    c = TestClient(server.app)
    r = c.get("/api/remote/hello")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "mindflock"
    assert body["remote_control"] is False  # default: permission not granted
    assert "device" in body and "version" in body


def test_devices_lists_discovered_peers():
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True, instances=[{"title": "x"}]
    )
    c = TestClient(server.app)
    body = c.get("/api/devices").json()
    assert [d["device"] for d in body["devices"]] == ["otherbox"]
    dev = body["devices"][0]
    assert dev["connected"] is True
    assert dev["sessions"] == 1
    assert dev["needs_token"] is False


def test_devices_hides_peers_without_mindflock():
    # An online tailnet peer that never answered hello (e.g. the same
    # laptop's Windows-side node) must not surface as a device group.
    remote._DEVICES["winbox"] = _fake_device(
        key="winbox",
        reachable=False,
        last_seen=0.0,
        base_url="",
    )
    c = TestClient(server.app)
    assert c.get("/api/devices").json()["devices"] == []


def test_devices_keeps_recently_seen_but_unreachable():
    # A device that DID answer hello stays listed through a blip (last_seen
    # set), shown as unreachable rather than vanishing.
    remote._DEVICES["otherbox"] = _fake_device(reachable=False, last_seen=1.0)
    c = TestClient(server.app)
    devs = c.get("/api/devices").json()["devices"]
    assert [d["device"] for d in devs] == ["otherbox"]
    assert devs[0]["reachable"] is False


def test_devices_flags_token_needed():
    remote._DEVICES["otherbox"] = _fake_device(auth=True)
    c = TestClient(server.app)
    dev = c.get("/api/devices").json()["devices"][0]
    assert dev["needs_token"] is True
    assert dev["connected"] is False


# --------------------------------------------------------------------------- #
# merged instances snapshot
# --------------------------------------------------------------------------- #
def test_merged_instances_namespaces_titles():
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True,
        instances=[{"title": "myrepo", "status": "running"}],
    )
    merged = remote.merged_instances()
    assert len(merged) == 1
    inst = merged[0]
    assert inst["title"] == "otherbox::myrepo"
    assert inst["display_title"] == "myrepo"
    assert inst["device"] == "otherbox"
    assert inst["device_label"] == "OtherBox"
    assert inst["status"] == "running"


def test_merged_instances_namespaces_lineage_refs():
    # Regression: the rail nests a worker under the row whose TITLE equals the
    # worker's `parent`. A remote row's title is "dev::x", so a bare parent
    # never matched — remote workers showed as top-level rows on the viewer.
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True,
        instances=[
            {"title": "lead", "parent": ""},
            {
                "title": "worker",
                "parent": "lead",
                "spawned": True,
                "order": {"state": "held", "after": ["sib", ""], "fence": None},
                "lane": {"target": "pr", "owner": "lead"},
            },
            {"title": "old-peer-row"},  # an older peer: no lineage fields at all
        ],
    )
    by = {i["display_title"]: i for i in remote.merged_instances()}
    assert by["lead"]["parent"] == ""
    w = by["worker"]
    assert w["parent"] == "otherbox::lead" == by["lead"]["title"]
    assert w["order"]["after"] == ["otherbox::sib", ""]
    assert w["order"]["state"] == "held"
    assert w["lane"] == {"target": "pr", "owner": "otherbox::lead"}
    assert "parent" not in by["old-peer-row"]


def test_merged_instances_does_not_mutate_the_device_snapshot():
    row = {"title": "w", "parent": "lead", "order": {"after": ["a"]}}
    remote._DEVICES["otherbox"] = _fake_device(instances_ok=True, instances=[row])
    remote.merged_instances()
    remote.merged_instances()
    assert row == {"title": "w", "parent": "lead", "order": {"after": ["a"]}}
    assert remote.merged_instances()[0]["parent"] == "otherbox::lead"


def test_merged_instances_skips_disconnected_devices():
    remote._DEVICES["otherbox"] = _fake_device(
        reachable=False,
        instances_ok=True,
        instances=[{"title": "myrepo"}],
    )
    assert remote.merged_instances() == []


def test_instances_endpoint_includes_remote_sessions():
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True,
        instances=[{"title": "myrepo", "status": "running"}],
    )
    c = TestClient(server.app)
    titles = [i["title"] for i in c.get("/api/instances").json()]
    assert "otherbox::myrepo" in titles


# --------------------------------------------------------------------------- #
# remote-control permission gate (target side)
# --------------------------------------------------------------------------- #
def test_remote_flagged_request_refused_when_toggle_off():
    c = TestClient(server.app)
    r = c.get("/api/instances", headers={remote.REMOTE_HEADER: "somebox"})
    assert r.status_code == 403


def test_remote_flagged_request_allowed_when_toggle_on(monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    c = TestClient(server.app)
    r = c.get("/api/instances", headers={remote.REMOTE_HEADER: "somebox"})
    assert r.status_code == 200


def test_hello_ignores_remote_header_even_when_toggle_off():
    c = TestClient(server.app)
    r = c.get("/api/remote/hello", headers={remote.REMOTE_HEADER: "somebox"})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# proxy middleware routing
# --------------------------------------------------------------------------- #
def test_proxy_path_split():
    assert remote._split_proxy_path("/api/instances/dev::t/queue") == (
        "dev",
        "/api/instances/t/queue",
    )
    assert remote._split_proxy_path("/api/instances/dev::t") == (
        "dev",
        "/api/instances/t",
    )
    assert remote._split_proxy_path("/api/instances/local-title/queue") is None
    assert remote._split_proxy_path("/api/instances") is None
    assert remote._split_proxy_path("/api/devices") is None


def test_unknown_device_gets_502():
    c = TestClient(server.app)
    r = c.get("/api/instances/ghost%3A%3Afoo/queue")
    assert r.status_code == 502
    assert "not connected" in r.json()["error"]


def test_unknown_device_ws_closes_1011():
    # The websocket half of the proxy's not-connected path: an unknown device
    # gets the accept-then-close handshake with the internal-error code.
    c = TestClient(server.app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/api/instances/ghost%3A%3Afoo/terminal"):
            pass
    assert ei.value.code == 1011


def test_local_titles_still_reach_local_routes():
    c = TestClient(server.app)
    # A plain local title must pass the proxy middleware untouched (404/200
    # from the local handler — anything but the proxy's 502).
    r = c.get("/api/instances/definitely-local/queue")
    assert r.status_code != 502


# --------------------------------------------------------------------------- #
# connect / disconnect endpoints
# --------------------------------------------------------------------------- #
def test_connect_unknown_device_fails():
    c = TestClient(server.app)
    r = c.post("/api/devices/ghost/connect", json={"token": "abc"})
    assert r.status_code == 400
    assert "not reachable" in r.json()["error"]


def test_disconnect_forgets_token():
    remote._DEVICES["otherbox"] = _fake_device(auth=True)
    remote.set_token("otherbox", "abc")
    assert remote.token_for("otherbox") == "abc"
    c = TestClient(server.app)
    r = c.post("/api/devices/otherbox/disconnect")
    assert r.status_code == 200
    assert remote.token_for("otherbox") == ""
    assert r.json()["devices"][0]["has_token"] is False


# --------------------------------------------------------------------------- #
# starting a session on another device (/api/devices/<dev>/fwd/…)
# --------------------------------------------------------------------------- #
def test_fwd_path_split():
    assert remote._split_fwd_path("/api/devices/otherbox/fwd/api/repos/suggest") == (
        "otherbox",
        "/api/repos/suggest",
    )
    assert remote._split_fwd_path("/api/devices/otherbox/fwd/api/instances") == (
        "otherbox",
        "/api/instances",
    )
    # The pairing routes share the prefix and must never be taken for a forward.
    assert remote._split_fwd_path("/api/devices/otherbox/connect") is None
    assert remote._split_fwd_path("/api/devices/otherbox/fwdx/api/config") is None
    assert remote._split_fwd_path("/api/devices") is None


@pytest.fixture
def _captured_proxy(monkeypatch):
    """Record what the proxy would send instead of opening a socket."""

    class _Seen(list):
        timeouts: list

    seen = _Seen()
    timeouts = []

    async def fake_http(self, scope, receive, send, dev, url, timeout=None):
        seen.append((scope["method"], dev["key"], url))
        timeouts.append(timeout)
        await remote.JSONResponse({"title": "made-there"})(scope, receive, send)

    monkeypatch.setattr(remote.RemoteProxyMiddleware, "_proxy_http", fake_http)
    monkeypatch.setattr(remote, "aiohttp", object())
    remote._DEVICES["otherbox"] = _fake_device()
    seen.timeouts = timeouts
    return seen


def test_fwd_creates_on_the_chosen_device(_captured_proxy):
    c = TestClient(server.app)
    r = c.post("/api/devices/otherbox/fwd/api/instances", json={"title": "x"})
    assert r.status_code == 200
    assert r.json() == {"title": "made-there"}
    assert _captured_proxy == [
        ("POST", "otherbox", "http://100.1.2.3:8765/api/instances")
    ]


def test_fwd_keeps_the_query_string(_captured_proxy):
    c = TestClient(server.app)
    c.get("/api/devices/otherbox/fwd/api/repos/check?path=%7E%2Fcode")
    assert (
        _captured_proxy[0][2] == "http://100.1.2.3:8765/api/repos/check?path=%7E%2Fcode"
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/settings"),  # a settings WRITE is not the dialog's
        ("GET", "/api/auth/token"),
        ("POST", "/api/server/restart"),
        ("DELETE", "/api/instances"),
    ],
)
def test_fwd_refuses_paths_outside_the_allow_list(_captured_proxy, method, path):
    c = TestClient(server.app)
    r = c.request(method, "/api/devices/otherbox/fwd" + path)
    assert r.status_code == 404
    assert _captured_proxy == []


def test_fwd_to_unpaired_device_is_502():
    c = TestClient(server.app)
    r = c.get("/api/devices/ghost/fwd/api/config")
    assert r.status_code == 502


def test_remote_requests_are_never_relayed(_captured_proxy, monkeypatch):
    # One hop only: a request another MindFlock sent us is not forwarded on,
    # either by namespaced title or by the device forward.
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    c = TestClient(server.app)
    h = {remote.REMOTE_HEADER: "thirdbox"}
    assert c.get("/api/instances/otherbox%3A%3At/queue", headers=h).status_code == 400
    assert c.get("/api/devices/otherbox/fwd/api/config", headers=h).status_code == 400
    assert _captured_proxy == []


# --------------------------------------------------------------------------- #
# no echo: each device's sessions come from that device, once
# --------------------------------------------------------------------------- #
def test_remote_caller_gets_only_local_sessions(monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True, instances=[{"title": "theirs"}]
    )
    c = TestClient(server.app)
    browser = [i["title"] for i in c.get("/api/instances").json()]
    peer = [
        i["title"]
        for i in c.get("/api/instances", headers={remote.REMOTE_HEADER: "x"}).json()
    ]
    assert "otherbox::theirs" in browser
    assert not any("::" in t for t in peer)


def test_merged_instances_drops_rows_a_peer_mirrors():
    # An older peer still hands on what IT mirrors (including our own rows);
    # those must not come back double-namespaced.
    remote._DEVICES["otherbox"] = _fake_device(
        instances_ok=True,
        instances=[
            {"title": "theirs"},
            {"title": "me::mine", "device": "me"},
            {"title": "third::x"},
        ],
    )
    assert [i["title"] for i in remote.merged_instances()] == ["otherbox::theirs"]


def test_fwd_create_refreshes_the_device_before_answering(_captured_proxy, monkeypatch):
    fetched = []

    async def fake_fetch(dev):
        fetched.append(dev["key"])

    monkeypatch.setattr(remote, "_fetch_instances", fake_fetch)
    c = TestClient(server.app)
    c.get("/api/devices/otherbox/fwd/api/config")
    assert fetched == []  # only a create refreshes
    c.post("/api/devices/otherbox/fwd/api/instances", json={"title": "x"})
    assert fetched == ["otherbox"]


def test_fwd_session_plan_outlasts_the_targets_own_plan_budget(_captured_proxy):
    # The target answers a slow plan with a FALLBACK at its own TIMEOUT_PLAN;
    # a forwarder that gives up first turns that into a bare 502 on the phone.
    from backend.web.core import session_plan

    c = TestClient(server.app)
    c.post("/api/devices/otherbox/fwd/api/session-plan", json={"text": "x"})
    c.get("/api/devices/otherbox/fwd/api/config")
    plan_timeout, config_timeout = _captured_proxy.timeouts
    assert plan_timeout > session_plan.TIMEOUT_PLAN + session_plan.CANDIDATES_BUDGET
    assert config_timeout == remote._HTTP_TIMEOUT


def test_fwd_timeout_says_what_happened(monkeypatch):
    # asyncio.TimeoutError() has an empty str(), which used to produce
    # "device 'otherbox' unreachable: " with nothing after the colon.
    import asyncio

    class _Boom:
        def request(self, *a, **k):
            raise asyncio.TimeoutError()

    async def _session():
        return _Boom()

    monkeypatch.setattr(remote, "_http_session", _session)
    monkeypatch.setattr(
        remote, "aiohttp", types.SimpleNamespace(ClientTimeout=lambda total: None)
    )
    remote._DEVICES["otherbox"] = _fake_device()
    r = TestClient(server.app).get("/api/devices/otherbox/fwd/api/config")
    assert r.status_code == 502
    assert r.json()["error"].endswith(
        "no answer within %ds" % int(remote._HTTP_TIMEOUT)
    )
