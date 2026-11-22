"""Reach my devices from anywhere (docs/devices-anywhere.md): the GitHub
store's plain failures, the orchestrator's lifecycle over a loopback carrier
and a folder store (never real GitHub or Cloudflare), and the web server's
gate for tunneled requests."""

from __future__ import annotations

import asyncio
import gc
import base64
import os
import time

import pytest

from backend.peer import fleet_record as fr
from backend.peer import fleet_tunnel as ft
from backend.peer import tunnel_peers
from backend.web.core import devices_anywhere as da
from backend.web.core import github_records as gr

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

KEYS = fr.derive("k" * 43, "0123456789abcdef", 1)


@pytest.fixture(autouse=True)
def _finalize_earlier_loops():
    """Finalize what earlier tests' (closed) event loops left behind, then
    keep the cycle collector out of this test. Relay/tunnel coroutines left
    pending when a test's loop closed are otherwise finalized by the
    collector at a random allocation DURING a later test, running their
    cleanup inside that test's loop — which cut a live stream of the later
    test now and then (seen on the base commit too, depending on file
    order). Test-only: a server runs one loop for its whole life."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


# --------------------------------------------------------------------------- #
# The GitHub store, with a fake GitHub
# --------------------------------------------------------------------------- #
class FakeGitHub:
    def __init__(self, scopes="repo, read:org", private=True):
        self.scopes = scopes
        self.repo = None if private is None else self._repo(private)
        self.files = {}
        self.calls = []
        self.fail = None  # (status, headers) for every call

    @staticmethod
    def _repo(private, owner="me"):
        return {
            "private": private,
            "permissions": {"push": True},
            "full_name": "%s/mindflock-devices" % owner,
            "owner": {"login": owner},
        }

    async def __call__(self, method, path, token, body=None):
        self.calls.append((method, path))
        if self.fail:
            return self.fail[0], self.fail[1], None
        h = {"X-OAuth-Scopes": self.scopes} if self.scopes is not None else {}
        if path == "/user":
            return 200, h, {"login": "me"}
        if path == "/repos/me/mindflock-devices":
            return (200, h, dict(self.repo)) if self.repo else (404, h, {})
        if path == "/user/repos" and method == "POST":
            self.repo = self._repo(body["private"])
            return 201, h, dict(self.repo)
        prefix = "/repos/me/mindflock-devices/contents/"
        if path.startswith(prefix):
            name = path[len(prefix) :]
            if not name:
                return (
                    200,
                    h,
                    [
                        {"type": "file", "name": n, "sha": s}
                        for n, (_t, s) in self.files.items()
                    ]
                    + [{"type": "file", "name": "README.md", "sha": "x"}],
                )
            if method == "GET":
                if name not in self.files:
                    return 404, h, {}
                t, s = self.files[name]
                return (
                    200,
                    h,
                    {
                        "content": base64.b64encode(t.encode()).decode(),
                        "sha": s,
                        "size": len(t),
                    },
                )
            if method == "PUT":
                if name in self.files and body.get("sha") != self.files[name][1]:
                    return 409, h, {}
                t = base64.b64decode(body["content"]).decode()
                s = "sha%d" % len(self.calls)
                self.files[name] = (t, s)
                return 201, h, {"content": {"sha": s}}
            if method == "DELETE":
                self.files.pop(name, None)
                return 200, h, {}
        return 500, h, {}


def _store(fake, token="tok"):
    async def tok():
        return token

    return gr.GitHubStore(tok, http=fake)


NAME = "a" * 32 + ".rec"


async def test_store_round_trip_creates_a_private_repo():
    fake = FakeGitHub(private=None)
    s = _store(fake)
    assert await s.list() == {}
    sha = await s.write(NAME, "mfd1.abc")
    assert fake.repo["private"] is True
    assert ("POST", "/user/repos") in fake.calls
    assert await s.list() == {NAME: sha}  # README is not a record
    assert await s.read(NAME) == ("mfd1.abc", sha)
    assert await s.delete(NAME)
    assert await s.list() == {}


async def test_missing_repo_permission_is_a_plain_message():
    s = _store(FakeGitHub(scopes="read:org, gist"))
    with pytest.raises(gr.StoreError) as e:
        await s.write(NAME, "mfd1.abc")
    assert e.value.kind == "permission"
    assert "missing the repo permission" in e.value.text
    assert "Settings → GitHub" in e.value.text


async def test_fine_grained_token_403_is_the_same_message():
    fake = FakeGitHub(scopes=None, private=None)

    async def http(method, path, token, body=None):
        if path == "/user/repos":
            return (
                403,
                {},
                {"message": "Resource not accessible by personal access token"},
            )
        return await fake(method, path, token, body)

    with pytest.raises(gr.StoreError) as e:
        await _store(http).write(NAME, "mfd1.abc")
    assert e.value.kind == "permission"


async def test_no_token():
    with pytest.raises(gr.StoreError) as e:
        await _store(FakeGitHub(), token="").list()
    assert e.value.kind == "no_token" and "connect GitHub" in e.value.text


async def test_public_repo_refused():
    with pytest.raises(gr.StoreError) as e:
        await _store(FakeGitHub(private=False)).list()
    assert e.value.kind == "public_repo"


async def test_unreachable_and_rate_limited():
    fake = FakeGitHub()
    fake.fail = (0, {})
    with pytest.raises(gr.StoreError) as e:
        await _store(fake).list()
    assert e.value.kind == "unreachable"
    reset = time.time() + 900
    fake.fail = (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
    with pytest.raises(gr.StoreError) as e:
        await _store(fake).list()
    assert e.value.kind == "rate_limited" and e.value.retry_at == pytest.approx(reset)
    assert "retrying in" in e.value.text


async def test_write_conflict_takes_the_latest_sha():
    fake = FakeGitHub()
    s = _store(fake)
    await s.write(NAME, "mfd1.one")
    fake.files[NAME] = ("mfd1.x", "changed")
    await s.write(NAME, "mfd1.two", sha="stale")
    assert fake.files[NAME][0] == "mfd1.two"


# --------------------------------------------------------------------------- #
# The orchestrator over a loopback carrier + folder store
# --------------------------------------------------------------------------- #
class Origin:
    def __init__(self):
        self.conns = []

    async def _on(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.conns.append(tunnel_peers.is_tunneled(peer))
        while line := await reader.readline():
            writer.write(b"pong:" + line)
            await writer.drain()
        writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """One process playing both devices: 'laptop' runs the tunnel; reads are
    done as 'rig'. Everything under tmp_path; no GitHub, no cloudflared."""
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    monkeypatch.setenv(da.LOOPBACK_ENV, "1")
    da._reset_for_tests()
    store = gr.DirStore(str(tmp_path / "store"))
    da._set_store_for_tests(store)
    origin = Origin()
    oport = await origin.start()
    who = {"me": "laptop", "on": True, "keys": KEYS}
    monkeypatch.setattr(da, "enabled", lambda: who["on"])
    monkeypatch.setattr(da, "_keys", lambda: who["keys"])
    monkeypatch.setattr(da, "_self_key", lambda: who["me"])
    monkeypatch.setattr(da, "_members", lambda: {"laptop", "rig"})

    class FakeFleet:
        @staticmethod
        def live_members():
            return {
                "laptop": {"host": "Laptop", "dns": "laptop.x.ts.net"},
                "rig": {"host": "Rig"},
            }

    monkeypatch.setattr(da, "_fleet", lambda: FakeFleet)
    real_make = da._make_server

    def make_server():
        # laptop's server keeps answering as laptop even while the test
        # reads as rig.
        from backend.peer import identity
        from backend.peer.transport import server_ssl_context

        ident = identity.load_or_create()
        return (
            ft.TunnelServer(
                server_ssl_context(ident),
                ident.pub,
                keys=lambda: who["keys"],
                self_key=lambda: "laptop",
                members=lambda: {"laptop", "rig"},
                origin_port=lambda: oport,
            ),
            ident.fingerprint().hex(),
        )

    monkeypatch.setattr(da, "_make_server", make_server)
    monkeypatch.setattr(da, "_origin_port", lambda: oport)
    del real_make
    yield who, store, origin
    await da._shutdown()
    origin.server.close()
    da._reset_for_tests()


async def _up():
    await da.apply()
    if da._S.start_task is not None:
        await da._S.start_task
    assert da._S.tunnel_state == "up", da.status()


HI = b"pong:GET /hi HTTP/1.1\r\n"


async def _through(proxy_url: str) -> bytes:
    port = int(proxy_url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    # The proxy takes only its own server's HTTP client: a request head
    # addressed to it (the stand-in origin answers it line by line).
    writer.write(b"GET /hi HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.readline(), 5)
    finally:
        writer.close()


async def test_publish_then_found_and_reached(env):
    who, store, origin = env
    await _up()
    st = da.status()
    assert st["state"] == "on" and st["text"] == da.TUNNEL_TEXT and st["published_at"]
    files = await store.list()
    assert list(files) == [fr.file_name(KEYS, "laptop")]
    text, _ = await store.read(list(files)[0])
    assert "laptop" not in text and "127.0.0.1" not in text

    who["me"] = "rig"
    assert await da.read_records() == 1
    peers = await da.candidates(exclude={"rig"})
    assert [p["key"] for p in peers] == ["laptop"]
    assert peers[0]["via"] == "tunnel" and peers[0]["dns"] == "laptop.x.ts.net"
    assert await _through(peers[0]["base_url"]) == HI
    assert origin.conns == [True]
    # Tailnet answered for it: not offered.
    assert await da.candidates(exclude={"rig", "laptop"}) == []


async def test_address_change_after_tunnel_restart_is_found_again(env, monkeypatch):
    who, store, origin = env
    monkeypatch.setattr(da, "READ_MIN", 0.0)
    await _up()
    who["me"] = "rig"
    await da.read_records()
    first = da._S.addrs["laptop"].url
    proxy = (await da.candidates(exclude={"rig"}))[0]["base_url"]
    assert await _through(proxy) == HI

    # cloudflared dies; a fresh tunnel (new token, new address) comes up and
    # is published — as laptop.
    who["me"] = "laptop"
    await da._stop_tunnel()
    da._exited()
    da._S.restart_at = 0.0
    da._S.read_at = time.time()  # nothing due on this side
    await da.tick()
    await da._S.start_task
    assert da._S.url and da._S.url != first

    # rig dials the old address, fails, asks for a re-read, finds the new one.
    who["me"] = "rig"
    da._S.addrs["laptop"] = fr.Record(
        "laptop", first, da._S.addrs["laptop"].fp, 0, time.time()
    )
    da._S.file_sha.clear()
    port = int(proxy.rsplit(":", 1)[1])
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"GET /hi HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    assert await asyncio.wait_for(r.read(), 5) == b""
    w.close()
    assert da._S.read_wanted and da._S.dial.get("laptop")
    da._S.read_delay = 0.0
    assert da._read_due(time.time())
    await da.read_records()
    assert da._S.addrs["laptop"].url != first
    assert await _through(proxy) == HI


async def test_switch_off_deletes_the_record_and_stops(env):
    who, store, origin = env
    await _up()
    assert await store.list()
    who["on"] = False
    st = await da.apply()
    assert await store.list() == {}
    assert st["state"] == "off" and st["text"] == "Off"
    assert da._S.ingress is None and da._S.url == ""


async def test_github_unreachable_keeps_what_was_found(env):
    who, store, origin = env
    await _up()
    who["me"] = "rig"
    await da.read_records()

    class Down:
        async def list(self):
            raise gr.StoreError("unreachable", gr.UNREACHABLE_TEXT)

    da._set_store_for_tests(Down())
    await da.read_records()
    assert "laptop" in da._S.addrs  # the last addresses stay in use
    st = da.status()
    assert st["state"] == "on"
    assert (
        "Couldn't reach GitHub" in st["text"]
        and "using the addresses found earlier" in st["text"]
    )
    # The read backs off rather than hammering GitHub.
    assert not da._read_due(time.time())


async def test_missing_github_permission_is_shown(env):
    who, store, origin = env

    class NoPerm:
        async def write(self, *a, **k):
            raise gr.StoreError("permission", gr.PERMISSION_TEXT)

        async def list(self):
            raise gr.StoreError("permission", gr.PERMISSION_TEXT)

        async def delete(self, *a, **k):
            return True

    da._set_store_for_tests(NoPerm())
    await _up()
    st = da.status()
    assert st["state"] == "needs_github" and st["retry"]
    assert st["text"] == gr.PERMISSION_TEXT
    # Not retried on every tick.
    assert da._S.write_retry_at > time.time()


async def test_rate_limit_is_respected(env):
    who, store, origin = env
    await _up()
    da._note_github(gr.StoreError("rate_limited", "x", retry_at=time.time() + 600))
    da._S.read_at = 1.0
    da._S.read_wanted = True
    assert not da._read_due(time.time())


async def test_cloudflared_missing_offers_the_install(env, monkeypatch):
    who, store, origin = env
    monkeypatch.delenv(da.LOOPBACK_ENV)
    monkeypatch.setattr(da, "_binary", lambda: None)
    await da.apply()
    if da._S.start_task is not None:
        await da._S.start_task
    st = da.status()
    assert st["install"] and not st["installed"]
    assert st["text"] == "Couldn't start the tunnel — " + da.NOT_INSTALLED


async def test_not_in_a_group(env):
    who, store, origin = env
    who["keys"] = None
    st = await da.apply()
    assert st["state"] == "waiting" and da._S.ingress is None


async def test_off_leaves_no_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    da._reset_for_tests()
    monkeypatch.setattr(da, "enabled", lambda: False)
    await da.tick()
    await da.stop()
    assert not os.path.exists(tmp_path / "home" / "devices_anywhere")


# --------------------------------------------------------------------------- #
# The web server's gate for tunneled requests
# --------------------------------------------------------------------------- #
async def _asgi(scope_client, path="/api/instances", headers=()):
    from backend.web.core import auth
    from backend.web.core.tailnet_trust import PeerCaptureMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope
        seen["this_machine"] = auth._from_this_machine(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    stack = PeerCaptureMiddleware(auth.AuthMiddleware(app))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8765"), *headers],
        "client": scope_client,
        "server": ("127.0.0.1", 8765),
        "scheme": "http",
        "http_version": "1.1",
    }
    await stack(scope, receive, send)
    return sent[0]["status"], seen


@pytest.fixture
def gate_off(monkeypatch):
    from backend.web.core import auth

    monkeypatch.setattr(auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "_fleet_key_valid", lambda c: c == "FLEETKEY")
    monkeypatch.setattr(auth, "own_token_valid", lambda c: False)
    tunnel_peers.add(40001)
    yield
    tunnel_peers.discard(40001)


async def test_tunneled_request_needs_the_key_even_with_the_gate_off(gate_off):
    status, seen = await _asgi(("127.0.0.1", 40001))
    assert status == 401 and "scope" not in seen
    # A plain local request still passes the gate-off server.
    status, seen = await _asgi(("127.0.0.1", 40002))
    assert status == 200


async def test_tunneled_request_with_the_key_is_not_this_machine(gate_off):
    status, seen = await _asgi(
        ("127.0.0.1", 40001), headers=[(b"authorization", b"Bearer FLEETKEY")]
    )
    assert status == 200
    assert seen["this_machine"] is False
    assert seen["scope"]["client"] == tunnel_peers.CLIENT


async def test_tunneled_request_cannot_spoof_its_address(gate_off):
    status, seen = await _asgi(
        ("127.0.0.1", 40001),
        headers=[
            (b"authorization", b"Bearer FLEETKEY"),
            (b"x-forwarded-for", b"100.64.1.2"),
        ],
    )
    assert status == 200
    assert seen["scope"]["client"] == tunnel_peers.CLIENT


async def test_public_hello_still_answers_through_the_tunnel(gate_off):
    status, _seen = await _asgi(("127.0.0.1", 40001), path="/api/remote/hello")
    assert status == 200


def test_setting_round_trips_and_is_local():
    from backend.config import settings as st
    from backend.web.core import settings_sync

    g = st.GeneralSettings.from_dict({"devices_anywhere": True})
    assert g.devices_anywhere is True
    assert g.to_dict().get("devices_anywhere") is True
    assert (
        st.GeneralSettings.from_dict({"devices_anywhere": "yes"}).devices_anywhere
        is False
    )
    assert st.GeneralSettings().devices_anywhere is False
    assert "devices_anywhere" not in st.GeneralSettings().to_dict()
    # Per device: never synced.
    assert "devices_anywhere" in settings_sync.LOCAL["general"]


async def test_proxy_port_is_kept_when_an_address_goes(env):
    """A vanished record never frees the proxy's port (another local process
    could take it and be sent the fleet key); it just refuses connections."""
    who, store, origin = env
    await _up()
    who["me"] = "rig"
    await da.read_records()
    proxy = da._S.proxies["laptop"]
    port = proxy.port
    for name in list(await store.list()):
        await store.delete(name)
    await da.read_records()
    assert "laptop" not in da._S.addrs
    assert da._S.proxies["laptop"] is proxy and proxy.port == port
    assert await da.candidates(exclude={"rig"}) == []
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"GET /hi HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    assert await asyncio.wait_for(r.read(), 5) == b""
    w.close()


async def test_switch_off_forgets_tunnel_devices_in_discovery(env):
    from backend.web.core import remote

    who, store, origin = env
    await _up()
    dev = remote._device_state("zz-tunnel-test")
    dev.update(reachable=True, base_url="http://127.0.0.1:1", via="tunnel")
    try:
        who["on"] = False
        await da.apply()
        assert dev["reachable"] is False and dev["base_url"] == "" and dev["via"] == ""
    finally:
        remote._DEVICES.pop("zz-tunnel-test", None)
