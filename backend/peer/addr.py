"""Peer addresses: where a dialer reaches its listener.

Two carriers, both carrying the SAME pinned-key TLS 1.3 stream:

* ``tcp`` — ``host:port`` (``[v6]:port``): a direct TCP connection to the
  inviter's TLS listener (``peer.listen_port``).
* ``wss`` — ``wss://host:port/path``: a WebSocket over HTTPS to a relay
  (a Cloudflare quick tunnel, or any WebSocket-capable reverse proxy) that
  forwards to the inviter's relay ingress (:mod:`backend.peer.relay`). The
  WebSocket's own TLS is NOT trusted: the peer TLS runs inside it, end to end.

An address is not a secret and not trusted: whoever controls it can at most
deny service, because both ends pin each other's Ed25519 keys. Parsing is
still strict because addresses arrive in pasted codes and API bodies.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

__all__ = [
    "PeerAddr",
    "parse_addr",
    "check_host",
    "check_port",
    "check_path",
    "MAX_PATH_LEN",
]

MAX_PATH_LEN = 200
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\Z")
# Unreserved characters only (RFC 3986): no query, fragment, %-escapes, "..".
_SEGMENT = r"[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,63}"
_PATH_RE = re.compile(rf"^(?:/{_SEGMENT})+\Z")
_TCP_RE = re.compile(
    r"^(?:\[([0-9A-Fa-f:.]{2,45})\]|([A-Za-z0-9.-]{1,253})):([0-9]{1,5})\Z"
)
_WSS_RE = re.compile(
    r"^wss://(?:\[([0-9A-Fa-f:.]{2,45})\]|([A-Za-z0-9.-]{1,253}))"
    r"(?::([0-9]{1,5}))?(/[A-Za-z0-9._~/-]{1,200})\Z"
)


def check_host(host: str) -> str:
    """A DNS name or an IP literal that names a reachable host."""
    if not isinstance(host, str) or not host:
        raise ValueError("bad host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError("bad host: not a reachable address")
        return host
    if len(host) > 253 or not _HOSTNAME_RE.match(host):
        raise ValueError("bad host")
    return host


def check_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("bad port")
    return port


def check_path(path) -> str:
    """A relay path: ``/seg[/seg…]``, unreserved characters only, no ``.``
    or ``..`` segment, at most :data:`MAX_PATH_LEN` characters."""
    if (
        not isinstance(path, str)
        or len(path) > MAX_PATH_LEN
        or not _PATH_RE.match(path)
        or any(seg in (".", "..") for seg in path.split("/")[1:])
    ):
        raise ValueError("bad relay path")
    return path


def _fmt_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@dataclass(frozen=True)
class PeerAddr:
    carrier: str  # "tcp" | "wss"
    host: str
    port: int
    path: str = ""  # wss only

    def __post_init__(self):
        if self.carrier not in ("tcp", "wss"):
            raise ValueError("bad carrier")
        check_host(self.host)
        check_port(self.port)
        if self.carrier == "wss":
            check_path(self.path)
        elif self.path:
            raise ValueError("tcp addresses have no path")

    def __str__(self) -> str:
        if self.carrier == "tcp":
            return f"{_fmt_host(self.host)}:{self.port}"
        return f"wss://{_fmt_host(self.host)}:{self.port}{self.path}"

    @property
    def is_relay(self) -> bool:
        return self.carrier == "wss"

    def public(self) -> str:
        """For display: a relay address without its path (the path carries
        the ingress token, which only keeps scanners out)."""
        if self.carrier == "tcp":
            return str(self)
        return f"wss://{_fmt_host(self.host)}:{self.port}/…"


def parse_addr(text) -> PeerAddr:
    """Parse ``host:port``, ``[v6]:port`` or ``wss://host[:port]/path``
    (port 443 by default). Raises ``ValueError``."""
    if not isinstance(text, str) or not 0 < len(text) <= 600:
        raise ValueError("bad peer address")
    if text.startswith("wss://"):
        m = _WSS_RE.match(text)
        if not m:
            raise ValueError("bad relay address")
        host = m.group(1) or m.group(2)
        if m.group(1) is not None:
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                raise ValueError("bad relay address") from None
        port = int(m.group(3)) if m.group(3) is not None else 443
        return PeerAddr("wss", host, port, m.group(4))
    m = _TCP_RE.match(text)
    if not m:
        raise ValueError("bad peer address")
    host = m.group(1) or m.group(2)
    if m.group(1) is not None:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("bad peer address") from None
    return PeerAddr("tcp", host, int(m.group(3)))
