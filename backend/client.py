"""Tiny stdlib HTTP client for a running MindFlock server (J1).

Used by the terminal commands (``mindflock new/ls/attach/open/events`` in
:mod:`backend.cli`) so the terminal and the web UI stay one system: the CLI
never spawns its own engine, it talks to the same ``/api/*`` the browser uses.

Server discovery order (:func:`discover`):

1. explicit ``--host`` / ``--port`` flags,
2. ``MINDFLOCK_HOST`` / ``MINDFLOCK_PORT`` environment variables,
3. probe the default ``127.0.0.1:8765``.

A candidate only counts as "found" when ``GET /api/config`` answers quickly
(~1s) with the MindFlock config shape (``default_program`` + ``caps``),
so a random service squatting the port is not mistaken for a server.

Deliberately urllik-only (no aiohttp/requests): these are four small
JSON calls, and the CLI must work even on an engine-only install where the
``web`` dependency group was never synced.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ClientError",
    "ServerNotFound",
    "ApiError",
    "base_url",
    "probe",
    "discover",
    "get",
    "post",
    "put",
    "delete",
    "ws_url",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Probe budget — discovery must feel instant even when nothing is listening.
PROBE_TIMEOUT_S = 1.0
#: Normal request budget (create/ide can shell out server-side).
REQUEST_TIMEOUT_S = 30.0

# What a `mindflock new` failure should tell the user (also used by ls/attach/…).
NO_SERVER_HINT = "no MindFlock server found — start one with `mindflock serve`"


class ClientError(Exception):
    """Base for everything this module raises on purpose."""


class ServerNotFound(ClientError):
    """No running MindFlock server could be located (or verified)."""

    def __init__(self, message: str = NO_SERVER_HINT) -> None:
        super().__init__(message)


class ApiError(ClientError):
    """The server answered with an HTTP error; ``message`` is its ``error``
    field when the body was the usual ``{"error": "..."}`` JSON."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def base_url(host: str, port: int) -> str:
    """The ``http://host:port`` base URL for the given host/port."""
    return "http://%s:%d" % (host, port)


def ws_url(base: str, path: str) -> str:
    """``http://…`` base → ``ws://…`` URL for the given path."""
    return "ws" + base[len("http") :] + path


def _request(
    url: str, data: Optional[bytes], timeout: float, method: Optional[str] = None
) -> Any:
    """One JSON round-trip. GET when ``data`` is None, else POST — unless an
    explicit ``method`` (e.g. ``DELETE``) overrides it."""
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data is not None else "GET")
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310 — http to localhost
            body = resp.read()
    except urllib.error.HTTPError as err:
        # FastAPI error responses are {"error": "..."} JSON; surface that text.
        try:
            payload = json.loads(err.read().decode("utf-8", "replace"))
            message = str(payload.get("error") or payload)
        except Exception:  # noqa: BLE001 — non-JSON error body
            message = "%s %s" % (err.code, err.reason)
        raise ApiError(err.code, message) from None
    except (urllib.error.URLError, OSError, TimeoutError) as err:
        raise ServerNotFound(
            "%s (%s)" % (NO_SERVER_HINT, getattr(err, "reason", err))
        ) from None
    return json.loads(body.decode("utf-8", "replace")) if body else None


def get(base: str, path: str, timeout: float = REQUEST_TIMEOUT_S) -> Any:
    """GET ``base + path``; returns the decoded JSON body (``None`` when empty)."""
    return _request(base + path, None, timeout)


def post(
    base: str,
    path: str,
    payload: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> Any:
    """POST ``payload`` as JSON to ``base + path``; returns the decoded JSON body."""
    data = json.dumps(payload or {}).encode("utf-8")
    return _request(base + path, data, timeout)


def put(
    base: str,
    path: str,
    payload: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> Any:
    """PUT ``payload`` as JSON to ``base + path`` (used by ``mindflock
    accounts`` → PUT /api/settings/auth-profiles)."""
    data = json.dumps(payload or {}).encode("utf-8")
    return _request(base + path, data, timeout, method="PUT")


def delete(base: str, path: str, timeout: float = REQUEST_TIMEOUT_S) -> Any:
    """DELETE round-trip (used by ``mindflock rm`` → DELETE /api/instances/…)."""
    return _request(base + path, None, timeout, method="DELETE")


def probe(base: str, timeout: float = PROBE_TIMEOUT_S) -> Optional[dict]:
    """Return the ``/api/config`` payload when ``base`` is a MindFlock server,
    else ``None``. Never raises."""
    try:
        cfg = get(base, "/api/config", timeout=timeout)
    except ClientError:
        return None
    # The MindFlock fingerprint: two distinctive keys the config endpoint always
    # returns. (``repo_root`` was dropped from /api/config in the 2026-07 legacy
    # cleanup; ``caps`` replaces it here so discovery still recognizes a server.)
    if isinstance(cfg, dict) and "default_program" in cfg and "caps" in cfg:
        return cfg
    return None


def discover(
    host: Optional[str] = None,
    port: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Find a running server; returns its base URL or raises ServerNotFound.

    Explicit ``host``/``port`` (CLI flags) win, then ``MINDFLOCK_HOST`` /
    ``MINDFLOCK_PORT``, then the default ``127.0.0.1:8765``. Whatever address
    is chosen must pass :func:`probe` — an explicit-but-dead address is still
    "not found" (with the address named so the mistake is visible).
    """
    env = os.environ if env is None else env
    explicit = host is not None or port is not None
    if not explicit:
        env_host = (env.get("MINDFLOCK_HOST") or "").strip()
        env_port = (env.get("MINDFLOCK_PORT") or "").strip()
        if env_host:
            host = env_host
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                raise ServerNotFound("MINDFLOCK_PORT is not a number: %r" % env_port)
            explicit = True
        explicit = explicit or bool(env_host)
    base = base_url(host or DEFAULT_HOST, port or DEFAULT_PORT)
    if probe(base) is None:
        if explicit:
            raise ServerNotFound(
                "no MindFlock server answering at %s — start one with `mindflock serve`"
                % base
            )
        raise ServerNotFound()
    return base
