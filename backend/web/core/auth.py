"""Bearer-token auth for the web server — the productization safety gate.

The server prints a Tailscale URL and now drives real workflows (creating
sessions, sending prompts, committing), so exposing it on a tailnet with no
login is a real risk. This adds a single shared bearer token, checked by one
ASGI middleware that covers **both** HTTP routes and websockets (the terminal /
events sockets included) before any handler runs.

Design goals: zero friction for the existing localhost workflow, one-scan setup
for the phone.

* **When it's on.** Auth engages when the server is started EXPLICITLY beyond
  localhost (``CS_WEB_MODE`` set to a non-local mode, e.g. tailscale) OR an env
  token is provided (``MINDFLOCK_AUTH_TOKEN``) OR ``MINDFLOCK_AUTH=1``. It stays
  off for a plain localhost run, a bare ``uvicorn``, and the test suite (all of
  which leave ``CS_WEB_MODE`` local/unset) unless forced. ``MINDFLOCK_AUTH=0``
  forces it off. A *persisted* ``general.auth_token`` is only the token VALUE —
  it never flips the gate on by itself (so a local test run whose real settings
  carry one isn't suddenly gated).
* **The token.** Resolved ``MINDFLOCK_AUTH_TOKEN`` env → ``general.auth_token``
  setting → auto-generated once and persisted to the settings store. Printed in
  the startup banner and baked into the ``/m?token=…`` QR so a phone lands
  authenticated in one scan.
* **How a request proves it.** A ``mf_auth`` cookie (set after first auth), an
  ``Authorization: Bearer <token>`` header, or a ``?token=`` query param. A
  valid ``?token=`` triggers a redirect that sets the cookie and strips the
  token from the URL so it doesn't linger in history. A browser navigation with
  no valid token gets a tiny inline login page; an API/websocket call gets a
  401 / close.

Comparisons use ``hmac.compare_digest`` (constant-time). The token is a
capability, not a password — treat the URL+token like an SSH key. A
compromised token is rotated via :func:`rotate_token` (Settings → Security),
which invalidates every issued cookie/QR/paired device at once.

Independent of the token gate — enforced even when it's off — the middleware
refuses browser cross-origin requests (:func:`origin_ok`; WebSocket handshakes
ignore CORS, so this is what stops a malicious webpage from driving the agent
terminals on 127.0.0.1) and DNS-rebinding ``Host`` headers in local mode
(:func:`host_ok`).
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Optional
from urllib.parse import parse_qs

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "mf_auth"
QUERY_PARAM = "token"
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_FORBIDDEN = 4403

# Hostnames that are always this machine. Used by the browser-attack guards
# below (Origin / Host checks) — compared with the port stripped.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Paths reachable without a token (the login page posts here; the favicon keeps
# the tab from 404-spamming the login page; the remote hello is the tailnet
# device-discovery identity ping — tiny, read-only, and it must answer before
# any pairing has happened, see backend.web.core.remote).
_PUBLIC_PATHS = frozenset({"/api/auth", "/favicon.ico", "/api/remote/hello"})

# Requests proxied by ANOTHER MindFlock device carry this header (lower-case
# for the ASGI header list). They're refused outright unless this device's
# `general.remote_control` toggle is on — that toggle is the permission the
# user grants, independent of (and checked before) the token gate.
_REMOTE_HEADER = b"x-mindflock-remote"


def _truthy(v: Optional[str]) -> Optional[bool]:
    if v is None or v == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def _exposed_mode() -> bool:
    """True only when the server was started EXPLICITLY beyond localhost.

    ``run.py`` always exports ``CS_WEB_MODE`` (``tailscale`` by default,
    ``local`` for a localhost run). An UNSET value means neither — a bare
    ``uvicorn`` run or the test suite — and must NOT auto-enable auth, or every
    TestClient call would 401. So: set AND not a local mode == exposed.
    """
    mode = (os.environ.get("CS_WEB_MODE") or "").strip().lower()
    return bool(mode) and mode not in ("local", "localhost")


def _env_token() -> str:
    """Token from the environment only (an explicit enable signal)."""
    return (os.environ.get("MINDFLOCK_AUTH_TOKEN") or "").strip()


def _configured_token() -> str:
    """Env → settings token (no generation). Empty when none is set.

    Used to resolve the token VALUE — not to decide whether auth is on. A
    persisted ``general.auth_token`` is auto-generated as a side effect, so its
    mere presence must not flip auth on (that would break a local test run whose
    real settings happen to carry one); enabling is an explicit signal only
    (see :func:`auth_enabled`).
    """
    env = _env_token()
    if env:
        return env
    try:
        from backend.config import settings as _settings

        return (_settings.load_settings().general.auth_token or "").strip()
    except Exception:  # noqa: BLE001 — settings must never break the request path
        return ""


def _auth_mode() -> str:
    """User's persisted gate choice: ``"on"`` | ``"off"`` | ``"auto"`` (default).

    Read fresh each call so a change in Settings takes effect without a restart.
    Anything unrecognised (including ``""``) means auto.
    """
    try:
        from backend.config import settings as _settings

        mode = (_settings.load_settings().general.auth_mode or "").strip().lower()
    except Exception:  # noqa: BLE001 — settings must never break the request path
        return "auto"
    return mode if mode in ("on", "off", "auto") else "auto"


def effective_mode() -> str:
    """The user's chosen gate mode for display in Settings (``on``/``off``/``auto``).

    Reflects only the persisted preference, not env overrides — the UI select is
    bound to ``general.auth_mode`` and shows what the user picked.
    """
    return _auth_mode()


def auth_enabled() -> bool:
    """Whether the auth gate is active for this process (see module docstring).

    Precedence: the ``MINDFLOCK_AUTH`` env var (operator override) → the user's
    persisted ``general.auth_mode`` (on/off) → an explicit env token → the
    exposed-beyond-localhost heuristic.
    """
    forced = _truthy(os.environ.get("MINDFLOCK_AUTH"))
    if forced is not None:
        return forced
    mode = _auth_mode()
    if mode == "on":
        return True
    if mode == "off":
        return False
    if _env_token():  # an explicitly-provided env token opts in
        return True
    return _exposed_mode()


def get_token() -> str:
    """The active token, generating + persisting one on first use when auth is
    on but nothing is configured. Returns "" only if persistence itself fails."""
    tok = _configured_token()
    if tok:
        return tok
    # Generate once and persist to the settings store so it survives restarts.
    new = secrets.token_urlsafe(32)  # 256 bits — a network-facing capability
    try:
        from backend.config import settings as _settings

        _settings.update_settings(general={"auth_token": new})
        return new
    except Exception:  # noqa: BLE001
        # Couldn't persist — fall back to a process-lifetime token so the server
        # is still protected (it just rotates on restart).
        global _EPHEMERAL
        if not _EPHEMERAL:
            _EPHEMERAL = new
        return _EPHEMERAL


_EPHEMERAL = ""


def rotate_token() -> str:
    """Mint, persist, and return a NEW token — compromise recovery.

    Every previously issued credential (signed-in browser cookies, ``/m?token=``
    QR codes, tokens stored by paired MindFlock devices) stops working the
    moment this returns; the caller re-issues its own cookie from the return
    value. Raises ``RuntimeError`` when the token is pinned by the
    ``MINDFLOCK_AUTH_TOKEN`` env var (rotating the setting would be a silent
    no-op — the env always wins), and lets a settings-store failure propagate
    (a rotation that didn't persist hasn't rotated anything).
    """
    if _env_token():
        raise RuntimeError(
            "the access token is set via MINDFLOCK_AUTH_TOKEN — unset the "
            "env var (and restart) to manage the token here"
        )
    new = secrets.token_urlsafe(32)  # 256 bits — a network-facing capability
    from backend.config import settings as _settings

    _settings.update_settings(general={"auth_token": new})
    # A process-lifetime fallback token from an earlier failed persist (see
    # get_token) is dead now — the settings store is writable again.
    global _EPHEMERAL
    _EPHEMERAL = ""
    return new


def token_valid(candidate: Optional[str]) -> bool:
    """Constant-time compare ``candidate`` against the active token."""
    if not candidate:
        return False
    tok = get_token()
    if not tok:
        return False
    return hmac.compare_digest(str(candidate), tok)


def _cookie_from(headers: list) -> Optional[str]:
    for k, v in headers:
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE_NAME:
                    return val
    return None


def _bearer_from(headers: list) -> Optional[str]:
    for k, v in headers:
        if k == b"authorization":
            s = v.decode("latin-1")
            if s.lower().startswith("bearer "):
                return s[7:].strip()
    return None


def _query_token(query_string: bytes) -> Optional[str]:
    if not query_string:
        return None
    vals = parse_qs(query_string.decode("latin-1")).get(QUERY_PARAM)
    return vals[0] if vals else None


def _set_cookie_kwargs(scope) -> dict:
    secure = scope.get("scheme") in ("https", "wss")
    return {
        "key": COOKIE_NAME,
        "value": get_token(),
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": 60 * 60 * 24 * 365,
    }


def login_page_html() -> str:
    """A tiny self-contained login page (no external assets — a strict deploy
    could otherwise block them). Posts the token to ``/api/auth`` and reloads."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>MindFlock — sign in</title><style>"
        "body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#0f1117;color:#d7dae3;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif}"
        ".card{width:320px;max-width:calc(100vw - 32px);background:#171a24;border:1px solid #2a2f3c;"
        "border-radius:12px;padding:22px}"
        "h1{font-size:16px;margin:0 0 4px}p{font-size:12px;color:#8a90a2;margin:0 0 14px}"
        "input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #2a2f3c;"
        "background:#0f1117;color:#d7dae3;font-size:16px}"
        "button{width:100%;margin-top:10px;padding:10px;border:0;border-radius:8px;background:#7d56f4;"
        "color:#fff;font-size:14px;cursor:pointer}"
        ".err{color:#de613e;font-size:12px;min-height:16px;margin-top:8px}"
        "</style></head><body><form class='card' id='f'>"
        "<h1>MindFlock</h1><p>Enter the access token shown in the server's startup banner.</p>"
        "<input id='t' type='password' autocomplete='current-password' placeholder='Access token' autofocus>"
        "<button type='submit'>Sign in</button><div class='err' id='e'></div></form>"
        "<script>document.getElementById('f').addEventListener('submit',async function(ev){"
        "ev.preventDefault();var t=document.getElementById('t').value.trim();if(!t)return;"
        "try{var r=await fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({token:t})});if(r.ok){location.reload();return;}"
        "document.getElementById('e').textContent='Invalid token';}"
        "catch(e){document.getElementById('e').textContent='Network error';}});</script>"
        "</body></html>"
    )


