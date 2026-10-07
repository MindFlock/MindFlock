"""Tailnet multi-device control — discovery, permission, and proxying.

Every MindFlock server stays standalone; the one the browser is looking at
acts as a *gateway* to the other MindFlock servers it can see on the same
Tailscale network. The moving parts:

* **Discovery.** A background loop shells out to ``tailscale status --json``,
  keeps the *online, non-mobile* peers (phones/tablets never count as
  controllable devices), and probes each for a MindFlock server by GETting
  ``/api/remote/hello`` (a tiny public identity endpoint) on a few candidate
  ports. Peers that answer become "devices" in :data:`_DEVICES`.

* **Permission.** Two-sided, both explicit:

  - the *target* must have ``general.remote_control = "on"`` — every request
    this gateway proxies carries the ``X-MindFlock-Remote`` header, and the
    target's auth middleware 403s remote-flagged requests while the toggle is
    off (see :mod:`backend.web.core.auth`);
  - the *controller* must hold the target's bearer token (entered once in the
    UI, validated against the target, then persisted to
    ``remote_devices.json`` next to the other state files — deliberately NOT
    in the settings document, so it never transits the settings GET).

* **Proxying.** Remote sessions are merged into ``GET /api/instances`` with
  their title namespaced as ``<device>::<title>`` (device = the MagicDNS
  short label — unique per tailnet, unlike hostnames, and can never contain
  ``:``). :class:`RemoteProxyMiddleware` then transparently forwards any HTTP
  *or websocket* request for ``/api/instances/<device>::<title>/…`` to that
  device with the stored token attached. Every per-session feature — live
  terminal, send, queue, diff, commit, push, PR — works on a remote session
  with no per-endpoint code.

* **Starting sessions elsewhere.** A session started from this browser can
  live on any connected device: ``/api/devices/<device>/fwd/<path>`` forwards
  a short allow-list (:data:`_FWD_ALLOWED` — the folder/agent probes behind
  the New Session dialog plus the create itself) to that device, so the
  dialog browses the TARGET's filesystem and the session is born there, a
  peer of the ones started on that device directly.

* **One hop only.** A device answering a remote-flagged ``GET
  /api/instances`` lists only its OWN sessions (never the ones it mirrors),
  and a remote-flagged request is never proxied onward — otherwise two
  paired devices would echo each other's sessions back as ``a::b::title``.

Everything here is best-effort: discovery failures mark devices unreachable
instead of raising, and a missing ``aiohttp``/``tailscale`` just means the
device list stays empty.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from starlette.responses import JSONResponse

from backend import log
from backend.config import config

try:
    import aiohttp
except Exception:  # noqa: BLE001 — engine-only installs have no web deps
    aiohttp = None  # type: ignore[assignment]

# Title namespace separator. Device keys are MagicDNS labels ([a-z0-9-]) so
# the FIRST "::" always splits device from title, even if a title contains it.
NS = "::"
HELLO_PATH = "/api/remote/hello"
# Header stamped on every proxied request so the target can tell "another
# MindFlock is driving me" apart from its own user's browser and apply the
# remote_control permission gate.
REMOTE_HEADER = "X-MindFlock-Remote"

# Mobile OSes never count as controllable devices (tailscale `status` OS names).
_MOBILE_OS = frozenset({"ios", "android", "ipados"})

_DISCOVERY_INTERVAL = 20.0  # s between tailnet sweeps
_INSTANCES_INTERVAL = 5.0  # s between remote /api/instances refreshes
_STALE_AFTER = 90.0  # keep a device visible through this many s of failed probes
_PROBE_TIMEOUT = 2.0
_HTTP_TIMEOUT = 60.0
#: Forwarded routes that legitimately run longer than ``_HTTP_TIMEOUT``. The
#: session plan waits on a model turn the TARGET bounds at its own
#: ``session_plan.TIMEOUT_PLAN`` (75s) before answering with a fallback; giving
#: up at 60s turned every slow plan on another device into a bare
#: "unreachable" 502 instead of that fallback.
_SLOW_FWD_TIMEOUT = {("POST", "/api/session-plan"): 100.0}

# device key (MagicDNS label) -> mutable state dict; single event loop, no lock.
_DEVICES: Dict[str, dict] = {}
_SELF: dict = {}  # identity of this node (filled by the discovery loop)
_SERVER_PORT = 8765  # set by start-up (server passes its real port)

_HTTP: Optional["aiohttp.ClientSession"] = None
# Serializes _HTTP creation: two coroutines racing the check-then-create would
# each build a ClientSession and leak one (never closed by shutdown()).
_HTTP_LOCK = asyncio.Lock()
_TOKENS: Optional[Dict[str, str]] = None


# --------------------------------------------------------------------------- #
# Title namespacing
# --------------------------------------------------------------------------- #
def is_remote_title(title: str) -> bool:
    """True when ``title`` is namespaced to a remote device (``dev::title``)."""
    return NS in (title or "")


def join_title(device: str, title: str) -> str:
    """Namespace a device's session ``title`` under its key (``dev::title``)."""
    return device + NS + title


