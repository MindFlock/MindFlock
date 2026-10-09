"""/api/peer routes: refused for tailnet remote control, never leak a secret
(the invite code appears ONLY in the POST /api/peer/invites response)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.peer import service as svc_mod

CODE = "mfp1:" + "s" * 60 + "-abcd"
SECRET = b"\x01" * 20


class FakeInvite:
    def __init__(self, iid):
        self.invite_id = iid
        self.code = CODE
        self.secret = SECRET
        self.expires_at = time.monotonic() + 600


class FakeInvites:
    def __init__(self):
        self.book = {}

    def create(self, host, port, ttl_s=600):
        inv = FakeInvite("%016x" % (len(self.book) + 1))
        inv.host, inv.port = host, port
        self.book[inv.invite_id] = inv
        return inv

    def revoke(self, iid):
        self.book.pop(iid, None)

    def active(self):
        # Deliberately hands back the whole object (code + secret included):
        # the service must whitelist what it shows.
        return list(self.book.values())


class FakeStore:
    def __init__(self, links=()):
        self.links = {l.link_id: l for l in links}

    def list(self):
        return list(self.links.values())

    def get(self, lid):
        return self.links.get(lid)

    def update(self, lid, **fields):
        for k, v in fields.items():
            setattr(self.links[lid], k, v)

    def remove(self, lid):
        self.links.pop(lid, None)

    def add(self, link):
        self.links[link.link_id] = link


class FakeTransport:
    def __init__(self, *a):
        self.listening = False
        self.calls = []

    def start(self):
        self.calls.append("start")

    def start_listener(self, host, port):
        self.listening = True

    def stop_listener(self):
        self.listening = False

    def is_connected(self, lid):
        return True

    async def pair(self, code):
        self.calls.append(("pair", code))
        return link("cd" * 16, role="dialer")

    def unlink(self, lid):
        self.calls.append(("unlink", lid))

    def close(self):
        pass


def link(lid="ab" * 16, role="listener", **kw):
    base = dict(
        link_id=lid,
        peer_name="Bob",
        peer_pub="ee" * 32,  # a KEY: must never be shown
        role=role,
        peer_addr="100.64.0.2:8799",
        created=1.0,
        last_seen=2.0,
        sas="482-019-337-5",
        perms={"messages": True, "diff": True, "read_file": True},
        share_id=None,
        session_title=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def svc(monkeypatch):
    s = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore([link()]),
        invites_factory=FakeInvites,
        transport_factory=FakeTransport,
        settings_getter=lambda: {
            "enabled": True,
            "listen_host": "0.0.0.0",
            "listen_port": 8799,
            "display_name": "alice",
            "advertise_host": "100.64.0.1",
            "egress_allow": [],
            # Direct invites: "auto" would pick up a cloudflared that happens
            # to be installed on the machine running the tests.
            "relay": "off",
        },
    )
    monkeypatch.setattr(svc_mod, "_SERVICE", s)
    return s


@pytest.fixture
def client():
    from backend.web import server

    return TestClient(server.app)


REMOTE = {"X-MindFlock-Remote": "other-device-key"}

ROUTES = [
    ("get", "/api/peer", None),
    ("post", "/api/peer/invites", {}),
    ("delete", "/api/peer/invites/0000000000000001", None),
    ("post", "/api/peer/join", {"code": CODE}),
    ("delete", "/api/peer/links/" + "ab" * 16, None),
    ("post", "/api/peer/links/" + "ab" * 16 + "/perms", {"diff": False}),
    (
        "post",
        "/api/peer/links/" + "ab" * 16 + "/address",
        {"address": "wss://x.trycloudflare.com/abc"},
    ),
    ("post", "/api/peer/links/" + "ab" * 16 + "/share", {"repo_path": "/tmp"}),
    ("delete", "/api/peer/links/" + "ab" * 16 + "/share", None),
    (
        "post",
        "/api/peer/links/" + "ab" * 16 + "/export",
        {"target_repo": "/tmp", "branch_name": "peer/x"},
    ),
    ("post", "/api/peer/enable", {}),
    ("post", "/api/peer/links/" + "ab" * 16 + "/verified", {"verified": True}),
    ("get", "/api/peer/links/" + "ab" * 16 + "/messages", None),
    ("post", "/api/peer/links/" + "ab" * 16 + "/messages/read", {}),
    ("post", "/api/peer/links/" + "ab" * 16 + "/message", {"text": "hi"}),
    ("get", "/api/peer/links/" + "ab" * 16 + "/diff", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_peer_route_refuses_remote_control(
    svc, client, monkeypatch, method, path, body
):
    from backend.web.core import remote

    # Remote control ON, so it is the peer routes' own refusal being tested
    # (with it off the auth gate already says no).
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    before = svc.store.list()[:]
    kw = {"headers": REMOTE}
    if body is not None:
        kw["json"] = body
    r = (
        getattr(client, method)(path, **kw)
        if method != "delete"
        else client.request("DELETE", path, **kw)
    )
    assert r.status_code == 403, (path, r.text)
    assert "this device" in r.json()["error"]
    assert svc.store.list() == before
    assert svc._invites is None or not svc._invites.book


def test_invite_code_only_in_create_response(svc, client):
    r = client.post("/api/peer/invites", json={"ttl_s": 300})
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == CODE
    assert body["host"] == "100.64.0.1"
    status = client.get("/api/peer")
    assert status.status_code == 200
    text = status.text
    assert CODE not in text
    assert "s" * 60 not in text
    assert SECRET.hex() not in text
    assert "ee" * 32 not in text  # the peer's pinned key
    assert status.json()["invites"][0]["invite_id"] == body["invite_id"]
    assert set(status.json()["invites"][0]) == {"invite_id", "expires_in", "direct"}
    assert status.json()["fingerprint"] == "07" * 16
    assert status.json()["links"][0]["sas"] == "482-019-337-5"
    assert "peer_pub" not in status.json()["links"][0]


def _switched_off(svc, monkeypatch):
    """Peer links off, with a settings store the service can flip on."""
    state = {
        "enabled": False,
        "listen_host": "0.0.0.0",
        "listen_port": 8799,
        "advertise_host": "100.64.0.1",
        "relay": "off",
    }
    saved = []
    monkeypatch.setattr(svc, "_settings_getter", lambda: dict(state))
    monkeypatch.setattr(
        svc,
        "_settings_setter",
        lambda patch: (saved.append(patch), state.update(patch)),
    )
    return saved


def test_inviting_turns_peer_links_on(svc, client, monkeypatch):
    # Making an invite IS saying yes: no "turn them on first" detour.
    saved = _switched_off(svc, monkeypatch)
    r = client.post("/api/peer/invites", json={})
    assert r.status_code == 201
    assert saved == [{"enabled": True}]
    assert r.json()["code"] == CODE


def test_joining_turns_peer_links_on(svc, client, monkeypatch):
    saved = _switched_off(svc, monkeypatch)
    r = client.post("/api/peer/join", json={"code": CODE})
    assert r.status_code == 201
    assert saved == [{"enabled": True}]


def test_invite_refused_when_off_and_nothing_can_turn_it_on(svc, client, monkeypatch):
    monkeypatch.setattr(
        svc, "_settings_getter", lambda: {"enabled": False, "listen_port": 8799}
    )
    r = client.post("/api/peer/invites", json={})
    assert r.status_code == 409
    assert "off" in r.json()["error"]


def test_join_takes_the_whole_invite_message(svc, client):
    # People paste what they were sent — the message, not just the code.
    msg = svc_mod.invite_message(CODE, 600)
    assert msg.count(CODE) == 1 and "mindflock peer join" in msg
    r = client.post("/api/peer/join", json={"code": "hey!\n" + msg.upper()})
    assert r.status_code == 201
    assert ("pair", CODE) in svc.transport.calls


def test_join_without_a_code_says_so(svc, client):
    r = client.post("/api/peer/join", json={"code": "here you go: (forgot to paste)"})
    assert r.status_code == 400
    assert "mfp1:" in r.json()["error"]


def test_invite_response_carries_a_ready_to_send_message(svc, client):
    body = client.post("/api/peer/invites", json={"ttl_s": 600}).json()
    assert body["message"] == svc_mod.invite_message(CODE, 600)
    assert "10 min" in body["message"]


def test_invite_refuses_unspecified_advertise_host(svc, client):
    r = client.post("/api/peer/invites", json={"advertise_host": "0.0.0.0"})
    assert r.status_code == 400


def test_join_returns_link_with_sas(svc, client):
    r = client.post("/api/peer/join", json={"code": CODE})
    assert r.status_code == 201
    assert r.json()["sas"] == "482-019-337-5"
    assert "peer_pub" not in r.json()


def test_join_maps_bad_code_to_400(svc, client, monkeypatch):
    async def bad(code):
        raise ValueError("malformed code")

    t = svc.transport
    monkeypatch.setattr(t, "pair", bad)
    r = client.post("/api/peer/join", json={"code": "nope"})
    assert r.status_code == 400


def test_perms_route_validates(svc, client):
    lid = "ab" * 16
    r = client.post("/api/peer/links/%s/perms" % lid, json={"diff": False})
    assert r.status_code == 200 and r.json()["perms"]["diff"] is False
    assert (
        client.post("/api/peer/links/%s/perms" % lid, json={"diff": "no"}).status_code
        == 400
    )
    assert (
        client.post("/api/peer/links/%s/perms" % lid, json={"root": True}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/peer/links/%s/perms" % ("ff" * 16), json={"diff": True}
        ).status_code
        == 404
    )


def test_unlink_route(svc, client):
    lid = "ab" * 16
    r = client.request("DELETE", "/api/peer/links/" + lid)
    assert r.status_code == 200
    assert svc.store.get(lid) is None
    assert ("unlink", lid) in svc.transport.calls


def test_settings_peer_group_refused_from_remote(client, monkeypatch):
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    r = client.post(
        "/api/settings",
        json={"peer": {"egress_allow": ["evil.example"]}},
        headers=REMOTE,
    )
    assert r.status_code == 403
    from backend.config import settings

    settings.invalidate()
    assert settings.load_settings().peer.egress_allow == []


def test_settings_peer_group_saves_locally(client):
    r = client.post(
        "/api/settings", json={"peer": {"enabled": True, "listen_port": 9001}}
    )
    assert r.status_code == 200
    from backend.config import settings

    assert settings.load_settings().peer.listen_port == 9001
