"""Which local connections to the HTTP server came through the device tunnel.

The device tunnel (:mod:`backend.peer.fleet_tunnel`) hands an authenticated
stream to this machine's own web server over a loopback TCP connection, so
the server would see it as coming from 127.0.0.1 — "the person at this
machine". It isn't: it is another of the user's devices. The tunnel notes
the LOCAL port of each such connection here before sending a byte, and
:class:`backend.web.core.tailnet_trust.PeerCaptureMiddleware` marks a request
from one of those ports as tunneled (:mod:`backend.web.core.auth` then treats
it as a remote caller with the gate on).

Stdlib only and import-cheap: the middleware asks on every request.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_PORTS: set = set()

#: What ``scope["client"]`` becomes for a tunneled request: not an IP, so no
#: loopback, tailnet or proxy-trust rule can match it.
CLIENT = ("devices-tunnel", 0)


def add(port: int) -> None:
    with _LOCK:
        _PORTS.add(int(port))


def discard(port: int) -> None:
    with _LOCK:
        _PORTS.discard(int(port))


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
