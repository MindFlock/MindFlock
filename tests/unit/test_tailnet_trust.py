"""Tailscale-account trust (:mod:`backend.web.core.tailnet_trust`).

The user's own untagged devices skip the access token; everything else —
tagged nodes, other accounts, relayed requests, forged forwarding headers,
other local OS users — still meets the gate. ``tailscale whois`` and the
``/proc/net/tcp`` owner lookup are stubbed; the middleware runs for real.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import settings as settings_store
from backend.web import server
from backend.web.core import tailnet_trust
from backend.web.core.tailnet_trust import Peer

TOKEN = "test-token-abc123"
ME = "me@example.com"
PHONE = "100.102.55.124"
TAGGED_HOST = "100.124.126.101"
FRIEND = "100.90.1.2"

_PEERS = {
    PHONE: Peer(login=ME, tagged=False, node="iphone"),
    TAGGED_HOST: Peer(login="tagged-devices", tagged=True, node="mac-mini"),
    FRIEND: Peer(login="friend@example.com", tagged=False, node="their-laptop"),
}


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    calls = []

    def fake_whois(ip):
        calls.append(ip)
        return _PEERS.get(ip)

    monkeypatch.setattr(tailnet_trust, "_whois_uncached", fake_whois)
    tailnet_trust.clear_cache()
    yield calls
    tailnet_trust.clear_cache()


def _trust(*logins):
    settings_store.update_settings(general={"tailnet_trusted_logins": list(logins)})


def _get(client_addr, path="/api/instances", headers=None):
    c = TestClient(server.app, client=client_addr)
    h = {"accept": "application/json"}
    h.update(headers or {})
    return c.get(path, headers=h)


# --------------------------------------------------------------------------- #
# direct tailnet connections
# --------------------------------------------------------------------------- #
def test_off_by_default_even_for_own_device():
    assert _get((PHONE, 50000)).status_code == 401


def test_trusted_login_untagged_device_skips_token():
    _trust(ME)
    assert _get((PHONE, 50000)).status_code == 200


def test_trusted_device_websocket_skips_token():
    _trust(ME)
    c = TestClient(server.app, client=(PHONE, 50000))
    with c.websocket_connect("/api/events") as ws:
        assert ws.receive_json()["event"] == "hello"


def test_untrusted_device_websocket_still_closed_4401():
    _trust(ME)
    c = TestClient(server.app, client=(FRIEND, 50000))
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/api/events"):
            pass
    assert ei.value.code == 4401


def test_tagged_device_is_never_trusted():
    _trust(ME, "tagged-devices")
    assert _get((TAGGED_HOST, 50000)).status_code == 401


def test_other_account_is_not_trusted():
    _trust(ME)
    assert _get((FRIEND, 50000)).status_code == 401


def test_unknown_address_and_non_tailnet_address_are_not_trusted():
    _trust(ME)
    assert _get(("100.100.100.100", 50000)).status_code == 401  # whois miss
    assert _get(("192.168.1.20", 50000)).status_code == 401  # not a tailnet IP


def test_login_page_still_served_to_untrusted_browser():
    _trust(ME)
    r = _get((FRIEND, 50000), path="/", headers={"accept": "text/html"})
    assert r.status_code == 200 and "sign in" in r.text


def test_direct_peer_with_forwarding_headers_is_refused():
    """A 'direct' tailnet peer carrying X-Forwarded-For may be one uvicorn
    rewrote from a forged header — fail closed."""
    _trust(ME)
    r = _get((PHONE, 50000), headers={"x-forwarded-for": PHONE})
    assert r.status_code == 401


def test_relayed_remote_request_is_not_trusted(monkeypatch):
    """Another MindFlock device relaying for its own callers never inherits
    trust from being the user's device."""
    _trust(ME)
    from backend.web.core import remote as _remote

    monkeypatch.setattr(_remote, "remote_control_enabled", lambda: True)
    r = _get((PHONE, 50000), headers={"x-mindflock-remote": "laptop"})
    assert r.status_code == 401


def test_whois_answers_are_cached(_gate_on):
    _trust(ME)
    for _ in range(3):
        assert _get((PHONE, 50000)).status_code == 200
    assert _gate_on.count(PHONE) == 1


