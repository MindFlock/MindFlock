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
  401 / close. Untagged devices owned by a trusted Tailscale login skip the
  token entirely (opt-in, :mod:`backend.web.core.tailnet_trust`).
* **One origin, several servers.** The shared phone link
  (:mod:`backend.web.core.shared_link`) is one hostname answered by whichever
  of the user's devices is up, each with its OWN token. So the browser keeps a
  cookie per token — ``mf_auth_<hash>`` beside the plain ``mf_auth`` — and a
  server accepts the request when ANY of them is its token. The shared QR
  carries every paired device's token (``?token=a&token=b``); whichever device
  answers the scan validates its own and stores all of them, so the next
  request is signed in no matter which device takes it. A server never accepts
  another device's token: the fan-out widens what the browser remembers, not
  what any server trusts.
* **The fleet key.** The one exception is the key the owner's devices share
  once they are joined ("Your devices", :mod:`backend.web.core.fleet`). Every
  member accepts it beside its own token (:func:`token_valid`), so members
  authenticate to each other with it and a phone that holds it is accepted by
  any of them — on the origin it signed in on (cookies are per origin; the
  shared link is the one origin every member answers). Holding it IS being
  one of the owner's devices; it only ever leaves a member inside an approved
  join. :func:`own_token_valid` is the check that excludes it.
* **Privileged actions.** Some routes (approving a device into the fleet,
  showing a join code) must be done by the person AT this device, never
  relayed by another MindFlock nor reached by an anonymous tailnet caller of a
  gate-off server: :func:`privileged`.

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

import hashlib
import hmac
import os
import re
import secrets
from typing import Iterable, List, Optional
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

# The device-to-device half of joining "Your devices" (backend.web.core.fleet):
# a device that is not yet a member holds no credential here, so redeeming a
# join code, asking to join, and polling for the answer must get past both the
# token gate and the remote-control gate. Matched on (METHOD, exact path) — the
# approve/deny routes beside them are privileged and must stay gated, so no
# prefix match. Each route guards itself (single-use codes, a per-request
# secret, a per-IP rate limit).
_PUBLIC_FLEET_ROUTES = frozenset(
    {("POST", "/api/fleet/redeem"), ("POST", "/api/fleet/requests")}
)
#: ``GET /api/fleet/requests/<id>`` — the joiner's poll; the id is
#: ``token_hex(8)``, so exactly one segment of 16 lower-case hex.
_PUBLIC_FLEET_POLL = re.compile(r"/api/fleet/requests/[a-f0-9]{16}")
#: ``POST /api/fleet/requests/<id>/cancel`` — the joiner withdraws its own
#: request (it proves which with the request's secret).
_PUBLIC_FLEET_CANCEL = re.compile(r"/api/fleet/requests/[a-f0-9]{16}/cancel")

# Member-to-member routes: each handler checks the fleet key itself and
# refuses with the fleet-aware 401 (fleet.unauthorized_body: id, key epoch,
# key fingerprint — nothing secret). A member that missed a key change holds
# an OLD key, so if the gates here answered first (a generic 401, or the
# remote-control 403 for a key that isn't the current one) the caller could
# never learn that, and never hand it the new key. So these exact (METHOD,
# path) pairs get past both gates — except a relayed request bearing this
# device's OWN token while remote control is off: that is a paired
# non-member, which the toggle governs (the export route would accept it).
_MEMBER_FLEET_ROUTES = frozenset(
    {
        ("GET", "/api/fleet/roster"),
        ("POST", "/api/fleet/roster"),
        ("POST", "/api/fleet/rekey"),
        ("POST", "/api/fleet/rotate-token"),
        # Approve from wherever you are: a member hands over the requests
        # waiting on it, and sends back the answer given there.
        ("POST", "/api/fleet/pending"),
        ("POST", "/api/fleet/member-approve"),
        ("POST", "/api/settings/sync/nudge"),
        ("GET", "/api/settings/sync/export"),
    }
)

# Requests proxied by ANOTHER MindFlock device carry this header (lower-case
# for the ASGI header list). They're refused outright unless this device's
# `general.remote_control` toggle is on — that toggle is the permission the
# user grants, independent of (and checked before) the token gate. It only
# governs devices paired by pasted token that are NOT in this device's fleet:
# a request bearing the current fleet key is a member's, and passes.
_REMOTE_HEADER = b"x-mindflock-remote"

