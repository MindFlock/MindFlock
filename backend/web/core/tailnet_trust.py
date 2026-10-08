"""Skip the access token for the user's own Tailscale devices.

The token gate (:mod:`backend.web.core.auth`) is per server and the browser's
sign-in is a per-origin cookie, so a phone that is plainly *yours* — same
tailnet, same Tailscale login — still meets the sign-in page on every machine,
every address and every iOS cookie jar it hasn't seen. Tailscale already knows
who is on the other end of a tailnet connection; this module asks it.

**The rule.** ``general.tailnet_trusted_logins`` lists Tailscale login names
(``you@example.com``). A request skips the token when its tailnet peer is an
UNTAGGED node owned by one of them, per ``tailscale whois``. Empty list = off
(the default). Tagged nodes never qualify — a tag means "owned by the tailnet,
not a person", which is exactly what the MindFlock hosts themselves usually
are — and neither does a node shared in from another account (its owner is the
sharer, who is not on the list). Requests relayed by another MindFlock device
(``X-MindFlock-Remote``) never qualify either: that device's own gate decided
who may use it, and trusting the hop would launder its callers into ours.

**Which peer.** Two ways a tailnet request reaches us:

* **Directly** (``http://<device>:8765``): the transport peer IS the tailnet
  address. Unforgeable — it's the TCP source.
* **Through ``tailscale serve``** (the shared phone link): the connection comes
  from 127.0.0.1 and ``X-Forwarded-For`` names the tailnet client. Any local
  process could send that header, so it is believed only when the loopback
  socket belongs to root (tailscaled) or to this server's own user (who can
  already read the token from settings.json), read from ``/proc/net/tcp``.
  Off Linux there is no cheap way to tell, so serve-proxied requests are never
  trusted there and fall back to the token.

The transport peer is the one uvicorn saw BEFORE its proxy-headers rewrite —
:class:`PeerCaptureMiddleware` stashes it in ``scope["mf_peer"]``. Should a
launch path ever let uvicorn rewrite first, a "direct" peer arrives carrying
forwarding headers and is refused, so the check fails closed either way.

``whois`` answers are cached for :data:`CACHE_TTL` seconds per address (a
subprocess per request would be the slowest thing in the stack). Everything
here is best-effort and never raises: no ``tailscale`` binary, a stopped
daemon, or an unparseable answer all just mean "not trusted".
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: Tailscale's address ranges: CGNAT for IPv4, its ULA prefix for IPv6.
_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

#: Seconds a ``whois`` answer (or a miss) is reused for one address.
CACHE_TTL = 60.0

_WHOIS_TIMEOUT = 3.0

_FORWARD_HEADERS = (b"x-forwarded-for", b"forwarded", b"x-real-ip")
_REMOTE_HEADER = b"x-mindflock-remote"


@dataclass(frozen=True)
class Peer:
    """Who ``tailscale whois`` says is behind a tailnet address."""

    login: str
    tagged: bool
    node: str


_cache: Dict[str, Tuple[float, Optional[Peer]]] = {}
_cache_lock = threading.Lock()


def trusted_logins() -> List[str]:
    """The configured logins, lower-cased (empty = the feature is off)."""
    try:
        from backend.config import settings as _settings

        return list(_settings.load_settings().general.tailnet_trusted_logins)
    except Exception:  # noqa: BLE001 — settings must never break the request path
        return []


def is_tailnet_ip(host: Optional[str]) -> bool:
    try:
        ip = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _TAILNET_NETS)


def _is_loopback(host: Optional[str]) -> bool:
    try:
        ip = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _parse_whois(data: dict) -> Optional[Peer]:
    node = data.get("Node") or {}
    login = str((data.get("UserProfile") or {}).get("LoginName") or "").strip().lower()
    if not login:
        return None
    return Peer(
        login=login,
        tagged=bool(node.get("Tags")),
        node=str(node.get("ComputedName") or node.get("Name") or "").rstrip("."),
    )


def _whois_uncached(ip: str) -> Optional[Peer]:
    if shutil.which("tailscale") is None:
        return None
    try:
        cp = subprocess.run(
            ["tailscale", "whois", "--json", ip],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_WHOIS_TIMEOUT,
        )
        if cp.returncode != 0:
            return None
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return _parse_whois(data) if isinstance(data, dict) else None


def _cached(ip: str) -> Tuple[bool, Optional[Peer]]:
    with _cache_lock:
        hit = _cache.get(ip)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return True, hit[1]
    return False, None


def whois(ip: str) -> Optional[Peer]:
    """The owner of tailnet address ``ip`` (cached), or None."""
    found, peer = _cached(ip)
    if found:
        return peer
    peer = _whois_uncached(ip)
    with _cache_lock:
        _cache[ip] = (time.monotonic(), peer)
    return peer


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# Which tailnet address is this request from?
# --------------------------------------------------------------------------- #
def _header_values(headers: list, name: bytes) -> List[str]:
    return [v.decode("latin-1") for k, v in headers if k == name]


def _forwarded_for(headers: list) -> Optional[str]:
    """The LAST ``X-Forwarded-For`` hop — the one the proxy we verified added
    (a client-supplied value is kept in front of it, never after)."""
    vals = _header_values(headers, b"x-forwarded-for")
    if not vals:
        return None
    return vals[-1].split(",")[-1].strip() or None


#: Loopback addresses as ``/proc/net/tcp{,6}`` prints them (either byte
#: order): 127.0.0.1, ::1, and ::ffff:127.0.0.1.
_PROC_LOOPBACK = frozenset(
    {
        "0100007F",
        "7F000001",
        "00000000000000000000000001000000",
        "00000000000000000000000000000001",
        "0000000000000000FFFF00000100007F",
        "00000000000000000000FFFF7F000001",
    }
)
_TCP_ESTABLISHED = "01"


def _proc_socket_uid(client_port: int, server_port: int) -> Optional[int]:
    """The uid owning the loopback socket ``:client_port -> :server_port``
    (Linux ``/proc/net/tcp{,6}``), or None when it can't be found.

    Only a live ESTABLISHED socket with an inode counts: once a client closes,
    the kernel keeps an orphaned FIN_WAIT/TIME_WAIT mini-socket that it prints
    with uid 0 — so a local user could forge a request, close at once, and
    read as root. Such a row is no answer at all (None → not vouched)."""
    want_local = "%04X" % client_port
    want_remote = "%04X" % server_port
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                next(f, None)  # header
                for line in f:
                    cols = line.split()
                    if len(cols) < 10:
                        continue
                    laddr, _, lport = cols[1].rpartition(":")
                    raddr, _, rport = cols[2].rpartition(":")
                    if (
                        lport == want_local
                        and rport == want_remote
                        and laddr in _PROC_LOOPBACK
                        and raddr in _PROC_LOOPBACK
                        and cols[3] == _TCP_ESTABLISHED
                        and cols[9] != "0"
                    ):
                        return int(cols[7])
        except (OSError, ValueError):
            continue
    return None


def _loopback_peer_vouched(client_port: int, server_port: int) -> bool:
    """True when the loopback connection is tailscaled's (root) or our own
    user's — never another local account forging ``X-Forwarded-For``."""
    if not sys.platform.startswith("linux"):
        return False
    uid = _proc_socket_uid(client_port, server_port)
    return uid is not None and uid in (0, os.getuid())