def _wants_html(headers: list) -> bool:
    for k, v in headers:
        if k == b"accept" and b"text/html" in v.lower():
            return True
    return False


# --------------------------------------------------------------------------- #
# Browser-attack guards: Origin (cross-site WS hijack / CSRF) + Host
# (DNS rebinding). Enforced even when the token gate is OFF — the gate being
# off means "processes on this machine are trusted" (they can `tmux attach`
# to the agent sessions directly anyway), NOT "any webpage in the user's
# browser may drive the agents". WebSocket handshakes ignore CORS entirely,
# so without these checks a malicious page could open
# ``ws://127.0.0.1:8765/api/instances/<title>/terminal`` and type into a
# session with repo write access.
# --------------------------------------------------------------------------- #
def _header_value(headers: list, name: bytes) -> str:
    for k, v in headers:
        if k == name:
            return v.decode("latin-1")
    return ""


def _hostname(value: str) -> str:
    """Lowercased hostname of a ``host[:port]`` value or a URL origin (``""``
    when there's nothing parseable — e.g. the literal ``null`` Origin)."""
    v = (value or "").strip().lower()
    if v in ("", "null"):
        return ""
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8765
        return v[1:].split("]", 1)[0]
    if v.count(":") == 1:  # host:port (a bare IPv6 literal is never valid here)
        return v.rsplit(":", 1)[0]
    return v