#: Most ``mf_auth*`` cookies / ``?token=`` values one request is checked
#: against — one per device on the shared link, with room to spare. A bound,
#: so a request stuffed with candidates can't turn each check into a loop.
_MAX_CANDIDATES = 16

#: What a token may look like to be stored as a cookie. Generated tokens are
#: ``token_urlsafe`` (base64url); an operator-set one is held to the same
#: alphabet before it is ever echoed into a ``Set-Cookie`` header.
_COOKIE_SAFE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")

#: Hostnames that reach this server through a ``tailscale serve`` front — the
#: shared phone link's service name. ``host_ok`` accepts them in local mode
#: (tailscale serve preserves the ``Host`` header, and the proxied request
#: arrives on 127.0.0.1). Registered by :mod:`backend.web.core.shared_link`.
_FRONTED_HOSTS: set = set()


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


def own_token_valid(candidate: Optional[str]) -> bool:
    """Constant-time compare ``candidate`` against THIS device's token only.

    What a route asks when the fleet key must not do: ``POST /api/fleet/adopt``
    hands this device to a fleet, so it takes the token the user pasted on the
    other device (proof they control this one), never a fleet key a member
    already holds."""
    if not candidate:
        return False
    tok = get_token()
    if not tok:
        return False
    return hmac.compare_digest(str(candidate), tok)


def _fleet_key_valid(candidate: Optional[str]) -> bool:
    """Whether ``candidate`` is the fleet key (see :mod:`backend.web.core.fleet`).

    Imported lazily (the fleet store reads settings paths) and never raises —
    a broken fleet store must degrade to "own token only", not to a 500 on
    every request."""
    if not candidate:
        return False
    try:
        from backend.web.core import fleet as _fleet

        return bool(_fleet.key_valid(str(candidate)))
    except Exception:  # noqa: BLE001 — the request path must never break
        return False


def token_valid(candidate: Optional[str]) -> bool:
    """Whether ``candidate`` is a credential this device accepts: its own token
    or the fleet key every one of the owner's devices holds. Constant-time.

    The fleet key is a full credential on every member (holding it == being
    one of the owner's devices), so the bearer, the cookies, ``?token=`` and
    the websocket path all accept it through here."""
    return own_token_valid(candidate) or _fleet_key_valid(candidate)


def any_token_valid(candidates: Iterable[Optional[str]]) -> bool:
    """True when any of ``candidates`` is the active token (each compared in
    constant time; at most :data:`_MAX_CANDIDATES` are looked at)."""
    for i, cand in enumerate(candidates):
        if i >= _MAX_CANDIDATES:
            break
        if token_valid(cand):
            return True
    return False


