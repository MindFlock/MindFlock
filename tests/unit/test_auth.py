"""Bearer-token auth gate (:mod:`backend.web.core.auth`).

Covers the enable logic (must default OFF for localhost / bare uvicorn / the
test suite), the token resolution, and the middleware's HTTP + websocket paths.
Auth is toggled per-request off env, so tests flip ``MINDFLOCK_AUTH_TOKEN`` /
``MINDFLOCK_AUTH`` / ``CS_WEB_MODE`` with monkeypatch (auto-reverted) and use a
fresh TestClient so cookies don't leak between cases.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.web import server
from backend.web.core import auth

TOKEN = "test-token-abc123"


@pytest.fixture
def authed(monkeypatch):
    """Enable auth with a known env token (no settings I/O)."""
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# enable logic
# --------------------------------------------------------------------------- #
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    assert auth.auth_enabled() is False


def test_local_mode_stays_off(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CS_WEB_MODE", "local")
    assert auth.auth_enabled() is False


def test_tailscale_mode_enables(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert auth.auth_enabled() is True


def test_env_flag_forces_off_even_in_tailscale(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    assert auth.auth_enabled() is False


def test_env_token_opts_in(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    assert auth.auth_enabled() is True and auth.get_token() == TOKEN


def test_token_valid_is_constant_time_compare(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    assert auth.token_valid(TOKEN) is True
    assert auth.token_valid("nope") is False
    assert auth.token_valid("") is False


# --------------------------------------------------------------------------- #
# HTTP gate
# --------------------------------------------------------------------------- #
def test_disabled_lets_everything_through(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    c = TestClient(server.app)
    assert c.get("/api/instances").status_code == 200


def test_api_without_token_401(authed):
    r = authed.get("/api/instances", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_navigation_without_token_gets_login_page(authed):
    r = authed.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "Access token" in r.text and "/api/auth" in r.text


def test_bearer_header_passes(authed):
    r = authed.get("/api/instances", headers={"Authorization": "Bearer " + TOKEN})
    assert r.status_code == 200


def test_query_token_redirects_and_sets_cookie(authed):
    r = authed.get(
        "/?token=" + TOKEN, headers={"accept": "text/html"}, follow_redirects=False
    )
    assert r.status_code == 302
    assert auth.COOKIE_NAME in r.headers.get("set-cookie", "")


def test_login_endpoint_sets_cookie_then_requests_pass(authed):
    bad = authed.post("/api/auth", json={"token": "wrong"})
    assert bad.status_code == 401
    ok = authed.post("/api/auth", json={"token": TOKEN})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    # The client now holds the cookie — a subsequent API call is allowed.
    assert authed.get("/api/instances").status_code == 200


def test_auth_endpoint_never_echoes_token(authed):
    body = authed.post("/api/auth", json={"token": TOKEN}).json()
    assert TOKEN not in str(body)


# --------------------------------------------------------------------------- #
# websocket gate
# --------------------------------------------------------------------------- #
def test_ws_rejected_without_token(authed):
    with pytest.raises(WebSocketDisconnect) as ei:
        with authed.websocket_connect("/api/events"):
            pass
    assert ei.value.code == 4401


def test_ws_allowed_with_query_token(authed):
    with authed.websocket_connect("/api/events?token=" + TOKEN) as ws:
        hello = ws.receive_json()
        assert hello["event"] == "hello"


# --------------------------------------------------------------------------- #
# _hostname parser (underpins origin_ok / host_ok — the DNS-rebind + cross-
# origin refusals). The bracketed / bare IPv6 branches are the tricky bits the
# integration tests above don't isolate.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, expected",
    [
        # bracketed IPv6 literal with a port -> the address inside the brackets
        ("[::1]:8765", "::1"),
        # bracketed IPv6 literal, no port
        ("[::1]", "::1"),
        # bare IPv6 literal: >1 colon, so the host:port split must NOT fire
        ("::1", "::1"),
        # ordinary host:port
        ("localhost:8765", "localhost"),
        # a full URL origin is lowercased and reduced to its host
        ("http://Host:9/x", "host"),
        # the literal null origin (sandboxed iframe / opaque redirect)
        ("null", ""),
        # empty input
        ("", ""),
        # a foreign host passes through verbatim (the caller compares it)
        ("evil.example", "evil.example"),
    ],
)
def test_hostname_parses(value, expected):
    assert auth._hostname(value) == expected


# --------------------------------------------------------------------------- #
# browser-attack guards: Origin (cross-site WS hijack / CSRF) + Host
# (DNS rebinding) — enforced even with the token gate OFF
# --------------------------------------------------------------------------- #
@pytest.fixture
def open_client(monkeypatch):
    """Token gate off (the default localhost posture)."""
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    return TestClient(server.app)


def test_cross_origin_http_refused_even_with_gate_off(open_client):
    r = open_client.post(
        "/api/auth", json={"token": "x"}, headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403


def test_cross_origin_ws_refused_even_with_gate_off(open_client):
    with pytest.raises(WebSocketDisconnect) as ei:
        with open_client.websocket_connect(
            "/api/events", headers={"origin": "http://evil.example"}
        ):
            pass
    assert ei.value.code == 4403


def test_null_origin_refused(open_client):
    r = open_client.get("/api/instances", headers={"origin": "null"})
    assert r.status_code == 403


def test_same_origin_and_loopback_origins_pass(open_client):
    # Same host the request is addressed to (what the UI's own fetches send).
    assert (
        open_client.get(
            "/api/instances", headers={"origin": "http://testserver"}
        ).status_code
        == 200
    )
    # Loopback origins are always this machine, whatever the Host says.
    assert (
        open_client.get(
            "/api/instances", headers={"origin": "http://localhost:8765"}
        ).status_code
        == 200
    )


def test_cross_origin_ws_refused_with_gate_on_and_valid_token(authed):
    """Defense in depth: a leaked token in a malicious page's URL still can't
    open a socket from a foreign origin."""
    with pytest.raises(WebSocketDisconnect) as ei:
        with authed.websocket_connect(
            "/api/events?token=" + TOKEN, headers={"origin": "http://evil.example"}
        ):
            pass
    assert ei.value.code == 4403


def test_local_mode_refuses_foreign_host_header(monkeypatch):
    """DNS rebinding: local mode (127.0.0.1 bind) only answers loopback Hosts."""
    monkeypatch.setenv("CS_WEB_MODE", "local")
    c = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/instances").status_code == 200
    assert c.get("/api/instances", headers={"host": "evil.example"}).status_code == 403


def test_unset_mode_does_not_enforce_host(open_client):
    """Bare uvicorn / the test suite (CS_WEB_MODE unset) keep working."""
    assert open_client.get("/api/instances").status_code == 200


# A node as `tailscale status --json` describes it (host_ok's own names).
NODE_IP = "100.101.1.2"
NODE_STATUS = {
    "BackendState": "Running",
    "Self": {
        "HostName": "Box",
        "DNSName": "box.tail0000.ts.net.",
        "TailscaleIPs": [NODE_IP, "fd7a:115c:a1e0::1"],
    },
    "CertDomains": ["box.tail0000.ts.net"],
}


@pytest.fixture
def tailscale_node(monkeypatch):
    """Tailscale mode, gate off, on a node read from NODE_STATUS."""
    from backend import tailscale_cli
    from backend.web.core import tailnet_bind

    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv(tailnet_bind.BIND_ALL_ENV, raising=False)
    monkeypatch.setattr(tailscale_cli, "status_json", lambda **kw: dict(NODE_STATUS))
    # Addresses on this machine: its tailnet ones and one LAN one.
    mine = (NODE_IP, "fd7a:115c:a1e0::1", "192.168.1.20")
    monkeypatch.setattr(tailnet_bind, "bindable", lambda ip: ip in mine)
    monkeypatch.setattr(auth, "_LOCAL_IPS", {})
    monkeypatch.setattr(auth, "_FRONTED_HOSTS", set())
    monkeypatch.setattr(
        auth,
        "_NODE",
        {
            "hosts": frozenset(),
            "lan": frozenset(),
            "unbindable": False,
            "at": 0.0,
            "pending": False,
        },
    )
    auth.read_node_hosts()


def _host(value: str) -> dict:
    return {"type": "http", "headers": [(b"host", value.encode())]}


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8765",
        "127.0.0.1:8765",
        "[::1]:8765",
        NODE_IP + ":8765",  # another member / the CLI by IP
        "[fd7a:115c:a1e0::1]:8765",
        "box.tail0000.ts.net:8765",  # the phone over MagicDNS
        "box.tail0000.ts.net",  # tailscale serve (https, no port)
        "box:8765",  # MagicDNS short name
    ],
)
def test_tailscale_mode_answers_its_own_names(tailscale_node, host):
    assert auth.host_ok(_host(host)) is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",  # DNS rebinding onto the tailnet IP or 127.0.0.1
        "evil.example:8765",
        "other.tail0000.ts.net:8765",  # another node's name
        "100.64.0.77:8765",  # another node's address
        "192.168.1.20:8765",  # a LAN address: not while bound to the tailnet only
        "",
    ],
)
def test_tailscale_mode_refuses_other_hosts(tailscale_node, host):
    assert auth.host_ok(_host(host)) is False


def test_tailscale_mode_answers_the_shared_link(tailscale_node):
    # The shared phone link: Host is the service name, fronted by serve.
    assert auth.host_ok(_host("mindflock.tail0000.ts.net")) is False
    auth.allow_fronted_host("mindflock.tail0000.ts.net")
    assert auth.host_ok(_host("mindflock.tail0000.ts.net")) is True


def test_bound_to_every_interface_answers_lan_addresses(tailscale_node, monkeypatch):
    from backend.web.core import tailnet_bind

    for env in (tailnet_bind.BIND_ALL_ENV, tailnet_bind.FALLBACK_ENV):
        monkeypatch.setenv(env, "1")
        assert auth.host_ok(_host("192.168.1.20:8765")) is True
        # Only this machine's own addresses, and never a rebinding name.
        assert auth.host_ok(_host("192.168.1.99:8765")) is False
        assert auth.host_ok(_host("evil.example")) is False
        monkeypatch.delenv(env)
    assert auth.host_ok(_host("192.168.1.20:8765")) is False


def test_unbindable_tailnet_addresses_count_as_every_interface(
    monkeypatch, tailscale_node
):
    # WSL reading the Windows side's tailscale.exe: its IPs can't be bound,
    # so tailscale mode fell back to 0.0.0.0.
    from backend.web.core import tailnet_bind

    monkeypatch.setattr(tailnet_bind, "bindable", lambda ip: ip == "192.168.1.20")
    auth.read_node_hosts()
    assert auth.host_ok(_host("192.168.1.20:8765")) is True


def test_a_failed_status_read_keeps_the_last_names(tailscale_node, monkeypatch):
    from backend import tailscale_cli

    monkeypatch.setattr(tailscale_cli, "status_json", lambda **kw: None)
    auth.read_node_hosts()
    assert auth.host_ok(_host("box.tail0000.ts.net:8765")) is True


def test_tailscale_mode_middleware_refuses_a_rebound_host(tailscale_node):
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.get("/api/instances", headers={"host": "evil.example"}).status_code == 403
    assert (
        c.get("/api/instances", headers={"host": "localhost:8765"}).status_code == 200
    )
    phone = TestClient(server.app, client=(NODE_IP, 50000))
    r = phone.get("/api/instances", headers={"host": "box.tail0000.ts.net:8765"})
    assert r.status_code == 200


def test_first_exposed_request_reads_the_node_names(monkeypatch, tailscale_node):
    import asyncio

    calls = []
    monkeypatch.setattr(auth, "read_node_hosts", lambda: calls.append(1))
    monkeypatch.setitem(auth._NODE, "at", 0.0)
    asyncio.run(auth._ensure_node_hosts())
    assert calls == [1]
    # Fresh: no re-read. Unexposed: never.
    monkeypatch.setitem(auth._NODE, "at", float("inf"))
    asyncio.run(auth._ensure_node_hosts())
    monkeypatch.setenv("CS_WEB_MODE", "local")
    monkeypatch.setitem(auth._NODE, "at", 0.0)
    asyncio.run(auth._ensure_node_hosts())
    assert calls == [1]


# --------------------------------------------------------------------------- #
# token rotation (compromise recovery)
# --------------------------------------------------------------------------- #
def test_rotate_token_invalidates_old_and_persists_new(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    old = auth.get_token()  # generates + persists on first use
    new = auth.rotate_token()
    assert new != old
    assert auth.token_valid(new) is True
    assert auth.token_valid(old) is False
    assert auth.get_token() == new  # persisted — survives re-resolution


def test_rotate_token_refuses_env_pinned_token(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    with pytest.raises(RuntimeError):
        auth.rotate_token()


def test_rotate_endpoint_reissues_callers_cookie(monkeypatch):
    """POST /api/settings/auth-token/rotate: old cookie dies, but the response
    carries the new one so the rotating client stays signed in."""
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")  # gate on, settings-stored token
    c = TestClient(server.app)
    first = auth.get_token()
    assert c.post("/api/auth", json={"token": first}).status_code == 200
    r = c.post("/api/settings/auth-token/rotate")
    assert r.status_code == 200
    new = r.json()["token"]
    assert new != first
    assert auth.COOKIE_NAME in r.headers.get("set-cookie", "")
    # The TestClient picked up the new cookie — still signed in.
    assert c.get("/api/instances").status_code == 200
    # A client still holding the OLD cookie is signed out.
    stale = TestClient(server.app)
    stale.cookies.set(auth.COOKIE_NAME, first)
    assert (
        stale.get("/api/instances", headers={"accept": "application/json"}).status_code
        == 401
    )


def test_rotate_endpoint_409_when_env_pinned(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    c = TestClient(server.app)
    r = c.post(
        "/api/settings/auth-token/rotate",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# the update routes
# --------------------------------------------------------------------------- #
# These are the highest-privilege routes on the surface: `POST /api/update/start`
# replaces the tool venv the server is executing out of, and `GET
# /api/update/state` re-execs the process. They are ordinary `@app` routes, so
# the middleware covers them the same way it covers everything else — which is
# exactly the claim worth pinning, because "it's just another route" is also how
# a route ends up on an allow-list by accident.
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/update/check"),
        ("post", "/api/update/start"),
        ("get", "/api/update/state"),
    ],
)
def test_update_routes_are_behind_the_gate(authed, monkeypatch, method, path):
    def _never(*a, **kw):
        raise AssertionError("the route body ran for an unauthenticated caller")

    monkeypatch.setattr(server._self_update, "installed_version", _never)
    monkeypatch.setattr(server._self_update, "start_update", _never)
    monkeypatch.setattr(server._self_update, "finish_state", _never)

    r = getattr(authed, method)(path, headers={"accept": "application/json"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/update/check"), ("get", "/api/update/state")],
)
def test_update_routes_pass_with_a_bearer_token(authed, monkeypatch, method, path):
    async def _latest(force=False):
        return None

    monkeypatch.setattr(server._self_update, "latest_release", _latest)
    monkeypatch.setattr(
        server._self_update, "finish_state", lambda: ({"state": "idle"}, False)
    )
    r = getattr(authed, method)(path, headers={"Authorization": "Bearer " + TOKEN})
    assert r.status_code == 200
