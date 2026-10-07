"""TLS-in-WebSocket through an untrusted relay (backend/peer/relay.py).

Real PeerTransports on 127.0.0.1 talk through a real RelayIngress. Between
them sits :class:`EvilRelay`, a WebSocket-terminating middlebox (as
Cloudflare is) that records every byte and can alter, drop or replay them.
The claims under test: the relay can't read plaintext, can't make pairing
succeed with a wrong key, can't replay a handshake, and garbage or scanning
gets nothing; the existing pinned-key handshake runs unchanged inside.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import ipaddress
import os
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.peer import identity, relay, transport, wire
from backend.peer.addr import PeerAddr
from backend.peer.relay import NOT_FOUND, RelayError, RelayIngress, WsError
from backend.peer.transport import IdentityMismatch, PairingFailed, PeerUnavailable
from tests.unit.peer.conftest import b64, eventually

# CPython 3.12 race: when the far end closes just as a TLS handshake
# completes, StreamWriter.start_tls() stores a None transport and the
# writer's __del__ then raises (ignored, reported as "unraisable"). These
# tests kill connections mid-handshake on purpose; the code under test fails
# closed on it (checked), so the GC noise is filtered here.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

PATH = "/k" + "a" * 25  # shape of a real relay path: "/<26-char token>"


# -- plumbing --------------------------------------------------------------------


@pytest.fixture
async def ingresses():
    made: list[RelayIngress] = []
    yield made
    for ing in made:
        await ing.stop()


async def open_ingress(node, ingresses, path=PATH, **kw) -> RelayIngress:
    ing = RelayIngress(node.t.accept_relayed, path, **kw)
    await ing.start()
    node.t.open_relay()
    ingresses.append(ing)
    return ing


def relay_code(node, port: int, path: str = PATH) -> str:
    return node.invites.create("127.0.0.1", port, relay_path=path).code


def upgrade_head(path: str = PATH, extra: str = "", key: str | None = None) -> bytes:
    key = key or base64.b64encode(os.urandom(16)).decode()
    return (
        f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n{extra}\r\n"
    ).encode()


async def http_exchange(port: int, data: bytes, timeout: float = 3.0) -> bytes:
    """Send raw bytes, return everything the server sends until it closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        out = bytearray()
        try:
            async with asyncio.timeout(timeout):
                while chunk := await reader.read(65536):
                    out += chunk
        except ConnectionError:
            pass  # an abort after the reply (or a drop) is a close too
        return bytes(out)
    finally:
        writer.transport.abort()