def split_title(ns_title: str) -> Tuple[str, str]:
    """``"dev::title" -> ("dev", "title")``; a local title comes back as ``("", title)``."""
    if NS not in (ns_title or ""):
        return "", ns_title
    dev, _, bare = ns_title.partition(NS)
    return dev, bare


# --------------------------------------------------------------------------- #
# Settings / token store
# --------------------------------------------------------------------------- #
def remote_control_enabled() -> bool:
    """The *target-side* permission toggle (``general.remote_control``)."""
    try:
        from backend.config import settings as _settings

        return (
            _settings.load_settings().general.remote_control or ""
        ).strip().lower() == "on"
    except Exception:  # noqa: BLE001 — settings must never break the request path
        return False


def _tokens_path() -> str:
    return os.path.join(config.GetConfigDir(), "remote_devices.json")


def _tokens() -> Dict[str, str]:
    global _TOKENS
    if _TOKENS is None:
        try:
            with open(_tokens_path()) as f:
                d = json.load(f)
            _TOKENS = (
                {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
            )
        except (OSError, ValueError):
            _TOKENS = {}
    return _TOKENS


def _persist_tokens() -> None:
    path = _tokens_path()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(_TOKENS or {}, f)
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save remote device tokens: %v", err)


def token_for(device: str) -> str:
    """The stored bearer token for ``device`` (``""`` when unpaired)."""
    return _tokens().get(device, "")


def paired_tokens() -> Dict[str, str]:
    """Every stored device token, ``{device: token}`` (a copy).

    The shared phone link's QR carries these alongside this server's own, so a
    phone that scans it here is signed in on whichever paired device Tailscale
    routes it to (see :mod:`backend.web.core.shared_link`)."""
    return {k: v for k, v in _tokens().items() if v}


def set_token(device: str, token: str) -> None:
    """Persist ``token`` as the credential for ``device``."""
    _tokens()[device] = token
    _persist_tokens()


def forget_device(device: str) -> None:
    """Drop ``device``'s token and clear its cached session snapshot (disconnect)."""
    _tokens().pop(device, None)
    _persist_tokens()
    dev = _DEVICES.get(device)
    if dev:
        dev["instances"] = []
        dev["instances_ok"] = False
        dev["error"] = ""


# --------------------------------------------------------------------------- #
# Tailscale peers
# --------------------------------------------------------------------------- #
def _dns_label(dns_name: str) -> str:
    return (dns_name or "").rstrip(".").split(".")[0].lower()


def _node_entry(node: dict) -> dict:
    ip4 = ""
    for a in node.get("TailscaleIPs") or []:
        if ":" not in a:
            ip4 = a
            break
    return {
        "key": _dns_label(node.get("DNSName") or "")
        or (node.get("HostName") or "").lower(),
        "host": node.get("HostName") or "",
        "dns": (node.get("DNSName") or "").rstrip("."),
        "ip": ip4,
        "os": node.get("OS") or "",
        "online": bool(node.get("Online")),
    }


def tailscale_nodes() -> Tuple[Optional[dict], List[dict]]:
    """``(self_entry | None, [peer entries])`` from ``tailscale status --json``.

    Peers are filtered to *online, non-mobile* nodes — a phone on the tailnet
    must never show up as a controllable device.
    """
    if shutil.which("tailscale") is None:
        return None, []
    try:
        cp = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode != 0:
            return None, []
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None, []
    self_node = data.get("Self") or None
    self_entry = _node_entry(self_node) if self_node else None
    peers = []
    for node in (data.get("Peer") or {}).values():
        entry = _node_entry(node)
        if not entry["online"] or not entry["ip"]:
            continue
        if entry["os"].strip().lower() in _MOBILE_OS:
            continue
        peers.append(entry)
    return self_entry, peers


def self_identity() -> dict:
    """Identity advertised by ``/api/remote/hello`` (and shown as the local
    device group's header). Falls back to the OS hostname off-tailnet."""
    if _SELF:
        return dict(_SELF)
    host = socket.gethostname()
    return {
        "key": host.lower(),
        "host": host,
        "dns": "",
        "ip": "",
        "os": "",
        "online": True,
    }


def hello_json() -> dict:
    """The identity + capability payload served at ``/api/remote/hello`` — what
    a probing gateway reads to decide this node is a controllable MindFlock."""
    from backend import __version__

    ident = self_identity()
    return {
        "app": "mindflock",
        "version": __version__,
        "device": ident["key"],
        "host": ident["host"],
        "remote_control": remote_control_enabled(),
        "auth": _auth_enabled(),
        "shared_link": _shared_link_name(),
    }


def _shared_link_name() -> str:
    """The service this device answers the shared phone link on (``""``)."""
    from backend.web.core import shared_link as _shared_link

    return _shared_link.configured_name()


def _auth_enabled() -> bool:
    from backend.web.core import auth as _auth

    return _auth.auth_enabled()


# --------------------------------------------------------------------------- #
# Probing + background loops
# --------------------------------------------------------------------------- #
async def _http_session() -> "aiohttp.ClientSession":
    global _HTTP
    if _HTTP is None or _HTTP.closed:
        async with _HTTP_LOCK:
            if _HTTP is None or _HTTP.closed:
                _HTTP = aiohttp.ClientSession()
    return _HTTP


def _headers_for(device: str) -> dict:
    headers = {REMOTE_HEADER: self_identity()["key"]}
    tok = token_for(device)
    if tok:
        headers["Authorization"] = "Bearer " + tok
    return headers


def _candidate_bases(peer: dict) -> List[str]:
    """Base URLs worth probing for a peer's MindFlock server, best first:
    the port this server runs on (fleets usually share a config), the default
    web port, then HTTPS via MagicDNS (a peer fronted by ``tailscale serve``)."""
    bases = []
    for port in dict.fromkeys((_SERVER_PORT, 8765)):
        bases.append("http://%s:%d" % (peer["ip"], port))
    if peer.get("dns"):
        bases.append("https://%s" % peer["dns"])
    return bases


async def _probe_peer(peer: dict) -> Optional[Tuple[str, dict]]:
    """``(base_url, hello_dict)`` for the first candidate that answers, else None."""
    session = await _http_session()
    for base in _candidate_bases(peer):
        try:
            async with session.get(
                base + HELLO_PATH,
                timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    continue
                hello = await resp.json(content_type=None)
                if isinstance(hello, dict) and hello.get("app") == "mindflock":
                    return base, hello
        except Exception:  # noqa: BLE001 — closed port, timeout, TLS, not-JSON …
            continue
    return None


def _device_state(key: str) -> dict:
    return _DEVICES.setdefault(
        key,
        {
            "key": key,
            "host": "",
            "os": "",
            "ip": "",
            "base_url": "",
            "reachable": False,
            "remote_control": False,
            "auth": False,
            "version": "",
            "shared_link": "",
            "last_seen": 0.0,
            "instances": [],
            "instances_ok": False,
            "error": "",
        },
    )


async def _discover_once() -> None:
    global _SELF
    self_entry, peers = await asyncio.to_thread(tailscale_nodes)
    if self_entry:
        _SELF = self_entry
    results = await asyncio.gather(*(_probe_peer(p) for p in peers))
    now = time.time()
    seen = set()
    for peer, hit in zip(peers, results):
        key = peer["key"]
        if key == self_identity()["key"]:
            continue  # never list ourselves as a remote device
        seen.add(key)
        dev = _device_state(key)
        dev.update(host=peer["host"], os=peer["os"], ip=peer["ip"])
        if hit:
            base, hello = hit
            dev.update(
                base_url=base,
                reachable=True,
                last_seen=now,
                remote_control=bool(hello.get("remote_control")),
                auth=bool(hello.get("auth")),
                version=str(hello.get("version") or ""),
                shared_link=str(hello.get("shared_link") or ""),
            )
        else:
            dev["reachable"] = False
    # Drop devices that left the tailnet / stopped answering for a while.
    for key in list(_DEVICES):
        dev = _DEVICES[key]
        if key not in seen and now - dev.get("last_seen", 0) > _STALE_AFTER:
            del _DEVICES[key]


def _connected(dev: dict) -> bool:
    """Can we actually drive this device right now?"""
    return bool(
        dev.get("reachable")
        and dev.get("remote_control")
        and (not dev.get("auth") or token_for(dev["key"]))
    )


async def _fetch_instances(dev: dict) -> None:
    session = await _http_session()
    try:
        async with session.get(
            dev["base_url"] + "/api/instances",
            headers=_headers_for(dev["key"]),
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 2),
        ) as resp:
            if resp.status == 401:
                dev.update(instances=[], instances_ok=False, error="invalid token")
                return
            if resp.status == 403:
                dev.update(
                    instances=[],
                    instances_ok=False,
                    error="remote control is off on that device",
                )
                return
            data = await resp.json(content_type=None)
            if isinstance(data, list):
                dev.update(instances=data, instances_ok=True, error="")
    except Exception as err:  # noqa: BLE001
        dev.update(instances_ok=False, error=str(err) or "unreachable")


async def discovery_loop(server_port: int) -> None:
    """Sweep the tailnet for MindFlock devices every ``_DISCOVERY_INTERVAL`` s."""
    global _SERVER_PORT
    _SERVER_PORT = server_port
    if aiohttp is None:
        return
    while True:
        try:
            await _discover_once()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must never die
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("device discovery failed: %v", err)
        await asyncio.sleep(_DISCOVERY_INTERVAL)


async def instances_loop() -> None:
    """Refresh connected devices' session snapshots (feeds the merged sidebar)."""
    if aiohttp is None:
        return
    while True:
        try:
            devs = [d for d in _DEVICES.values() if _connected(d)]
            if devs:
                await asyncio.gather(*(_fetch_instances(d) for d in devs))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("remote instances refresh failed: %v", err)
        await asyncio.sleep(_INSTANCES_INTERVAL)


async def shutdown() -> None:
    """Close the shared HTTP session on server shutdown (best-effort)."""
    global _HTTP
    if _HTTP is not None and not _HTTP.closed:
        try:
            await _HTTP.close()
        except Exception:  # noqa: BLE001
            pass
    _HTTP = None


# --------------------------------------------------------------------------- #
# API payloads
# --------------------------------------------------------------------------- #
def merged_instances() -> List[dict]:
    """Connected devices' sessions, title-namespaced for the merged snapshot."""
    out: List[dict] = []
    for dev in _DEVICES.values():
        if not _connected(dev) or not dev.get("instances_ok"):
            continue
        for inst in dev["instances"]:
            if not isinstance(inst, dict) or not inst.get("title"):
                continue
            # A row the device itself mirrors from a THIRD device (or from us)
            # — an older peer still echoes them. Each device's sessions come
            # from that device, once.
            if inst.get("device") or is_remote_title(str(inst["title"])):
                continue
            entry = dict(inst)
            entry["device"] = dev["key"]
            entry["device_label"] = dev["host"] or dev["key"]
            entry["display_title"] = entry["title"]
            entry["title"] = join_title(dev["key"], entry["title"])
            _namespace_refs(entry, dev["key"])
            out.append(entry)
    return out


def _namespace_refs(entry: dict, device: str) -> None:
    """Namespace the fields of a remote row that name ANOTHER session on the
    same device, the way its own ``title`` is. A bare ``parent`` would point at
    a local session (or at nothing), so the rail could never nest a remote
    worker under its orchestrator nor name the right one in its ``↳`` line."""

    def ref(t):
        if isinstance(t, str) and t and not is_remote_title(t):
            return join_title(device, t)
        return t

    if entry.get("parent"):
        entry["parent"] = ref(entry["parent"])
    order = entry.get("order")
    if isinstance(order, dict) and isinstance(order.get("after"), list):
        entry["order"] = dict(order, after=[ref(t) for t in order["after"]])
    lane = entry.get("lane")
    if isinstance(lane, dict) and lane.get("owner"):
        entry["lane"] = dict(lane, owner=ref(lane["owner"]))


def devices_json() -> dict:
    """The ``GET /api/devices`` payload: this node's identity, the target-side
    remote-control toggle, and every discovered device with its pairing state."""
    ident = self_identity()
    devices = []
    for dev in sorted(_DEVICES.values(), key=lambda d: d["key"]):
        # Only devices where MindFlock has actually answered count — an online
        # peer that merely EXISTS on the tailnet (e.g. this laptop's own
        # Windows-side node) is noise, not a controllable device. last_seen is
        # only ever set by a successful hello, so a device that answered once
        # stays listed (as unreachable) through the 90s stale grace.
        if not dev["reachable"] and not dev.get("last_seen"):
            continue
        devices.append(
            {
                "device": dev["key"],
                "host": dev["host"] or dev["key"],
                "os": dev["os"],
                "ip": dev["ip"],
                "version": dev["version"],
                "shared_link": dev.get("shared_link", ""),
                "reachable": bool(dev["reachable"]),
                "remote_control": bool(dev["remote_control"]),
                "auth": bool(dev["auth"]),
                "has_token": bool(token_for(dev["key"])),
                "needs_token": bool(
                    dev["reachable"]
                    and dev["remote_control"]
                    and dev["auth"]
                    and not token_for(dev["key"])
                ),
                "connected": _connected(dev) and bool(dev.get("instances_ok")),
                "error": dev.get("error", ""),
                "sessions": len(dev.get("instances") or []) if _connected(dev) else 0,
            }
        )
    return {
        "self": {"device": ident["key"], "host": ident["host"], "os": ident["os"]},
        "remote_control": remote_control_enabled(),
        "devices": devices,
    }


async def connect_device(device: str, token: str) -> Tuple[bool, str]:
    """Validate ``token`` against the device and persist it. ``(ok, error)``."""
    dev = _DEVICES.get(device)
    if aiohttp is None or dev is None or not dev.get("reachable"):
        return False, "device not reachable"
    headers = {REMOTE_HEADER: self_identity()["key"]}
    if token:
        headers["Authorization"] = "Bearer " + token
    session = await _http_session()
    try:
        async with session.get(
            dev["base_url"] + "/api/instances",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT * 2),
        ) as resp:
            if resp.status == 401:
                return False, "invalid token"
            if resp.status == 403:
                return False, "remote control is off on that device"
            if resp.status != 200:
                return False, "device answered HTTP %d" % resp.status
            data = await resp.json(content_type=None)
    except Exception as err:  # noqa: BLE001
        return False, str(err) or "unreachable"
    if token:
        set_token(device, token)
    if isinstance(data, list):
        dev.update(instances=data, instances_ok=True, error="")
    return True, ""


