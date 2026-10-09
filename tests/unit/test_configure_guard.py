"""Who may change what runs on the owner's devices (``auth.may_configure``).

The laundering path this closes: on a gate-off device reachable on the tailnet
(``CS_WEB_MODE=tailscale`` with ``MINDFLOCK_AUTH=0``), any tailnet node got
past the middleware and could ``POST /api/settings`` a synced field such as
``coding_cli.default_launch_args``. Settings sync stamped it as this device's
edit and spread it to every device holding the fleet key — gated ones too.

Pinned here: an anonymous tailnet caller is refused (403, nothing saved) on
every route that writes such config; this machine (loopback), a caller
presenting this device's token or the fleet key, and a trusted Tailscale
account are not; another MindFlock relaying is refused even from loopback
with the token; a loopback request carrying a forwarding header
(``tailscale serve`` fronting a local-mode server) counts as remote; and the
gate-off localhost run — what the rest of the suite is — is unchanged.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import providers
from backend.config import settings as S
from backend.web.core import auth, fleet, tailnet_trust

TOKEN = "own-token-abc123"
FLEET_KEY = "fleet-key-XYZ_0123456789"
TAILNET = ("100.64.0.5", 41000)
LOOPBACK = ("127.0.0.1", 41000)


@pytest.fixture()
def untrusted(monkeypatch):
    """No Tailscale trust (and no real ``tailscale whois``)."""

    async def _no(scope):
        return False

    monkeypatch.setattr(tailnet_trust, "request_trusted", _no)


@pytest.fixture()
def app(tmp_path, monkeypatch, untrusted):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    S.invalidate()
    providers.rebuild_registry()
    from backend.web.server import app as _app

    with TestClient(_app):
        # Exposed + gate off, the way the owner's laptop runs: set AFTER the
        # lifespan so boot doesn't act on a tailscale mode.
        monkeypatch.setenv("CS_WEB_MODE", "tailscale")
        monkeypatch.setenv("MINDFLOCK_AUTH", "0")
        monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
        yield _app
    S.invalidate()
    providers.rebuild_registry()


def _client(app, peer, **kw) -> TestClient:
    # Not entered: the lifespan already ran once in the fixture.
    return TestClient(app, client=peer, **kw)


def _launch_args() -> str:
    S.invalidate()
    return S.load_settings().coding_cli.launch_args_for("claude")


LAUNCH = {"coding_cli": {"default_launch_args": {"claude": "--evil"}}}


# --------------------------------------------------------------------------- #
# POST /api/settings
# --------------------------------------------------------------------------- #
def test_anonymous_tailnet_caller_cannot_write_launch_args(app):
    r = _client(app, TAILNET).post("/api/settings", json=LAUNCH)
    assert r.status_code == 403
    assert "sign-in" in r.json()["error"]
    assert _launch_args() != "--evil"


def test_loopback_may_write_launch_args(app):
    r = _client(app, LOOPBACK).post("/api/settings", json=LAUNCH)
    assert r.status_code == 200, r.text
    assert _launch_args() == "--evil"


@pytest.mark.parametrize("cred", [TOKEN, FLEET_KEY])
def test_a_credential_holder_on_the_tailnet_may_write(app, monkeypatch, cred):
    """This device's token, or the fleet key a member (or a phone signed in
    with it) presents."""
    monkeypatch.setattr(fleet, "key_valid", lambda c: c == FLEET_KEY)
    c = _client(app, TAILNET, headers={"Authorization": "Bearer " + cred})
    assert c.post("/api/settings", json=LAUNCH).status_code == 200
    assert _launch_args() == "--evil"


def test_a_trusted_tailscale_account_may_write(app, monkeypatch):
    async def _yes(scope):
        return True

    monkeypatch.setattr(tailnet_trust, "request_trusted", _yes)
    assert _client(app, TAILNET).post("/api/settings", json=LAUNCH).status_code == 200


def test_relayed_is_refused_even_from_loopback_with_the_token(app, monkeypatch):
    # Remote control on, so the middleware lets the relayed request through
    # and the route itself has to refuse it.
    S.update_settings(general={"remote_control": True})
    c = _client(
        app,
        LOOPBACK,
        headers={"Authorization": "Bearer " + TOKEN, "X-MindFlock-Remote": "otherbox"},
    )
    assert c.post("/api/settings", json=LAUNCH).status_code == 403
    assert _launch_args() != "--evil"


def test_serve_fronted_loopback_counts_as_remote(app, monkeypatch):
    """Local mode fronted by ``tailscale serve``: the tailnet caller arrives
    from 127.0.0.1 with X-Forwarded-For."""
    monkeypatch.setenv("CS_WEB_MODE", "local")
    c = _client(app, LOOPBACK, headers={"X-Forwarded-For": "100.64.0.9"})
    assert c.post("/api/settings", json=LAUNCH).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"general": {"auth_mode": "off"}},
        {"general": {"remote_control": True}},
        {"general": {"serve_mode": "tailscale"}},
        {"coding_cli": {"binary_paths": {"claude": "/tmp/x"}}},
        {"platform": {"ide_command": "sh -c evil"}},
        {"peer": {"enabled": True}},
        {"github": {"token": "ghp_x"}},
        {"general": {"agent_mcp": False}},
        {"extensions": {"disabled": ["x"]}},
    ],
)
def test_security_and_synced_fields_are_guarded(app, payload):
    assert _client(app, TAILNET).post("/api/settings", json=payload).status_code == 403


def test_open_fields_stay_writable_by_anyone(app):
    r = _client(app, TAILNET).post("/api/settings", json={"ui": {"surface": "calm"}})
    assert r.status_code == 200, r.text


def test_one_guarded_field_refuses_the_whole_save(app):
    r = _client(app, TAILNET).post(
        "/api/settings", json={"ui": {"surface": "calm", "accent": "#fff"}}
    )
    assert r.status_code == 403
    S.invalidate()
    assert S.load_settings().ui.surface == ""


def test_gate_off_localhost_run_is_unchanged(app, monkeypatch):
    """CS_WEB_MODE unset (a bare run, the test suite): TestClient's own
    non-IP peer still saves — nothing beyond this machine can be the caller."""
    monkeypatch.delenv("CS_WEB_MODE")
    assert TestClient(app).post("/api/settings", json=LAUNCH).status_code == 200


# --------------------------------------------------------------------------- #
# The other routes that write what runs on the owner's devices
# --------------------------------------------------------------------------- #
_PROVIDER = {"name": "evil", "binary": "/bin/sh", "display_name": "Evil"}

GUARDED = [
    ("put", "/api/settings/ticketing/sources", {"sources": []}),
    ("put", "/api/settings/auth-profiles", {"profiles": []}),
    ("post", "/api/providers", _PROVIDER),
    ("put", "/api/providers/evil", _PROVIDER),
    ("delete", "/api/providers/evil", None),
    ("post", "/api/templates", {"name": "t", "program": "sh -c evil"}),
    ("delete", "/api/templates/t", None),
    ("post", "/api/prefs", {"prompt_presets": [{"name": "x", "prompt": "rm"}]}),
    ("post", "/api/prefs", {"keymap": {"keys": {}}}),
    ("post", "/api/notify/ntfy", {"server": "https://evil.example"}),
    ("post", "/api/notify/rules/device_join", {"enabled": False}),
    ("post", "/api/red-zones", {"repo_id": "r", "pattern": "x"}),
    ("delete", "/api/red-zones/abc", None),
    ("put", "/api/red-zones/companions", {"repo_id": "r", "patterns": ["**"]}),
    ("post", "/api/red-zones/plan-first", {"repo_id": "r", "on": False}),
    ("post", "/api/cursor/autoadopt", {"enabled": True}),
    ("post", "/api/settings/sync", {"enabled": False}),
    ("post", "/api/settings/sync/now", None),
    ("post", "/api/settings/sync/resume", {"keep": "mine"}),
    ("post", "/api/settings/sync/pin", {"path": "ui.accent", "pinned": True}),
]


def _send(c: TestClient, method: str, path: str, body):
    if body is None:
        return c.request(method.upper(), path)
    return c.request(method.upper(), path, json=body)


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_anonymous_tailnet_caller_is_refused(app, method, path, body):
    r = _send(_client(app, TAILNET), method, path, body)
    assert r.status_code == 403, (path, r.status_code, r.text)


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_a_signed_in_tailnet_caller_gets_past_the_guard(
    app, tmp_path, monkeypatch, method, path, body
):
    # The route runs for real now: keep its stores (templates, red zones…)
    # out of the real ~/.mindflock.
    monkeypatch.setenv("HOME", str(tmp_path))
    c = _client(app, TAILNET, headers={"Authorization": "Bearer " + TOKEN})
    r = _send(c, method, path, body)
    assert r.status_code != 403, (path, r.text)


def test_cosmetic_prefs_stay_writable_by_anyone(app):
    r = _client(app, TAILNET).post("/api/prefs", json={"theme": "dark"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# may_configure itself
# --------------------------------------------------------------------------- #
def _scope(peer, headers=()):
    return {"type": "http", "headers": list(headers), "mf_peer": peer}


def test_may_configure_scopes(monkeypatch, untrusted):
    run = asyncio.run
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert run(auth.may_configure(_scope(LOOPBACK))) is True
    assert run(auth.may_configure(_scope(TAILNET))) is False
    # Unexposed (unset / local mode): a direct non-loopback peer can only be
    # an in-process client — but a forwarding header or a relay never passes.
    monkeypatch.setenv("CS_WEB_MODE", "local")
    assert run(auth.may_configure(_scope(("testclient", 1)))) is True
    assert (
        run(auth.may_configure(_scope(LOOPBACK, [(b"x-forwarded-for", b"100.64.0.9")])))
        is False
    )
    assert (
        run(auth.may_configure(_scope(LOOPBACK, [(b"x-mindflock-remote", b"box")])))
        is False
    )
    assert run(auth.may_configure({})) is False


def test_no_guarded_write_is_forwardable():
    """The remote-control forward allow-list (``/api/devices/<d>/fwd/…``)
    must never carry a guarded write: the forward attaches THIS device's
    credential, which would make it privileged on the target."""
    from backend.web.core import remote

    for method, path, _ in GUARDED:
        assert (method.upper(), path) not in remote._FWD_ALLOWED, path
    assert ("POST", "/api/settings") not in remote._FWD_ALLOWED
