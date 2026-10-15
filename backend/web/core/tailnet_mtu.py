"""Cap the TCP segment size on tailnet connections when the network adapter's
MTU leaves no room for Tailscale's headers.

Tailscale wraps every tunnel packet in WireGuard + UDP + an outer IP header —
up to :data:`OVERHEAD` bytes (outer IPv6). ``tailscale0`` defaults to MTU
1280, so a full tunnel packet leaves the machine as ~1340 bytes. On an
underlay whose MTU is itself 1280 (WSL2's NAT'd ``eth0`` is a measured case)
those packets are dropped, and with no path-MTU feedback through the NAT
nothing ever shrinks them: small requests and pings get through, a large
response body (``/api/instances``, 76 KB) never arrives, and the other device
sees "reachable, timed out".

:func:`clamp_listeners` sets ``TCP_MAXSEG`` on the tailnet LISTENING sockets
(``run.py`` pre-binds them — :mod:`backend.web.core.tailnet_bind`); Linux
copies it to every accepted connection, capping both the MSS we advertise and
the segments we send. :func:`client_connector` does the same for this
server's own connections to other devices (:mod:`backend.web.core.remote`).
Loopback is never touched. ``mindflock doctor`` (``tailnet-mtu``) names the
machine-wide fix for everything that isn't MindFlock.

Linux only (``/sys`` + ``/proc``; no new dependencies), and nothing here
raises: a probe that can't answer means "don't clamp".
``MINDFLOCK_TAILNET_MSS=0`` turns it off; an integer forces that MSS.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple

#: Outer IPv6 (40) + UDP (8) + WireGuard (32): the most Tailscale adds.
OVERHEAD = 80
#: Inner IP + TCP headers the MSS excludes.
INNER_V4 = 40
INNER_V6 = 60
#: Never advertise less than the IPv4 default MSS.
MIN_MSS = 536
#: ``0`` / ``off`` disables the clamp; an integer (88–65535) forces that MSS.
ENV = "MINDFLOCK_TAILNET_MSS"

#: Tailscale's address ranges (CGNAT IPv4, its own ULA prefix).
_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
_RTF_UP = 0x0001
_RTF_REJECT = 0x0200
_SIOCGIFADDR = 0x8915


def is_tailnet_addr(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in net for net in _TAILNET_NETS)


def safe_mss(tunnel_mtu: int, underlay_mtu: int) -> int:
    """The IPv4 MSS that keeps a wrapped tunnel packet within the underlay,
    or ``0`` when the tunnel already fits (no clamp needed)."""
    if tunnel_mtu <= 0 or underlay_mtu <= 0 or tunnel_mtu + OVERHEAD <= underlay_mtu:
        return 0
    return max(MIN_MSS, underlay_mtu - OVERHEAD - INNER_V4)


# --------------------------------------------------------------------------- #
# Interface discovery (roots are parameters so tests can hand in fake trees)
# --------------------------------------------------------------------------- #
def read_mtu(iface: str, sys_root: str = "/") -> int:
    """``/sys/class/net/<iface>/mtu``, or ``0`` when unreadable."""
    try:
        return int(
            (Path(sys_root) / "sys/class/net" / iface / "mtu").read_text().strip()
        )
    except (OSError, ValueError):
        return 0


def _ifaces(sys_root: str) -> List[str]:
    try:
        return sorted(p.name for p in (Path(sys_root) / "sys/class/net").iterdir())
    except OSError:
        return []


def _ipv4_of(iface: str) -> str:
    """The interface's IPv4 address (``SIOCGIFADDR``), ``""`` when none."""
    try:
        import fcntl

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            req = struct.pack("256s", iface.encode()[:15])
            res = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, req)
        return socket.inet_ntoa(res[20:24])
    except Exception:  # noqa: BLE001
        return ""


def tunnel_interface(
    sys_root: str = "/",
    proc_root: str = "/",
    ipv4_of: Callable[[str], str] = _ipv4_of,
) -> str:
    """The interface carrying this node's tailnet addresses: ``tailscale*`` by
    name, else the one holding a Tailscale IPv6 (``/proc/net/if_inet6``) or
    IPv4 address. ``""`` when there is none (no Tailscale here, or WSL reading
    the Windows side's)."""
    names = _ifaces(sys_root)
    for name in names:
        if name.startswith("tailscale"):
            return name
    try:
        lines = (Path(proc_root) / "proc/net/if_inet6").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 6 and len(parts[0]) == 32:
            addr = ":".join(parts[0][i : i + 4] for i in range(0, 32, 4))
            if is_tailnet_addr(addr):
                return parts[5]
    for name in names:
        if name != "lo" and is_tailnet_addr(ipv4_of(name)):
            return name
    return ""


