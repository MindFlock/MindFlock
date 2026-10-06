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

Authentication: when the server's auth gate is on, every request must carry
``Authorization: Bearer <token>``. The token is resolved lazily and cached
(:func:`auth_token`): ``MINDFLOCK_AUTH_TOKEN`` first, else the
``general.auth_token`` the server persisted in ``settings.json``
(``MINDFLOCK_SETTINGS_FILE`` honored) — read-only, this module never mints or
writes a token. A 401 re-reads it once (the token may have been rotated) and
retries. With the gate off the header is simply ignored by the server.

The token is scoped to the server it belongs to: the settings-file token is
THIS machine's server's credential, so it only ever goes to a loopback address
— a ``--host``/``MINDFLOCK_HOST`` pointing elsewhere gets a token only when
``MINDFLOCK_AUTH_TOKEN`` names one explicitly. And :func:`probe` asks
``/api/config`` WITHOUT a token first, sending one only after MindFlock's own
auth gate answered — so whatever squats the port never sees the credential.

Deliberately urllib-only (no aiohttp/requests): these are a handful of small
JSON calls, and the CLI — and the ``mindflock mcp`` stdio server built on this
module — must work even on an engine-only install where the ``web`` dependency
group was never synced.
"""

from __future__ import annotations

import http.client
import json
import os
import ipaddress
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ClientError",
    "ServerNotFound",
    "RequestTimeout",
    "ApiError",
    "base_url",
    "probe",
    "discover",
    "auth_token",
    "reset_auth_token",
    "get",
    "get_text",
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


class RequestTimeout(ServerNotFound):
    """The server accepted the connection but did not answer within the
    timeout. A subclass of :class:`ServerNotFound` so every existing handler
    still catches it, but distinct because the request may HAVE been processed
    — callers that retry must only retry idempotent requests on this one."""


class ConnectionDropped(ServerNotFound):
    """The connection broke AFTER the request was sent (reset, remote hung up,
    truncated body) — like :class:`RequestTimeout`, the server may have acted
    on it, so only idempotent requests may be retried."""


class ApiError(ClientError):
    """The server answered with an HTTP error; ``message`` is its ``error``
    field when the body was the usual ``{"error": "..."}`` JSON."""

    def __init__(self, status: int, message: str, payload=None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        #: The whole JSON error body when there was one (a 422 carries more
        #: than its sentence — a plan's ``problems``, say).
        self.payload = payload if isinstance(payload, dict) else None


class AuthRejected(ApiError):
    """A server answered 401/403: it is there, but refuses our token. Distinct
    from "no server" so nobody is told to start one, and never retried."""


def auth_hint(base: str) -> str:
    """What to say when ``base`` rejects our token."""
    return (
        "MindFlock server at %s rejected the auth token — set "
        "MINDFLOCK_AUTH_TOKEN to the server's token, or MINDFLOCK_SETTINGS_FILE "
        "to the settings.json it uses (its token may come from its own "
        "environment, not general.auth_token)" % base
    )


def base_url(host: str, port: int) -> str:
    """The ``http://host:port`` base URL for the given host/port."""
    return "http://%s:%d" % (host, port)


def ws_url(base: str, path: str) -> str:
    """``http://…`` base → ``ws://…`` URL for the given path."""
    return "ws" + base[len("http") :] + path


# --------------------------------------------------------------------------- #
# Auth token (read-only; the server owns minting/rotation)
# --------------------------------------------------------------------------- #
_TOKEN_LOCK = threading.Lock()
#: Cached token: ``None`` = not resolved yet, ``""`` = resolved to "no token".
_TOKEN: Optional[str] = None


def _settings_file(env: Mapping[str, str]) -> str:
    """Where the server keeps ``settings.json`` — the same rule as
    :func:`backend.config.settings.settings_path`, restated here so this
    stdlib-only module never imports the settings layer."""
    override = (env.get("MINDFLOCK_SETTINGS_FILE") or "").strip()
    if override:
        return override
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".mindflock", "settings.json")