def cookie_name_for(token: str) -> str:
    """The per-token cookie name (``mf_auth_<12 hex>``) — stable for a token,
    distinct across devices, and reveals nothing about the token itself."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return "%s_%s" % (COOKIE_NAME, digest)


def _cookies_from(headers: list) -> List[str]:
    """Every ``mf_auth`` / ``mf_auth_<hash>`` cookie value, plain one first."""
    plain: List[str] = []
    keyed: List[str] = []
    for k, v in headers:
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE_NAME:
                    plain.append(val)
                elif name.startswith(COOKIE_NAME + "_"):
                    keyed.append(val)
    return (plain + keyed)[:_MAX_CANDIDATES]


def _cookie_from(headers: list) -> Optional[str]:
    vals = _cookies_from(headers)
    return vals[0] if vals else None


def _bearer_from(headers: list) -> Optional[str]:
    for k, v in headers:
        if k == b"authorization":
            s = v.decode("latin-1")
            if s.lower().startswith("bearer "):
                return s[7:].strip()
    return None


def presented_token(scope) -> bool:
    """Whether the request itself carries a credential this device accepts —
    its token or the fleet key, as a cookie or a bearer header — regardless of
    whether the gate is on. What a route that hands out secrets (settings
    sync's export) asks: with the gate off any tailnet caller gets through the
    middleware, but only a credential holder may read credentials."""
    headers = scope.get("headers") or []
    return any_token_valid(_cookies_from(headers)) or token_valid(_bearer_from(headers))


def _from_this_machine(scope) -> bool:
    """Whether the request's TRANSPORT peer is this machine, unproxied.

    The peer is the one :class:`~backend.web.core.tailnet_trust.PeerCaptureMiddleware`
    recorded before any proxy-headers rewrite (``scope["client"]`` as the
    fallback when it isn't mounted). Loopback alone is not enough:
    ``tailscale serve`` delivers tailnet traffic from 127.0.0.1 as well,
    naming the real client in ``X-Forwarded-For`` — so any forwarding header
    disqualifies (fails closed: a local process that adds one just loses the
    shortcut and has to present the token)."""
    from backend.web.core import tailnet_trust as _tailnet_trust

    peer = scope.get("mf_peer") or scope.get("client")
    if not peer:
        return False
    loopback = _tailnet_trust.is_loopback(peer[0])
    return loopback and not _tailnet_trust.has_forward_headers(scope)


def may_see_own_token(scope, *, open_gate: bool = False) -> bool:
    """Whether a route may hand this request THIS device's own access token
    (Settings → Security's reveal, Settings → Mobile's QR, the answer to a
    rotate): the request presents that own token (cookie or bearer), or it
    comes straight from this machine and isn't relayed by another MindFlock.
    ``open_gate``: also yes while the gate is off and the request isn't
    relayed (the whole server is open then anyway).

    NOT a caller that got past the gate with the fleet key — a member, or a
    phone signed in with the devices' key, must not be able to collect every
    device's own token, which would outlive its removal from the group.
    Never raises (fails closed)."""
    try:
        headers = scope.get("headers") or []
        relayed = any(k == _REMOTE_HEADER for k, _ in headers)
        if any(own_token_valid(c) for c in _cookies_from(headers)):
            return True
        if own_token_valid(_bearer_from(headers)):
            return True
        if relayed:
            return False
        if _from_this_machine(scope):
            return True
        return bool(open_gate) and not auth_enabled()
    except Exception:  # noqa: BLE001
        return False


async def privileged(scope) -> bool:
    """Whether this request comes from the person AT this device — what the
    fleet's join/approve/remove routes require (a 403 otherwise).

    * Never when another MindFlock relays it (``X-MindFlock-Remote``): being
      allowed to drive this device is not being here, and a relayed approve
      would let anything that can drive this device enrol new computers into
      the user's devices.
    * Yes when it carries a credential (:func:`presented_token` — the token,
      or the fleet key a signed-in phone holds),
    * or comes straight from this machine (:func:`_from_this_machine` — the
      desktop app, a local browser; the same trust the gate-off localhost run
      already extends),
    * or from one of the user's trusted Tailscale devices
      (:func:`backend.web.core.tailnet_trust.request_trusted`).

    With the gate ON every request that reached a route already passed one of
    these, so this only narrows the gate-OFF case: an anonymous tailnet caller
    of an exposed gate-off server can still use the app, but not hand out the
    fleet key. Never raises."""
    try:
        headers = scope.get("headers") or []
        if any(k == _REMOTE_HEADER for k, _ in headers):
            return False
        if presented_token(scope) or _from_this_machine(scope):
            return True
        from backend.web.core import tailnet_trust as _tailnet_trust

        return bool(await _tailnet_trust.request_trusted(scope))
    except Exception:  # noqa: BLE001 — refuse rather than 500
        return False


def _query_tokens(query_string: bytes) -> List[str]:
    """Every ``?token=`` value — the shared-link QR carries one per device."""
    if not query_string:
        return []
    vals = parse_qs(query_string.decode("latin-1")).get(QUERY_PARAM) or []
    return vals[:_MAX_CANDIDATES]


def _query_token(query_string: bytes) -> Optional[str]:
    vals = _query_tokens(query_string)
    return vals[0] if vals else None


def set_auth_cookies(response, *, secure: bool = False, extra: Iterable[str] = ()):
    """Sign ``response``'s browser in: the plain ``mf_auth`` cookie plus one
    ``mf_auth_<hash>`` per token — this server's own and every ``extra`` one
    (the other devices' tokens from a shared-link QR).

    The plain cookie is what a single-device setup has always used; the keyed
    copies are what survive on the shared origin, where the plain one is
    overwritten by whichever device signed the browser in last. Only the
    caller's already-authenticated request gets here, and each extra token is
    held to :data:`_COOKIE_SAFE` before it is written into a header.
    """
    own = get_token()
    kwargs = {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": 60 * 60 * 24 * 365,
    }
    response.set_cookie(key=COOKIE_NAME, value=own, **kwargs)
    seen = set()
    for i, tok in enumerate([own, *extra]):
        if i >= _MAX_CANDIDATES or not tok or tok in seen:
            continue
        seen.add(tok)
        if not _COOKIE_SAFE.match(tok):
            continue
        response.set_cookie(key=cookie_name_for(tok), value=tok, **kwargs)
    return response


def allow_fronted_host(host: str) -> None:
    """Accept ``host`` as this server's name in local mode (see
    :data:`_FRONTED_HOSTS`)."""
    h = _hostname(host)
    if h:
        _FRONTED_HOSTS.add(h)


def forget_fronted_host(host: str) -> None:
    _FRONTED_HOSTS.discard(_hostname(host))


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
        "<h1>MindFlock</h1><p>Enter this machine's access token: Settings → Security on a "
        "signed-in window, or run <code>mindflock token</code> on this machine.</p>"
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

    A name in :data:`_FRONTED_HOSTS` passes too: that is the shared phone
    link's ``tailscale serve`` hostname, a ``*.ts.net`` name only the tailnet
    resolves — no page can be rebound onto it.
    """
    mode = (os.environ.get("CS_WEB_MODE") or "").strip().lower()
    if mode not in ("local", "localhost"):
        return True
    host = _hostname(_header_value(scope.get("headers") or [], b"host"))
    return host in _LOOPBACK_HOSTS or host in _FRONTED_HOSTS


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


def _member_fleet_route(scope) -> bool:
    """Whether ``scope`` is one of the member-to-member routes (see
    :data:`_MEMBER_FLEET_ROUTES`) — HTTP only, exact method and path."""
    if scope.get("type") != "http":
        return False
    method = str(scope.get("method") or "").upper()
    return (method, scope.get("path", "")) in _MEMBER_FLEET_ROUTES


def _public_fleet_route(scope) -> bool:
    """Whether ``scope`` is one of the public join routes (see
    :data:`_PUBLIC_FLEET_ROUTES`) — HTTP only, exact method and path."""
    if scope.get("type") != "http":
        return False
    method = str(scope.get("method") or "").upper()
    path = scope.get("path", "")
    if (method, path) in _PUBLIC_FLEET_ROUTES:
        return True
    if method == "POST":
        return _PUBLIC_FLEET_CANCEL.fullmatch(path) is not None
    return method == "GET" and _PUBLIC_FLEET_POLL.fullmatch(path) is not None


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
        public = path in _PUBLIC_PATHS or _public_fleet_route(scope)
        member_route = _member_fleet_route(scope)

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
        # refuse other MindFlock devices until the user opts in). It governs
        # devices paired with a pasted token that are NOT members of this
        # device's fleet: a member presenting the CURRENT fleet key passes —
        # membership is the permission (joining turned remote control on, and
        # roster gossip, key changes and settings sync must keep working even
        # if the toggle is switched off later).
        # A member route checks the key itself (see _MEMBER_FLEET_ROUTES) —
        # but this device's own token, relayed, is a paired non-member's.
        if (
            not public
            and any(k == _REMOTE_HEADER for k, _ in headers)
            and not _fleet_key_valid(_bearer_from(headers))
            and not (member_route and not own_token_valid(_bearer_from(headers)))
        ):
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
        if public or member_route:
            await self.app(scope, receive, send)
            return

        cookies = _cookies_from(headers)
        qtoks = _query_tokens(scope.get("query_string") or b"")
        bearer = _bearer_from(headers)

        if any_token_valid(cookies) or token_valid(bearer):
            await self.app(scope, receive, send)
            return

        # The user's own Tailscale devices (Settings → Security → Trusted
        # Tailscale accounts) need no token — see backend.web.core.tailnet_trust.
        from backend.web.core import tailnet_trust as _tailnet_trust

        if await _tailnet_trust.request_trusted(scope):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._reject_unauthenticated_http(
                scope, receive, send, path, headers, qtoks
            )
            return

        # websocket: a valid ?token= is enough (browsers can't set headers on a
        # WS handshake, and the cookie may be absent on a cross-origin phone).
        if any_token_valid(qtoks):
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
        self, scope, receive, send, path: str, headers: list, qtoks: List[str]
    ) -> None:
        """Respond to an HTTP request that failed the cookie/bearer check: honor a
        valid ``?token=`` (QR path) with a cookie-setting redirect, serve the
        inline login page to a browser navigation, or 401 an API call."""
        # A valid ?token= (the QR path): set the cookie and redirect to the
        # same path without the token so it never lingers in history. The
        # shared-link QR carries one token per device; whichever device took
        # the scan stores them all, so the next request is signed in on any.
        if any_token_valid(qtoks):
            resp = RedirectResponse(url=path or "/", status_code=302)
            set_auth_cookies(
                resp,
                secure=scope.get("scheme") in ("https", "wss"),
                extra=qtoks,
            )
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