def peer_ip(scope) -> Optional[str]:
    """The tailnet address this request comes from, when it can be believed."""
    headers = scope.get("headers") or []
    peer = scope.get("mf_peer") or scope.get("client")
    if not peer:
        return None
    host, port = peer[0], peer[1]
    forwarded = any(k in _FORWARD_HEADERS for k, _ in headers)
    if is_tailnet_ip(host):
        # A direct tailnet connection carries no forwarding headers. If it
        # does, uvicorn may have rewritten the peer from them — don't guess.
        return None if forwarded else host
    if _is_loopback(host):
        fwd = _forwarded_for(headers)
        if not fwd or not is_tailnet_ip(fwd):
            return None
        server = scope.get("server") or (None, None)
        if not port or not server[1]:
            return None
        if not _loopback_peer_vouched(int(port), int(server[1])):
            return None
        return fwd
    return None


async def request_trusted(scope) -> bool:
    """Whether this request's tailnet peer is one of the user's own devices."""
    logins = trusted_logins()
    if not logins:
        return False
    headers = scope.get("headers") or []
    if any(k == _REMOTE_HEADER for k, _ in headers):
        return False
    ip = peer_ip(scope)
    if not ip:
        return False
    found, peer = _cached(ip)
    if not found:
        peer = await asyncio.to_thread(whois, ip)
    return bool(peer and not peer.tagged and peer.login in logins)


# --------------------------------------------------------------------------- #
# Settings → Security: what can be trusted here
# --------------------------------------------------------------------------- #
def status() -> dict:
    """What Settings → Security shows: whether ``tailscale`` answers, this
    node's own login, the logins owning untagged devices on the tailnet (the
    choices), and whether shared-link (``tailscale serve``) requests can be
    vouched for on this OS."""
    out: dict = {
        "available": False,
        "self_login": "",
        "self_tagged": False,
        "logins": [],
        "shared_link_supported": sys.platform.startswith("linux"),
        "trusted": trusted_logins(),
    }
    if shutil.which("tailscale") is None:
        return out
    try:
        cp = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode != 0:
            return out
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    users = data.get("User") or {}

    def login_of(uid) -> str:
        return str((users.get(str(uid)) or {}).get("LoginName") or "").strip().lower()

    self_ = data.get("Self") or {}
    out["available"] = True
    out["self_tagged"] = bool(self_.get("Tags"))
    out["self_login"] = "" if out["self_tagged"] else login_of(self_.get("UserID"))
    owners = set()
    for node in [self_, *(data.get("Peer") or {}).values()]:
        if not isinstance(node, dict) or node.get("Tags"):
            continue
        login = login_of(node.get("UserID"))
        if login:
            owners.add(login)
    out["logins"] = sorted(owners)
    return out


class PeerCaptureMiddleware:
    """Outermost ASGI layer: remember the transport peer, then apply
    uvicorn's proxy-headers rewrite (which ``run.py`` turns off at the server
    so it happens here, after the capture). Behaviour for everything
    downstream is unchanged — ``scope["client"]``/``scheme`` are rewritten
    for ``FORWARDED_ALLOW_IPS`` (default 127.0.0.1) exactly as uvicorn would."""

    def __init__(self, app) -> None:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        self._inner = ProxyHeadersMiddleware(
            app, trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and "mf_peer" not in scope:
            scope["mf_peer"] = scope.get("client")
        await self._inner(scope, receive, send)
