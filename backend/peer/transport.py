"""Peer transport: the TLS 1.3 listener/dialer and the pair/auth handshakes.

One :class:`PeerTransport` per server. The **inviter** listens; the **joiner**
dials, keeps the connection up and reconnects with backoff. Once a connection
is authenticated, both sides send requests over it (``req``/``res``
multiplexed by id) and keep it alive with ``ping``.

Security properties (see ``docs/peer-link.md``):

* TLS 1.3 only, both directions. The dialer checks the server cert's raw
  Ed25519 key — against the code's fingerprint when pairing, against the
  pinned key when reconnecting — **before sending a single application byte**.
  TLS 1.3's CertificateVerify proves the server holds that key.
* The client authenticates at the application layer by signing a transcript
  that binds the server's key and a fresh server nonce (no replay, no relay).
* Every failure on the server side gets the same ``{"t":"denied"}``.
* Unauthenticated connections are capped (count, per-IP rate) and dropped
  before TLS; the whole handshake has a deadline; pairing attempts are rate
  limited globally (in :class:`InviteBook`).
* Every inbound frame is validated by :mod:`wire` before it is acted on; an
  invalid request closes the connection, a well-formed request for an op this
  version doesn't know is answered ``err:"unsupported"`` and the connection
  stays up (a newer peer). Permissions are enforced here, per link, from the
  store (so a revoked link or changed perms apply at once).
* Pairing again with someone already linked (the same pinned key, same role)
  reuses that link — its id, permissions and shared folder — and draws a new
  safety number, instead of minting a duplicate that would hold nothing.
* Message text, codes, secrets and proofs are never logged.

Two carriers bring the TLS stream (see :mod:`backend.peer.addr`): a direct
TCP connection to :meth:`PeerTransport.start_listener`, or a WebSocket relay
(:mod:`backend.peer.relay`): the dialer reaches ``wss://…`` addresses through
:func:`relay.dial`, and the inviter's relay ingress hands each inbound stream
to :meth:`PeerTransport.accept_relayed`. Everything above (TLS 1.3, key
pinning, the handshake, limits) runs unchanged on both; the relay is just
another untrusted network path.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import ipaddress
import logging
import secrets
import socket
import ssl
import time
from collections import deque
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.peer import relay, wire
from backend.peer.addr import PeerAddr, parse_addr
from backend.peer.identity import Identity, fingerprint_of, pub_from_cert_der
from backend.peer.invite import InviteBook, parse_code
from backend.peer.store import Link, LinkStore, sanitize_name

__all__ = [
    "PeerTransport",
    "PeerError",
    "PeerOpError",
    "PeerUnavailable",
    "PairingFailed",
    "IdentityMismatch",
    "MITM_MESSAGE",
    "pair_transcript",
    "auth_transcript",
    "compute_sas",
    "server_ssl_context",
    "client_ssl_context",
]

log = logging.getLogger(__name__)

PAIR_CONTEXT = b"mfpeer-pair-v1"
AUTH_CONTEXT = b"mfpeer-auth-v1"
SAS_CONTEXT = b"mfpeer-sas-v1"
MITM_MESSAGE = "peer identity changed: possible MITM"
UNLINK_REASON = "unlinked"  # a bye with this reason revokes the link on both sides

HANDSHAKE_TIMEOUT = 10.0
REQUEST_TIMEOUT = 60.0
PING_INTERVAL = 30.0
IDLE_TIMEOUT = 90.0
WRITE_TIMEOUT = 30.0
MAX_UNAUTH = 16
CONN_RATE_PER_IP = 30
CONN_RATE_WINDOW = 60.0
MAX_TRACKED_IPS = 4096
MAX_IN_FLIGHT = 8
MSG_RATE = 30
REQ_RATE = 120
FRAME_RATE = 600  # any inbound frame, per connection: caps ping/res floods
RATE_WINDOW = 60.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
STABLE_CONN_S = 30.0
LINK_IDLE_EXPIRY_S = 30 * 86400

_EDGE_DEFAULT = object()  # relay_edge_ssl default: WebPKI-verified TLS
# A brand-new quick-tunnel name can take a while to appear in DNS, and
# trycloudflare.com caches NXDOMAIN for 60 s: before pairing through a relay,
# wait (up to this long) for its name to resolve. The invite isn't touched.
RELAY_DNS_WAIT = 75.0
RELAY_DNS_RETRY_S = 5.0

# Which link permission each inbound op needs (None: always allowed).
_OP_PERM = {
    "msg": "messages",
    "diff": "diff",
    "read_file": "read_file",
    "list_files": "read_file",
    "status": None,
}


class PeerError(Exception):
    pass


class PeerOpError(PeerError):
    """The op failed (``ok:false``). Handlers raise it to refuse an op; the
    message goes to the peer, so keep it short and free of secrets."""


class PeerUnavailable(PeerError):
    """Not connected, connection lost, or the request timed out."""


class PairingFailed(PeerError):
    pass


class IdentityMismatch(PairingFailed):
    def __init__(self, msg: str = MITM_MESSAGE):
        super().__init__(msg)


# -- crypto helpers ----------------------------------------------------------


def pair_transcript(server_pub: bytes, nonce: bytes, client_pub: bytes) -> bytes:
    return PAIR_CONTEXT + server_pub + nonce + client_pub


def auth_transcript(server_pub: bytes, nonce: bytes, link_id: str) -> bytes:
    return AUTH_CONTEXT + server_pub + nonce + link_id.encode("ascii")


def compute_sas(server_pub: bytes, client_pub: bytes, nonce: bytes) -> str:
    """First 30 bits of the SAS hash as ``ddd-ddd-ddd-d``."""
    h = hashlib.sha256(SAS_CONTEXT + server_pub + client_pub + nonce).digest()
    n = int.from_bytes(h[:4], "big") >> 2
    s = f"{n:010d}"
    return f"{s[0:3]}-{s[3:6]}-{s[6:9]}-{s[9]}"


def server_ssl_context(identity: Identity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_NONE  # clients authenticate in the app layer
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    ctx.num_tickets = 0  # no resumption: every connection presents the cert
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we pin the raw key ourselves
    return ctx


def _fmt_addr(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _abort(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    with contextlib.suppress(Exception):
        writer.transport.abort()


def _short(link_id: str) -> str:
    return link_id[:8]


def _rate_key(ip: str) -> str:
    """The per-source rate-limit bucket: an IPv6 client owns a whole /64 (so
    it can't rotate addresses to dodge the limit); an IPv4-mapped address is
    its IPv4 address (else every v4 client of a dual-stack listener would
    share one bucket). Relayed sources (``relay:<ip>``) get their own
    buckets, keyed the same way."""
    if ip.startswith("relay:"):
        return "relay:" + _rate_key(ip[6:])
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(addr) >> 64 << 64, 64)))
    return str(addr)


async def _send_raw(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write(wire.encode(obj))
    await writer.drain()


class _Window:
    """Sliding-window counter. A refused hit is still recorded (up to the
    limit), so a client hammering the limit stays refused."""

    def __init__(self, limit: int, window: float, clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: deque[float] = deque(maxlen=max(1, limit))

    def hit(self) -> bool:
        now = self.clock()
        while self.hits and self.hits[0] <= now - self.window:
            self.hits.popleft()
        allowed = len(self.hits) < self.limit
        self.hits.append(now)
        return allowed

    def idle(self) -> bool:
        return not self.hits or self.hits[-1] <= self.clock() - self.window


@dataclass
class _LinkLimits:
    msg: _Window
    req: _Window


# -- one authenticated connection ---------------------------------------------


class _Conn:
    def __init__(self, owner: "PeerTransport", link_id: str, reader, writer):
        self.owner = owner
        self.link_id = link_id
        self.reader = reader
        self.writer = writer
        self.closed = asyncio.Event()
        self.started = time.monotonic()
        self.dead = False
        self._wlock = asyncio.Lock()
        self._out_sem = asyncio.Semaphore(owner.max_in_flight)
        self._frames = _Window(owner.frame_rate, owner.rate_window, owner.clock)
        self._pending: dict[int, tuple[asyncio.Future, str, dict]] = {}
        self._inbound: dict[int, asyncio.Task] = {}
        self._next_id = 0
        self._main: asyncio.Task | None = None
        self._pinger: asyncio.Task | None = None

    def start(self) -> None:
        self._main = asyncio.create_task(
            self._run(), name=f"peer-conn-{_short(self.link_id)}"
        )
        self._pinger = asyncio.create_task(
            self._ping_loop(), name=f"peer-ping-{_short(self.link_id)}"
        )

    async def send(self, obj: dict) -> None:
        data = wire.encode(obj)
        async with self._wlock:
            if self.dead:
                raise PeerUnavailable("connection closed")
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), self.owner.write_timeout)

    async def request(self, op: str, p: dict, timeout: float) -> dict:
        try:
            async with asyncio.timeout(timeout):
                async with self._out_sem:
                    if self.dead:
                        raise PeerUnavailable("peer not connected")
                    self._next_id = self._next_id % wire.MAX_ID + 1
                    rid = self._next_id
                    fut = asyncio.get_running_loop().create_future()
                    self._pending[rid] = (fut, op, p)
                    try:
                        try:
                            await self.send({"t": "req", "id": rid, "op": op, "p": p})
                        except (
                            OSError,
                            TimeoutError,
                            wire.ProtocolError,
                            ssl.SSLError,
                        ):
                            self.teardown("write failed")
                            raise PeerUnavailable("connection lost") from None
                        return await fut
                    finally:
                        self._pending.pop(rid, None)
        except TimeoutError:
            raise PeerUnavailable("request timed out") from None

    async def close(self, reason: str = "", send_bye: bool = False) -> None:
        if self.dead:
            return
        if send_bye:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.send(
                        {"t": "bye", "reason": wire.clip_text(reason, wire.MAX_BYE)}
                    ),
                    2.0,
                )
        self.teardown(reason)

    def teardown(self, reason: str = "") -> None:
        if self.dead:
            return
        self.dead = True
        log.info(
            "peer: link %s disconnected (%s)", _short(self.link_id), reason or "closed"
        )
        current = asyncio.current_task()
        for task in (self._main, self._pinger, *self._inbound.values()):
            if task is not None and task is not current:
                task.cancel()
        for fut, _, _ in self._pending.values():
            if not fut.done():
                fut.set_exception(PeerUnavailable("connection lost"))
        self._pending.clear()
        with contextlib.suppress(Exception):
            self.writer.close()
        # A graceful TLS close can stall on a dead peer; abort shortly after.
        asyncio.get_running_loop().call_later(2.0, _abort, self.writer)
        self.owner._conn_closed(self)
        self.closed.set()

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.owner.ping_interval)
                # A link removed behind our back (another process, a race with
                # a handshake) must not live on through pings alone.
                if not self.owner._link_exists(self.link_id):
                    self.teardown("link removed")
                    return
                await self.send({"t": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.teardown("ping failed")

    async def _run(self) -> None:
        reason = "closed"
        try:
            while True:
                try:
                    obj = await asyncio.wait_for(
                        wire.read_frame(self.reader), self.owner.idle_timeout
                    )
                except TimeoutError:
                    reason = "idle timeout"
                    return
                if not self._frames.hit():
                    raise wire.ProtocolError("frame flood")
                try:
                    t = wire.validate_message(obj)
                except wire.UnsupportedOp as e:
                    # A newer peer asked for something we don't have: say so
                    # and keep the link (still counted against its rates).
                    self.owner._limits_for(self.link_id).req.hit()
                    await self._reply_err(e.rid, "unsupported")
                    continue
                if t == "ping":
                    await self.send({"t": "pong"})
                elif t == "bye":
                    reason = "peer said bye"
                    if obj["reason"] == UNLINK_REASON:
                        # Only reachable on a connection authenticated for
                        # THIS link: the peer can revoke its own link only.
                        reason = "peer unlinked"
                        self.owner._peer_unlinked(self.link_id)
                    return
                elif t == "req":
                    await self._on_req(obj)
                elif t == "res":
                    self._on_res(obj)
        except asyncio.CancelledError:
            raise
        except wire.ConnectionClosed:
            reason = "connection closed"
        except wire.ProtocolError as e:
            reason = f"protocol error: {e}"
            log.warning("peer: link %s protocol error: %s", _short(self.link_id), e)
        except (OSError, TimeoutError, ssl.SSLError, PeerUnavailable) as e:
            reason = type(e).__name__
        finally:
            self.teardown(reason)

    async def _reply_err(self, rid: int, err: str) -> None:
        await self.send(
            {
                "t": "res",
                "id": rid,
                "ok": False,
                "err": wire.clip_text(err, wire.MAX_ERR),
            }
        )

    async def _on_req(self, obj: dict) -> None:
        rid, op = obj["id"], obj["op"]
        if rid in self._inbound:
            raise wire.ProtocolError("duplicate request id")
        limits = self.owner._limits_for(self.link_id)
        if not limits.req.hit() or (op == "msg" and not limits.msg.hit()):
            await self._reply_err(rid, "rate limited")
            return
        if len(self._inbound) >= self.owner.max_in_flight:
            await self._reply_err(rid, "too many requests in flight")
            return
        task = asyncio.create_task(self._serve(rid, op, obj["p"]))
        self._inbound[rid] = task
        task.add_done_callback(lambda _t, rid=rid: self._inbound.pop(rid, None))

    async def _serve(self, rid: int, op: str, p: dict) -> None:
        owner = self.owner
        link = owner.store.get(self.link_id)
        if link is None:
            self.teardown("link removed")
            return
        perm = _OP_PERM[op]
        if perm is not None and not link.perms.get(perm, False):
            resp = {"t": "res", "id": rid, "ok": False, "err": "not permitted"}
        else:
            try:
                async with asyncio.timeout(owner.request_timeout):
                    result = await owner.handler.handle_request(link, op, p)
                wire.validate_response(op, result, p)
                resp = {"t": "res", "id": rid, "ok": True, "p": result}
            except asyncio.CancelledError:
                raise
            except PeerOpError as e:
                resp = {
                    "t": "res",
                    "id": rid,
                    "ok": False,
                    "err": wire.clip_text(str(e), wire.MAX_ERR),
                }
            except TimeoutError:
                resp = {"t": "res", "id": rid, "ok": False, "err": "timeout"}
            except wire.ProtocolError:
                log.warning("peer: handler returned an invalid %s response", op)
                resp = {"t": "res", "id": rid, "ok": False, "err": "internal error"}
            except Exception as e:
                log.warning("peer: handler failed on %s: %s", op, type(e).__name__)
                resp = {"t": "res", "id": rid, "ok": False, "err": "internal error"}
        try:
            try:
                await self.send(resp)
            except wire.ProtocolError:  # too large to frame
                await self._reply_err(rid, "response too large")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.teardown("write failed")

    def _on_res(self, obj: dict) -> None:
        entry = self._pending.pop(obj["id"], None)
        if entry is None:
            return  # unknown or timed-out id: ignore
        fut, op, req = entry
        if fut.done():
            return
        if obj["ok"]:
            try:
                p = wire.validate_response(op, obj["p"], req, strict=False)
            except wire.ProtocolError:
                fut.set_exception(PeerOpError("invalid response from peer"))
                raise
            fut.set_result(p)
        else:
            fut.set_exception(PeerOpError(obj["err"]))


# -- the transport -------------------------------------------------------------


class PeerTransport:
    """See the module docstring. ``handler`` provides
    ``async handle_request(link, op, p) -> dict`` and optionally
    ``on_link_added(link)``, ``on_link_repaired(link)`` (the same person
    paired again: same link, new safety number), ``on_link_removed(link)``
    (the peer unlinked) and ``on_state(link_id, connected)`` (sync or
    async)."""

    def __init__(
        self,
        identity: Identity,
        store: LinkStore,
        invites: InviteBook | None,
        display_name: str,
        handler,
        *,
        clock=time.monotonic,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        request_timeout: float = REQUEST_TIMEOUT,
        ping_interval: float = PING_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
        write_timeout: float = WRITE_TIMEOUT,
        max_unauth: int = MAX_UNAUTH,
        conn_rate_per_ip: int = CONN_RATE_PER_IP,
        conn_rate_window: float = CONN_RATE_WINDOW,
        max_in_flight: int = MAX_IN_FLIGHT,
        msg_rate: int = MSG_RATE,
        req_rate: int = REQ_RATE,
        frame_rate: int = FRAME_RATE,
        rate_window: float = RATE_WINDOW,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
        link_idle_expiry_s: float = LINK_IDLE_EXPIRY_S,
        relay_edge_ssl=_EDGE_DEFAULT,
        relay_dns_wait: float = RELAY_DNS_WAIT,
    ):
        self.identity = identity
        self.store = store
        self.invites = invites
        self.display_name = sanitize_name(display_name)
        self.handler = handler
        self.clock = clock
        self.handshake_timeout = handshake_timeout
        self.request_timeout = request_timeout
        self.ping_interval = ping_interval
        self.idle_timeout = idle_timeout
        self.write_timeout = write_timeout
        self.max_unauth = max_unauth
        self.conn_rate_per_ip = conn_rate_per_ip
        self.conn_rate_window = conn_rate_window
        self.max_in_flight = max_in_flight
        self.msg_rate = msg_rate
        self.req_rate = req_rate
        self.frame_rate = frame_rate
        self.rate_window = rate_window
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.link_idle_expiry_s = link_idle_expiry_s

        self._server_ctx = server_ssl_context(identity)
        self._client_ctx = client_ssl_context()
        # TLS to a relay's edge (outer layer, defense in depth only; None =
        # plain ws://, tests only). The pinned peer TLS runs inside either way.
        self._relay_edge_ssl = (
            relay.edge_ssl_context()
            if relay_edge_ssl is _EDGE_DEFAULT
            else relay_edge_ssl
        )
        self.relay_dns_wait = relay_dns_wait
        self._relay_open = False
        self._carriers: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self._conns: dict[str, _Conn] = {}
        self._supervisors: dict[str, asyncio.Task] = {}
        self._limits: dict[str, _LinkLimits] = {}
        self._ip_windows: dict[str, _Window] = {}
        self._handshakes: dict[asyncio.Task, bool] = {}  # task -> relayed
        # What each linked peer said about itself in its handshake ({"app",
        # "caps"}; empty from peers of this release), for display only.
        self._peer_info: dict[str, dict] = {}
        self._bg: set[asyncio.Task] = set()
        self._unauth = 0
        self._closed = False
        # A real key nobody holds: unknown link ids still cost one verify.
        self._dummy_pub = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )

    # -- public API ------------------------------------------------------------

    @property
    def listening(self) -> tuple[str, int] | None:
        if self._server is None or not self._server.sockets:
            return None
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def start_listener(self, host: str, port: int) -> tuple[str, int]:
        if self._closed:
            raise PeerError("transport is closed")
        if self._server is None:
            self._server = await asyncio.start_server(self._on_accept, host, port)
            log.info("peer: listening on %s", _fmt_addr(*self.listening))
        return self.listening

    async def stop_listener(self) -> None:
        """Stop accepting and drop in-progress handshakes; authenticated
        connections stay up."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task, relayed in list(self._handshakes.items()):
            if not relayed:
                task.cancel()

    @property
    def relay_open(self) -> bool:
        return self._relay_open and not self._closed

    def open_relay(self) -> None:
        """Accept streams from the relay ingress (:meth:`accept_relayed`)."""
        if self._closed:
            raise PeerError("transport is closed")
        self._relay_open = True

    def close_relay(self) -> None:
        """Stop accepting relayed streams and drop their in-progress
        handshakes; authenticated connections stay up."""
        self._relay_open = False
        for task, relayed in list(self._handshakes.items()):
            if relayed:
                task.cancel()

    async def accept_relayed(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, source: str
    ) -> None:
        """One inbound stream from the relay ingress: exactly what a direct
        TCP accept gets (TLS server handshake, pre-auth limits, pair/auth),
        rate-limited per ``source`` (the client IP the relay reported, or a
        shared bucket)."""
        await self._accept(reader, writer, "relay:" + str(source)[:64], relayed=True)

    async def start(self) -> None:
        """Keep a connection up for every dialer link in the store."""
        for link in self.store.list():
            if link.role == "dialer":
                self._ensure_supervisor(link.link_id)

    def is_connected(self, link_id: str) -> bool:
        conn = self._conns.get(link_id)
        return conn is not None and not conn.dead

    def peer_info(self, link_id: str) -> dict:
        """``{"app", "caps"}`` the peer advertised on its last handshake
        (``{}`` when it advertised nothing or never connected here)."""
        return dict(self._peer_info.get(link_id) or {})

    def _note_peer_info(self, link_id: str, frame: dict) -> None:
        info = wire.peer_info(frame)
        if info["app"] or info["caps"]:
            self._peer_info[link_id] = info
        else:
            self._peer_info.pop(link_id, None)

    async def pair(self, code: str, progress=None) -> Link:
        """Join an invite. Raises ValueError for a malformed code,
        :class:`IdentityMismatch` if the listener's key doesn't match the
        code, and :class:`PairingFailed` for everything else.

        ``progress(stage)`` (optional, sync) hears ``waiting_relay`` (a relay
        code: waiting for its name to resolve), ``connecting`` and
        ``verifying``. When the listener hands back a link we already hold
        for the same key (it re-paired us), that link is updated in place and
        the handler hears ``on_link_repaired``."""
        if self._closed:
            raise PeerError("transport is closed")
        info = parse_code(code)
        addr = info.addr

        def stage(name: str) -> None:
            if progress is not None:
                with contextlib.suppress(Exception):
                    progress(name)

        if addr.is_relay:
            stage("waiting_relay")
        await self._await_relay_dns(addr)
        stage("connecting")
        writer = None
        repaired = False
        try:
            async with asyncio.timeout(self.handshake_timeout):
                reader, writer, server_pub = await self._dial(
                    addr, expect_fp=info.server_fp
                )
                stage("verifying")
                hello = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                nonce = wire.validate_hello(hello)
                transcript = pair_transcript(server_pub, nonce, self.identity.pub)
                await _send_raw(
                    writer,
                    {
                        "t": "pair",
                        "v": wire.PROTOCOL_VERSION,
                        "invite_id": info.invite_id,
                        "pub": self.identity.pub.hex(),
                        "name": self.display_name,
                        "proof": hmac.new(
                            info.secret, transcript, hashlib.sha256
                        ).hexdigest(),
                        "sig": self.identity.sign(transcript).hex(),
                    },
                )
                reply = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                if reply.get("t") == "denied":
                    raise PairingFailed(
                        "pairing refused: the code is wrong, used or expired"
                    )
                wire.validate_welcome(reply)
                sas = compute_sas(server_pub, self.identity.pub, nonce)
                if not hmac.compare_digest(reply["sas"], sas):
                    raise PairingFailed("safety number mismatch")
                carrier = "relay" if addr.is_relay else "tcp"
                old = self.store.get(reply["link_id"])
                if old is not None:
                    # The listener kept our link (we paired again): the same
                    # pinned key may update it; anything else is refused.
                    if old.role != "dialer" or not hmac.compare_digest(
                        old.peer_pub, server_pub.hex()
                    ):
                        raise ValueError("link id clash")
                    link = self.store.update(
                        old.link_id,
                        peer_name=reply["name"],
                        peer_addr=str(addr),
                        sas=sas,
                        sas_verified=False,
                        carrier=carrier,
                        last_seen=time.time(),
                    )
                    if link is None:  # unlinked here mid-handshake
                        raise ValueError("link gone")
                    repaired = True
                else:
                    link = self.store.add(
                        Link(
                            link_id=reply["link_id"],
                            peer_name=reply["name"],
                            peer_pub=server_pub.hex(),
                            role="dialer",
                            peer_addr=str(addr),
                            sas=sas,
                            carrier=carrier,
                        )
                    )
        except PairingFailed:
            _abort(writer)
            raise
        except TimeoutError:
            _abort(writer)
            raise PairingFailed("pairing timed out") from None
        except wire.ProtocolError:
            _abort(writer)
            raise PairingFailed("pairing failed: protocol error") from None
        except (OSError, ssl.SSLError) as e:
            _abort(writer)
            detail = str(e) if isinstance(e, relay.RelayError) else type(e).__name__
            raise PairingFailed(f"cannot reach {addr.public()}: {detail}") from None
        except ValueError:  # e.g. link id already in the store
            _abort(writer)
            raise PairingFailed("pairing failed: bad link") from None
        except BaseException as e:
            # Never leave the socket open, nor echo an unexpected error's text.
            _abort(writer)
            if isinstance(e, Exception):
                raise PairingFailed("pairing failed") from None
            raise
        log.info(
            "peer: %s as dialer, link %s",
            "re-paired" if repaired else "paired",
            _short(link.link_id),
        )
        self._note_peer_info(link.link_id, hello)
        self._hook("on_link_repaired" if repaired else "on_link_added", link)
        self._adopt(link.link_id, reader, writer)
        self._ensure_supervisor(link.link_id)
        return link

    async def _await_relay_dns(self, addr: PeerAddr) -> None:
        """Best effort: wait until a relay's hostname resolves (see
        :data:`RELAY_DNS_WAIT`). Never raises; the dial reports failures."""
        if not addr.is_relay or self.relay_dns_wait <= 0:
            return
        try:
            ipaddress.ip_address(addr.host)
            return
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.relay_dns_wait
        while not self._closed:
            try:
                await loop.getaddrinfo(addr.host, addr.port, type=socket.SOCK_STREAM)
                return
            except (OSError, UnicodeError):
                if loop.time() + RELAY_DNS_RETRY_S > deadline:
                    return
                await asyncio.sleep(RELAY_DNS_RETRY_S)

    async def request(
        self, link_id: str, op: str, p: dict, timeout: float = REQUEST_TIMEOUT
    ) -> dict:
        try:
            wire.validate_request(op, p)
        except wire.ProtocolError as e:
            raise ValueError(f"invalid peer request: {e}") from None
        conn = self._conns.get(link_id)
        if conn is None or conn.dead:
            raise PeerUnavailable("peer not connected")
        return await conn.request(op, p, timeout)

    async def redial(self, link_id: str) -> None:
        """The link's address changed: if it is down, dial now (with the
        new address) instead of waiting out the backoff."""
        if self._closed or self.is_connected(link_id):
            return
        sup = self._supervisors.pop(link_id, None)
        if sup is not None:
            sup.cancel()
        self._ensure_supervisor(link_id)

    async def unlink(self, link_id: str) -> bool:
        sup = self._supervisors.pop(link_id, None)
        if sup is not None:
            sup.cancel()
        removed = self.store.remove(link_id)
        conn = self._conns.get(link_id)
        if conn is not None:
            await conn.close(UNLINK_REASON, send_bye=True)
        self._limits.pop(link_id, None)
        self._peer_info.pop(link_id, None)
        return removed

    def _peer_unlinked(self, link_id: str) -> None:
        """The peer unlinked: forget its key here too (so it can't come back
        with the same key and link id), stop redialing, and tell the handler
        (``on_link_removed(link)``) so it can stop what the link was running."""
        sup = self._supervisors.pop(link_id, None)
        if sup is not None and sup is not asyncio.current_task():
            sup.cancel()
        self._limits.pop(link_id, None)
        try:
            link = self.store.get(link_id)
            removed = link is not None and self.store.remove(link_id)
        except Exception as e:
            log.warning(
                "peer: link %s: could not forget the unlinked peer: %s",
                _short(link_id),
                type(e).__name__,
            )
            return
        log.info("peer: link %s unlinked by the peer", _short(link_id))
        if removed:
            self._hook("on_link_removed", link)

    async def close(self) -> None:
        self._closed = True
        await self.stop_listener()
        for sup in self._supervisors.values():
            sup.cancel()
        self._supervisors.clear()
        self.close_relay()
        for conn in list(self._conns.values()):
            await conn.close("shutdown", send_bye=True)
        for task in list(self._bg) + list(self._carriers):
            task.cancel()

    # -- internals: bookkeeping ----------------------------------------------

    def _limits_for(self, link_id: str) -> _LinkLimits:
        lim = self._limits.get(link_id)
        if lim is None:
            lim = _LinkLimits(
                msg=_Window(self.msg_rate, self.rate_window, self.clock),
                req=_Window(self.req_rate, self.rate_window, self.clock),
            )
            self._limits[link_id] = lim
        return lim

    def _ip_allowed(self, ip: str) -> bool:
        ip = _rate_key(ip)
        win = self._ip_windows.get(ip)
        if win is None:
            if len(self._ip_windows) >= MAX_TRACKED_IPS:
                for k in [k for k, w in self._ip_windows.items() if w.idle()]:
                    del self._ip_windows[k]
                while len(self._ip_windows) >= MAX_TRACKED_IPS:
                    del self._ip_windows[next(iter(self._ip_windows))]
            win = self._ip_windows[ip] = _Window(
                self.conn_rate_per_ip, self.conn_rate_window, self.clock
            )
        return win.hit()

    def _hook(self, name: str, *args) -> None:
        fn = getattr(self.handler, name, None)
        if fn is None:
            return
        try:
            res = fn(*args)
        except Exception as e:
            log.warning("peer: %s hook failed: %s", name, type(e).__name__)
            return
        if inspect.isawaitable(res):
            task = asyncio.ensure_future(res)
            self._bg.add(task)

            def _done(t: asyncio.Task) -> None:
                self._bg.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    log.warning(
                        "peer: %s hook failed: %s", name, type(t.exception()).__name__
                    )

            task.add_done_callback(_done)

    def _link_exists(self, link_id: str) -> bool:
        try:
            return self.store.get(link_id) is not None
        except Exception:
            return False

    def _touch(self, link_id: str) -> None:
        try:
            self.store.update(link_id, last_seen=time.time())
        except (OSError, ValueError):
            pass

    def _expired(self, link: Link) -> bool:
        return time.time() - link.last_seen > self.link_idle_expiry_s

    def _adopt(self, link_id: str, reader, writer) -> None:
        """Make this the link's one live connection (replacing any old one)."""
        old = self._conns.get(link_id)
        conn = _Conn(self, link_id, reader, writer)
        self._conns[link_id] = conn
        if old is not None:
            old.teardown("replaced by a new connection")
        self._touch(link_id)
        conn.start()
        log.info("peer: link %s connected", _short(link_id))
        self._hook("on_state", link_id, True)

    def _conn_closed(self, conn: _Conn) -> None:
        if self._conns.get(conn.link_id) is not conn:
            return  # already replaced
        del self._conns[conn.link_id]
        self._touch(conn.link_id)
        self._hook("on_state", conn.link_id, False)

    # -- internals: listener side ----------------------------------------------

    async def _on_accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        ip = str(peer[0]) if peer else "?"
        await self._accept(reader, writer, ip, relayed=False)

    async def _accept(self, reader, writer, ip: str, *, relayed: bool) -> None:
        accepting = self._relay_open if relayed else self._server is not None
        if (
            self._closed
            or not accepting
            or self._unauth >= self.max_unauth
            or not self._ip_allowed(ip)
        ):
            _abort(writer)  # before TLS: costs us nothing
            return
        task = asyncio.current_task()
        self._handshakes[task] = relayed
        self._unauth += 1
        result = None
        try:
            async with asyncio.timeout(self.handshake_timeout):
                await writer.start_tls(self._server_ctx)
                if writer.get_extra_info("ssl_object").version() != "TLSv1.3":
                    raise wire.ProtocolError("not TLS 1.3")
                result = await self._server_handshake(reader, writer, relayed)
        except asyncio.CancelledError:
            _abort(writer)
            raise
        except (
            TimeoutError,
            wire.ProtocolError,
            OSError,
            ssl.SSLError,
            ConnectionError,
        ):
            pass
        except Exception as e:
            log.warning("peer: handshake error: %s", type(e).__name__)
        finally:
            self._unauth -= 1
            self._handshakes.pop(task, None)
        if result is None or self._closed or not self._link_exists(result[0].link_id):
            # (An unlink can land while the welcome drains: adopting then would
            # leave an authenticated connection for a link that is gone.)
            _abort(writer)
            return
        link, kind = result
        if kind == "new":
            self._hook("on_link_added", link)
        elif kind == "repaired":
            self._hook("on_link_repaired", link)
        self._adopt(link.link_id, reader, writer)

    async def _server_handshake(
        self, reader, writer, relayed: bool = False
    ) -> tuple[Link, str] | None:
        """Hello, then pair or auth. Returns ``(link, kind)`` — ``kind`` is
        ``"new"``, ``"repaired"`` (paired again: same link) or ``"auth"`` —
        or None after sending the one generic ``denied``."""
        nonce = secrets.token_bytes(32)
        await _send_raw(
            writer,
            {
                "t": "hello",
                "v": wire.PROTOCOL_VERSION,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "name": self.display_name,
            },
        )
        msg = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
        link, kind = None, "auth"
        carrier = "relay" if relayed else "tcp"
        try:
            if msg.get("t") == "pair":
                wire.validate_pair(msg)
                link, kind = self._accept_pair(msg, nonce, carrier) or (None, "")
            elif msg.get("t") == "auth":
                wire.validate_auth(msg)
                link = self._accept_auth(msg, nonce, carrier)
        except wire.ProtocolError:
            link = None
        if link is None:
            # One generic answer for every failure.
            with contextlib.suppress(Exception):
                await _send_raw(writer, {"t": "denied"})
            return None
        await _send_raw(
            writer,
            {
                "t": "welcome",
                "link_id": link.link_id,
                "name": self.display_name,
                "sas": link.sas,
            },
        )
        self._note_peer_info(link.link_id, msg)
        return link, kind

    def _accept_pair(
        self, msg: dict, nonce: bytes, carrier: str = ""
    ) -> tuple[Link, str] | None:
        if self.invites is None:
            return None
        client_pub = bytes.fromhex(msg["pub"])
        transcript = pair_transcript(self.identity.pub, nonce, client_pub)
        # check() rate-limits globally, counts failures and consumes on success.
        if not self.invites.check(
            msg["invite_id"], bytes.fromhex(msg["proof"]), transcript
        ):
            log.info("peer: pairing attempt refused")
            return None
        if not Identity.verify(client_pub, bytes.fromhex(msg["sig"]), transcript):
            log.info("peer: pairing attempt refused (bad signature)")
            return None
        if hmac.compare_digest(client_pub, self.identity.pub):
            return None  # pairing with ourselves
        sas = compute_sas(self.identity.pub, client_pub, nonce)
        # The same person again (a fresh invite after our relay address
        # changed, a reinstall that kept their identity): keep their link —
        # id, permissions, shared folder — with the new safety number. The
        # invite and their signature were checked exactly as for a new link.
        existing = self.store.find_by_pub(msg["pub"], "listener")
        if existing:
            link = self.store.update(
                existing[0].link_id,
                peer_name=msg["name"],
                sas=sas,
                sas_verified=False,
                carrier=carrier,
                last_seen=time.time(),
            )
            if link is not None:
                log.info("peer: re-paired as listener, link %s", _short(link.link_id))
                return link, "repaired"
        link = self.store.add(
            Link(
                link_id=secrets.token_hex(16),
                peer_name=msg["name"],
                peer_pub=msg["pub"],
                role="listener",
                sas=sas,
                carrier=carrier,
            )
        )
        log.info("peer: paired as listener, link %s", _short(link.link_id))
        return link, "new"

    def _accept_auth(self, msg: dict, nonce: bytes, carrier: str = "") -> Link | None:
        link = self.store.get(msg["link_id"])
        usable = (
            link is not None and link.role == "listener" and not self._expired(link)
        )
        pub = bytes.fromhex(link.peer_pub) if usable else self._dummy_pub
        transcript = auth_transcript(self.identity.pub, nonce, msg["link_id"])
        ok = Identity.verify(pub, bytes.fromhex(msg["sig"]), transcript)
        if not (ok and usable):
            log.info("peer: authentication refused")
            return None
        if carrier and link.carrier != carrier:
            # Remember how they reach us now: a direct link keeps the direct
            # listener up even while a relay serves new invites.
            with contextlib.suppress(OSError, ValueError):
                link = self.store.update(link.link_id, carrier=carrier) or link
        return link

    # -- internals: dialer side ------------------------------------------------

    async def _dial(
        self,
        addr: PeerAddr,
        *,
        expect_fp: bytes | None = None,
        expect_pub: bytes | None = None,
    ):
        """TLS-connect (directly, or inside a relay's WebSocket) and check the
        server's key before anything is sent. Returns
        ``(reader, writer, server_pub)``."""
        if addr.is_relay:
            reader, writer = await relay.dial(
                addr, self._client_ctx, self._relay_edge_ssl, self._carriers
            )
        else:
            reader, writer = await asyncio.open_connection(
                addr.host, addr.port, ssl=self._client_ctx
            )
        try:
            sslobj = writer.get_extra_info("ssl_object")
            pub = (
                pub_from_cert_der(sslobj.getpeercert(binary_form=True))
                if sslobj
                else None
            )
            if sslobj is None or sslobj.version() != "TLSv1.3":
                raise wire.ProtocolError("not TLS 1.3")
            if expect_fp is not None:
                ok = (
                    hmac.compare_digest(fingerprint_of(pub or b""), expect_fp)
                    and pub is not None
                )
            else:
                ok = (
                    hmac.compare_digest(pub or bytes(32), expect_pub)
                    and pub is not None
                )
            if not ok:
                raise IdentityMismatch()
        except BaseException:
            _abort(writer)
            raise
        return reader, writer, pub

    async def _connect_link(self, link: Link) -> None:
        addr = parse_addr(link.peer_addr)
        writer = None
        try:
            async with asyncio.timeout(self.handshake_timeout):
                reader, writer, server_pub = await self._dial(
                    addr, expect_pub=bytes.fromhex(link.peer_pub)
                )
                hello = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                nonce = wire.validate_hello(hello)
                transcript = auth_transcript(server_pub, nonce, link.link_id)
                await _send_raw(
                    writer,
                    {
                        "t": "auth",
                        "v": wire.PROTOCOL_VERSION,
                        "link_id": link.link_id,
                        "sig": self.identity.sign(transcript).hex(),
                    },
                )
                reply = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                if reply.get("t") == "denied":
                    raise PeerUnavailable("peer refused authentication (link revoked?)")
                wire.validate_welcome(reply)
                if reply["link_id"] != link.link_id:
                    raise wire.ProtocolError("welcome for another link")
        except BaseException:
            _abort(writer)
            raise
        name = sanitize_name(reply["name"])
        if name != link.peer_name:
            with contextlib.suppress(OSError, ValueError):
                self.store.update(link.link_id, peer_name=name)
        self._note_peer_info(link.link_id, hello)
        self._adopt(link.link_id, reader, writer)

    def _ensure_supervisor(self, link_id: str) -> None:
        sup = self._supervisors.get(link_id)
        if sup is None or sup.done():
            self._supervisors[link_id] = asyncio.create_task(
                self._supervise(link_id), name=f"peer-dial-{_short(link_id)}"
            )

    async def _supervise(self, link_id: str) -> None:
        delay = self.backoff_initial
        while not self._closed:
            conn = self._conns.get(link_id)
            if conn is not None:
                await conn.closed.wait()
                lived = time.monotonic() - conn.started
                delay = self.backoff_initial if lived >= STABLE_CONN_S else delay
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff_max)
                continue
            link = self.store.get(link_id)
            if link is None or link.role != "dialer":
                break
            if self._expired(link):
                log.info(
                    "peer: link %s idle-expired; not reconnecting", _short(link_id)
                )
                break
            try:
                await self._connect_link(link)
                continue
            except asyncio.CancelledError:
                raise
            except IdentityMismatch:
                log.warning("peer: link %s: %s", _short(link_id), MITM_MESSAGE)
            except (
                PeerError,
                wire.ProtocolError,
                OSError,
                ssl.SSLError,
                TimeoutError,
            ) as e:
                log.info(
                    "peer: link %s reconnect failed: %s",
                    _short(link_id),
                    type(e).__name__,
                )
            except Exception as e:
                log.warning(
                    "peer: link %s reconnect error: %s",
                    _short(link_id),
                    type(e).__name__,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff_max)
        if self._supervisors.get(link_id) is asyncio.current_task():
            del self._supervisors[link_id]
