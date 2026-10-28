"""Guest links (:mod:`backend.web.core.guests`): someone else types into a FEW
of this device's sessions through the access-token gate.

Covers the store, the per-request policy (own session yes, other session no,
owner-only routes no), the gate's second token kind (cookie, bearer, the
``?token=`` redirect that never hands out the owner's cookie), the stubbed
``GET /api/instances`` rows, the ``/api/config`` flag, the event filter, and
the owner's ``/api/guests`` routes (gate-off 409, a guest can't reach them).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import auth, guests

TOKEN = "owner-token-abc123"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from backend.web.core import guest_tunnel

    monkeypatch.setattr(guests, "_path", lambda: str(tmp_path / "guest_links.json"))
    monkeypatch.setattr(guests, "_LINKS", None)
    # Never a real cloudflared here: a route that ensures the tunnel must not
    # start a process (or touch the real config dir) under pytest.
    monkeypatch.setattr(guest_tunnel, "_binary", lambda: None)
    for k, v in {
        "_TUNNEL": None,
        "_TASK": None,
        "_HOST": "",
        "_ERROR": "",
        "_REASON": "",
        "_WANTED": False,
    }.items():
        monkeypatch.setattr(guest_tunnel, k, v)
    yield
    monkeypatch.setattr(guests, "_LINKS", None)


@pytest.fixture
def gate(monkeypatch):
    """The access gate on with a known owner token (no settings I/O)."""
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)


@pytest.fixture
def owner(gate):
    c = TestClient(server.app)
    c.headers["Authorization"] = "Bearer " + TOKEN
    return c


@pytest.fixture
def link(gate):
    """One link for sessions a and b; returns (record, token)."""
    rec = guests.create("Mauricio", ["alpha", "beta"])
    return rec, rec["token"]


def _guest_client(token: str) -> TestClient:
    c = TestClient(server.app)
    c.headers["Authorization"] = "Bearer " + token
    return c


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_create_persists_and_reloads(monkeypatch):
    rec = guests.create("  Pat  ", ["one", "one", "", "two"])
    assert rec["label"] == "Pat" and rec["sessions"] == ["one", "two"]
    assert guests.guest_for(rec["token"])["id"] == rec["id"]
    monkeypatch.setattr(guests, "_LINKS", None)  # "restart"
    assert guests.guest_for(rec["token"])["sessions"] == ["one", "two"]
    assert [r["id"] for r in guests.list_links()] == [rec["id"]]
    assert "token" not in guests.list_links()[0]


def test_create_needs_a_session():
    with pytest.raises(ValueError):
        guests.create("x", [])


def test_update_and_revoke():
    rec = guests.create("x", ["one"])
    assert guests.update(rec["id"], sessions=["two"])["sessions"] == ["two"]
    assert guests.guest_for(rec["token"])["sessions"] == ["two"]
    with pytest.raises(ValueError):
        guests.update(rec["id"], sessions=[])
    assert guests.update("nope", label="y") is None
    assert guests.revoke(rec["id"]) is True
    assert guests.revoke(rec["id"]) is False
    assert guests.guest_for(rec["token"]) is None


def test_guest_for_unknown_is_none():
    guests.create("x", ["one"])
    assert guests.guest_for("") is None
    assert guests.guest_for("not-a-token") is None
    assert guests.any_links() is True


# --------------------------------------------------------------------------- #
# the policy
# --------------------------------------------------------------------------- #
G = {"id": "g", "label": "", "sessions": ["alpha"]}


@pytest.mark.parametrize(
    "method,path,ok",
    [
        ("GET", "/", True),
        ("GET", "/m", True),
        ("GET", "/api/events", True),
        ("GET", "/api/instances", True),
        ("POST", "/api/instances", False),
        ("GET", "/api/instances/alpha", True),
        ("GET", "/api/instances/alpha/diff", True),
        ("POST", "/api/instances/alpha/send", True),
        ("POST", "/api/instances/alpha/queue/edit", True),
        ("GET", "/api/instances/alpha/terminal", True),
        ("POST", "/api/instances/alpha/close", False),
        ("POST", "/api/instances/alpha/commit", False),
        ("POST", "/api/instances/alpha/make-pr", False),
        ("DELETE", "/api/instances/alpha", False),
        ("GET", "/api/instances/beta", False),
        ("POST", "/api/instances/beta/send", False),
        ("GET", "/api/config", True),
        ("GET", "/api/settings", False),
        ("GET", "/api/settings/auth-token", False),
        ("POST", "/api/settings", False),
        ("GET", "/api/guests", False),
        ("POST", "/api/guests", False),
        ("POST", "/api/voice/transcribe", True),
        ("POST", "/api/aliases", False),
        ("GET", "/api/fleet", False),
    ],
)
def test_path_allowed(method, path, ok):
    assert guests.path_allowed(method, path, G) is ok


def test_stub_rows_keep_own_sessions_whole_and_cross_out_the_rest():
    rows = [
        {"title": "alpha", "status": "running", "branch": "x", "folder": "/w"},
        {
            "title": "beta",
            "status": "paused",
            "branch": "y",
            "folder": "/v",
            "diff_stat": {},
        },
        {"title": "mac::alpha", "status": "running", "device": "mac", "branch": "z"},
    ]
    out = guests.stub_rows(rows, G)
    assert out[0] == rows[0]
    assert out[1] == {
        "title": "beta",
        "status": "paused",
        "device": "",
        "hidden_by": "",
        "pending": False,
        "locked": True,
    }
    # Another device's same-titled session is not the one on the link.
    assert out[2]["locked"] is True and "branch" not in out[2]


def test_event_allowed():
    assert guests.event_allowed({"session": ""}, G)
    assert guests.event_allowed({"session": "alpha"}, G)
    assert not guests.event_allowed({"session": "beta"}, G)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_guest_bearer_reaches_its_session_and_not_another(link, monkeypatch):
    _, tok = link
    c = _guest_client(tok)
    # The list is served (stubbed) …
    r = c.get("/api/instances")
    assert r.status_code == 200
    # … a session on the link answers for itself (404: no such session here) …
    r = c.get("/api/instances/alpha/diff")
    assert r.status_code != 401 and r.status_code != 403
    # … one off the link is the invite's 403, not the session's answer.
    r = c.get("/api/instances/gamma/diff")
    assert r.status_code == 403 and r.json()["error"] == guests.REFUSAL
    # The owner's surfaces are closed.
    assert c.get("/api/settings").status_code == 403
    assert c.post("/api/instances", json={"title": "x"}).status_code == 403
    assert c.get("/api/guests").status_code == 403


def test_guest_sees_itself_in_config(link):
    _, tok = link
    r = _guest_client(tok).get("/api/config")
    assert r.status_code == 200
    assert r.json()["guest"] == {"label": "Mauricio", "sessions": ["alpha", "beta"]}


def test_owner_config_has_no_guest(owner):
    assert owner.get("/api/config").json()["guest"] is None


def test_query_token_redirect_sets_only_the_guest_cookie(link):
    _, tok = link
    c = TestClient(server.app)
    r = c.get("/?token=" + tok, follow_redirects=False, headers={"accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == "/"
    cookies = r.headers.get_list("set-cookie")
    assert any(auth.cookie_name_for(tok) + "=" in ck for ck in cookies)
    # Never the owner's plain cookie, never the owner's token.
    assert not any(ck.startswith(auth.COOKIE_NAME + "=") for ck in cookies)
    assert not any(TOKEN in ck for ck in cookies)
    # The cookie signs the next request in, as a guest.
    r = c.get("/api/config")
    assert r.status_code == 200 and r.json()["guest"]["label"] == "Mauricio"
    assert c.get("/api/settings").status_code == 403


def test_revoked_link_is_a_401(link):
    rec, tok = link
    c = _guest_client(tok)
    assert c.get("/api/config").status_code == 200
    guests.revoke(rec["id"])
    assert (
        c.get("/api/config", headers={"accept": "application/json"}).status_code == 401
    )


def test_guest_token_is_not_the_owners(link):
    _, tok = link
    assert auth.token_valid(tok) is False
    assert auth.own_token_valid(tok) is False


def test_instances_rows_are_stubbed_for_a_guest(link, monkeypatch):
    _, tok = link
    monkeypatch.setattr(
        server,
        "_guest_rows",
        lambda request, rows: guests.stub_rows(
            [
                {"title": "alpha", "branch": "b", "status": "running"},
                {"title": "zeta", "branch": "z", "status": "idle"},
            ],
            guests.scope_guest(request),
        ),
    )
    rows = _guest_client(tok).get("/api/instances").json()
    by = {r["title"]: r for r in rows}
    assert by["alpha"]["branch"] == "b"
    assert by["zeta"] == {
        "title": "zeta",
        "status": "idle",
        "device": "",
        "hidden_by": "",
        "pending": False,
        "locked": True,
    }


# --------------------------------------------------------------------------- #
# the owner's routes
# --------------------------------------------------------------------------- #
def test_owner_creates_lists_and_revokes(owner, monkeypatch):

    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    r = owner.post("/api/guests", json={"label": "Pat", "sessions": ["alpha"]})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["label"] == "Pat" and row["sessions"] == ["alpha"]
    # No tunnel in tests: the public URLs are null, the on-your-network one
    # carries the token (test_urls_come_from_the_tunnel covers the tunnel).
    assert row["url"] is None and row["phone_url"] is None
    assert row["local_url"].endswith("/?token=" + guests.token_of(row["id"]))
    assert "token" not in row

    r = owner.get("/api/guests")
    assert r.status_code == 200 and r.json()["gate_on"] is True
    assert [l["id"] for l in r.json()["links"]] == [row["id"]]
    assert r.json()["links"][0]["local_url"] == row["local_url"]

    r = owner.post("/api/guests/" + row["id"], json={"sessions": ["alpha", "beta"]})
    assert r.status_code == 200 and r.json()["sessions"] == ["alpha", "beta"]
    assert (
        owner.post("/api/guests/" + row["id"], json={"sessions": []}).status_code == 400
    )
    assert owner.post("/api/guests", json={"sessions": []}).status_code == 400

    assert owner.delete("/api/guests/" + row["id"]).status_code == 200
    assert owner.delete("/api/guests/" + row["id"]).status_code == 404
    assert owner.get("/api/guests").json()["links"] == []


def test_create_refused_while_the_gate_is_off(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)

    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    c = TestClient(server.app)
    r = c.post("/api/guests", json={"sessions": ["alpha"]})
    assert r.status_code == 409 and "gate" in r.json()["error"]
    assert c.get("/api/guests").json()["gate_on"] is False


def test_relayed_device_cannot_manage_links(owner):
    r = owner.get("/api/guests", headers={"X-MindFlock-Remote": "1"})
    assert r.status_code in (403,)


# --------------------------------------------------------------------------- #
# the public address and the kill switch
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_tunnel(monkeypatch):
    """No cloudflared, no process: the tunnel module reports 'off/not installed'."""
    from backend.web.core import guest_tunnel

    monkeypatch.setattr(guest_tunnel, "_binary", lambda: None)
    monkeypatch.setattr(guest_tunnel, "_HOST", "")
    monkeypatch.setattr(guest_tunnel, "_ERROR", "")
    monkeypatch.setattr(guest_tunnel, "_TASK", None)
    monkeypatch.setattr(guest_tunnel, "_WANTED", False)
    yield guest_tunnel
    monkeypatch.setattr(guest_tunnel, "_HOST", "")


def test_urls_come_from_the_tunnel(owner, no_tunnel, monkeypatch):
    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    r = owner.post("/api/guests", json={"sessions": ["alpha"]})
    assert r.status_code == 201, r.text
    row = r.json()
    tok = guests.token_of(row["id"])
    # No tunnel: no public URL, the on-your-network one, and why.
    assert row["url"] is None and row["phone_url"] is None
    assert row["local_url"].endswith("/?token=" + tok)
    assert row["tunnel"]["installed"] is False
    # The tunnel up: the public address carries the token, at / and /m.
    monkeypatch.setattr(no_tunnel, "_HOST", "quiet-fox-1234.trycloudflare.com")
    r = owner.get("/api/guests").json()
    assert (
        r["tunnel"]["state"] == "up"
        and r["tunnel"]["url"] == "https://quiet-fox-1234.trycloudflare.com/"
    )
    assert (
        r["links"][0]["url"] == "https://quiet-fox-1234.trycloudflare.com/?token=" + tok
    )
    assert (
        r["links"][0]["phone_url"]
        == "https://quiet-fox-1234.trycloudflare.com/m?token=" + tok
    )


def test_kill_revokes_everything_and_stops_the_tunnel(owner, no_tunnel, monkeypatch):
    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    stopped = []

    async def _stop():
        stopped.append(True)

    monkeypatch.setattr(no_tunnel, "stop", _stop)
    a = guests.create("a", ["alpha"])
    b = guests.create("b", ["beta"])
    ca, cb = _guest_client(a["token"]), _guest_client(b["token"])
    assert ca.get("/api/config").status_code == 200
    r = owner.delete("/api/guests/tunnel")
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": 2}
    assert stopped == [True]
    assert guests.list_links() == []
    assert (
        ca.get("/api/config", headers={"accept": "application/json"}).status_code == 401
    )
    assert (
        cb.get("/api/config", headers={"accept": "application/json"}).status_code == 401
    )


def test_tunnel_start_route_wants_and_reports(owner, no_tunnel, monkeypatch):
    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    r = owner.post("/api/guests/tunnel", json={})
    assert r.status_code == 200
    assert r.json()["installed"] is False and r.json()["state"] in ("off", "failed")
    assert no_tunnel.wanted() is True


def test_tunnel_refused_while_gate_off(monkeypatch, no_tunnel):
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.delenv("CS_WEB_MODE", raising=False)

    async def _yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", _yes)
    assert TestClient(server.app).post("/api/guests/tunnel", json={}).status_code == 409


def test_revoke_closes_an_open_guest_socket(link):
    """The middleware's socket guard: a revoked link's terminal closes on the
    next frame in either direction, with the unauthorized close code."""
    import asyncio

    from starlette.websockets import WebSocketDisconnect

    rec, tok = link
    guest = guests.guest_for(tok)
    sent = []

    async def receive():
        return {"type": "websocket.receive", "text": "x"}

    async def send(msg):
        sent.append(msg)

    g_recv, g_send = auth._guest_socket_guard(
        "/api/instances/alpha/terminal", guest, receive, send
    )

    async def run():
        await g_send({"type": "websocket.send", "text": "out"})
        assert (await g_recv())["text"] == "x"
        guests.revoke(rec["id"])
        with pytest.raises(WebSocketDisconnect):
            await g_send({"type": "websocket.send", "text": "more"})
        with pytest.raises(WebSocketDisconnect):
            await g_recv()

    asyncio.run(run())
    assert sent[0]["type"] == "websocket.send"
    assert sent[1] == {"type": "websocket.close", "code": 4401}
    assert len(sent) == 2  # closed once, "more" never went out


def test_narrowing_a_link_closes_the_socket_it_lost(link):
    import asyncio

    from starlette.websockets import WebSocketDisconnect

    rec, tok = link
    guest = guests.guest_for(tok)

    async def receive():
        return {"type": "websocket.receive", "text": "x"}

    async def send(msg):
        pass

    g_recv, _ = auth._guest_socket_guard(
        "/api/instances/beta/terminal", guest, receive, send
    )

    async def run():
        await g_recv()
        guests.update(rec["id"], sessions=["alpha"])  # beta dropped
        with pytest.raises(WebSocketDisconnect):
            await g_recv()

    asyncio.run(run())


def test_public_scope_never_carries_the_token(link):
    _, tok = link
    g = guests.guest_for(tok)
    assert g["_token"] == tok
    scope = {"state": {"guest": g}}
    assert guests.public_scope(scope) == {
        "label": "Mauricio",
        "sessions": ["alpha", "beta"],
    }


def test_doctor_wants_cloudflared_for_guest_links(monkeypatch):
    from backend import doctor
    from backend.web.core import guest_tunnel

    monkeypatch.setattr(
        doctor, "_peer_settings", lambda: {"relay": "auto", "enabled": False}
    )
    monkeypatch.setattr(
        "backend.peer.tunnel.find_cloudflared", lambda configured="": None
    )
    monkeypatch.setattr(
        doctor,
        "_cloudflared_install",
        lambda: ("brew install cloudflared", "cloudflared"),
    )
    monkeypatch.setattr(guest_tunnel, "_WANTED", False)
    assert doctor.check_cloudflared().status == "info"
    guests.create("x", ["alpha"])
    c = doctor.check_cloudflared()
    assert c.status == "warn" and c.install is True and "guest link" in c.detail