def origin_ok(scope) -> bool:
    """False for a browser request from a FOREIGN origin.

    Only requests carrying an ``Origin`` header are judged (browsers attach it
    to every WebSocket handshake and every cross-site/non-GET fetch; curl, the
    CLI, and other MindFlock servers send none). The origin's host must be this
    machine (loopback) or exactly the host the request was addressed to (the
    tailnet name/IP the UI was loaded from). The literal ``null`` origin —
    sandboxed iframes, opaque redirects — is refused.
    """
    headers = scope.get("headers") or []
    origin = _header_value(headers, b"origin")
    if not origin:
        return True
    ohost = _hostname(origin)
    if not ohost:
        return False
    if ohost in _LOOPBACK_HOSTS:
        return True
    return ohost == _hostname(_header_value(headers, b"host"))


def host_ok(scope) -> bool:
    """False for a DNS-rebinding request in local mode.

    A server bound to 127.0.0.1 (``CS_WEB_MODE=local``) is only legitimately
    reachable as a loopback name — a request whose ``Host`` is some public
    domain means a page the browser thinks is that domain has been pointed at
    127.0.0.1 (rebinding), so cross-origin protections no longer apply and we
    refuse it outright. Not enforced for exposed modes (real tailnet/LAN
    hostnames can't be enumerated here — the token gate covers those) or when
    the mode is unset (bare uvicorn, the test suite).
    """
    mode = (os.environ.get("CS_WEB_MODE") or "").strip().lower()
    if mode not in ("local", "localhost"):
        return True
    return (
        _hostname(_header_value(scope.get("headers") or [], b"host")) in _LOOPBACK_HOSTS
    )


