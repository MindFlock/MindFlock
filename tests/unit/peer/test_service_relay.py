"""PeerService with ``peer.relay`` on: the relay (ingress + quick tunnel)
runs exactly while an invite or listener link needs it, invites carry the
relay address, the direct TCP listener stays closed, failures are clear and
leave nothing running, and a joined link can be re-addressed."""

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from backend.peer import paths
from backend.peer import service as svc_mod
from backend.peer.invite import parse_code
from backend.peer.service import PeerServiceError

from .test_integration_routes import FakeInvites, FakeStore, FakeTransport, link


class RelayInvites(FakeInvites):
    def create(self, host, port, ttl_s=600, relay_path=None):
        inv = super().create(host, port, ttl_s)
        inv.relay_path = relay_path
        return inv


class RelayTransport(FakeTransport):
    def __init__(self, *a):
        super().__init__(*a)
        self.relay = False
        self.redials = []

    async def accept_relayed(self, reader, writer, source):
        pass

    def open_relay(self):
        self.relay = True

    def close_relay(self):
        self.relay = False

    async def redial(self, link_id):
        self.redials.append(link_id)


class FakeIngress:
    made: list = []

    def __init__(self, on_stream, path, *, port=0, trust_cf_ip=False):
        self.on_stream, self.path = on_stream, path
        self.want_port, self.trust_cf_ip = port, trust_cf_ip
        self.port = None
        self.stopped = False
        FakeIngress.made.append(self)

    async def start(self):
        self.port = self.want_port or 41000 + len(FakeIngress.made)
        return self.port

    async def stop(self):
        self.stopped = True


class FakeTunnel:
    made: list = []
    host = "quiet-lake-blue-sky.trycloudflare.com"
    fail: Exception | None = None

    def __init__(self, binary, port, *, on_exit=None):
        self.binary, self.port, self.on_exit = binary, port, on_exit
        self.alive = False
        self.hostname = None
        self.stopped = False
        FakeTunnel.made.append(self)

    async def start(self):
        if FakeTunnel.fail is not None:
            raise FakeTunnel.fail
        self.alive, self.hostname = True, FakeTunnel.host
        return self.hostname

    async def stop(self):
        self.alive, self.hostname, self.stopped = False, None, True

    def die(self):
        self.alive, self.hostname = False, None
        self.on_exit()


@pytest.fixture(autouse=True)
def _reset():
    FakeIngress.made = []
    FakeTunnel.made = []
    FakeTunnel.fail = None
    FakeTunnel.host = "quiet-lake-blue-sky.trycloudflare.com"


def make_service(*links, cloudflared="/usr/bin/cloudflared", **settings):
    conf = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 8799,
        "display_name": "alice",
        "advertise_host": "",
        "egress_allow": [],
        "relay": "cloudflare",
        "relay_url": "",
        "relay_port": 0,
    }
    conf.update(settings)
    svc = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(list(links)),
        invites_factory=RelayInvites,
        transport_factory=RelayTransport,
        settings_getter=lambda: conf,
        ingress_factory=FakeIngress,
        tunnel_factory=FakeTunnel,
        cloudflared_finder=lambda: cloudflared,
    )
    svc.conf = conf
    return svc


async def test_cloudflare_invite_carries_the_tunnel(monkeypatch):
    svc = make_service()
    inv = await svc.create_invite(300)
    assert inv["relay"] == "cloudflare"
    assert (inv["host"], inv["port"]) == (FakeTunnel.host, 443)
    book = list(svc.invites.book.values())[0]
    token = svc._relay_token()
    assert book.relay_path == "/" + token
    assert (book.host, book.port) == (FakeTunnel.host, 443)
    (ing,) = FakeIngress.made
    (tun,) = FakeTunnel.made
    # The tunnel points at the relay ingress — the only origin it has.
    assert tun.port == ing.port and ing.path == "/" + token
    assert ing.trust_cf_ip is True
    t = svc.transport
    assert t.relay is True and t.listening is False  # no direct listener
    st = svc.status()["relay"]
    assert st["running"] and st["public_host"] == FakeTunnel.host
    assert st["address"] == f"wss://{FakeTunnel.host}:443/{token}"
    assert st["cloudflared"] is True


async def test_relay_stops_when_nothing_needs_it():
    svc = make_service(link("cd" * 16, role="dialer"))  # dialer links don't
    inv = await svc.create_invite(300)
    await svc.revoke_invite(inv["invite_id"])
    (ing,), (tun,) = FakeIngress.made, FakeTunnel.made
    assert ing.stopped and tun.stopped
    assert svc.transport.relay is False
    assert svc.status()["relay"]["running"] is False


