"""Fixtures for the peer-link network tests: real PeerTransport instances on
127.0.0.1, each with its own identity/store under ``tmp_path``, plus a raw
TLS client for speaking the wire protocol adversarially."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sys
from dataclasses import dataclass, field

import pytest

from backend.peer import identity, invite, store, transport, wire

# The shared-folder runtime (bubblewrap sandbox, egress proxy + in-sandbox
# bridge, the agent API and the share's file access) only ever runs on Linux:
# elsewhere sharing a folder is refused before any of it starts. Its tests
# lean on Linux facilities (/proc/self/fd, O_PATH, memfd, SO_PEERCRED).
_LINUX_ONLY = {
    "test_sandbox.py",
    "test_egress.py",
    "test_bridge.py",
    "test_long_socket_paths.py",
    "test_agent_api.py",
    "test_share.py",
    "test_share_attacks.py",
}


def pytest_collection_modifyitems(config, items):
    if sys.platform.startswith("linux"):
        return
    skip = pytest.mark.skip(reason="peer shared-folder runtime is Linux-only")
    for item in items:
        if item.path.parent.name == "peer" and item.path.name in _LINUX_ONLY:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def peer_home(tmp_path, monkeypatch):
    home = tmp_path / "peer-home"
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(home))
    return home


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


DEFAULT_RESPONSES = {
    "msg": lambda p: {"accepted": True},
    "diff": lambda p: {
        "stat": [{"path": "a.txt", "added": 1, "removed": 0}],
        "diff": "+hi\n",
        "truncated": False,
    },
    "read_file": lambda p: {
        "path": p["path"],
        "size": 2,
        "encoding": "utf-8",
        "content": "hi",
        "truncated": False,
    },
    "list_files": lambda p: {"files": ["a.txt"], "truncated": False},
    "status": lambda p: {"shared": True, "agent": "none", "name": "x"},
}


class Handler:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.links: list = []
        self.states: list[tuple[str, bool]] = []
        self.added: list = []
        self.behavior = None  # optional async (link, op, p) -> dict

    async def handle_request(self, link, op, p):
        self.calls.append((link.link_id, op, p))
        self.links.append(link)
        if self.behavior is not None:
            return await self.behavior(link, op, p)
        return DEFAULT_RESPONSES[op](p)

    def on_link_added(self, link):
        self.added.append(link)

    def on_state(self, link_id, connected):
        self.states.append((link_id, connected))


FAST = dict(
    handshake_timeout=2.0,
    request_timeout=5.0,
    ping_interval=30.0,
    idle_timeout=30.0,
    backoff_initial=0.05,
    backoff_max=0.2,
)


@dataclass
class Node:
    name: str
    ident: identity.Identity
    store: store.LinkStore
    invites: invite.InviteBook
    handler: Handler
    t: transport.PeerTransport
    clock: FakeClock
    port: int | None = None
    extra: dict = field(default_factory=dict)

    async def listen(self) -> int:
        _, self.port = await self.t.start_listener("127.0.0.1", 0)
        return self.port

    def code(self, ttl_s: float = 600) -> str:
        assert self.port, "listen() first"
        return self.invites.create("127.0.0.1", self.port, ttl_s).code


@pytest.fixture
async def make_node(tmp_path):
    nodes: list[Node] = []

    def make(
        name: str, *, invite_kw: dict | None = None, reuse: Node | None = None, **kw
    ) -> Node:
        """A fresh node, or (``reuse=``) a restarted one with the same
        identity, store, invites, handler and clock."""
        if reuse is not None:
            ident, st, invites, handler, clock = (
                reuse.ident,
                reuse.store,
                reuse.invites,
                reuse.handler,
                reuse.clock,
            )
        else:
            base = tmp_path / name
            ident = identity.load_or_create(str(base / "identity"))
            st = store.LinkStore(str(base / "links.json"))
            clock = FakeClock()
            invites = invite.InviteBook(
                ident.fingerprint(), **{"clock": clock, **(invite_kw or {})}
            )
            handler = Handler()
        t = transport.PeerTransport(
            ident, st, invites, name, handler, **{"clock": clock, **FAST, **kw}
        )
        node = Node(name, ident, st, invites, handler, t, clock)
        nodes.append(node)
        return node

    yield make
    for n in nodes:
        await n.t.close()


async def eventually(cond, timeout: float = 3.0, interval: float = 0.01):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


async def paired(a: Node, b: Node):
    """B joins A's invite; returns the dialer-side Link once both are up."""
    if a.port is None:
        await a.listen()
    link = await b.t.pair(a.code())
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id)
    )
    return link


# -- raw adversarial client ---------------------------------------------------


@dataclass
class Raw:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    server_pub: bytes
    nonce: bytes | None = None
    link_id: str | None = None
    ident: identity.Identity | None = None

    async def send(self, obj: dict) -> None:
        self.writer.write(wire.encode(obj))
        await self.writer.drain()

    async def send_bytes(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, timeout: float = 3.0) -> dict:
        return await asyncio.wait_for(wire.read_frame(self.reader), timeout)

    def close(self) -> None:
        self.writer.transport.abort()


async def tls_connect(port: int, ctx=None) -> Raw:
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=ctx or transport.client_ssl_context()
    )
    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    return Raw(reader, writer, identity.pub_from_cert_der(der))


async def raw_hello(port: int) -> Raw:
    raw = await tls_connect(port)
    raw.nonce = wire.validate_hello(await raw.recv())
    return raw


async def raw_pair(
    node: Node, ident: identity.Identity, *, code: str | None = None
) -> tuple[Raw, dict]:
    info = invite.parse_code(code or node.code())
    raw = await raw_hello(node.port)
    tr = transport.pair_transcript(raw.server_pub, raw.nonce, ident.pub)
    await raw.send(
        {
            "t": "pair",
            "v": 1,
            "invite_id": info.invite_id,
            "pub": ident.pub.hex(),
            "name": "raw",
            "proof": hmac.new(info.secret, tr, hashlib.sha256).hexdigest(),
            "sig": ident.sign(tr).hex(),
        }
    )
    reply = await raw.recv()
    raw.ident = ident
    raw.link_id = reply.get("link_id")
    return raw, reply


async def raw_auth(
    node: Node, ident: identity.Identity, link_id: str, *, sig: bytes | None = None
) -> tuple[Raw, dict]:
    raw = await raw_hello(node.port)
    tr = transport.auth_transcript(raw.server_pub, raw.nonce, link_id)
    sig = sig if sig is not None else ident.sign(tr)
    await raw.send({"t": "auth", "v": 1, "link_id": link_id, "sig": sig.hex()})
    raw.ident, raw.link_id = ident, link_id
    return raw, await raw.recv()


async def authed_raw(node: Node, tmp_path, name: str = "rawpeer") -> Raw:
    """A raw client that has paired with ``node`` (so it holds a live,
    authenticated connection)."""
    if node.port is None:
        await node.listen()
    ident = identity.load_or_create(str(tmp_path / name / "identity"))
    raw, reply = await raw_pair(node, ident)
    assert reply["t"] == "welcome"
    assert await eventually(lambda: node.t.is_connected(raw.link_id))
    return raw


async def closed_by_peer(reader: asyncio.StreamReader, timeout: float = 3.0) -> bool:
    """True if the other side closes the connection within ``timeout``
    (frames received before the close are drained and ignored)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    try:
        while True:
            left = end - loop.time()
            if left <= 0:
                return False
            data = await asyncio.wait_for(reader.read(65536), left)
            if not data:
                return True
    except (TimeoutError, asyncio.TimeoutError):
        return False
    except (ConnectionError, OSError):
        return True


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