async def raw_ws(port: int, path: str = PATH, extra: str = ""):
    """A raw upgraded connection to the ingress: (reader, writer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(upgrade_head(path, extra))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
    assert head.startswith(b"HTTP/1.1 101 "), head
    return reader, writer


async def closes_soon(reader, timeout: float = 3.0) -> bool:
    """True once the server closes (EOF/reset), draining whatever it sends."""
    try:
        async with asyncio.timeout(timeout):
            while await reader.read(65536):
                pass
        return True
    except (ConnectionError, OSError):
        return True
    except TimeoutError:
        return False


def _cert_for_localhost(tmp_path, name: str):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cpath, kpath = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kpath.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cpath), str(kpath)


class EvilRelay:
    """A WebSocket-terminating relay in the middle. It forwards the upgrade
    to ``upstream_port``, then re-frames every frame both ways, recording
    the payloads (the inner TLS bytes) per connection. ``mutate(direction,
    payload, conn_index) -> bytes | None`` may alter a payload or drop it
    (None); ``blackhole`` swallows everything; ``edge_ctx`` makes it a
    ``wss://`` edge."""

    def __init__(self, upstream_port: int, *, edge_ctx=None):
        self.up_port = upstream_port
        self.edge_ctx = edge_ctx
        self.streams: list[dict] = []
        self.raw = bytearray()  # every byte seen on the wire, both ways
        self.mutate = None
        self.blackhole = False
        self.server = None
        self._writers: list = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(
            self._cb, "127.0.0.1", 0, ssl=self.edge_ctx
        )
        return self.server.sockets[0].getsockname()[1]

    def payload(self, direction: str) -> bytes:
        return b"".join(bytes(s[direction]) for s in self.streams)

    def kill_all(self) -> None:
        for w in self._writers:
            w.transport.abort()

    async def stop(self) -> None:
        self.kill_all()
        if self.server is not None:
            self.server.close()

    async def _cb(self, cr, cw):
        idx = len(self.streams)
        rec = {"c2s": bytearray(), "s2c": bytearray()}
        self.streams.append(rec)
        uw = None
        self._writers.append(cw)
        try:
            head = await cr.readuntil(b"\r\n\r\n")
            self.raw += head
            if self.blackhole:
                await asyncio.sleep(3600)
            ur, uw = await asyncio.open_connection("127.0.0.1", self.up_port)
            self._writers.append(uw)
            uw.write(head)
            resp = await ur.readuntil(b"\r\n\r\n")
            self.raw += resp
            cw.write(resp)
            if not resp.startswith(b"HTTP/1.1 101"):
                return

            async def pump(r, w, direction, masked):
                while True:
                    fin, op, data = await relay.read_frame(
                        r, masked=masked, max_payload=1 << 22
                    )
                    self.raw += data
                    if op in (relay.OP_BIN, relay.OP_CONT):
                        rec[direction] += data
                        if self.mutate is not None:
                            data = self.mutate(direction, data, idx)
                            if data is None:
                                continue
                    w.write(relay.encode_frame(op, data, mask=masked, fin=fin))
                    await w.drain()
                    if op == relay.OP_CLOSE:
                        return

            await asyncio.wait(
                [
                    asyncio.create_task(pump(cr, uw, "c2s", True)),
                    asyncio.create_task(pump(ur, cw, "s2c", False)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            pass
        finally:
            cw.transport.abort()
            if uw is not None:
                uw.transport.abort()


@pytest.fixture
async def evil():
    made: list[EvilRelay] = []

    async def make(upstream_port, **kw):
        e = EvilRelay(upstream_port, **kw)
        port = await e.start()
        made.append(e)
        return e, port

    yield make
    for e in made:
        await e.stop()


async def relay_paired(a, b, ingresses, evil, **ing_kw):
    """B joins A's relay invite THROUGH an EvilRelay; returns (link, relay)."""
    ing = await open_ingress(a, ingresses, **ing_kw)
    mitm, mport = await evil(ing.port)
    link = await b.t.pair(relay_code(a, mport))
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id)
    )
    return link, mitm, ing


# -- end to end ------------------------------------------------------------------


async def test_pair_and_talk_through_the_relay(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    la, lb = a.store.get(link.link_id), b.store.get(link.link_id)
    assert (la.role, lb.role) == ("listener", "dialer")
    assert (
        lb.peer_addr
        == f"wss://127.0.0.1:{mitm.server.sockets[0].getsockname()[1]}{PATH}"
    )
    assert la.peer_pub == b.ident.pub.hex() and lb.peer_pub == a.ident.pub.hex()
    assert la.sas == lb.sas
    assert (await b.t.request(link.link_id, "status", {}))["shared"] is True
    assert await a.t.request(link.link_id, "msg", {"msg_id": "m1", "text": "hi"}) == {
        "accepted": True
    }
    # Large responses cross many WebSocket frames.
    big = "y" * 300_000

    async def behavior(link_, op, p):
        return {"stat": [], "diff": big[: p["max_chars"]], "truncated": False}

    a.handler.behavior = behavior
    res = await b.t.request(link.link_id, "diff", {"max_chars": 200_000})
    assert res["diff"] == big[:200_000]
    assert ing.stats["upgraded"] == 1
    # The direct TCP listener was never started.
    assert a.t.listening is None


async def test_reconnects_through_the_relay(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    mitm.kill_all()  # the relay drops every connection
    assert await eventually(lambda: not b.t.is_connected(link.link_id))
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id),
        timeout=5,
    )
    assert len(mitm.streams) >= 2  # the redial went through the relay again
    assert (await b.t.request(link.link_id, "status", {}))["name"] == "x"


async def test_relay_never_sees_plaintext_or_secrets(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    ing = await open_ingress(a, ingresses)
    mitm, mport = await evil(ing.port)
    code = relay_code(a, mport)
    from backend.peer.invite import parse_code

    info = parse_code(code)
    link = await b.t.pair(code)
    assert await eventually(lambda: a.t.is_connected(link.link_id))
    marker = "TOP-SECRET-MARKER-" + os.urandom(8).hex()
    await b.t.request(link.link_id, "msg", {"msg_id": "m1", "text": marker})

    async def behavior(link_, op, p):
        return {
            "path": p["path"],
            "size": len(marker),
            "encoding": "utf-8",
            "content": marker + "-file",
            "truncated": False,
        }

    a.handler.behavior = behavior
    await b.t.request(link.link_id, "read_file", {"path": "secret.txt"})
    seen = bytes(mitm.raw)
    for needle in (
        marker.encode(),
        (marker + "-file").encode(),
        b"secret.txt",
        info.secret,
        info.secret.hex().encode(),
        link.sas.encode(),
        link.link_id.encode(),
        b'"t":"pair"',
        b"alice",
        b"bob",
    ):
        assert needle not in seen, needle
    # It saw the relay path (it routes on it) and TLS records, nothing else.
    assert PATH.encode() in seen
    assert mitm.payload("c2s")[:1] == b"\x16"  # a TLS handshake record


async def test_tampering_breaks_the_connection_not_the_data(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    a.handler.calls.clear()

    def flip(direction, data, idx):
        if direction == "c2s" and len(data) > 10:
            data = bytearray(data)
            data[len(data) // 2] ^= 0x01
            return bytes(data)
        return data

    mitm.mutate = flip
    with pytest.raises(PeerUnavailable):
        await b.t.request(link.link_id, "msg", {"msg_id": "m2", "text": "tampered"}, 3)
    # The AEAD rejected the record: nothing (altered or not) reached the app.
    assert a.handler.calls == []
    mitm.mutate = None
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id),
        timeout=5,
    )


async def test_tampering_during_pairing_fails_closed(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    ing = await open_ingress(a, ingresses)
    mitm, mport = await evil(ing.port)
    n = {"frames": 0}

    def flip_late(direction, data, idx):
        n["frames"] += 1
        if direction == "s2c" and n["frames"] > 2:  # past the ServerHello
            data = bytearray(data)
            data[-1] ^= 0x80
            return bytes(data)
        return data

    mitm.mutate = flip_late
    with pytest.raises(PairingFailed):
        await b.t.pair(relay_code(a, mport))
    assert b.store.list() == []
    assert a.handler.added == []


async def test_dropping_relay_times_out_without_a_link(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node(
        "bob", relay_edge_ssl=None, handshake_timeout=0.5
    )
    ing = await open_ingress(a, ingresses)
    mitm, mport = await evil(ing.port)
    mitm.mutate = lambda d, data, i: None  # swallow every data frame
    with pytest.raises(PairingFailed):
        await b.t.pair(relay_code(a, mport))
    assert b.store.list() == [] and a.store.list() == []


async def test_relay_routing_to_an_impostor_is_caught_before_any_secret(
    make_node, ingresses, evil
):
    """The relay sends the joiner to ANOTHER MindFlock (its own key, its own
    ingress): the pinned fingerprint fails before the pair frame is sent,
    so the impostor learns nothing it could use against the real inviter."""
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    m = make_node("mallory")
    await open_ingress(a, ingresses)
    m_ing = await open_ingress(m, ingresses)
    received = bytearray()

    class Recorder:
        async def __call__(self, reader, writer, source):
            ctx = transport.server_ssl_context(m.ident)
            try:
                await writer.start_tls(ctx)
                writer.write(
                    wire.encode(
                        {
                            "t": "hello",
                            "v": 1,
                            "nonce": b64(os.urandom(32)),
                            "name": "a",
                        }
                    )
                )
                while data := await reader.read(65536):
                    received.extend(data)
            except Exception:
                pass
            finally:
                writer.transport.abort()

    m_ing._on_stream = Recorder()
    mitm, mport = await evil(m_ing.port)  # code says A's key; relay routes to M
    code = a.invites.create("127.0.0.1", mport, relay_path=PATH).code
    with pytest.raises(IdentityMismatch):
        await b.t.pair(code)
    assert bytes(received) == b""
    assert b.store.list() == []
    assert a.invites.active() != []  # the real invite is untouched


async def test_relay_terminating_inner_tls_with_wrong_key_on_reconnect(
    make_node, ingresses, evil
):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    m = make_node("mallory")
    m_ing = await open_ingress(m, ingresses)
    mitm.up_port = m_ing.port  # from now on, route to the impostor
    mitm.kill_all()
    await asyncio.sleep(1.0)
    assert not b.t.is_connected(link.link_id)
    assert m.store.list() == [] and m.handler.calls == []
    assert b.store.get(link.link_id).peer_pub == a.ident.pub.hex()


async def test_replayed_pairing_stream_gets_nothing(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    recorded = bytes(mitm.streams[0]["c2s"])
    assert recorded
    added = len(a.handler.added)
    # The relay replays the joiner's whole byte stream on a fresh WebSocket.
    ws = await relay.open_ws(PeerAddr("wss", "127.0.0.1", ing.port, PATH), None)
    await ws.send(recorded)
    got = bytearray()
    try:
        async with asyncio.timeout(3):
            while (data := await ws.recv()) is not None:
                got += data
    except (TimeoutError, WsError, ConnectionError):
        pass
    ws.abort()
    assert len(a.store.list()) == 1
    assert len(a.handler.added) == added
    assert b'"welcome"' not in got and b"denied" not in got  # all ciphertext


async def test_replayed_auth_stream_is_not_adopted(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    mitm.kill_all()
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and len(mitm.streams) >= 2, timeout=5
    )
    auth_stream = bytes(mitm.streams[-1]["c2s"])
    conn = a.t._conns[link.link_id]
    ws = await relay.open_ws(PeerAddr("wss", "127.0.0.1", ing.port, PATH), None)
    await ws.send(auth_stream)
    with pytest.raises((TimeoutError, WsError, ConnectionError)):
        async with asyncio.timeout(3):
            while await ws.recv() is not None:
                pass
            raise ConnectionError
    ws.abort()
    # The live connection was not replaced by the replay.
    assert a.t._conns[link.link_id] is conn and not conn.dead


async def test_wss_edge_tls_is_verified(tmp_path, make_node, ingresses, evil):
    cert, key = _cert_for_localhost(tmp_path, "edge")
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(cert, key)
    trusting = ssl.create_default_context(cafile=cert)
    a = make_node("alice")
    ing = await open_ingress(a, ingresses)
    mitm, mport = await evil(ing.port, edge_ctx=server_ctx)

    # A joiner whose trust store doesn't know the edge refuses it outright.
    b_strict = make_node("bob1", relay_edge_ssl=ssl.create_default_context())
    with pytest.raises(PairingFailed, match="relay TLS failed"):
        await b_strict.t.pair(relay_code(a, mport))
    # One that trusts it pairs (and the pinned inner TLS still runs).
    b = make_node("bob2", relay_edge_ssl=trusting)
    link = await b.t.pair(relay_code(a, mport))
    assert b.store.get(link.link_id).peer_pub == a.ident.pub.hex()


# -- the public endpoint: scanning ------------------------------------------------

SCANS = [
    b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /robots.txt HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /api/peer HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /api/sessions HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /.env HTTP/1.1\r\n\r\n",
    b"HEAD / HTTP/1.1\r\n\r\n",
    b"OPTIONS * HTTP/1.1\r\n\r\n",
    b"TRACE / HTTP/1.1\r\n\r\n",
    b"CONNECT 127.0.0.1:8765 HTTP/1.1\r\n\r\n",
    b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n",
    b"GET " + PATH.encode() + b" HTTP/1.0\r\n\r\n",
    b"GET " + PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n",  # no upgrade
    b"POST " + PATH.encode() + b" HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc",
    b"PUT " + PATH.encode() + b" HTTP/1.1\r\n\r\n",
    b"DELETE " + PATH.encode() + b" HTTP/1.1\r\n\r\n",
    upgrade_head(PATH + "/"),
    upgrade_head(PATH[:-1]),
    upgrade_head(PATH + "?x=1"),
    upgrade_head(PATH.upper()),
    upgrade_head("/" + PATH),
    upgrade_head("http://x" + PATH),
    upgrade_head(PATH, key="short"),
    upgrade_head(PATH, key="!" * 24),
    upgrade_head(PATH).replace(b"Version: 13", b"Version: 8"),
    upgrade_head(PATH).replace(b"Upgrade: websocket", b"Upgrade: h2c"),
    upgrade_head(PATH, extra="Upgrade: websocket\r\n"),  # duplicate
    upgrade_head(PATH, extra="Transfer-Encoding: chunked\r\n"),
    upgrade_head(PATH, extra="Content-Length: 5\r\n"),
    upgrade_head(PATH, extra=" folded\r\n"),  # obs-fold
    upgrade_head(PATH, extra="Bad Name: x\r\n"),
    upgrade_head(PATH, extra="X-Ctl: a\x01b\r\n"),
    upgrade_head(PATH, extra="NoColon\r\n"),
    upgrade_head(PATH).replace(b"\r\n", b"\n"),  # bare LF never completes
    b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + os.urandom(64),  # TLS hello
    b"\x00" * 100 + b"\r\n\r\n",
    b"GET " + b"/a" * 3000 + b" HTTP/1.1\r\n\r\n",  # oversized head
]


async def test_scanning_gets_one_constant_404(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses, head_timeout=0.5)
    for req in SCANS:
        resp = await http_exchange(ing.port, req)
        # The same bytes for every probe (or a silent close on garbage that
        # never forms a head): no banner, no hint which part was wrong.
        assert resp in (NOT_FOUND, b""), (req[:60], resp[:120])
    assert ing.stats["upgraded"] == 0
    assert a.t._unauth == 0 and not a.t._ip_windows
    assert a.handler.calls == [] and a.store.list() == []


async def test_valid_path_without_upgrade_looks_like_any_other_404(
    make_node, ingresses
):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses)
    good_path = await http_exchange(
        ing.port, b"GET " + PATH.encode() + b" HTTP/1.1\r\n\r\n"
    )
    bad_path = await http_exchange(ing.port, b"GET /nope HTTP/1.1\r\n\r\n")
    assert good_path == bad_path == NOT_FOUND


async def test_slow_head_is_cut_off(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses, head_timeout=0.3)
    reader, writer = await asyncio.open_connection("127.0.0.1", ing.port)
    writer.write(b"GET " + PATH.encode())
    await writer.drain()
    assert await closes_soon(reader, 2.0)
    writer.transport.abort()


async def test_pending_connection_cap(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses, max_pending=3, head_timeout=1.5)
    idle = [await asyncio.open_connection("127.0.0.1", ing.port) for _ in range(3)]
    await asyncio.sleep(0.1)
    resp = await http_exchange(ing.port, upgrade_head(), timeout=2)
    assert resp == b""  # dropped before a byte is read
    assert ing.stats["dropped"] >= 1
    for _, w in idle:
        w.transport.abort()
    await asyncio.sleep(0.1)
    reader, writer = await raw_ws(ing.port)  # capacity is back
    writer.transport.abort()


async def test_upgraded_connection_cap(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses, max_upgraded=2)
    held = [await raw_ws(ing.port) for _ in range(2)]
    assert await http_exchange(ing.port, upgrade_head()) == NOT_FOUND
    for _, w in held:
        w.transport.abort()


async def test_ingress_binds_loopback_only():
    with pytest.raises(ValueError):
        RelayIngress(lambda *a: None, PATH, host="0.0.0.0")
    with pytest.raises(ValueError):
        RelayIngress(lambda *a: None, "/../etc")


async def test_closed_relay_drops_the_inner_stream(make_node, ingresses):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    ing = await open_ingress(a, ingresses)
    code = relay_code(a, ing.port)
    a.t.close_relay()
    with pytest.raises(PairingFailed):
        await b.t.pair(code)
    assert a.store.list() == [] and a.invites.active() != []


async def test_direct_listener_does_not_accept_relay_and_vice_versa(
    make_node, ingresses
):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    await a.listen()
    ing = RelayIngress(a.t.accept_relayed, PATH)  # relay NOT opened on A
    await ing.start()
    ingresses.append(ing)
    with pytest.raises(PairingFailed):
        await b.t.pair(relay_code(a, ing.port))
    await a.t.stop_listener()
    a.t.open_relay()
    link = await b.t.pair(relay_code(a, ing.port))
    assert link.peer_addr.startswith("wss://")


# -- garbage after the upgrade -----------------------------------------------------

_LONG = (2**63).to_bytes(8, "big")
GARBAGE_FRAMES = [
    relay.encode_frame(relay.OP_TEXT, b"hello", mask=True),  # text: refused
    relay.encode_frame(relay.OP_BIN, b"\x16\x03", mask=False),  # unmasked
    bytes([0x80 | 0x40 | relay.OP_BIN, 0x80 | 1]) + b"mask" + b"x",  # RSV1
    bytes([0x80 | 0x3, 0x80]) + b"mask",  # reserved opcode
    bytes([0x80 | relay.OP_BIN, 0x80 | 127]) + _LONG + b"mask",  # 2^63
    bytes([0x80 | relay.OP_BIN, 0x80 | 127]) + (10 << 20).to_bytes(8, "big"),
    bytes([0x80 | relay.OP_BIN, 0x80 | 126]) + (5).to_bytes(2, "big") + b"mask",
    bytes([relay.OP_PING, 0x80]) + b"mask",  # fragmented control
    bytes([0x80 | relay.OP_PING, 0x80 | 126]) + (200).to_bytes(2, "big"),
    relay.encode_frame(relay.OP_CONT, b"x", mask=True),  # no message to continue
    relay.encode_frame(relay.OP_CLOSE, b"\x03", mask=True),  # 1-byte close
    relay.encode_frame(relay.OP_BIN, b"x", mask=True, fin=False)
    + relay.encode_frame(relay.OP_BIN, b"y", mask=True),  # new msg mid-fragment
    relay.encode_frame(relay.OP_PING, b"", mask=True) * (relay.CONTROL_RATE + 5),
    relay.encode_frame(relay.OP_BIN, b"", mask=True) * (relay.CONTROL_RATE + 5),
    relay.encode_frame(relay.OP_BIN, os.urandom(200), mask=True),  # not TLS
]


@pytest.mark.parametrize("frames", GARBAGE_FRAMES)
async def test_garbage_frames_close_the_connection(make_node, ingresses, frames):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    ing = await open_ingress(a, ingresses)
    reader, writer = await raw_ws(ing.port)
    writer.write(frames)
    await writer.drain()
    assert await closes_soon(reader, 4.0)
    writer.transport.abort()
    assert a.store.list() == [] and a.handler.calls == []
    # The ingress (and A) shrug it off: a real pairing works right after.
    link = await b.t.pair(relay_code(a, ing.port))
    assert a.store.get(link.link_id) is not None


async def test_oversized_frame_is_refused_without_reading_it(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses, max_payload=1024)
    reader, writer = await raw_ws(ing.port)
    writer.write(bytes([0x80 | relay.OP_BIN, 0x80 | 126]) + (2048).to_bytes(2, "big"))
    await writer.drain()
    assert await closes_soon(reader, 2.0)
    writer.transport.abort()


async def test_ping_is_answered_with_pong(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses)
    reader, writer = await raw_ws(ing.port)
    writer.write(relay.encode_frame(relay.OP_PING, b"abc", mask=True))
    await writer.drain()
    fin, op, data = await asyncio.wait_for(relay.read_frame(reader, masked=False), 3)
    assert (fin, op, data) == (True, relay.OP_PONG, b"abc")
    writer.transport.abort()


# -- per-source limits through the relay ----------------------------------------------


def test_source_from_cf_connecting_ip_only_when_trusted():
    head = upgrade_head(PATH, extra="Cf-Connecting-Ip: 203.0.113.9\r\n")
    trusted = RelayIngress(lambda *a: None, PATH, trust_cf_ip=True)
    untrusted = RelayIngress(lambda *a: None, PATH, trust_cf_ip=False)
    assert trusted._check_upgrade(head)[1] == "203.0.113.9"
    assert untrusted._check_upgrade(head)[1] == "relay"
    bogus = upgrade_head(PATH, extra="Cf-Connecting-Ip: not-an-ip\r\n")
    assert trusted._check_upgrade(bogus)[1] == "relay"
    dup = upgrade_head(
        PATH, extra="Cf-Connecting-Ip: 1.1.1.1\r\nCf-Connecting-Ip: 2.2.2.2\r\n"
    )
    with pytest.raises(WsError):
        trusted._check_upgrade(dup)


async def test_relayed_sources_are_rate_limited_per_ip(make_node, ingresses):
    a = make_node("alice", conn_rate_per_ip=2)
    ing = await open_ingress(a, ingresses, trust_cf_ip=True)
    for ip in ("198.51.100.1",) * 3 + ("198.51.100.2",):
        r, w = await raw_ws(ing.port, extra=f"Cf-Connecting-Ip: {ip}\r\n")
        await asyncio.sleep(0.05)
        w.transport.abort()
    windows = a.t._ip_windows
    assert set(windows) == {"relay:198.51.100.1", "relay:198.51.100.2"}
    assert len(windows["relay:198.51.100.1"].hits) == 2  # capped; 3rd refused


# -- the dialer against a hostile relay ---------------------------------------------


async def _fake_relay(respond: bytes, after: bytes = b""):
    async def cb(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(respond + after)
            await writer.drain()
            await asyncio.sleep(2)
        except Exception:
            pass
        finally:
            writer.transport.abort()

    server = await asyncio.start_server(cb, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.parametrize(
    "respond,match",
    [
        (b"HTTP/1.1 404 Not Found\r\n\r\n", "HTTP 404"),
        (b"HTTP/1.1 530 x\r\n\r\n", "HTTP 530"),
        (b"SSH-2.0-OpenSSH\r\n\r\n", "bad response"),
        (
            b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: AAAAAAAAAAAAAAAAAAAAAAAAAAA=\r\n\r\n",
            "bad WebSocket",
        ),
        (b"HTTP/1.1 101 OK\r\nConnection: Upgrade\r\n\r\n", "bad WebSocket"),
        (b"HTTP/1.1 101 OK\r\nX: " + b"a" * 9000 + b"\r\n\r\n", "bad response"),
        (b"HTTP/1.1 101 OK\r\nX: \x00\r\n\r\n", "bad response"),
    ],
)
async def test_dialer_rejects_bad_upgrade_responses(respond, match):
    server, port = await _fake_relay(respond)
    try:
        with pytest.raises(RelayError, match=match):
            await relay.open_ws(PeerAddr("wss", "127.0.0.1", port, PATH), None)
    finally:
        server.close()


async def test_dialer_rejects_extensions_even_with_valid_accept():
    async def cb(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        key = [
            l.split(b": ", 1)[1].decode()
            for l in head.split(b"\r\n")
            if l.lower().startswith(b"sec-websocket-key")
        ][0]
        writer.write(
            b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Extensions: permessage-deflate\r\n"
            b"Sec-WebSocket-Accept: " + relay.accept_key(key).encode() + b"\r\n\r\n"
        )
        await writer.drain()

    server = await asyncio.start_server(cb, "127.0.0.1", 0)
    try:
        with pytest.raises(RelayError, match="bad WebSocket"):
            await relay.open_ws(
                PeerAddr("wss", "127.0.0.1", server.sockets[0].getsockname()[1], PATH),
                None,
            )
    finally:
        server.close()


@pytest.mark.parametrize(
    "after",
    [
        relay.encode_frame(relay.OP_BIN, b"\x16\x03\x03\x00\x01\x00", mask=True),
        relay.encode_frame(relay.OP_TEXT, b"hi", mask=False),
        b"\xff" * 64,
    ],
)
async def test_pairing_through_a_garbage_relay_fails_closed(make_node, after):
    async def cb(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            key = [
                l.split(b": ", 1)[1].decode()
                for l in head.split(b"\r\n")
                if l.lower().startswith(b"sec-websocket-key")
            ][0]
            writer.write(
                b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: "
                + relay.accept_key(key).encode()
                + b"\r\n\r\n"
                + after
            )
            await writer.drain()
            await asyncio.sleep(3)
        except Exception:
            pass
        finally:
            writer.transport.abort()

    server = await asyncio.start_server(cb, "127.0.0.1", 0)
    a = make_node("alice")
    b = make_node("bob", relay_edge_ssl=None)
    code = a.invites.create(
        "127.0.0.1", server.sockets[0].getsockname()[1], relay_path=PATH
    ).code
    try:
        with pytest.raises(PairingFailed):
            await b.t.pair(code)
        assert b.store.list() == []
    finally:
        server.close()


# -- framing: properties ---------------------------------------------------------------


def _reader_with(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    op=st.sampled_from([relay.OP_BIN, relay.OP_CONT, relay.OP_PING, relay.OP_CLOSE]),
    payload=st.binary(max_size=70_000),
    mask=st.booleans(),
)
def test_frame_roundtrip(op, payload, mask):
    if op >= 0x8:
        payload = payload[:125]

    async def go():
        return await relay.read_frame(
            _reader_with(relay.encode_frame(op, payload, mask=mask)),
            masked=mask,
            max_payload=1 << 20,
        )

    assert asyncio.run(go()) == (True, op, payload)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.binary(max_size=300), masked=st.booleans())
def test_frame_parser_never_misbehaves_on_garbage(data, masked):
    async def go():
        try:
            fin, op, payload = await relay.read_frame(
                _reader_with(data), masked=masked, max_payload=4096
            )
        except (WsError, asyncio.IncompleteReadError):
            return
        assert op in (0x0, 0x2, 0x8, 0x9, 0xA)
        assert len(payload) <= 4096

    asyncio.run(go())


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.binary(max_size=2000))
def test_upgrade_check_rejects_arbitrary_heads(data):
    ing = RelayIngress(lambda *a: None, PATH)
    with pytest.raises(WsError):
        ing._check_upgrade(data + b"\r\n\r\n")


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    path=st.text(max_size=80),
    extra=st.lists(
        st.tuples(
            st.text("abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=20),
            st.text(max_size=40),
        ),
        max_size=6,
    ),
)
def test_upgrade_check_only_accepts_the_exact_path(path, extra):
    ing = RelayIngress(lambda *a: None, PATH)
    lines = "".join(f"{k}: {v}\r\n" for k, v in extra)
    head = upgrade_head(path.replace("\r", "").replace("\n", ""), lines)
    try:
        ing._check_upgrade(head)
    except (WsError, UnicodeEncodeError):
        return
    assert path == PATH


async def test_full_size_frames_pass_a_small_read_limit():
    """The ingress reads with a small StreamReader limit (it bounds the
    request head); frames up to MAX_WS_PAYLOAD must still get through."""
    got = asyncio.get_running_loop().create_future()

    async def cb(reader, writer):
        ws = relay.WsStream(reader, writer, client=False)
        buf = bytearray()
        while len(buf) < relay.MAX_WS_PAYLOAD:
            data = await ws.recv()
            if data is None:
                break
            buf += data
        got.set_result(bytes(buf))
        writer.transport.abort()

    server = await asyncio.start_server(cb, "127.0.0.1", 0, limit=2 * relay.MAX_HEAD)
    try:
        r, w = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        payload = os.urandom(relay.MAX_WS_PAYLOAD)
        w.write(relay.encode_frame(relay.OP_BIN, payload, mask=True))
        await w.drain()
        assert await asyncio.wait_for(got, 5) == payload
        w.transport.abort()
    finally:
        server.close()


async def test_large_data_from_the_dialer_side(make_node, ingresses, evil):
    a, b = make_node("alice"), make_node("bob", relay_edge_ssl=None)
    link, mitm, ing = await relay_paired(a, b, ingresses, evil)
    content = "z" * 900_000

    async def behavior(link_, op, p):
        return {
            "path": p["path"],
            "size": len(content),
            "encoding": "utf-8",
            "content": content,
            "truncated": False,
        }

    b.handler.behavior = behavior
    res = await a.t.request(link.link_id, "read_file", {"path": "big.txt"})
    assert res["content"] == content


# -- a brand-new tunnel name that doesn't resolve yet ------------------------------


def _fake_dns(monkeypatch, name: str, fail_times: int):
    """Make ``name`` fail to resolve ``fail_times`` times, then point at
    127.0.0.1 (the loop's resolver is what asyncio connections use)."""
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo
    calls = {"n": 0}

    async def fake(host, port, *a, **kw):
        if host == name:
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise __import__("socket").gaierror(-2, "Name or service not known")
            host = "127.0.0.1"
        return await real(host, port, *a, **kw)

    monkeypatch.setattr(loop, "getaddrinfo", fake)
    return calls


async def test_pairing_waits_for_a_new_relay_name(make_node, ingresses, monkeypatch):
    monkeypatch.setattr(transport, "RELAY_DNS_RETRY_S", 0.05)
    a = make_node("alice")
    b = make_node("bob", relay_edge_ssl=None, relay_dns_wait=5.0)
    ing = await open_ingress(a, ingresses)
    calls = _fake_dns(monkeypatch, "fresh-tunnel.trycloudflare.com", 3)
    code = a.invites.create(
        "fresh-tunnel.trycloudflare.com", ing.port, relay_path=PATH
    ).code
    link = await b.t.pair(code)
    assert calls["n"] >= 4
    assert b.store.get(link.link_id).peer_addr.startswith(
        "wss://fresh-tunnel.trycloudflare.com:"
    )


async def test_a_name_that_never_resolves_fails_clearly_and_keeps_the_invite(
    make_node, ingresses, monkeypatch
):
    monkeypatch.setattr(transport, "RELAY_DNS_RETRY_S", 0.05)
    a = make_node("alice")
    b = make_node("bob", relay_edge_ssl=None, relay_dns_wait=0.3)
    ing = await open_ingress(a, ingresses)
    _fake_dns(monkeypatch, "gone.trycloudflare.com", 10**6)
    code = a.invites.create("gone.trycloudflare.com", ing.port, relay_path=PATH).code
    with pytest.raises(PairingFailed, match="does not resolve"):
        await b.t.pair(code)
    assert len(a.invites.active()) == 1  # untouched: try again later


async def test_404_survives_an_unread_request_body(make_node, ingresses):
    a = make_node("alice")
    ing = await open_ingress(a, ingresses)
    body = b"x" * 200_000
    req = (
        b"POST " + PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    for _ in range(5):
        assert await http_exchange(ing.port, req) == NOT_FOUND