# --------------------------------------------------------------------------- #
# The transparent proxy
# --------------------------------------------------------------------------- #
_INSTANCES_PREFIX = "/api/instances/"

# WebSocket close codes used when proxying fails (RFC 6455 §7.4.1): 1011 for an
# unexpected/internal condition (target unreachable, relay handshake failed),
# 1000 for a normal close once the peer stream ends.
_WS_CLOSE_INTERNAL_ERROR = 1011
_WS_CLOSE_NORMAL = 1000


def _split_proxy_path(path: str) -> Optional[Tuple[str, str]]:
    """``(device, rewritten_path)`` when ``path`` targets a namespaced title."""
    if not path.startswith(_INSTANCES_PREFIX):
        return None
    rest = path[len(_INSTANCES_PREFIX) :]
    seg, slash, tail = rest.partition("/")
    if NS not in seg:
        return None
    device, _, bare = seg.partition(NS)
    return device, _INSTANCES_PREFIX + quote(bare, safe="") + (
        slash + tail if slash else ""
    )


_DEVICES_PREFIX = "/api/devices/"
_FWD_SEG = "fwd/"

# What ``/api/devices/<device>/fwd/…`` may reach on the target: exactly what
# the New Session dialog asks while it is aimed at another device. Per-session
# routes don't need it (they ride the namespaced title); settings writes,
# token rotation, restarts … are deliberately not here.
_FWD_ALLOWED = frozenset(
    {
        ("GET", "/api/config"),
        ("GET", "/api/settings"),
        ("GET", "/api/templates"),
        ("GET", "/api/providers"),
        ("GET", "/api/providers/manage"),
        ("GET", "/api/repos/suggest"),
        ("GET", "/api/repos/search"),
        ("GET", "/api/repos/check"),
        ("GET", "/api/browse"),
        ("POST", "/api/mkdir"),
        ("POST", "/api/session-plan"),
        ("POST", "/api/instances"),
    }
)