def underlay_interface(proc_root: str = "/", exclude: str = "") -> str:
    """The interface carrying the default route (IPv4, else IPv6), lowest
    metric first; ``exclude`` (the tunnel) and loopback never count."""
    best: Optional[Tuple[int, str]] = None
    try:
        rows = (Path(proc_root) / "proc/net/route").read_text().splitlines()[1:]
    except OSError:
        rows = []
    for row in rows:
        f = row.split()
        try:
            iface, dest, flags, metric, mask = (
                f[0],
                f[1],
                int(f[3], 16),
                int(f[6]),
                f[7],
            )
        except (IndexError, ValueError):
            continue
        if dest != "00000000" or mask != "00000000" or not flags & _RTF_UP:
            continue
        if iface in (exclude, "lo") or (best and best[0] <= metric):
            continue
        best = (metric, iface)
    if best:
        return best[1]
    try:
        rows = (Path(proc_root) / "proc/net/ipv6_route").read_text().splitlines()
    except OSError:
        rows = []
    for row in rows:
        f = row.split()
        try:
            dest, plen, metric, flags, iface = (
                f[0],
                f[1],
                int(f[5], 16),
                int(f[8], 16),
                f[9],
            )
        except (IndexError, ValueError):
            continue
        if (
            dest != "0" * 32
            or plen != "00"
            or not flags & _RTF_UP
            or flags & _RTF_REJECT
        ):
            continue
        if iface in (exclude, "lo") or (best and best[0] <= metric):
            continue
        best = (metric, iface)
    return best[1] if best else ""


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Plan:
    tunnel_if: str = ""
    tunnel_mtu: int = 0
    underlay_if: str = ""
    underlay_mtu: int = 0
    #: The IPv4 MSS to set (``0`` = leave the kernel's own).
    mss: int = 0
    #: ``auto`` (from the MTUs), ``env`` (forced), ``off`` (disabled by env).
    source: str = "auto"

    @property
    def mismatch(self) -> bool:
        """The tunnel doesn't fit inside the underlay (whatever the env says)."""
        return safe_mss(self.tunnel_mtu, self.underlay_mtu) > 0

    def mss_for(self, family: int) -> int:
        """The MSS for a socket of ``family``: an IPv6 inner header is 20 bytes
        bigger, so the computed value shrinks with it (a forced one doesn't)."""
        if not self.mss or self.source != "auto" or family != socket.AF_INET6:
            return self.mss
        return max(MIN_MSS, self.mss - (INNER_V6 - INNER_V4))


def _env_mss(environ) -> Optional[int]:
    """``None`` = auto; ``0`` = off; else the forced MSS."""
    raw = str(environ.get(ENV, "") or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return 0
    if raw.isdigit() and 88 <= int(raw) <= 65535:
        return int(raw)
    return None


def detect(sys_root: str = "/", proc_root: str = "/", ipv4_of=_ipv4_of) -> Plan:
    """The MTUs on this machine and the MSS they call for (env ignored)."""
    if not sys.platform.startswith("linux"):
        return Plan()
    try:
        tun = tunnel_interface(sys_root, proc_root, ipv4_of)
        if not tun:
            return Plan()
        under = underlay_interface(proc_root, exclude=tun)
        tmtu, umtu = read_mtu(tun, sys_root), read_mtu(under, sys_root) if under else 0
        return Plan(tun, tmtu, under, umtu, safe_mss(tmtu, umtu))
    except Exception:  # noqa: BLE001 — a probe that can't answer: no clamp
        return Plan()


def plan(environ=None, **roots) -> Plan:
    """:func:`detect` with ``MINDFLOCK_TAILNET_MSS`` applied."""
    env = os.environ if environ is None else environ
    found = detect(**roots)
    forced = _env_mss(env)
    if forced == 0:
        return replace(found, mss=0, source="off")
    if forced and sys.platform.startswith("linux"):
        return replace(found, mss=forced, source="env")
    return found


_CURRENT: Optional[Plan] = None


def current() -> Plan:
    """:func:`plan` for this process, computed once (MTUs don't move under a
    running server often enough to re-read per connection)."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = plan()
    return _CURRENT


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #
def apply(sock, mss: int) -> bool:
    """``TCP_MAXSEG`` on ``sock``. Never raises."""
    if not mss or not hasattr(socket, "TCP_MAXSEG"):
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, mss)
        return True
    except Exception:  # noqa: BLE001
        return False


def clamp_listeners(socks, the_plan: Optional[Plan] = None) -> str:
    """Clamp every bound (not yet listening) socket in ``socks`` whose address
    is a tailnet one — never loopback. Returns the one startup line to print
    when something was clamped, else ``""``. Never raises."""
    p = the_plan or current()
    if not p.mss:
        return ""
    done = 0
    for s in socks:
        try:
            host = s.getsockname()[0]
        except Exception:  # noqa: BLE001
            continue
        if is_tailnet_addr(host) and apply(s, p.mss_for(s.family)):
            done += 1
    if not done:
        return ""
    if p.source == "env":
        return "tailnet MTU: clamping TCP MSS to %d (%s)" % (p.mss, ENV)
    return (
        "tailnet MTU: tunnel %d over underlay %d — clamping TCP MSS to %d so "
        "large responses reach other devices" % (p.tunnel_mtu, p.underlay_mtu, p.mss)
    )


def socket_factory(addr_info) -> socket.socket:
    """aiohttp's ``socket_factory``: the socket aiohttp would make, clamped
    when it is about to connect to a tailnet address."""
    family, type_, proto, _, address = addr_info
    sock = socket.socket(family=family, type=type_, proto=proto)
    if is_tailnet_addr(address[0]):
        apply(sock, current().mss_for(family))
    return sock


def client_connector():
    """A ``TCPConnector`` for this server's own requests to other devices that
    clamps its tailnet connections, or ``None`` (aiohttp's default) when
    there's nothing to clamp or this aiohttp has no ``socket_factory``
    (< 3.12)."""
    if not current().mss:
        return None
    try:
        import aiohttp

        return aiohttp.TCPConnector(socket_factory=socket_factory)
    except Exception:  # noqa: BLE001
        return None
