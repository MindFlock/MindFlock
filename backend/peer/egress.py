"""Allow-listed CONNECT proxy for the sandboxed shared-session agent.

The sandbox has no network of its own. Its only way out is this proxy, a
unix socket at ``run/egress.sock`` reached through the in-sandbox bridge.
It accepts exactly ``CONNECT <name>:443`` for names on the allow-list,
resolves the name *here on the host*, refuses if **any** resolved address is
not globally routable (so DNS can't point the agent at loopback, the LAN,
link-local metadata services, CGNAT, ULA, multicast ...), then connects to
the checked IP literal, never re-resolving.

Only host names and allow/deny decisions are logged, never payloads.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import stat
import time

from backend.peer import paths

__all__ = [
    "EgressProxy",
    "parse_connect",
    "host_allowed",
    "is_public_ip",
    "ConnectError",
]

log = logging.getLogger("mindflock.peer.egress")

HEADER_CAP = 8192
HEADER_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
TUNNEL_MAX_S = 3600.0
MAX_TUNNELS = 64
MAX_HEADER_LINES = 64
ALLOWED_PORT = 443

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(
    rf"^(?=.{{1,253}}\Z)(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]\Z"
)
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,64}\Z")
_VALUE_RE = re.compile(r"^[\x20-\x7e\t]*\Z")

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_SIXTOFOUR = ipaddress.ip_network("2002::/16")


class ConnectError(ValueError):
    """Malformed or disallowed CONNECT request. ``status`` is the HTTP reply."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_connect(head: bytes) -> tuple[str, int]:
    """Parse a request head (without the final blank line) strictly.

    Returns ``(host, port)``; host is a lowercase DNS name (IP literals are
    refused). Raises :class:`ConnectError` for anything else.
    """
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise ConnectError(400, "non-ascii request") from None
    lines = text.split("\r\n")
    if len(lines) > MAX_HEADER_LINES:
        raise ConnectError(431, "too many header lines")
    for line in lines:
        if "\r" in line or "\n" in line or "\0" in line:
            raise ConnectError(400, "bad line ending")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ConnectError(400, "bad request line")
    method, target, version = parts
    if method != "CONNECT":
        raise ConnectError(405, "only CONNECT is supported")
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise ConnectError(400, "bad http version")
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN_RE.match(name) or not _VALUE_RE.match(value):
            raise ConnectError(400, "bad header")
    host, sep, port_s = target.rpartition(":")
    if not sep or not port_s.isdigit() or not port_s.isascii() or len(port_s) > 5:
        raise ConnectError(400, "bad authority")
    port = int(port_s)
    if port_s != str(port):
        raise ConnectError(400, "bad port")
    host = host.lower()
    if not _HOST_RE.match(host):
        raise ConnectError(400, "host must be a DNS name")
    return host, port


def host_allowed(host: str, allow) -> bool:
    """Exact match, or a strict subdomain of a ``.suffix`` entry."""
    for entry in allow:
        e = entry.lower()
        if e.startswith("."):
            if len(host) > len(e) and host.endswith(e):
                return True
        elif host == e:
            return True
    return False


def is_public_ip(ip: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_public_ip(str(addr.ipv4_mapped))
        if any(addr in n for n in _NAT64):
            return is_public_ip(str(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)))
        if addr in _SIXTOFOUR or addr.teredo is not None:
            return False
    elif addr in _CGNAT:
        return False
    return bool(
        addr.is_global
        and not addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
        and not addr.is_multicast
        and not addr.is_reserved
        and not addr.is_unspecified
        and not getattr(addr, "is_site_local", False)
    )


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host, ALLOWED_PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    out: list[str] = []
    for fam, *_rest, sa in infos:
        if fam in (socket.AF_INET, socket.AF_INET6) and sa[0] not in out:
            out.append(sa[0])
    return out


_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def _reply(status: int) -> bytes:
    return f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