def _split_fwd_path(path: str) -> Optional[Tuple[str, str]]:
    """``(device, target_path)`` for ``/api/devices/<device>/fwd/api/…``."""
    if not path.startswith(_DEVICES_PREFIX):
        return None
    device, slash, rest = path[len(_DEVICES_PREFIX) :].partition("/")
    if not device or not slash or not rest.startswith(_FWD_SEG):
        return None
    return device, rest[len(_FWD_SEG) - 1 :]


def _has_remote_header(scope) -> bool:
    return any(
        k == REMOTE_HEADER.lower().encode() for k, _ in scope.get("headers") or []
    )


def from_remote(request) -> bool:
    """True when ``request`` was sent by another MindFlock (not a browser)."""
    try:
        return REMOTE_HEADER in request.headers
    except Exception:  # noqa: BLE001
        return False


def _refresh_before_done(send, dev: dict):
    """Wrap ``send`` so a successful create re-reads ``dev``'s sessions BEFORE
    the response finishes: the browser's next ``GET /api/instances`` then
    already lists the new session, instead of waiting out the mirror poll."""
    status = {"code": 0}

    async def wrapped(msg) -> None:
        if msg.get("type") == "http.response.start":
            status["code"] = msg.get("status", 0)
        elif (
            msg.get("type") == "http.response.body"
            and not msg.get("more_body")
            and status["code"] == 200
        ):
            try:
                await _fetch_instances(dev)
            except Exception:  # noqa: BLE001 — the poll will catch up
                pass
        await send(msg)

    return wrapped


