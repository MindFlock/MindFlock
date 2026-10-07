"""TLS-in-WebSocket: carry the peer TLS stream through an untrusted relay.

When the inviter can't be reached directly (different networks, NAT, no
public IP), it can expose a tiny **relay ingress** through a public HTTPS
relay — a Cloudflare quick tunnel by default (:mod:`backend.peer.tunnel`), or
any WebSocket-capable reverse proxy. The joiner dials
``wss://<relay host>/<path>``; the WebSocket's binary frames carry, byte for
byte, the very same TLS 1.3 stream a direct connection would, and the same
pinned-key handshake (:mod:`backend.peer.transport`) runs inside it.

The relay terminates only the OUTER TLS (its own certificate, which we check
but do not rely on). It sees the inner TLS records, never their plaintext; it
can't alter them (TLS 1.3 AEAD), replay them (fresh keys and a fresh server
nonce per connection), or impersonate either side (both keys are pinned).
What it CAN do is drop, delay or count traffic: availability and metadata.

The ingress is the only thing the relay forwards to, and it is deliberately
minimal:

* It binds loopback only and is a separate server: it never routes to the
  MindFlock HTTP API (or anything else).
* It serves exactly one request shape: ``GET <path> HTTP/1.1`` with a valid
  WebSocket upgrade, where ``<path>`` holds a random 128-bit ingress token
  (it rides in the invite code). Every other request — any method, any other
  path, a malformed head, a body — gets the same fixed ``404`` with no body
  and no server banner, and the connection closes. Scanners learn nothing,
  and without the token they can't even reach the TLS handshake.
* Pre-upgrade: at most :data:`MAX_PENDING` connections, a
  :data:`HEAD_TIMEOUT` deadline and a :data:`MAX_HEAD` byte cap on the head.
  After the upgrade: at most :data:`MAX_UPGRADED` WebSockets, binary frames
  only, client frames must be masked, :data:`MAX_WS_PAYLOAD` per frame, no
  extensions, and a cap on control frames. Then the peer transport applies
  every existing pre-auth limit (16 unauthenticated, per-source rate,
  10 s handshake deadline, global pairing rate) to the inner stream.

The carrier is a plain ``socket.socketpair``: one end is spliced to the
WebSocket, the other is handed to asyncio's ordinary TLS machinery, so the
transport's TLS code runs unchanged over it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import logging
import os
import socket
import ssl
import time
from collections import deque
from typing import Awaitable, Callable

from backend.peer.addr import PeerAddr, check_path

__all__ = [
    "RelayError",
    "RelayNotResolvable",
    "WsError",
    "WsStream",
    "RelayIngress",
    "NOT_FOUND",
    "accept_key",
    "encode_frame",
    "read_frame",
    "splice",
    "open_ws",
    "dial",
    "edge_ssl_context",
    "MAX_WS_PAYLOAD",
]

log = logging.getLogger(__name__)

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

MAX_WS_PAYLOAD = 256 * 1024  # per frame; TLS records are ≤ ~16 KiB
SEND_CHUNK = 16 * 1024
MAX_HEAD = 8192  # request head the ingress accepts (Cloudflare adds ~1 KiB)
MAX_RESPONSE_HEAD = 8192  # response head the dialer accepts
MAX_HEADERS = 64
HEAD_TIMEOUT = 5.0
MAX_PENDING = 32
MAX_UPGRADED = 64
CONTROL_RATE = 60  # ping/pong/empty frames per window, per connection
CONTROL_WINDOW = 60.0
CLOSE_TIMEOUT = 2.0
LINGER_S = 1.0  # after a 404: how long (and how much) we discard input
LINGER_BYTES = 64 * 1024
INNER_SNI = "mindflock-peer"  # SNI for the inner TLS (keys are pinned, not names)

#: The ingress's one answer to everything that isn't a valid upgrade.
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

# Headers whose duplicates are refused (ambiguity is how smuggling starts).
_SINGLE = frozenset(
    {
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "content-length",
        "transfer-encoding",
        "cf-connecting-ip",
    }
)


class RelayError(ConnectionError):
    """The relay could not be reached or refused us. (A ``ConnectionError``,
    so callers that handle unreachable peers handle this too.)"""


class RelayNotResolvable(RelayError):
    """The relay's hostname doesn't resolve (yet)."""


class WsError(ConnectionError):
    """The other side broke the WebSocket protocol."""


# -- framing -------------------------------------------------------------------


def accept_key(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1(key.encode("ascii") + WS_GUID).digest()
    ).decode("ascii")


def _xor(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = (mask * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(m, "little")).to_bytes(
        n, "little"
    )


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True) -> bytes:
    n = len(payload)
    head = bytearray([(0x80 if fin else 0) | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(mbit | 127)
        head += n.to_bytes(8, "big")
    if mask:
        key = os.urandom(4)
        return bytes(head) + key + _xor(payload, key)
    return bytes(head) + payload


async def read_frame(
    reader: asyncio.StreamReader, *, masked: bool, max_payload: int = MAX_WS_PAYLOAD
) -> tuple[bool, int, bytes]:
    """Read one frame → ``(fin, opcode, unmasked payload)``. Raises
    :class:`WsError` for anything RFC 6455 forbids or we don't support, and
    ``asyncio.IncompleteReadError`` on EOF."""
    b0, b1 = await reader.readexactly(2)
    fin, rsv, opcode = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
    if rsv:
        raise WsError("reserved bits set (no extensions were negotiated)")
    if opcode not in (OP_CONT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG):
        raise WsError("unsupported opcode")  # text frames included
    if bool(b1 & 0x80) != masked:
        raise WsError("bad masking")
    n = b1 & 0x7F
    if opcode >= 0x8 and (not fin or n > 125):
        raise WsError("bad control frame")
    if n == 126:
        n = int.from_bytes(await reader.readexactly(2), "big")
        if n < 126:
            raise WsError("non-minimal length")
    elif n == 127:
        n = int.from_bytes(await reader.readexactly(8), "big")
        if n < 65536 or n >> 63:
            raise WsError("non-minimal length")
    if n > max_payload:
        raise WsError("frame too large")
    key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(n) if n else b""
    return fin, opcode, (_xor(payload, key) if masked else payload)


class WsStream:
    """A WebSocket used as a byte pipe: binary (and continuation) frames are
    data; ping is answered; close ends the stream. ``client=True`` masks
    what it sends and requires unmasked frames back (and vice versa)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        client: bool,
        max_payload: int = MAX_WS_PAYLOAD,
        clock=time.monotonic,
    ):
        self.reader, self.writer, self.client = reader, writer, client
        self.max_payload = max_payload
        self._clock = clock
        self._wlock = asyncio.Lock()
        self._in_msg = False  # inside a fragmented binary message
        self._close_sent = False
        self._controls: deque[float] = deque()

    def _control_hit(self) -> None:
        now = self._clock()
        while self._controls and self._controls[0] <= now - CONTROL_WINDOW:
            self._controls.popleft()
        if len(self._controls) >= CONTROL_RATE:
            raise WsError("control frame flood")
        self._controls.append(now)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = encode_frame(opcode, payload, mask=self.client)
        async with self._wlock:
            self.writer.write(frame)
            await self.writer.drain()

    async def recv(self) -> bytes | None:
        """The next chunk of data, or None once the stream has ended (close
        frame or EOF). Raises :class:`WsError` on a protocol violation."""
        while True:
            try:
                fin, op, payload = await read_frame(
                    self.reader, masked=not self.client, max_payload=self.max_payload
                )
            except asyncio.IncompleteReadError:
                return None
            if op in (OP_BIN, OP_CONT):
                if (op == OP_BIN) == self._in_msg:
                    raise WsError("bad fragmentation")
                self._in_msg = not fin
                if payload:
                    return payload
                self._control_hit()  # an empty data frame is just noise
            elif op == OP_PING:
                self._control_hit()
                await self._send_frame(OP_PONG, payload)
            elif op == OP_PONG:
                self._control_hit()
            else:  # OP_CLOSE
                if len(payload) == 1:
                    raise WsError("bad close frame")
                await self.close()
                return None

    async def send(self, data: bytes) -> None:
        for i in range(0, len(data), SEND_CHUNK):
            await self._send_frame(OP_BIN, data[i : i + SEND_CHUNK])

    async def close(self, code: int = 1000) -> None:
        """Send a close frame once (best effort, bounded)."""
        if self._close_sent:
            return
        self._close_sent = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._send_frame(OP_CLOSE, code.to_bytes(2, "big")), CLOSE_TIMEOUT
            )

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.transport.abort()


