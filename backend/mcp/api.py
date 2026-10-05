"""The MCP server's view of the MindFlock HTTP API — a thin, retrying wrapper
over :mod:`backend.client`.

Discovery is lazy (the first call probes; a server that is down at agent start
is not an error until a tool actually needs it) and is redone after any
connection failure, because the usual cause is a server restart.

Retries: connection failures are retried with backoff for a budget —
:data:`NORMAL_RETRY_S` for an ordinary call, :data:`WAIT_RETRY_S` inside the
long waits (a restart mid-wait must not read as "the worker failed"). Only
idempotent requests (GET) are retried after a READ timeout
(:class:`client.RequestTimeout`) or a connection that dropped after the
request was sent (:class:`client.ConnectionDropped`), since the server may
already have acted on a POST; a refused connection is safe to retry for any
method. The final failure becomes a :class:`ToolError` naming the address.

HTTP errors are not retried: :class:`client.ApiError` propagates for the tool
to translate (most become a ``ToolError`` with the server's own message); a
401/403 becomes a ``ToolError`` naming the auth problem at once.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable, List, Mapping, Optional

from backend import client
from backend.mcp.protocol import ToolError

__all__ = ["Api", "NORMAL_RETRY_S", "WAIT_RETRY_S", "quote_title"]

#: Connection-retry budget for an ordinary tool call.
NORMAL_RETRY_S = 5.0
#: Connection-retry budget inside the long waits (rides out a server restart).
WAIT_RETRY_S = 120.0

_IDEMPOTENT = ("GET",)


def quote_title(title: str) -> str:
    """A session title as one URL path segment (titles may hold spaces;
    ``::`` of remote titles stays readable — ``:`` is legal in a segment)."""
    return urllib.parse.quote(title, safe=":@")


def _is_remote_instance_path(path: str) -> bool:
    """A per-instance route for a REMOTE (``device::title``) session — one the
    local server proxies to the peer, passing the peer's status through."""
    bare = path.split("?", 1)[0]
    return bare.startswith("/api/instances/") and "::" in bare


def _auth_error(err: "client.ApiError", path: str, where: str) -> str:
    """The ToolError text for a 401/403 on ``path``.

    A proxied ``device::title`` route carries the PEER's refusal (remote
    control off, a rotated pairing key) — blaming the local token there sends
    the user to fix something that isn't broken, so the peer's reason is
    shown instead. Everything else gets the local-token hint, with the
    server's own words kept."""
    if isinstance(err, client.AuthRejected):
        return err.message
    if _is_remote_instance_path(path):
        return (
            "the remote device refused the request (HTTP %d: %s); the local "
            "server's token is fine — check remote control / pairing on that "
            "device" % (err.status, err.message)
        )
    hint = client.auth_hint(where)
    if err.message and err.message not in hint:
        hint = "%s (server said: %s)" % (hint, err.message)
    return hint


class Api:
    """Lazy-discovering, retrying JSON client for one MindFlock server."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        env: Optional[Mapping[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.port = port
        self.env = env
        self._sleep = sleep
        self._clock = clock
        self._base: Optional[str] = None
        self._config: Optional[dict] = None

    # -- discovery ------------------------------------------------------------ #
    def describe(self) -> str:
        """The address this wrapper talks to (for messages), discovered or not."""
        if self._base:
            return self._base
        host = (
            self.host or (self.env or {}).get("MINDFLOCK_HOST") or client.DEFAULT_HOST
        )
        port = (
            self.port or (self.env or {}).get("MINDFLOCK_PORT") or client.DEFAULT_PORT
        )
        return "http://%s:%s" % (host, port)

    def base(self) -> str:
        """The server's base URL, discovering it on first use."""
        if self._base is None:
            self._base = client.discover(self.host, self.port, self.env)
        return self._base

    def config(self, refresh: bool = False) -> dict:
        """``GET /api/config`` (cached; ``refresh`` re-reads)."""
        if self._config is None or refresh:
            cfg = self.get("/api/config")
            self._config = cfg if isinstance(cfg, dict) else {}
        return self._config

    # -- requests ------------------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        timeout: float = client.REQUEST_TIMEOUT_S,
        retry_s: float = NORMAL_RETRY_S,
        text: bool = False,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> Any:
        """One API call with connection retries (see the module docstring).

        ``sleep`` lets a waiting tool pass its cancellable sleep, so a cancel
        interrupts the backoff too."""
        nap = sleep or self._sleep
        deadline = self._clock() + max(0.0, retry_s)
        delay = 0.25
        while True:
            try:
                base = self.base()
                if text:
                    return client.get_text(base, path, timeout=timeout)
                if method == "GET":
                    return client.get(base, path, timeout=timeout)
                if method == "DELETE":
                    return client.delete(base, path, timeout=timeout)
                return client.post(base, path, payload, timeout=timeout)
            except (client.RequestTimeout, client.ConnectionDropped) as err:
                if method not in _IDEMPOTENT:
                    raise ToolError(
                        "MindFlock server at %s did not answer %s %s; it may "
                        "still have done it — check before retrying (%s)"
                        % (self.describe(), method, path, err)
                    ) from None
                last: Exception = err
            except client.ServerNotFound as err:
                last = err
            except client.ApiError as err:
                if err.status in (401, 403):
                    # A server that refuses our token is THERE: never retried,
                    # never reported as "not reachable".
                    raise ToolError(_auth_error(err, path, self.describe())) from None
                raise
            # Connection-level failure: forget the address (a restarted server
            # may come back elsewhere) and back off until the budget runs out.
            self._base = None
            if self._clock() + delay > deadline:
                raise ToolError(
                    "MindFlock server not reachable at %s (%s). Is `mindflock "
                    "serve` running?" % (self.describe(), last)
                ) from None
            nap(delay)
            delay = min(delay * 2, 5.0)

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, payload: Optional[dict] = None, **kw: Any) -> Any:
        return self.request("POST", path, payload, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def get_text(self, path: str, **kw: Any) -> str:
        return self.request("GET", path, text=True, **kw)

    # -- convenience ---------------------------------------------------------- #
    def instances(self, **kw: Any) -> List[dict]:
        """``GET /api/instances`` as a list of dict rows (never None)."""
        rows = self.get("/api/instances", **kw)
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        )

    @staticmethod
    def inst_path(title: str, suffix: str = "") -> str:
        return "/api/instances/" + quote_title(title) + suffix