class RemoteProxyMiddleware:
    """Forward ``/api/instances/<device>::<title>/…`` (HTTP + websocket) to the
    device that owns the session. Mounted INSIDE the auth gate, so the local
    token is checked first; the target's token is attached on the way out."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        hit = _split_proxy_path(path)
        fwd = None if hit else _split_fwd_path(path)
        if hit is None and fwd is None:
            await self.app(scope, receive, send)
            return
        if _has_remote_header(scope):
            # One hop only: a device never relays another device's request.
            await self._reject(
                scope, receive, send, "remote requests are not relayed", 400
            )
            return
        if fwd is not None:
            method = scope.get("method", "GET") if scope["type"] == "http" else "WS"
            if (method, fwd[1]) not in _FWD_ALLOWED:
                await self._reject(
                    scope,
                    receive,
                    send,
                    "%s %s is not forwarded" % (method, fwd[1]),
                    404,
                )
                return
            hit = fwd
            if method == "POST" and fwd[1] == "/api/instances":
                dev = _DEVICES.get(fwd[0])
                if dev is not None:
                    send = _refresh_before_done(send, dev)
        device, target_path = hit
        dev = _DEVICES.get(device)
        if aiohttp is None or dev is None or not _connected(dev):
            await self._reject_not_connected(scope, receive, send, device)
            return
        qs = (scope.get("query_string") or b"").decode("latin-1")
        url = dev["base_url"] + target_path + (("?" + qs) if qs else "")
        if scope["type"] == "http":
            await self._proxy_http(
                scope,
                receive,
                send,
                dev,
                url,
                timeout=_SLOW_FWD_TIMEOUT.get(
                    (scope.get("method", "GET"), target_path), _HTTP_TIMEOUT
                ),
            )
        else:
            await self._proxy_ws(receive, send, dev, url)

    async def _reject_not_connected(self, scope, receive, send, device: str) -> None:
        """Tell the caller the target device isn't reachable/paired."""
        await self._reject(
            scope, receive, send, "device '%s' is not connected" % device, 502
        )

    async def _reject(self, scope, receive, send, message: str, status: int) -> None:
        """A JSON error for HTTP, or the accept-then-close handshake for a
        websocket (a WS client can't read a close code without the connect
        frame arriving first)."""
        if scope["type"] == "http":
            await JSONResponse({"error": message}, status_code=status)(
                scope, receive, send
            )
        else:
            try:
                await receive()  # websocket.connect
                await send(
                    {"type": "websocket.close", "code": _WS_CLOSE_INTERNAL_ERROR}
                )
            except Exception:  # noqa: BLE001
                pass

    async def _proxy_http(
        self,
        scope,
        receive,
        send,
        dev: dict,
        url: str,
        timeout: float = _HTTP_TIMEOUT,
    ) -> None:
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = _headers_for(dev["key"])
        for k, v in scope.get("headers") or []:
            if k == b"content-type":
                headers["Content-Type"] = v.decode("latin-1")
        session = await _http_session()
        started = False
        try:
            async with session.request(
                scope["method"],
                url,
                data=body or None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                ctype = resp.headers.get("Content-Type", "application/json")
                await send(
                    {
                        "type": "http.response.start",
                        "status": resp.status,
                        "headers": [(b"content-type", ctype.encode("latin-1"))],
                    }
                )
                started = True
                async for chunk in resp.content.iter_chunked(65536):
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
        except Exception as err:  # noqa: BLE001
            if not started:
                # A timeout's str() is empty, which used to read as the
                # meaningless "device 'x' unreachable: ".
                why = str(err) or (
                    "no answer within %ds" % int(timeout)
                    if isinstance(err, asyncio.TimeoutError)
                    else type(err).__name__
                )
                await JSONResponse(
                    {"error": "device '%s' unreachable: %s" % (dev["key"], why)},
                    status_code=502,
                )(scope, receive, send)

    async def _proxy_ws(self, receive, send, dev: dict, url: str) -> None:
        msg = await receive()
        if msg["type"] != "websocket.connect":
            return
        scheme = "wss" if url.startswith("https") else "ws"
        ws_url = scheme + url[url.index("://") :]
        session = await _http_session()
        try:
            async with session.ws_connect(
                ws_url,
                headers=_headers_for(dev["key"]),
                heartbeat=30,
            ) as peer:
                await send({"type": "websocket.accept"})

                async def client_to_peer() -> None:
                    while True:
                        m = await receive()
                        if m["type"] == "websocket.disconnect":
                            await peer.close()
                            return
                        if m["type"] != "websocket.receive":
                            continue
                        if m.get("text") is not None:
                            await peer.send_str(m["text"])
                        elif m.get("bytes") is not None:
                            await peer.send_bytes(m["bytes"])

                async def peer_to_client() -> None:
                    async for pm in peer:
                        if pm.type == aiohttp.WSMsgType.TEXT:
                            await send({"type": "websocket.send", "text": pm.data})
                        elif pm.type == aiohttp.WSMsgType.BINARY:
                            await send({"type": "websocket.send", "bytes": pm.data})
                        else:
                            break
                    try:
                        await send(
                            {"type": "websocket.close", "code": _WS_CLOSE_NORMAL}
                        )
                    except Exception:  # noqa: BLE001 — client already gone
                        pass

                done, pending = await asyncio.wait(
                    {
                        asyncio.create_task(client_to_peer()),
                        asyncio.create_task(peer_to_client()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
        except Exception:  # noqa: BLE001 — handshake with the device failed
            try:
                await send(
                    {"type": "websocket.close", "code": _WS_CLOSE_INTERNAL_ERROR}
                )
            except Exception:  # noqa: BLE001
                pass