async def test_listener_links_keep_the_relay_up():
    svc = make_service(link())  # a listener-role link
    await svc.sync_listener()
    assert len(FakeTunnel.made) == 1 and FakeTunnel.made[0].alive
    assert svc.transport.listening is False
    await svc.stop()
    assert FakeTunnel.made[0].stopped and FakeIngress.made[0].stopped


async def test_missing_cloudflared_is_a_clear_409_and_leaves_nothing_up():
    svc = make_service(cloudflared=None)
    with pytest.raises(PeerServiceError) as exc:
        await svc.create_invite(300)
    assert exc.value.status == 409
    assert "cloudflared is not installed" in exc.value.message
    assert "never downloads" in exc.value.message
    assert svc.invites.book == {}
    assert all(i.stopped for i in FakeIngress.made)
    assert svc.transport.relay is False


async def test_tunnel_failure_is_a_502_and_rolls_back():
    FakeTunnel.fail = RuntimeError("edge unreachable")
    svc = make_service()
    with pytest.raises(PeerServiceError) as exc:
        await svc.create_invite(300)
    assert exc.value.status == 502 and "edge unreachable" in exc.value.message
    assert svc.invites.book == {}
    assert all(i.stopped for i in FakeIngress.made)


async def test_url_mode_uses_the_configured_proxy_without_a_tunnel():
    svc = make_service(relay="url", relay_url="https://peer.example.com:8443/mf/")
    inv = await svc.create_invite(300)
    book = list(svc.invites.book.values())[0]
    assert (inv["host"], inv["port"]) == ("peer.example.com", 8443)
    assert book.relay_path == "/mf/" + svc._relay_token()
    assert FakeTunnel.made == []
    assert FakeIngress.made[0].trust_cf_ip is False  # no Cloudflare to vouch


@pytest.mark.parametrize(
    "url",
    ["", "ftp://x.example/a", "wss://", "wss://bad host/x", "wss://x.example/a/../b"],
)
async def test_url_mode_needs_a_valid_url(url):
    svc = make_service(relay="url", relay_url=url)
    with pytest.raises(PeerServiceError) as exc:
        await svc.create_invite(300)
    assert exc.value.status == 409
    assert svc.invites.book == {}


async def test_relay_off_is_the_direct_listener_as_before():
    svc = make_service(relay="off", advertise_host="100.64.0.9")
    inv = await svc.create_invite(300)
    assert (inv["host"], inv["port"]) == ("100.64.0.9", 8799)
    assert svc.transport.listening is True
    assert FakeIngress.made == [] and FakeTunnel.made == []
    assert list(svc.invites.book.values())[0].relay_path is None


async def test_unknown_relay_mode_is_off():
    svc = make_service(relay="carrier-pigeon")
    assert svc.relay_mode() == "off"


async def test_switching_relay_on_closes_the_direct_listener():
    svc = make_service(link(), relay="off")
    await svc.sync_listener()
    assert svc.transport.listening is True
    svc.conf["relay"] = "cloudflare"
    await svc.sync_listener()
    assert svc.transport.listening is False and svc.transport.relay is True
    svc.conf["relay"] = "off"
    await svc.sync_listener()
    assert svc.transport.listening is True and svc.transport.relay is False
    assert FakeTunnel.made[0].stopped


async def test_settings_change_restarts_the_ingress():
    svc = make_service(link())
    await svc.sync_listener()
    svc.conf["relay_port"] = 45555
    await svc.sync_listener()
    assert FakeIngress.made[0].stopped and FakeIngress.made[1].port == 45555
    assert FakeTunnel.made[0].stopped and FakeTunnel.made[1].port == 45555


async def test_tunnel_death_brings_up_a_new_one(monkeypatch):
    monkeypatch.setattr(svc_mod, "RELAY_RESTART_INITIAL_S", 0.0)
    svc = make_service(link())
    svc._relay_backoff = 0.0
    await svc.sync_listener()
    FakeTunnel.host = "brand-new-name.trycloudflare.com"
    FakeTunnel.made[0].die()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(FakeTunnel.made) == 2 and FakeTunnel.made[1].alive:
            break
    assert FakeTunnel.made[1].alive
    assert svc.status()["relay"]["public_host"] == "brand-new-name.trycloudflare.com"
    await svc.stop()


async def test_no_restart_once_relay_is_switched_off(monkeypatch):
    svc = make_service(link())
    svc._relay_backoff = 0.0
    await svc.sync_listener()
    svc.conf["relay"] = "off"
    FakeTunnel.made[0].die()
    await asyncio.sleep(0.05)
    assert len(FakeTunnel.made) == 1
    await svc.stop()