async def _deny(scope, receive, send, *, status, message, ws_code) -> None:
    """Reject a request on the security path: a JSON ``error`` for HTTP, or the
    accept-then-close handshake for a WebSocket (the client can't read a close
    code without the connect frame being accepted first)."""
    if scope["type"] == "http":
        await JSONResponse({"error": message}, status_code=status)(scope, receive, send)
    else:
        try:
            await receive()
            await send({"type": "websocket.close", "code": ws_code})
        except Exception:  # noqa: BLE001
            pass


class AuthMiddleware:
    """Pure-ASGI gate covering HTTP and websocket scopes alike."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = scope.get("headers") or []

        # Browser-attack guards (see origin_ok/host_ok): cross-origin pages and
        # DNS-rebinding hosts are refused before ANY other handling — public
        # paths and the token gate included (a cross-site POST /api/auth is
        # still a cross-site request).
        if not origin_ok(scope) or not host_ok(scope):
            await _deny(
                scope,
                receive,
                send,
                status=403,
                message="cross-origin request refused",
                ws_code=_WS_CLOSE_FORBIDDEN,
            )
            return

        # Remote-control permission gate — enforced even when the token gate is
        # off (a localhost-auth-off server on a tailnet must still be able to
        # refuse other MindFlock devices until the user opts in).
        if path not in _PUBLIC_PATHS and any(k == _REMOTE_HEADER for k, _ in headers):
            from backend.web.core import remote as _remote

            if not _remote.remote_control_enabled():
                await _deny(
                    scope,
                    receive,
                    send,
                    status=403,
                    message="remote control is disabled on this device",
                    ws_code=_WS_CLOSE_UNAUTHORIZED,
                )
                return

        if not auth_enabled():
            await self.app(scope, receive, send)
            return
        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        cookie = _cookie_from(headers)
        qtok = _query_token(scope.get("query_string") or b"")
        bearer = _bearer_from(headers)

        if token_valid(cookie) or token_valid(bearer):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._reject_unauthenticated_http(
                scope, receive, send, path, headers, qtok
            )
            return

        # websocket: a valid ?token= is enough (browsers can't set headers on a
        # WS handshake, and the cookie may be absent on a cross-origin phone).
        if token_valid(qtok):
            await self.app(scope, receive, send)
            return
        # Reject: accept the connect frame, then close with our code.
        await _deny(
            scope,
            receive,
            send,
            status=401,
            message="unauthorized",
            ws_code=_WS_CLOSE_UNAUTHORIZED,
        )

    async def _reject_unauthenticated_http(
        self, scope, receive, send, path: str, headers: list, qtok: Optional[str]
    ) -> None:
        """Respond to an HTTP request that failed the cookie/bearer check: honor a
        valid ``?token=`` (QR path) with a cookie-setting redirect, serve the
        inline login page to a browser navigation, or 401 an API call."""
        # A valid ?token= (the QR path): set the cookie and redirect to the
        # same path without the token so it never lingers in history.
        if token_valid(qtok):
            resp = RedirectResponse(url=path or "/", status_code=302)
            resp.set_cookie(**_set_cookie_kwargs(scope))
            await resp(scope, receive, send)
            return
        if _wants_html(headers):
            await HTMLResponse(login_page_html(), status_code=200)(scope, receive, send)
            return
        await _deny(
            scope,
            receive,
            send,
            status=401,
            message="unauthorized",
            ws_code=_WS_CLOSE_UNAUTHORIZED,
        )