class EgressProxy:
    """asyncio CONNECT proxy on a unix socket.

    ``resolver``, ``ip_ok`` and ``upstream_port`` exist for tests only (point
    an allowed name at a local test server); production uses the defaults.
    """

    def __init__(
        self,
        socket_path: str,
        allow: list[str],
        *,
        resolver=None,
        ip_ok=None,
        upstream_port: int = ALLOWED_PORT,
        max_tunnels: int = MAX_TUNNELS,
        header_timeout: float = HEADER_TIMEOUT_S,
        header_cap: int = HEADER_CAP,
        tunnel_max_s: float = TUNNEL_MAX_S,
        connect_timeout: float = CONNECT_TIMEOUT_S,
    ):
        self.socket_path = socket_path
        self.allow = [a.lower() for a in allow]
        self._resolve = resolver or _default_resolver
        self._ip_ok = ip_ok or is_public_ip
        self._upstream_port = upstream_port
        self.max_tunnels = max_tunnels
        self.header_timeout = header_timeout
        self.header_cap = header_cap
        self.tunnel_max_s = tunnel_max_s
        self.connect_timeout = connect_timeout
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._active = 0
        self._stats = {
            "accepted": 0,
            "allowed": 0,
            "denied": 0,
            "rejected": 0,
            "errors": 0,
            "busy": 0,
        }
        self.decisions: list[tuple[str, str]] = (
            []
        )  # (host or "-", "allow"/"deny:<why>"), last 200

    @property
    def stats(self) -> dict:
        return dict(self._stats, active=self._active)

    async def start(self) -> None:
        path = self.socket_path
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(f"refusing to replace non-socket {path}")
            os.unlink(path)
        # run/ is 0700, so the socket is private between bind and chmod.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with paths.unix_addr(path) as addr:
                sock.bind(addr)
            os.chmod(path, 0o600)
            sock.listen(128)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self._server = await asyncio.start_unix_server(self._accept, sock=sock)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):
                pass
            self._server = None
        try:
            if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except OSError:
            pass

    # -- internals -----------------------------------------------------

    def _decide(self, host: str, what: str) -> None:
        self.decisions.append((host, what))
        del self.decisions[:-200]
        if what == "allow":
            log.info("egress allow host=%s", host)
        else:
            log.info("egress deny host=%s (%s)", host, what)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._stats["accepted"] += 1
        if self._active >= self.max_tunnels:
            self._stats["busy"] += 1
            self._decide("-", "deny:busy")
            await self._close(writer, 503)
            return
        self._active += 1
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception:  # never let one connection take the server down
            self._stats["errors"] += 1
            log.debug("egress connection error", exc_info=True)
        finally:
            self._active -= 1
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    async def _close(self, writer: asyncio.StreamWriter, status: int) -> None:
        try:
            writer.write(_reply(status))
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:
            pass
        writer.close()

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        buf = b""
        while True:
            idx = buf.find(b"\r\n\r\n")
            if idx >= 0:
                if idx + 4 > self.header_cap:
                    raise ConnectError(431, "header too large")
                return buf[:idx], buf[idx + 4 :]
            if len(buf) >= self.header_cap:
                raise ConnectError(431, "header too large")
            chunk = await reader.read(min(4096, self.header_cap + 4 - len(buf)))
            if not chunk:
                raise ConnectError(400, "eof before end of header")
            buf += chunk

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head, rest = await asyncio.wait_for(
                self._read_head(reader), self.header_timeout
            )
            host, port = parse_connect(head)
        except asyncio.TimeoutError:
            self._stats["rejected"] += 1
            self._decide("-", "deny:header timeout")
            await self._close(writer, 408)
            return
        except ConnectError as exc:
            self._stats["rejected"] += 1
            self._decide("-", f"deny:{exc.reason}")
            await self._close(writer, exc.status)
            return

        if port != ALLOWED_PORT:
            self._stats["denied"] += 1
            self._decide(host, f"deny:port {port}")
            await self._close(writer, 403)
            return
        if not host_allowed(host, self.allow):
            self._stats["denied"] += 1
            self._decide(host, "deny:not on allow-list")
            await self._close(writer, 403)
            return
        try:
            ips = await asyncio.wait_for(self._resolve(host), self.connect_timeout)
        except Exception:
            ips = []
        if not ips:
            self._stats["denied"] += 1
            self._decide(host, "deny:no address")
            await self._close(writer, 502)
            return
        if not all(isinstance(ip, str) and self._ip_ok(ip) for ip in ips):
            self._stats["denied"] += 1
            self._decide(host, "deny:non-global address")
            await self._close(writer, 403)
            return

        up_r = up_w = None
        for ip in ips:
            try:
                up_r, up_w = await asyncio.wait_for(
                    asyncio.open_connection(ip.split("%", 1)[0], self._upstream_port),
                    self.connect_timeout,
                )
                break
            except Exception:
                continue
        if up_w is None:
            self._stats["errors"] += 1
            self._decide(host, "deny:upstream unreachable")
            await self._close(writer, 502)
            return

        self._stats["allowed"] += 1
        self._decide(host, "allow")
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up_w.write(rest)
            await writer.drain()
            started = time.monotonic()
            await asyncio.wait_for(
                asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer)),
                self.tunnel_max_s,
            )
            log.debug(
                "egress tunnel host=%s closed after %.0fs",
                host,
                time.monotonic() - started,
            )
        except asyncio.TimeoutError:
            log.info("egress tunnel host=%s hit the time cap", host)
        finally:
            up_w.close()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
        if dst.can_write_eof():
            dst.write_eof()
    except (ConnectionError, OSError, RuntimeError):
        pass
    finally:
        if not dst.can_write_eof():
            dst.close()