# --------------------------------------------------------------------------- #
# through `tailscale serve` (loopback + X-Forwarded-For)
# --------------------------------------------------------------------------- #
@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(tailnet_trust.sys, "platform", "linux")


def test_serve_proxied_request_trusted_when_socket_is_tailscaleds(monkeypatch, linux):
    _trust(ME)
    monkeypatch.setattr(tailnet_trust, "_proc_socket_uid", lambda c, s: 0)
    r = _get(("127.0.0.1", 41234), headers={"x-forwarded-for": PHONE})
    assert r.status_code == 200


def test_serve_proxied_header_from_another_local_user_is_refused(monkeypatch, linux):
    _trust(ME)
    monkeypatch.setattr(
        tailnet_trust, "_proc_socket_uid", lambda c, s: tailnet_trust.os.getuid() + 1
    )
    r = _get(("127.0.0.1", 41234), headers={"x-forwarded-for": PHONE})
    assert r.status_code == 401


def test_serve_proxied_uses_last_forwarded_hop(monkeypatch, linux):
    """A client-supplied XFF sits in FRONT of the proxy's own hop."""
    _trust(ME)
    monkeypatch.setattr(tailnet_trust, "_proc_socket_uid", lambda c, s: 0)
    r = _get(("127.0.0.1", 41234), headers={"x-forwarded-for": PHONE + ", " + FRIEND})
    assert r.status_code == 401


def test_serve_proxied_never_trusted_off_linux(monkeypatch):
    _trust(ME)
    monkeypatch.setattr(tailnet_trust.sys, "platform", "darwin")
    monkeypatch.setattr(tailnet_trust, "_proc_socket_uid", lambda c, s: 0)
    r = _get(("127.0.0.1", 41234), headers={"x-forwarded-for": PHONE})
    assert r.status_code == 401


def test_peer_capture_keeps_proxy_headers_rewrite():
    """The app-level rewrite still gives handlers the forwarded client, as
    uvicorn's own did before run.py turned it off."""
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope.get("client")
        seen["peer"] = scope.get("mf_peer")
        seen["scheme"] = scope.get("scheme")

    mw = tailnet_trust.PeerCaptureMiddleware(app)
    import asyncio

    scope = {
        "type": "http",
        "scheme": "http",
        "client": ("127.0.0.1", 41234),
        "server": ("127.0.0.1", 8765),
        "headers": [
            (b"x-forwarded-for", PHONE.encode()),
            (b"x-forwarded-proto", b"https"),
        ],
    }
    asyncio.run(mw(scope, None, None))
    assert seen["peer"] == ("127.0.0.1", 41234)
    assert seen["client"][0] == PHONE
    assert seen["scheme"] == "https"


# --------------------------------------------------------------------------- #
# pieces
# --------------------------------------------------------------------------- #
_PROC_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def _proc_row(local, remote, st, uid, inode):
    return "   0: %s %s %s 00000000:00000000 00:00000000 00000000 %5d        0 %s\n" % (
        local,
        remote,
        st,
        uid,
        inode,
    )


@pytest.fixture
def proc_tables(tmp_path, monkeypatch):
    """Point /proc/net/tcp{,6} at files the test writes; a table never
    written is unreadable (OSError), like a kernel built without IPv6."""
    files = {}
    real_open = open

    def fake_open(path, *a, **k):
        if path in files:
            return real_open(files[path], *a, **k)
        raise OSError("absent")

    monkeypatch.setattr("builtins.open", fake_open)

    def write(table, *rows):
        p = tmp_path / table.rsplit("/", 1)[-1]
        p.write_text(_PROC_HEADER + "".join(rows))
        files[table] = p

    return write


@pytest.fixture
def proc_tcp(proc_tables):
    """Point /proc/net/tcp at a file the test writes (tcp6 absent)."""
    return lambda *rows: proc_tables("/proc/net/tcp", *rows)


def test_proc_socket_uid_reads_the_client_side_row(proc_tcp):
    proc_tcp(
        # the server's accepted side (local :8765) — must not be picked
        _proc_row("0100007F:223D", "0100007F:A1B2", "01", 1000, "11"),
        # tailscaled's connecting side (local :41394 -> :8765)
        _proc_row("0100007F:A1B2", "0100007F:223D", "01", 0, "12"),
    )
    assert tailnet_trust._proc_socket_uid(0xA1B2, 0x223D) == 0
    assert tailnet_trust._proc_socket_uid(0x1111, 0x223D) is None