# -- HTTP heads ------------------------------------------------------------------


def _parse_head(raw: bytes) -> tuple[str, dict[str, str]]:
    """``raw`` ends with CRLFCRLF. Returns the start line and the headers
    (lower-case names). Strict: CRLF line endings only, no obs-fold, no
    control characters, no duplicate security-relevant header."""
    try:
        text = raw[:-4].decode("latin-1")
    except Exception:  # noqa: BLE001 — latin-1 decodes anything; belt and braces
        raise WsError("bad head") from None
    lines = text.split("\r\n")
    if len(lines) - 1 > MAX_HEADERS:
        raise WsError("too many headers")
    start, headers = lines[0], {}
    for line in [start, *lines[1:]]:
        if any(c in line for c in "\r\n\x00") or any(
            ord(c) < 0x20 and c != "\t" or ord(c) == 0x7F for c in line
        ):
            raise WsError("control character in head")
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or line[:1] in " \t":
            raise WsError("bad header line")
        name = name.lower()
        if not all(c.isascii() and (c.isalnum() or c in "-_") for c in name):
            raise WsError("bad header name")
        if name in headers and name in _SINGLE:
            raise WsError("duplicate header")
        headers[name] = value.strip(" \t")
    return start, headers


def _tokens(value: str) -> set[str]:
    return {t.strip().lower() for t in value.split(",") if t.strip()}


