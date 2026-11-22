"""The device tunnel (backend/peer/fleet_tunnel.py) over a loopback carrier:
a real RelayIngress (plain ws, no cloudflared), the pinned TLS 1.3 server and
the fleet-key handshake, piping to a stand-in "web server" — docs/
devices-anywhere.md §3-4. A forged address never authenticates, and nothing
reaches the web server before the proof."""

from __future__ import annotations

import asyncio
import gc

import pytest

from backend.peer import fleet_record as fr
from backend.peer import fleet_tunnel as ft
from backend.peer import identity, tunnel_peers
from backend.peer.relay import RelayIngress
from backend.peer.transport import server_ssl_context

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

PATH = "/" + "q" * 26
KEYS = fr.derive("k" * 43, "0123456789abcdef", 1)
MEMBERS = {"laptop", "rig"}


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


class Origin:
    """A stand-in web server: answers each line with ``pong:<line>`` and
    remembers who connected (and whether the tunnel had marked them)."""

    def __init__(self):
        self.conns = []  # (port, tunneled)
        self.bytes = 0
        self.server = None

    async def _on(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.conns.append((peer[1], tunnel_peers.is_tunneled(peer)))
        try:
            while line := await reader.readline():
                self.bytes += len(line)
                writer.write(b"pong:" + line)
                await writer.drain()
        finally:
            writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def rig(tmp_path):
    """The device being reached ('laptop'): origin + TunnelServer + ingress."""
    origin = Origin()
    oport = await origin.start()
    ident = identity.load_or_create(str(tmp_path / "laptop-id"))
    keys = {"k": KEYS}
    server = ft.TunnelServer(
        server_ssl_context(ident),
        ident.pub,
        keys=lambda: keys["k"],
        self_key=lambda: "laptop",
        members=lambda: MEMBERS,
        origin_port=lambda: oport,
    )
    ing = RelayIngress(server, PATH)
    port = await ing.start()

    class R:
        pass

    r = R()
    r.origin, r.server, r.ingress, r.ident, r.keys = origin, server, ing, ident, keys
    r.url = "wss://127.0.0.1:%d%s" % (port, PATH)
    r.fp = ident.fingerprint().hex()
    r.port = port
    yield r
    await ing.stop()
    origin.server.close()


def _head(port: int, *extra: bytes) -> bytes:
    """An HTTP request head as this server's own client sends it to a
    device proxy (Host = the proxy's own address)."""
    return (
        b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port
        + b"".join(h + b"\r\n" for h in extra)
        + b"\r\n"
    )


async def _roundtrip(reader, writer, msg=b"hello\n") -> bytes:
    writer.write(msg)
    await writer.drain()
    return await asyncio.wait_for(reader.readline(), 5)


async def test_member_reaches_the_web_server_marked_as_tunneled(rig):
    reader, writer = await ft.dial(
        rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None
    )
    assert await _roundtrip(reader, writer) == b"pong:hello\n"
    assert await _roundtrip(reader, writer, b"again\n") == b"pong:again\n"
    # The web server saw a loopback connection the tunnel had registered as
    # tunneled before sending it a byte.
    assert rig.origin.conns and rig.origin.conns[0][1] is True
    writer.close()


async def test_forged_pin_never_sends_a_byte(rig, tmp_path):
    """An address (say, a forged record) naming another key: the pin fails
    before the client says anything."""
    other = identity.load_or_create(str(tmp_path / "other-id")).fingerprint().hex()
    with pytest.raises(ft.TunnelAuthError) as e:
        await ft.dial(rig.url, other, KEYS, "rig", "laptop", edge_ssl=None)
    assert e.value.reason == "pin"
    assert rig.origin.conns == []


async def test_forged_address_to_an_impostor_fails(tmp_path, rig):
    """Someone who can write to the user's GitHub points 'laptop' at their own
    server, pinned to their own key — but they don't hold the fleet key, so
    the server proof fails and the client sends nothing further."""
    origin = Origin()
    oport = await origin.start()
    evil = identity.load_or_create(str(tmp_path / "evil-id"))
    wrong = fr.derive("x" * 43, "0123456789abcdef", 1)

    # The impostor answers every proof as if it were fine — it can't compute
    # the real one without the key.
    server = ft.TunnelServer(
        server_ssl_context(evil),
        evil.pub,
        keys=lambda: wrong,
        self_key=lambda: "laptop",
        members=lambda: MEMBERS,
        origin_port=lambda: oport,
    )
    ing = RelayIngress(server, PATH)
    port = await ing.start()
    try:
        url = "wss://127.0.0.1:%d%s" % (port, PATH)
        with pytest.raises(ft.TunnelAuthError) as e:
            await ft.dial(
                url, evil.fingerprint().hex(), KEYS, "rig", "laptop", edge_ssl=None
            )
        assert e.value.reason in ("denied", "proof")
        assert origin.conns == []
    finally:
        await ing.stop()
        origin.server.close()


async def test_impostor_that_skips_the_check_still_fails(tmp_path):
    """A server that says ``ok`` with a made-up proof is caught by the client."""
    import base64
    import secrets
    import ssl

    from backend.peer import wire

    evil = identity.load_or_create(str(tmp_path / "evil2-id"))
    ctx = server_ssl_context(evil)

    async def liar(reader, writer, source=""):
        await writer.start_tls(ctx)
        writer.write(
            wire.encode(
                {
                    "t": "hello",
                    "v": 1,
                    "nonce": base64.b64encode(secrets.token_bytes(32)).decode(),
                }
            )
        )
        await writer.drain()
        await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
        writer.write(wire.encode({"t": "ok", "proof": "0" * 64}))
        await writer.drain()
        await asyncio.sleep(1)

    ing = RelayIngress(liar, PATH)
    port = await ing.start()
    try:
        with pytest.raises(ft.TunnelAuthError) as e:
            await ft.dial(
                "wss://127.0.0.1:%d%s" % (port, PATH),
                evil.fingerprint().hex(),
                KEYS,
                "rig",
                "laptop",
                edge_ssl=None,
            )
        assert e.value.reason == "proof"
    finally:
        await ing.stop()
        del ssl


@pytest.mark.parametrize(
    "keys,frm,to",
    [
        (fr.derive("x" * 43, "0123456789abcdef", 1), "rig", "laptop"),  # wrong key
        (fr.derive("k" * 43, "0123456789abcdef", 0), "rig", "laptop"),  # old epoch
        (KEYS, "stranger", "laptop"),  # not a member
        (KEYS, "rig", "someone-else"),  # not addressed to this device
        (KEYS, "laptop", "laptop"),  # claims to be the device itself
    ],
)
async def test_server_denies_and_web_server_sees_nothing(rig, keys, frm, to):
    with pytest.raises(ft.TunnelAuthError) as e:
        await ft.dial(rig.url, rig.fp, keys, frm, to, edge_ssl=None)
    assert e.value.reason == "denied"
    await asyncio.sleep(0.05)
    assert rig.origin.conns == [] and rig.origin.bytes == 0
    assert rig.server.stats["denied"] >= 1


async def test_after_a_key_change_the_old_key_is_refused(rig):
    rig.keys["k"] = fr.derive("n" * 43, "0123456789abcdef", 2)
    with pytest.raises(ft.TunnelAuthError):
        await ft.dial(rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None)
    assert rig.origin.conns == []


async def test_outside_a_group_everything_is_denied(rig):
    rig.keys["k"] = None
    with pytest.raises(ft.TunnelAuthError):
        await ft.dial(rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None)


async def _http(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    out = b""
    try:
        async with asyncio.timeout(3):
            while chunk := await reader.read(65536):
                out += chunk
    except (ConnectionError, TimeoutError):
        pass
    writer.transport.abort()
    return out


async def test_nothing_served_unauthenticated_on_the_tunnel_path(rig):
    for req in (
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/remote/hello HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET " + PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n",  # no upgrade
        b"POST "
        + PATH.encode()
        + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\nhi",
    ):
        out = await _http(rig.port, req)
        assert out.startswith(b"HTTP/1.1 404 Not Found"), out[:60]
    # A visitor WITH the path but no key: upgrade, then speak garbage /
    # nothing — the web server never sees a byte.
    import base64
    import os

    reader, writer = await asyncio.open_connection("127.0.0.1", rig.port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(
        (
            f"GET {PATH} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
    assert head.startswith(b"HTTP/1.1 101")
    writer.write(b"\x82\x85abcd" + bytes(b ^ m for b, m in zip(b"GET /", b"abcda")))
    await writer.drain()
    await asyncio.sleep(0.3)
    writer.transport.abort()
    assert rig.origin.conns == []


async def test_proxy_pipes_and_reports(rig):
    results = []
    proxy = ft.DeviceProxy(
        "laptop",
        lambda: (rig.url, rig.fp, KEYS, "rig", "laptop"),
        edge_ssl=None,
        on_result=lambda ok, why: results.append((ok, why)),
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        head = _head(proxy.port)
        assert await _roundtrip(reader, writer, head) == b"pong:GET / HTTP/1.1\r\n"
        writer.close()
        assert results == [(True, "")]
    finally:
        await proxy.stop()


async def test_proxy_with_a_dead_address_reports_failure(rig):
    results = []
    proxy = ft.DeviceProxy(
        "laptop",
        lambda: ("wss://127.0.0.1:1/" + "z" * 26, rig.fp, KEYS, "rig", "laptop"),
        edge_ssl=None,
        on_result=lambda ok, why: results.append((ok, why)),
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(_head(proxy.port))
        assert await asyncio.wait_for(reader.read(), 5) == b""
        assert results == [(False, "unreachable")]
    finally:
        await proxy.stop()
