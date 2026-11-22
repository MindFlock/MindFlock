"""The device tunnel: one of your devices reaching another through a relay.

"Reach my devices from anywhere" (``docs/devices-anywhere.md``). It reuses
the peer-link carrier unchanged (:mod:`backend.peer.relay`: TLS-in-WebSocket
through a Cloudflare quick tunnel; :mod:`backend.peer.transport`'s TLS 1.3
contexts; this instance's Ed25519 identity cert), and adds a handshake of
its own, because the peer handshake pairs PEOPLE by invite and this one
proves membership of the same group of devices:

::

    S→C {"t":"hello","v":1,"nonce":sn}
    C→S {"t":"dev","v":1,"nonce":cn,"from":<client key>,"to":<server key>,
         "proof":HMAC(K_auth, "mfdev-client-v1"|server_pub|sn|cn|from|to)}
    S→C {"t":"ok","proof":HMAC(K_auth, "mfdev-server-v1"|…same…)}  or  {"t":"denied"}

* The client checks the server's TLS key against the fingerprint the
  (fleet-authenticated) address record carried BEFORE it sends anything, and
  checks the server's proof before it sends a single application byte.
* The server checks the client's proof before it answers anything but the
  hello; an unauthenticated visitor gets one generic ``denied``.
* Both proofs bind the server key the client SAW in TLS, so a middlebox that
  relays to the real device fails; the fleet key itself never travels.

The same hello also answers a ``pair`` frame — a NEW device joining with a
long code (:mod:`backend.peer.fleet_pair`) — when the server was given a
``pair`` handler. That branch does that exchange and nothing else: the
stream is closed after it and never reaches the web server.

After ``ok`` the stream is a plain byte pipe to the device's own web server
(127.0.0.1:<its port>). The server side registers the local port of that
connection in :mod:`backend.peer.tunnel_peers` first, so the web server's
auth layer treats what arrives as a remote caller with the access gate on —
never as "the person at this machine".

Membership and keys are checked at the handshake; :meth:`TunnelServer.sweep`
ends streams already open once their member leaves or the keys change (a
removal rotates the key).

The client side is :class:`DeviceProxy`: a loopback listener per remote
device; each connection to it is one tunnel connection, so ``aiohttp`` (and
everything in :mod:`backend.web.core.remote` that already talks to
``base_url``) works over it unchanged. Its requests still carry the fleet key
as the bearer — inside the end-to-end TLS — exactly as to a tailnet member.
A connection must open with a request addressed to that listener and carry
nothing a relay adds (:func:`local_request`), so a stale public tunnel that
ended up pointing at its port gets nothing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import socket
import ssl
from typing import Awaitable, Callable, Optional

from backend.peer import relay, tunnel_peers, wire
from backend.peer.addr import parse_addr
from backend.peer.fleet_record import Keys
from backend.peer.identity import fingerprint_of, pub_from_cert_der

log = logging.getLogger(__name__)

PROTO_V = 1
HANDSHAKE_TIMEOUT = 10.0
MAX_UNAUTH = 16
#: How long after a stream closed its loopback port stays marked tunneled
#: (the web server may still be finishing the last request on it).
RELEASE_GRACE = 5.0
#: Bytes per read when piping.
CHUNK = 64 * 1024
_NONCE_LEN = 32
#: A pairing exchange (fleet_pair) may take this long after the hello.
PAIR_TIMEOUT = 60.0
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


class _PairAsk:
    """The hello was answered with a ``pair`` frame (fleet_pair)."""

    __slots__ = ("sn", "msg")

    def __init__(self, sn: bytes, msg: dict):
        self.sn, self.msg = sn, msg


class TunnelAuthError(ConnectionError):
    """The far end failed authentication. ``reason``: ``pin`` (its TLS key
    isn't the one the record named), ``denied`` (it refused our proof),
    ``proof`` (its proof is wrong), ``protocol`` (it spoke nonsense)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s, n: int) -> bytes:
    if not isinstance(s, str) or len(s) > 4 * ((n + 2) // 3):
        raise wire.ProtocolError("bad nonce")
    try:
        raw = base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise wire.ProtocolError("bad nonce") from None
    if len(raw) != n:
        raise wire.ProtocolError("bad nonce")
    return raw


def transcript(
    label: bytes, server_pub: bytes, sn: bytes, cn: bytes, frm: str, to: str
) -> bytes:
    f, t = frm.encode("ascii"), to.encode("ascii")
    return b"|".join(
        (label, server_pub, sn, cn, bytes([len(f)]) + f, bytes([len(t)]) + t)
    )


def proof(key: bytes, label: bytes, *args) -> str:
    return hmac.new(key, transcript(label, *args), hashlib.sha256).hexdigest()


CLIENT_LABEL = b"mfdev-client-v1"
SERVER_LABEL = b"mfdev-server-v1"


def _abort(writer) -> None:
    with contextlib.suppress(Exception):
        writer.transport.abort()


async def pipe(r1, w1, r2, w2) -> None:
    """Copy bytes both ways between two streams until either side ends;
    then close both."""

    async def one(src, dst) -> None:
        try:
            while data := await src.read(CHUNK):
                dst.write(data)
                await dst.drain()
        except (OSError, ConnectionError, ssl.SSLError):
            pass
        finally:
            # Half-close where the transport can (plain TCP); a TLS stream
            # can't, so it is closed — which ends the other direction too.
            with contextlib.suppress(Exception):
                if dst.can_write_eof():
                    dst.write_eof()
                else:
                    dst.close()

    tasks = [asyncio.create_task(one(r1, w2)), asyncio.create_task(one(r2, w1))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for w in (w1, w2):
            with contextlib.suppress(Exception):
                w.close()


# --------------------------------------------------------------------------- #
# Server side (the device being reached)
# --------------------------------------------------------------------------- #
class TunnelServer:
    """The ``on_stream`` of a :class:`backend.peer.relay.RelayIngress`.

    ``keys()`` → the current :class:`Keys` (None outside a group: every
    connection is denied), ``self_key()`` → this device's key, ``members()``
    → the live roster keys a client may be, ``origin_port()`` → this
    device's web server port. All are read per connection, so a key change
    or a removal applies to the next one."""

    def __init__(
        self,
        server_ctx: ssl.SSLContext,
        server_pub: bytes,
        *,
        keys: Callable[[], Optional[Keys]],
        self_key: Callable[[], str],
        members: Callable[[], set],
        origin_port: Callable[[], int],
        on_member: Optional[Callable[[str], None]] = None,
        pair: Optional[Callable[..., Awaitable]] = None,
        max_unauth: int = MAX_UNAUTH,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        pair_timeout: float = PAIR_TIMEOUT,
    ):
        self._ctx = server_ctx
        self._pub = server_pub
        self._keys, self._self_key = keys, self_key
        self._members, self._origin_port = members, origin_port
        self._on_member = on_member
        self._pair = pair
        self.max_unauth = max_unauth
        self.handshake_timeout = handshake_timeout
        self.pair_timeout = pair_timeout
        self._unauth = 0
        self._streams: set = set()
        self.stats = {"accepted": 0, "denied": 0, "dropped": 0, "open": 0, "pair": 0}

    async def __call__(self, reader, writer, source: str = "") -> None:
        if self._unauth >= self.max_unauth:
            self.stats["dropped"] += 1
            _abort(writer)
            return
        self._unauth += 1
        ok = None
        paired = False
        try:
            async with asyncio.timeout(self.handshake_timeout):
                await writer.start_tls(self._ctx)
                sslobj = writer.get_extra_info("ssl_object")
                if sslobj is None or sslobj.version() != "TLSv1.3":
                    raise wire.ProtocolError("not TLS 1.3")
                ok = await self._handshake(reader, writer)
            if isinstance(ok, _PairAsk):
                # A new device with a pairing code: that exchange, under its
                # own deadline, still counted as unauthenticated — then the
                # stream ends. Nothing of it reaches the web server.
                ask, ok, paired = ok, None, True
                self.stats["pair"] += 1
                async with asyncio.timeout(self.pair_timeout):
                    await self._pair(reader, writer, self._pub, ask.sn, ask.msg)
        except (
            TimeoutError,
            wire.ProtocolError,
            OSError,
            ssl.SSLError,
            ConnectionError,
        ):
            ok = None
        except asyncio.CancelledError:
            _abort(writer)
            raise
        finally:
            self._unauth -= 1
        if paired:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            _abort(writer)
            return
        if not ok:
            self.stats["denied"] += 1
            _abort(writer)
            return
        self.stats["accepted"] += 1
        await self._serve(reader, writer, *ok)

    async def _handshake(self, reader, writer):
        """→ ``(member, keys)`` once the client proved it, a :class:`_PairAsk`
        for a pairing code (when this server takes them), else None."""
        sn = secrets.token_bytes(_NONCE_LEN)
        writer.write(wire.encode({"t": "hello", "v": PROTO_V, "nonce": _b64(sn)}))
        await writer.drain()
        msg = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
        if isinstance(msg, dict) and msg.get("t") == "pair" and self._pair is not None:
            return _PairAsk(sn, msg)
        keys = self._keys()
        me = self._self_key()
        good = False
        try:
            if set(msg) != {"t", "v", "nonce", "from", "to", "proof"}:
                raise wire.ProtocolError("bad frame")
            if msg["t"] != "dev" or msg["v"] != PROTO_V:
                raise wire.ProtocolError("bad frame")
            cn = _unb64(msg["nonce"], _NONCE_LEN)
            frm, to = msg["from"], msg["to"]
            got = msg["proof"]
            if not (
                isinstance(frm, str)
                and isinstance(to, str)
                and _KEY_RE.match(frm)
                and _KEY_RE.match(to)
                and isinstance(got, str)
                and _HEX64_RE.match(got)
            ):
                raise wire.ProtocolError("bad frame")
            # Compute the proof whatever the rest says (no timing oracle on
            # which check failed), then require all of it.
            want = proof(
                keys.auth if keys else secrets.token_bytes(32),
                CLIENT_LABEL,
                self._pub,
                sn,
                cn,
                frm,
                to,
            )
            good = (
                hmac.compare_digest(want, got)
                and keys is not None
                and bool(me)
                and to == me
                and frm != me
                and frm in self._members()
            )
        except wire.ProtocolError:
            good = False
        if not good:
            with contextlib.suppress(Exception):
                writer.write(wire.encode({"t": "denied"}))
                await writer.drain()
            return None
        writer.write(
            wire.encode(
                {
                    "t": "ok",
                    "proof": proof(keys.auth, SERVER_LABEL, self._pub, sn, cn, frm, to),
                }
            )
        )
        await writer.drain()
        if self._on_member is not None:
            try:
                self._on_member(frm)  # e.g. "it's up: look for its address"
            except Exception:  # noqa: BLE001
                pass
        return frm, keys

    async def _serve(self, reader, writer, member: str, keys: Keys) -> None:
        """Pipe the authenticated stream to this device's web server, with
        the local port noted as tunneled before a byte is sent. The stream
        is remembered with the member and the keys it proved, so
        :meth:`sweep` can end it once either stops being current."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held = 0
        try:
            sock.setblocking(False)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            tunnel_peers.add(port)
            held = port
            origin = self._origin_port()
            if not origin:
                raise OSError("origin port unknown")
            await loop.sock_connect(sock, ("127.0.0.1", int(origin)))
            o_reader, o_writer = await asyncio.open_connection(sock=sock)
        except (OSError, ValueError, TypeError):
            if held:
                tunnel_peers.discard(held)
            sock.close()
            _abort(writer)
            return
        stream = (member, keys.auth, writer, o_writer)
        self._streams.add(stream)
        self.stats["open"] += 1
        try:
            await pipe(reader, writer, o_reader, o_writer)
        finally:
            self.stats["open"] -= 1
            self._streams.discard(stream)
            # Give the port's hold back a moment after close: the server may
            # still be finishing the last request on it. Holds are counted
            # (tunnel_peers), so a new connection handed the same port in the
            # meantime keeps its own mark.
            loop.call_later(RELEASE_GRACE, tunnel_peers.discard, held)

    def sweep(self) -> int:
        """End every open stream whose member left the roster or whose keys
        are no longer the group's current ones (a removal rotates the key) —
        membership is otherwise only checked at the handshake. → how many."""
        keys = self._keys()
        members = self._members()
        ended = 0
        for stream in list(self._streams):
            member, auth, writer, o_writer = stream
            if (
                keys is not None
                and hmac.compare_digest(auth, keys.auth)
                and member in members
            ):
                continue
            self._streams.discard(stream)
            _abort(writer)
            _abort(o_writer)
            ended += 1
        if ended:
            self.stats["revoked"] = self.stats.get("revoked", 0) + ended
        return ended


# --------------------------------------------------------------------------- #
# Client side (the device reaching out)
# --------------------------------------------------------------------------- #
async def dial(
    url: str,
    fp_hex: str,
    keys: Keys,
    self_key: str,
    target: str,
    *,
    edge_ssl: Optional[ssl.SSLContext],
    tls_ctx: Optional[ssl.SSLContext] = None,
    timeout: float = HANDSHAKE_TIMEOUT,
):
    """Open an authenticated stream to device ``target`` at ``url`` (a relay
    address from its record) → ``(reader, writer)``. Raises
    :class:`TunnelAuthError` (the far end isn't that device), or
    ``OSError``/``relay.RelayError``/``TimeoutError`` (couldn't reach it)."""
    from backend.peer.transport import client_ssl_context

    addr = parse_addr(url)
    expect_fp = bytes.fromhex(fp_hex)
    async with asyncio.timeout(timeout):
        reader, writer = await relay.dial(
            addr, tls_ctx or client_ssl_context(), edge_ssl
        )
        try:
            sslobj = writer.get_extra_info("ssl_object")
            if sslobj is None or sslobj.version() != "TLSv1.3":
                raise TunnelAuthError("protocol")
            pub = pub_from_cert_der(sslobj.getpeercert(binary_form=True))
            # The pin, before anything is sent.
            if pub is None or not hmac.compare_digest(fingerprint_of(pub), expect_fp):
                raise TunnelAuthError("pin")
            try:
                hello = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                if (
                    set(hello) != {"t", "v", "nonce"}
                    or hello["t"] != "hello"
                    or hello["v"] != PROTO_V
                ):
                    raise TunnelAuthError("protocol")
                sn = _unb64(hello["nonce"], _NONCE_LEN)
            except wire.ProtocolError:
                raise TunnelAuthError("protocol") from None
            cn = secrets.token_bytes(_NONCE_LEN)
            writer.write(
                wire.encode(
                    {
                        "t": "dev",
                        "v": PROTO_V,
                        "nonce": _b64(cn),
                        "from": self_key,
                        "to": target,
                        "proof": proof(
                            keys.auth, CLIENT_LABEL, pub, sn, cn, self_key, target
                        ),
                    }
                )
            )
            await writer.drain()
            try:
                ans = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
            except wire.ConnectionClosed:
                raise TunnelAuthError("denied") from None
            except wire.ProtocolError:
                raise TunnelAuthError("protocol") from None
            if ans.get("t") == "denied":
                raise TunnelAuthError("denied")
            want = proof(keys.auth, SERVER_LABEL, pub, sn, cn, self_key, target)
            got = ans.get("proof")
            if (
                ans.get("t") != "ok"
                or set(ans) != {"t", "proof"}
                or not isinstance(got, str)
                or not hmac.compare_digest(want, got)
            ):
                raise TunnelAuthError("proof")
        except BaseException:
            _abort(writer)
            raise
    return reader, writer


#: Request headers a relay in front of a listener adds (Cloudflare's edge
#: and cloudflared always set some of these); this server's own client never
#: sends any of them.
_RELAYED_HEADERS = frozenset(
    {
        b"cdn-loop",
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-real-ip",
        b"true-client-ip",
    }
)


def local_request(head: bytes, port: Optional[int]) -> bool:
    """Whether an HTTP request head was sent straight to ``127.0.0.1:<port>``
    by a local client: exactly one ``Host`` naming this listener, and no
    header a relay (a Cloudflare tunnel) adds. Never raises."""
    if not port:
        return False
    try:
        lines = head.split(b"\r\n")
        if not lines or b" " not in lines[0]:
            return False
        hosts = []
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(b":")
            if not sep:
                return False
            name = name.strip().lower()
            if name.startswith(b"cf-") or name in _RELAYED_HEADERS:
                return False
            if name == b"host":
                hosts.append(value.strip().lower())
        want = {b"127.0.0.1:%d" % port, b"localhost:%d" % port}
        return len(hosts) == 1 and hosts[0] in want
    except Exception:  # noqa: BLE001
        return False


#: ``resolve()`` result: ``(url, fp_hex, keys, self_key, target)`` or None.
Route = Optional[tuple]


class DeviceProxy:
    """A loopback listener standing in for one remote device: each local
    connection becomes one tunnel connection to it. ``resolve()`` gives the
    current route (read per connection, so a re-read address applies to the
    next one); ``on_result(ok, reason)`` hears how each dial went."""

    def __init__(
        self,
        device: str,
        resolve: Callable[[], Route],
        *,
        edge_ssl: Optional[ssl.SSLContext],
        on_result: Optional[Callable[[bool, str], Awaitable | None]] = None,
        dialer=dial,
    ):
        self.device = device
        self._resolve = resolve
        self._edge_ssl = edge_ssl
        self._on_result = on_result
        self._dial = dialer
        self._server: Optional[asyncio.Server] = None
        self._tasks: set = set()

    @property
    def port(self) -> Optional[int]:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port if self.port else ""

    async def start(self) -> int:
        if self._server is None:
            self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        return self.port

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for t in list(self._tasks):
            t.cancel()
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2.0)

    async def _note(self, ok: bool, reason: str) -> None:
        if self._on_result is None:
            return
        try:
            res = self._on_result(ok, reason)
            if asyncio.iscoroutine(res):
                await res
        except Exception:  # noqa: BLE001
            log.debug("device proxy: result hook failed", exc_info=True)

    def drop_streams(self) -> int:
        """End every connection piped through this proxy now (the group's
        key changed: what was authenticated under the old one stops). The
        listener stays. → how many."""
        n = 0
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
                n += 1
        return n

    async def _on_conn(self, l_reader, l_writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            # Only this server's own HTTP client may use the proxy: its first
            # request must be addressed to this very listener and carry none
            # of what a relay adds. A stale Cloudflare tunnel (from a crashed
            # run) that happens to point at this freed-and-reused port is
            # refused before anything is dialed (docs/devices-anywhere.md §4).
            try:
                async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                    head = await l_reader.readuntil(b"\r\n\r\n")
            except (
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ValueError,
                OSError,
            ):
                _abort(l_writer)
                return
            if not local_request(head, self.port):
                _abort(l_writer)
                await self._note(False, "refused_local")
                return
            route = self._resolve()
            if not route:
                _abort(l_writer)
                await self._note(False, "no_address")
                return
            url, fp, keys, me, target = route
            try:
                r_reader, r_writer = await self._dial(
                    url, fp, keys, me, target, edge_ssl=self._edge_ssl
                )
            except TunnelAuthError as e:
                _abort(l_writer)
                await self._note(False, e.reason)
                return
            except (
                OSError,
                TimeoutError,
                ValueError,
                ssl.SSLError,
                wire.ProtocolError,
            ):
                _abort(l_writer)
                await self._note(False, "unreachable")
                return
            await self._note(True, "")
            try:
                r_writer.write(head)
            except (OSError, ConnectionError, ssl.SSLError):
                _abort(l_writer)
                _abort(r_writer)
                return
            await pipe(l_reader, l_writer, r_reader, r_writer)
        finally:
            self._tasks.discard(task)