async def _read_head(reader: asyncio.StreamReader, cap: int) -> bytes:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise WsError("head too large") from None
    except asyncio.IncompleteReadError:
        raise WsError("truncated head") from None
    if len(raw) > cap:
        raise WsError("head too large")
    return raw


# -- the splice --------------------------------------------------------------------


async def splice(
    ws: WsStream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Pump bytes both ways between a WebSocket and a stream until either
    side ends or errs; then close both."""

    async def ws_to_stream() -> None:
        while (data := await ws.recv()) is not None:
            writer.write(data)
            await writer.drain()

    async def stream_to_ws() -> None:
        while data := await reader.read(SEND_CHUNK):
            await ws.send(data)

    tasks = [
        asyncio.create_task(ws_to_stream()),
        asyncio.create_task(stream_to_ws()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            writer.close()  # graceful: what was written still gets delivered
        asyncio.get_running_loop().call_later(CLOSE_TIMEOUT, _abort, writer)
        await ws.close()
        ws.abort()


def _abort(writer) -> None:
    with contextlib.suppress(Exception):
        writer.transport.abort()


async def _socketpair_streams():
    """Two connected in-process stream pairs: ``(inner, outer)``, each a
    ``(reader, writer)``. The inner end is CLIENT-side for ``start_tls``."""
    a, b = socket.socketpair()
    try:
        inner = await asyncio.open_connection(sock=a)
        outer = await asyncio.open_connection(sock=b)
    except BaseException:
        a.close()
        b.close()
        raise
    return inner, outer


async def _serve_socketpair(cb):
    """Like :func:`_socketpair_streams`, but the inner end is served the way
    ``asyncio.start_server`` serves an accepted socket: ``cb(reader,
    writer)`` runs as a task, and ``start_tls`` on that writer does a
    SERVER-side handshake (asyncio decides the side by the presence of a
    client-connected callback). Returns the outer ``(reader, writer)``."""
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    try:
        await loop.connect_accepted_socket(
            lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader(), cb), sock=a
        )
        outer = await asyncio.open_connection(sock=b)
    except BaseException:
        a.close()
        b.close()
        raise
    return outer


# -- the ingress (inviter side) ------------------------------------------------------


class RelayIngress:
    """The one endpoint the relay forwards to (see the module docstring).

    ``on_stream(reader, writer, source)`` receives the inner byte stream of
    every accepted WebSocket — the transport runs its TLS server handshake on
    it. ``source`` is the client IP the relay vouches for
    (``Cf-Connecting-Ip``, only when ``trust_cf_ip``; Cloudflare overwrites
    that header and refuses client-supplied ones), else ``"relay"``; it only
    picks a rate-limit bucket."""

    def __init__(
        self,
        on_stream: Callable[
            [asyncio.StreamReader, asyncio.StreamWriter, str], Awaitable
        ],
        path: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        trust_cf_ip: bool = False,
        max_pending: int = MAX_PENDING,
        max_upgraded: int = MAX_UPGRADED,
        head_timeout: float = HEAD_TIMEOUT,
        max_head: int = MAX_HEAD,
        max_payload: int = MAX_WS_PAYLOAD,
    ):
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the relay ingress binds loopback only")
        self._on_stream = on_stream
        self._path = check_path(path).encode("ascii")
        self.host, self._want_port = host, port
        self.trust_cf_ip = trust_cf_ip
        self.max_pending, self.max_upgraded = max_pending, max_upgraded
        self.head_timeout, self.max_head = head_timeout, max_head
        self.max_payload = max_payload
        self._server: asyncio.Server | None = None
        self._pending = 0
        self._upgraded = 0
        self._tasks: set[asyncio.Task] = set()
        self.stats = {"requests": 0, "rejected": 0, "dropped": 0, "upgraded": 0}

    @property
    def port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> int:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._on_conn, self.host, self._want_port, limit=2 * self.max_head
            )
        return self.port

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in list(self._tasks):
            task.cancel()

    def _check_upgrade(self, raw: bytes) -> tuple[str, str]:
        """→ ``(Sec-WebSocket-Accept, source)``; :class:`WsError` otherwise."""
        start, h = _parse_head(raw)
        parts = start.split(" ")
        if len(parts) != 3 or parts[0] != "GET" or parts[2] != "HTTP/1.1":
            raise WsError("not a GET")
        target = parts[1].encode("latin-1")
        if not hmac.compare_digest(target, self._path):
            raise WsError("wrong path")
        if (
            h.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in _tokens(h.get("connection", ""))
            or h.get("sec-websocket-version") != "13"
            or "transfer-encoding" in h
            or h.get("content-length", "0") != "0"
        ):
            raise WsError("not a websocket upgrade")
        key = h.get("sec-websocket-key", "")
        try:
            if len(key) != 24 or len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError
        except ValueError:
            raise WsError("bad key") from None
        source = "relay"
        if self.trust_cf_ip and "cf-connecting-ip" in h:
            try:
                source = str(ipaddress.ip_address(h["cf-connecting-ip"]))
            except ValueError:
                pass
        return accept_key(key), source

    async def _reject(self, reader, writer) -> None:
        """Send the 404, then a lingering close: half-close and discard what
        the client still sends (bounded), so unread request bytes don't turn
        the close into a reset that destroys the 404 in flight."""
        self.stats["rejected"] += 1
        with contextlib.suppress(Exception):
            writer.write(NOT_FOUND)
            await asyncio.wait_for(writer.drain(), 1.0)
            if writer.can_write_eof():
                writer.write_eof()
            async with asyncio.timeout(LINGER_S):
                drained = 0
                while drained < LINGER_BYTES and (chunk := await reader.read(4096)):
                    drained += len(chunk)
        _abort(writer)

    async def _on_conn(self, reader, writer) -> None:
        self.stats["requests"] += 1
        if self._server is None or self._pending >= self.max_pending:
            self.stats["dropped"] += 1
            _abort(writer)
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        self._pending += 1
        try:
            try:
                async with asyncio.timeout(self.head_timeout):
                    raw = await _read_head(reader, self.max_head)
                accept, source = self._check_upgrade(raw)
            except (WsError, TimeoutError, OSError, ValueError, UnicodeError):
                await self._reject(reader, writer)
                return
            finally:
                self._pending -= 1
            if self._server is None or self._upgraded >= self.max_upgraded:
                await self._reject(reader, writer)
                return
            self._upgraded += 1
            self.stats["upgraded"] += 1
            try:
                await self._bridge(reader, writer, accept, source)
            finally:
                self._upgraded -= 1
        except asyncio.CancelledError:
            _abort(writer)
            raise
        except Exception as e:  # noqa: BLE001 — never let one client kill the server
            log.info("peer relay: connection error: %s", type(e).__name__)
            _abort(writer)
        finally:
            self._tasks.discard(task)

    async def _bridge(self, reader, writer, accept: str, source: str) -> None:
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        ws = WsStream(reader, writer, client=False, max_payload=self.max_payload)
        inner: dict = {}

        async def serve_inner(ir, iw) -> None:
            task = asyncio.current_task()
            inner.update(task=task, writer=iw)
            self._tasks.add(task)
            try:
                await self._on_stream(ir, iw, source)
            finally:
                self._tasks.discard(task)

        try:
            orr, ow = await _serve_socketpair(serve_inner)
        except OSError:
            ws.abort()
            return
        try:
            await splice(ws, orr, ow)
        finally:
            task = inner.get("task")
            if task is not None and not task.done():
                # The splice is over, so the inner stream is at EOF: the
                # handshake fails fast. (An adopted connection has already
                # returned from on_stream.)
                _abort(inner["writer"])


# -- the dialer side -------------------------------------------------------------------


def edge_ssl_context() -> ssl.SSLContext:
    """TLS to the relay's edge: normal WebPKI verification. Defense in depth
    only — the inner, pinned TLS is what we trust."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:  # Python builds without a usable system store (python.org macOS)
        import certifi

        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


async def open_ws(
    addr: PeerAddr,
    edge_ssl: ssl.SSLContext | None,
    *,
    max_payload: int = MAX_WS_PAYLOAD,
) -> WsStream:
    """Connect to ``wss://host:port/path`` and complete the upgrade.
    ``edge_ssl=None`` speaks plain ``ws://`` (tests only). Raises
    :class:`RelayError`."""
    if not addr.is_relay:
        raise ValueError("not a relay address")
    try:
        reader, writer = await asyncio.open_connection(
            addr.host,
            addr.port,
            ssl=edge_ssl,
            server_hostname=addr.host if edge_ssl is not None else None,
            limit=2 * MAX_RESPONSE_HEAD,
        )
    except ssl.SSLError as e:
        raise RelayError(f"relay TLS failed: {e.reason or type(e).__name__}") from None
    except socket.gaierror:
        raise RelayNotResolvable(
            "the relay's hostname does not resolve (a new quick tunnel can "
            "take up to a minute to appear in DNS, or the inviter's tunnel is "
            "gone); the code stays valid, try again shortly"
        ) from None
    except OSError as e:
        raise RelayError(f"cannot reach the relay: {type(e).__name__}") from None
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = f"[{addr.host}]" if ":" in addr.host else addr.host
        if addr.port != 443:
            host = f"{host}:{addr.port}"
        writer.write(
            (
                f"GET {addr.path} HTTP/1.1\r\nHost: {host}\r\n"
                "User-Agent: mindflock-peer\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        try:
            raw = await _read_head(reader, MAX_RESPONSE_HEAD)
            start, h = _parse_head(raw)
        except WsError:
            raise RelayError("bad response from the relay") from None
        parts = start.split(" ", 2)
        if len(parts) < 2 or parts[0] != "HTTP/1.1" or not parts[1].isdigit():
            raise RelayError("bad response from the relay")
        if parts[1] != "101":
            code = parts[1][:3]
            raise RelayError(
                f"the relay refused the connection (HTTP {code}); the invite "
                "or the relay address may be stale"
            )
        if (
            h.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in _tokens(h.get("connection", ""))
            or not hmac.compare_digest(
                h.get("sec-websocket-accept", "").encode("latin-1"),
                accept_key(key).encode("ascii"),
            )
            or "sec-websocket-extensions" in h
            or "sec-websocket-protocol" in h
        ):
            raise RelayError("bad WebSocket upgrade from the relay")
    except BaseException as e:
        _abort(writer)
        if isinstance(e, (OSError, ssl.SSLError)) and not isinstance(e, RelayError):
            raise RelayError(f"relay connection failed: {type(e).__name__}") from None
        raise
    return WsStream(reader, writer, client=True, max_payload=max_payload)


async def dial(
    addr: PeerAddr,
    tls_ctx: ssl.SSLContext,
    edge_ssl: ssl.SSLContext | None,
    tasks: set | None = None,
):
    """Open the WebSocket, splice it to a socketpair and run the (pinned,
    end-to-end) peer TLS client over the other end. Returns ``(reader,
    writer)`` of the inner TLS stream, exactly like
    ``asyncio.open_connection(host, port, ssl=tls_ctx)``. The splice task is
    added to ``tasks`` (if given) so the owner can cancel it."""
    ws = await open_ws(addr, edge_ssl)
    try:
        (ir, iw), (orr, ow) = await _socketpair_streams()
    except BaseException:
        ws.abort()
        raise
    pump = asyncio.create_task(splice(ws, orr, ow), name="peer-relay-splice")
    if tasks is not None:
        tasks.add(pump)
        pump.add_done_callback(tasks.discard)
    try:
        # Upgrade the plain inner stream to TLS in place: the transport's
        # pinned-key checks then run on it exactly as on a direct connection.
        await iw.start_tls(tls_ctx, server_hostname=INNER_SNI)
        if iw.transport is None:  # closed right as the handshake finished
            raise RelayError("the relay closed the connection")
    except BaseException:
        _abort(iw)
        pump.cancel()
        raise
    return ir, iw