def test_proc_socket_uid_ignores_closed_minisockets(proc_tcp):
    """A forger that closes at once leaves a FIN_WAIT2/TIME_WAIT row the
    kernel prints as uid 0 / inode 0 — that must not read as tailscaled."""
    proc_tcp(
        _proc_row("0100007F:A1B2", "0100007F:223D", "05", 0, "0"),
        _proc_row("0100007F:A1B3", "0100007F:223D", "06", 0, "0"),
    )
    assert tailnet_trust._proc_socket_uid(0xA1B2, 0x223D) is None
    assert tailnet_trust._proc_socket_uid(0xA1B3, 0x223D) is None


def test_proc_socket_uid_ignores_non_loopback_rows(proc_tcp):
    """Same ports, but a connection out to another host's :8765."""
    proc_tcp(_proc_row("3624B464:A1B2", "657C7C64:223D", "01", 0, "13"))
    assert tailnet_trust._proc_socket_uid(0xA1B2, 0x223D) is None


@pytest.mark.parametrize(
    "host,ok",
    [
        ("100.64.0.1", True),
        ("100.127.255.254", True),
        ("100.128.0.1", False),
        ("fd7a:115c:a1e0::1", True),
        ("::ffff:100.102.55.124", True),
        ("::ffff:100.64.0.1", True),
        ("100.63.255.255", False),  # just below 100.64.0.0/10
        ("100.128.0.0", False),  # just above it
        ("fd7a:115c:a1e1::1", False),  # outside Tailscale's /48
        ("10.0.0.1", False),
        ("testclient", False),
        ("100.64.0.1.5", False),
        ("", False),
        (None, False),
    ],
)
def test_is_tailnet_ip(host, ok):
    assert tailnet_trust.is_tailnet_ip(host) is ok


def test_parse_whois_reads_login_and_tags():
    p = tailnet_trust._parse_whois(
        {
            "Node": {"Name": "iphone.tail.ts.net.", "Tags": None},
            "UserProfile": {"LoginName": "Me@Example.com"},
        }
    )
    assert p == Peer(login=ME, tagged=False, node="iphone.tail.ts.net")
    tagged = tailnet_trust._parse_whois(
        {
            "Node": {"Tags": ["tag:mindflock"]},
            "UserProfile": {"LoginName": "tagged-devices"},
        }
    )
    assert tagged.tagged is True
    assert tailnet_trust._parse_whois({"Node": {}}) is None


def test_setting_normalizes_logins():
    _trust(" Me@Example.com ", "me@example.com", "", "Other@x.io")
    got = settings_store.load_settings().general.tailnet_trusted_logins
    assert got == [ME, "other@x.io"]
    _trust()
    assert settings_store.load_settings().general.tailnet_trusted_logins == []
    assert (
        "tailnet_trusted_logins" not in settings_store.load_settings().general.to_dict()
    )


def test_status_lists_owners_of_untagged_devices(monkeypatch):
    status_json = {
        "Self": {"UserID": 3, "Tags": ["tag:mindflock"]},
        "Peer": {
            "a": {"UserID": 1, "Tags": None},
            "b": {"UserID": 2, "Tags": ["tag:mindflock"]},
            "c": {"UserID": 4},
        },
        "User": {
            "1": {"LoginName": "Me@Example.com"},
            "2": {"LoginName": "tagged-devices"},
            "3": {"LoginName": "laptop.tail.ts.net"},
            "4": {"LoginName": "friend@example.com"},
        },
    }

    class CP:
        returncode = 0
        stdout = __import__("json").dumps(status_json).encode()

    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet_trust.subprocess, "run", lambda *a, **k: CP())
    st = tailnet_trust.status()
    assert st["available"] is True
    assert st["self_tagged"] is True and st["self_login"] == ""
    assert st["logins"] == ["friend@example.com", ME]


def test_status_route_is_behind_the_gate():
    c = TestClient(server.app)
    assert (
        c.get(
            "/api/settings/tailnet-trust", headers={"accept": "application/json"}
        ).status_code
        == 401
    )
    r = c.get(
        "/api/settings/tailnet-trust", headers={"Authorization": "Bearer " + TOKEN}
    )
    assert r.status_code == 200 and "logins" in r.json()
