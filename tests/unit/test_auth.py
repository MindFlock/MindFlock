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
