"""Which local connections to the HTTP server came through the device tunnel.

The device tunnel (:mod:`backend.peer.fleet_tunnel`) hands an authenticated
stream to this machine's own web server over a loopback TCP connection, so
the server would see it as coming from 127.0.0.1 — "the person at this
machine". It isn't: it is another of the user's devices. The tunnel notes
the LOCAL port of each such connection here before sending a byte, and
:class:`backend.web.core.tailnet_trust.PeerCaptureMiddleware` marks a request
from one of those ports as tunneled (:mod:`backend.web.core.auth` then treats
it as a remote caller with the gate on).

A mark is COUNTED, not a set member: each tunnel connection takes one hold
on its port (:func:`add`) and gives it back once (:func:`discard`, a moment
after the connection closed, so a request the server is still finishing on
it stays marked). A new connection that is handed the same ephemeral port
inside that moment takes its own hold, so the old connection's late release
can never unmark the live one — the port reads as tunneled until every
holder let go. (Staying marked a few seconds too long only ever makes a
plain local caller look remote: the safe direction.)

Stdlib only and import-cheap: the middleware asks on every request.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_PORTS: dict = {}  # port -> number of holds

#: What ``scope["client"]`` becomes for a tunneled request: not an IP, so no
#: loopback, tailnet or proxy-trust rule can match it.
CLIENT = ("devices-tunnel", 0)


def add(port: int) -> None:
    """Take one hold on ``port`` (one tunnel connection uses it)."""
    port = int(port)
    with _LOCK:
        _PORTS[port] = _PORTS.get(port, 0) + 1


def discard(port: int) -> None:
    """Give back one hold on ``port``; it is unmarked when none is left."""
    port = int(port)
    with _LOCK:
        n = _PORTS.get(port, 0) - 1
        if n > 0:
            _PORTS[port] = n
        else:
            _PORTS.pop(port, None)


def is_tunneled(client) -> bool:
    """Whether ``client`` (an ASGI ``(host, port)``) is one of ours."""
    if not _PORTS or not client:
        return False
    try:
        host, port = client[0], int(client[1])
    except (TypeError, ValueError, IndexError):
        return False
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return False
    with _LOCK:
        return port in _PORTS


def count() -> int:
    with _LOCK:
        return len(_PORTS)