def _read_token(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the bearer token now: env first, then the settings file.
    Never raises — a missing/unreadable/garbled file just means "no token"."""
    env = os.environ if env is None else env
    tok = (env.get("MINDFLOCK_AUTH_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(_settings_file(env), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        general = data.get("general") if isinstance(data, dict) else None
        if isinstance(general, dict):
            return str(general.get("auth_token") or "").strip()
    except (OSError, ValueError):
        pass
    return ""


def auth_token() -> str:
    """The bearer token to send (``""`` = none), resolved once and cached."""
    global _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN is None:
            _TOKEN = _read_token()
        return _TOKEN


def reset_auth_token() -> None:
    """Forget the cached token so the next request re-reads it."""
    global _TOKEN
    with _TOKEN_LOCK:
        _TOKEN = None


def _refresh_token_after_401(sent: str) -> bool:
    """Re-read the token after a 401; True when it changed (worth a retry)."""
    global _TOKEN
    fresh = _read_token()
    with _TOKEN_LOCK:
        _TOKEN = fresh
    return fresh != sent


def _is_loopback(url: str) -> bool:
    """Whether ``url`` targets this machine (127.0.0.0/8, ::1, localhost)."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _token_for(url: str, token: str) -> str:
    """The token ``url`` may receive: an explicit ``MINDFLOCK_AUTH_TOKEN`` goes
    wherever the user pointed the client; the settings-file token is this
    machine's server's credential and goes to loopback addresses only."""
    if not token:
        return ""
    if (os.environ.get("MINDFLOCK_AUTH_TOKEN") or "").strip():
        return token
    return token if _is_loopback(url) else ""


def _is_gate_refusal(err: "ApiError") -> bool:
    """A 401/403 from MindFlock's own auth gate (``{"error": "unauthorized"}``,
    see ``backend.web.core.auth._deny``) — not any service that says 401."""
    return err.status in (401, 403) and err.message == "unauthorized"


def _request(
    url: str,
    data: Optional[bytes],
    timeout: float,
    method: Optional[str] = None,
    raw: bool = False,
    auth: bool = True,
) -> Any:
    """One round-trip. GET when ``data`` is None, else POST — unless an
    explicit ``method`` (e.g. ``DELETE``) overrides it. Returns the decoded
    JSON body, or the body as text when ``raw`` (``text/plain`` routes).

    Sends the bearer token when one resolves for ``url`` (:func:`_token_for`;
    never when ``auth`` is False); on a 401 the token is re-read once (it may
    have been rotated) and the request retried if it changed."""
    token = _token_for(url, auth_token()) if auth else ""
    try:
        body = _send(url, data, timeout, method, token)
    except ApiError as err:
        if err.status != 401 or not auth:
            raise
        _refresh_token_after_401(token)
        fresh = _token_for(url, auth_token())
        if fresh == token:
            raise
        body = _send(url, data, timeout, method, fresh)
    if raw:
        return body.decode("utf-8", "replace")
    return json.loads(body.decode("utf-8", "replace")) if body else None


def _send(
    url: str,
    data: Optional[bytes],
    timeout: float,
    method: Optional[str],
    token: str,
) -> bytes:
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data is not None else "GET")
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310 — http to localhost
            return resp.read()
    except urllib.error.HTTPError as err:
        # FastAPI error responses are {"error": "..."} JSON; surface that text.
        payload = None
        try:
            payload = json.loads(err.read().decode("utf-8", "replace"))
            message = str(payload.get("error") or payload)
        except Exception:  # noqa: BLE001 — non-JSON error body
            message = "%s %s" % (err.code, err.reason)
        raise ApiError(err.code, message, payload) from None
    except TimeoutError as err:
        # A bare timeout (not wrapped in URLError) is a READ timeout: the
        # connection was made, so the server may have acted on the request.
        raise RequestTimeout(
            "MindFlock server did not answer in %.0fs (%s)" % (timeout, err)
        ) from None
    except urllib.error.URLError as err:
        # urllib wraps failures of connect + send in URLError: the request
        # never fully reached the server, so any method may be retried.
        raise ServerNotFound(
            "%s (%s)" % (NO_SERVER_HINT, getattr(err, "reason", err))
        ) from None
    except (OSError, http.client.HTTPException) as err:
        # Raised bare from getresponse()/read(): the request WAS sent, and the
        # connection broke before the answer (RemoteDisconnected, reset,
        # IncompleteRead) — the server may have acted on it.
        raise ConnectionDropped(
            "connection to the MindFlock server dropped after the request was "
            "sent (%s)" % (err,)
        ) from None


def get(base: str, path: str, timeout: float = REQUEST_TIMEOUT_S) -> Any:
    """GET ``base + path``; returns the decoded JSON body (``None`` when empty)."""
    return _request(base + path, None, timeout)


def get_text(base: str, path: str, timeout: float = REQUEST_TIMEOUT_S) -> str:
    """GET ``base + path`` from a ``text/plain`` route (e.g. ``/history``);
    returns the body as text. Errors are raised exactly like :func:`get`."""
    return _request(base + path, None, timeout, raw=True)


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
    else ``None``. Raises :class:`AuthRejected` when MindFlock's auth gate
    refuses our token there (reporting that as "no server" sent people to
    start a second one); never raises otherwise.

    The first ask carries NO token: only once MindFlock's own gate has
    answered (its ``{"error": "unauthorized"}`` body) is the token sent — so a
    service squatting the port never receives it, and one that merely says
    401 is "not a MindFlock server", not "MindFlock rejected your token"."""
    url = base + "/api/config"
    try:
        cfg = _request(url, None, timeout, auth=False)
    except ApiError as err:
        if not _is_gate_refusal(err):
            return None
        try:
            cfg = _request(url, None, timeout)
        except ApiError as err2:
            if _is_gate_refusal(err2):
                raise AuthRejected(err2.status, auth_hint(base)) from None
            return None
        except ClientError:
            return None
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
    "not found" (with the address named so the mistake is visible). A server
    that rejects our token raises :class:`AuthRejected` instead.
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
