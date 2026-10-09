"""Where tailscale mode binds: loopback plus this node's Tailscale addresses.

Tailscale mode used to bind every interface (``0.0.0.0``), which put the port
on the LAN as well as the tailnet. With the access gate off — the owner's
call, never forced on — a LAN neighbour could then drive the server too. So
:func:`plan` binds ``127.0.0.1`` (the desktop app, a local browser, ``tailscale
serve`` / the shared phone link, which forward to loopback) plus each address
in ``Self.TailscaleIPs`` that this machine can actually bind; nothing else.

Fallbacks, both to the old ``0.0.0.0`` with a console warning:

* tailscaled isn't up (or has no addresses) at boot — :func:`rebind_loop`
  then re-execs once it is, so the narrow bind takes over by itself;
* none of its addresses can be bound here (Tailscale in userspace-networking
  mode, or WSL reading the Windows side's ``tailscale.exe``) — re-execing
  would only find the same answer, so the loop checks bindability first.

``MINDFLOCK_BIND_ALL=1`` (or ``run.py all``) keeps the all-interfaces bind on
purpose — for a phone reaching the server over the LAN instead of Tailscale.

:func:`tailnet_ips` is a small local read of ``tailscale status --json``; a
shared Tailscale resolver can replace it without changing anything else here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import List, Tuple

from backend import log

LOOPBACK = "127.0.0.1"
ALL = "0.0.0.0"

#: Set (to ``1``) to bind every interface in tailscale mode, as before.
BIND_ALL_ENV = "MINDFLOCK_BIND_ALL"
#: Set by ``run.py`` when tailscale mode fell back to ``0.0.0.0`` because
#: tailscaled wasn't up — what arms :func:`rebind_loop`. Survives ``execv``
#: like the rest of the environment, and is cleared by the next boot's plan.
FALLBACK_ENV = "MINDFLOCK_BIND_FALLBACK"
#: How many times a chain of boots re-execed to narrow its bind (one is
#: enough; the cap is what keeps a wrong answer from restarting forever).
_REBINDS_ENV = "MINDFLOCK_BIND_REBINDS"
MAX_REBINDS = 2

#: Seconds between checks while fallen back.
REBIND_INTERVAL = 60.0

#: Why :func:`plan` chose what it did (``run.py`` prints the warnings).
WHY_TAILNET = "tailnet"
WHY_ALL = "all"  # BIND_ALL_ENV / `run.py all`
WHY_NO_TAILNET = "no-tailnet"  # tailscaled not up: fallback, rebind armed
WHY_UNBINDABLE = "unbindable"  # its addresses aren't on this machine


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def bind_all_requested() -> bool:
    return _truthy(os.environ.get(BIND_ALL_ENV, ""))


def tailnet_ips() -> List[str]:
    """This node's Tailscale addresses (IPv4 first) while tailscaled is
    running; ``[]`` otherwise (no CLI, stopped, logged out, an error).
    Never raises."""
    from backend import tailscale_cli

    try:
        data = tailscale_cli.status_json() or {}
    except Exception:  # noqa: BLE001
        return []
    if str(data.get("BackendState") or "") != "Running":
        return []
    out: List[str] = []
    for raw in (data.get("Self") or {}).get("TailscaleIPs") or []:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified:
            continue
        out.append(str(ip))
    return sorted(out, key=lambda a: ":" in a)


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def bindable(host: str) -> bool:
    """Whether ``host`` is an address on this machine (a throwaway bind to an
    ephemeral port). Never raises."""
    try:
        with socket.socket(_family(host), socket.SOCK_STREAM) as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def plan(mode: str) -> Tuple[List[str], str]:
    """``(hosts, why)`` for ``mode`` (``local`` / ``tailscale``): local is
    loopback alone; tailscale is loopback + this node's bindable Tailscale
    addresses, or ``[ALL]`` (asked for, or a fallback — see the module doc)."""
    if mode != "tailscale":
        return [LOOPBACK], ""
    if bind_all_requested():
        return [ALL], WHY_ALL
    ips = tailnet_ips()
    if not ips:
        return [ALL], WHY_NO_TAILNET
    usable = [ip for ip in ips if bindable(ip)]
    if not usable:
        return [ALL], WHY_UNBINDABLE
    return [LOOPBACK] + usable, WHY_TAILNET


def open_sockets(hosts: List[str], port: int) -> List[socket.socket]:
    """One listening-ready socket per host (what ``uvicorn.Server.run(
    sockets=…)`` takes). Raises ``OSError`` when one can't be bound (the port
    is taken) — after closing the ones already opened."""
    socks: List[socket.socket] = []
    try:
        for host in hosts:
            s = socket.socket(_family(host), socket.SOCK_STREAM)
            socks.append(s)
            if os.name != "nt":  # on Windows it lets another process share the port
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if ":" in host:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((host, port))
            s.set_inheritable(True)
    except OSError:
        for s in socks:
            s.close()
        raise
    return socks


def describe(hosts: List[str]) -> str:
    """``127.0.0.1 + 100.64.0.1`` — the banner's ``bind:``."""
    return " + ".join(hosts)


def fell_back() -> bool:
    """Whether this process bound everything because tailscaled wasn't up
    (``run.py`` sets :data:`FALLBACK_ENV`) — the lifespan starts
    :func:`rebind_loop` only then."""
    return os.environ.get(FALLBACK_ENV) == "1"


async def rebind_loop(interval: float = REBIND_INTERVAL) -> None:
    """While fallen back to ``0.0.0.0``: once tailscaled is up and one of its
    addresses can be bound here, re-exec (mode kept) so the narrow bind takes
    over. Capped at :data:`MAX_REBINDS` per chain of boots."""
    from backend.web.core import restart

    while True:
        await asyncio.sleep(interval)
        if not fell_back() or not restart.serving():
            return
        ips = await asyncio.to_thread(tailnet_ips)
        if not ips or not any([await asyncio.to_thread(bindable, ip) for ip in ips]):
            continue
        raw = os.environ.get(_REBINDS_ENV, "")
        done = int(raw) if raw.isdigit() else 0
        if done >= MAX_REBINDS:
            return
        os.environ[_REBINDS_ENV] = str(done + 1)
        msg = (
            "Tailscale is up now — restarting to bind only this machine and "
            "its Tailscale addresses (not every interface)."
        )
        print("  " + msg, flush=True)
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("%s", msg)
            except Exception:  # noqa: BLE001
                pass
        restart.reexec_soon(delay=0.5, keep_mode=True)
        return