def test_relay_token_is_persisted_private_and_stable():
    a = svc_mod.PeerService._relay_token()
    b = svc_mod.PeerService._relay_token()
    assert a == b and len(a) == 26
    path = os.path.join(paths.peer_root(), "relay", "token")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, "w") as f:
        f.write("../../etc/passwd")
    c = svc_mod.PeerService._relay_token()
    assert c != a and len(c) == 26 and c.isalnum()


_NOT_A_TOKEN = "abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret


def test_relay_token_refuses_a_symlink(tmp_path):
    d = paths.ensure_dir(os.path.join(paths.peer_root(), "relay"))
    target = tmp_path / "elsewhere"
    target.write_text(_NOT_A_TOKEN + "\n")
    os.symlink(target, os.path.join(d, "token"))
    tok = svc_mod.PeerService._relay_token()
    assert tok != _NOT_A_TOKEN
    assert not os.path.islink(os.path.join(d, "token"))
    assert target.read_text() == _NOT_A_TOKEN + "\n"


async def test_relay_code_is_a_real_v2_code_with_the_real_book():
    from backend.peer.invite import InviteBook

    svc = make_service()
    svc._invites_factory = lambda: InviteBook(b"\x07" * 16)
    inv = await svc.create_invite(300)
    info = parse_code(inv["code"])
    assert info.carrier == "wss" and info.host == FakeTunnel.host
    assert info.path == "/" + svc._relay_token()


# -- re-addressing a joined link ---------------------------------------------------


async def test_set_address_repoints_a_dialer_link():
    lnk = link("cd" * 16, role="dialer")
    svc = make_service(lnk)
    await svc._ensure_transport()
    out = await svc.set_address(
        "cd" * 16, "wss://new-name.trycloudflare.com/" + "a" * 26
    )
    assert out["peer_addr"] == "wss://new-name.trycloudflare.com:443/" + "a" * 26
    assert svc.transport.redials == ["cd" * 16]


async def test_set_address_refuses_listener_links_and_garbage():
    svc = make_service(link(), link("cd" * 16, role="dialer"))
    with pytest.raises(PeerServiceError) as exc:
        await svc.set_address("ab" * 16, "10.0.0.1:8799")
    assert exc.value.status == 409
    for bad in ("", "wss://x/../y", "http://x.example/a", "x" * 700, None):
        with pytest.raises(PeerServiceError) as exc:
            await svc.set_address("cd" * 16, bad)
        assert exc.value.status == 400
    with pytest.raises(PeerServiceError) as exc:
        await svc.set_address("ef" * 16, "10.0.0.1:8799")
    assert exc.value.status == 404


def test_address_route(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    svc = make_service(link("cd" * 16, role="dialer"))
    monkeypatch.setattr(svc_mod, "_SERVICE", svc)
    client = TestClient(server.app)
    r = client.post(
        "/api/peer/links/" + "cd" * 16 + "/address",
        json={"address": "wss://n.trycloudflare.com/" + "b" * 26},
    )
    assert r.status_code == 200, r.text
    assert r.json()["peer_addr"] == "wss://n.trycloudflare.com:443/" + "b" * 26
    r = client.post(
        "/api/peer/links/" + "cd" * 16 + "/address", json={"address": "nope"}
    )
    assert r.status_code == 400
    st = client.get("/api/peer").json()
    assert st["relay"]["mode"] == "cloudflare"


async def test_auto_relays_through_cloudflare_when_cloudflared_is_installed():
    # The default: "send someone a code" has to work without knowing their
    # network, so an installed cloudflared means the invite goes through it.
    svc = make_service(relay="auto")
    assert svc.relay_setting() == "auto"
    assert svc.relay_mode() == "cloudflare"
    inv = await svc.create_invite(300)
    assert inv["relay"] == "cloudflare"
    assert FakeTunnel.made
    assert svc.status()["relay"]["setting"] == "auto"


async def test_auto_dials_direct_without_cloudflared():
    svc = make_service(relay="auto", cloudflared=None, advertise_host="100.64.0.9")
    assert svc.relay_mode() == "off"
    inv = await svc.create_invite(300)
    assert (inv["host"], inv["port"]) == ("100.64.0.9", 8799)
    assert FakeTunnel.made == []
    # …and the status says why, so the UI can offer to install it.
    relay = svc.status()["relay"]
    assert relay["setting"] == "auto" and relay["cloudflared"] is False


async def test_unset_relay_is_auto():
    svc = make_service()
    del svc.conf["relay"]
    assert svc.relay_setting() == "auto"


def test_peer_settings_default_relay_is_auto_and_accepted():
    from backend.config.settings import PeerSettings

    assert PeerSettings().effective()["relay"] == "auto"
    assert PeerSettings.from_dict({"relay": "auto"}).relay == "auto"
    assert PeerSettings.from_dict({"relay": "off"}).effective()["relay"] == "off"
