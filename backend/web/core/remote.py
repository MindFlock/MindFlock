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
    in the settings document, so it never transits the settings GET) — or
    the target must be a member of this device's fleet ("Your devices",
    :mod:`backend.web.core.fleet`), whose shared key every member accepts.

* **Fleet peers.** :func:`fleet_devices` is the narrower list anything that
  ADOPTS another device's data uses: members that answer with this device's
  fleet id, never merely "connected" (a gate-off node needs no credential to
  be driven, and must not be able to push settings here).

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
import re
import socket
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from starlette.responses import JSONResponse

from backend import log
from backend import tailscale_cli as _tailscale_cli
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
        # Every tailnet address (v4 + v6): what a public join request claiming
        # to be this device must come from (backend.web.core.fleet).
        "ips": [str(a) for a in node.get("TailscaleIPs") or [] if a],
        "key": _dns_label(node.get("DNSName") or "")
        or (node.get("HostName") or "").lower(),
        "host": node.get("HostName") or "",
        "dns": (node.get("DNSName") or "").rstrip("."),
        "ip": ip4,
        "os": node.get("OS") or "",
        "online": bool(node.get("Online")),
        # ACL tags ("tag:mindflock") — tagged peers are listed first when
        # discovery says why one didn't answer.
        "tags": [str(t) for t in node.get("Tags") or [] if t],
        # When Tailscale last heard from it (epoch s, 0.0 unknown): "asleep,
        # Tailscale last saw it 2 h ago" for a peer that is offline.
        "ts_last_seen": _ts_time(node.get("LastSeen")),
    }


def _ts_time(raw) -> float:
    """A ``tailscale status`` RFC 3339 time as epoch seconds (0.0 unknown)."""
    try:
        t = _tailscale_cli._parse_time(raw)
    except Exception:  # noqa: BLE001
        return 0.0
    return t.timestamp() if t is not None else 0.0


#: Test/sandbox hook: when set, :func:`tailscale_nodes` reads this file (a
#: ``tailscale status --json`` document) instead of running the CLI — how an
#: end-to-end test stands up several servers on one machine and lets them
#: discover each other as "devices" without a real tailnet. Owned by
#: :mod:`backend.tailscale_cli` (every status reader honors it).
STATUS_FILE_ENV = _tailscale_cli.STATUS_FILE_ENV


def _tailscale_status() -> Optional[dict]:
    """The parsed ``tailscale status --json`` document, or None (the shared,
    briefly cached snapshot — :func:`backend.tailscale_cli.status_json`)."""
    return _tailscale_cli.status_json()


def tailscale_nodes() -> Tuple[Optional[dict], List[dict]]:
    """``(self_entry | None, [peer entries])`` from ``tailscale status --json``
    (or the :data:`STATUS_FILE_ENV` file).

    Peers are filtered to *online, non-mobile* nodes — a phone on the tailnet
    must never show up as a controllable device.
    """
    data = _tailscale_status()
    if data is None:
        _OFFLINE.clear()
        return None, []
    self_node = data.get("Self") or None
    self_entry = _node_entry(self_node) if self_node else None
    peers = []
    offline = {}
    for node in (data.get("Peer") or {}).values():
        entry = _node_entry(node)
        if entry["os"].strip().lower() in _MOBILE_OS:
            continue
        if not entry["online"] or not entry["ip"]:
            # Kept aside (not probed): "asleep, last seen …" when Settings →
            # Devices explains why a device doesn't show.
            if entry["key"]:
                offline[entry["key"]] = entry
            continue
        peers.append(entry)
    _OFFLINE.clear()
    _OFFLINE.update(offline)
    return self_entry, peers


#: The non-mobile peers the last :func:`tailscale_nodes` read found OFFLINE
#: (key -> node entry). Never probed; only ever used to say why a device
#: isn't answering.
_OFFLINE: Dict[str, dict] = {}


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


#: Header the desktop shell's engine check sends to the local hello with its
#: own version (``electron/main.js`` ``fetchLocalJSON``).
SHELL_HEADER = "x-mindflock-shell"

#: The desktop app version last seen on this machine ("" = none seen): a
#: member's engine and its desktop shell update separately, so "Update all my
#: devices" says when the shell on one lags (it updates on its next launch).
_SHELL = {"version": ""}

_SHELL_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")


def note_shell_version(version: str) -> None:
    """Remember the desktop app version this machine's shell reported."""
    v = str(version or "").strip()
    if _SHELL_VERSION_RE.match(v):
        _SHELL["version"] = v


def _update_facts() -> dict:
    """``{commit, install}`` for the hello — the installed commit (so two
    builds that both say 0.7.4 can be told apart) and how this engine is
    installed (``uv-tool`` · ``editable`` · ``other``: whether "Update all my
    devices" can update it). Never raises."""
    try:
        from backend.web.core import self_update as _su

        return {"commit": _su.installed_commit(), "install": _su.install_kind()}
    except Exception:  # noqa: BLE001 — the hello must never fail
        return {"commit": "", "install": ""}


def hello_json() -> dict:
    """The identity + capability payload served at ``/api/remote/hello`` — what
    a probing gateway reads to decide this node is a controllable MindFlock."""
    from backend import __version__

    ident = self_identity()
    return {
        "app": "mindflock",
        "version": __version__,
        **_update_facts(),
        # The desktop app version on this machine, when its shell reported
        # one ("" otherwise — a headless server, or a browser-only setup).
        "shell_version": _SHELL["version"],
        "device": ident["key"],
        "host": ident["host"],
        "remote_control": remote_control_enabled(),
        "auth": _auth_enabled(),
        "shared_link": _shared_link_name(),
        # Whether THIS process answers the shared link right now (its
        # `tailscale serve` for it is up) — the phone-link host table on
        # Settings → Devices ("hosted by mac-mini ✓, rig ✓ · laptop ⚠").
        "shared_link_live": _shared_link_live(),
        # "Your devices": which group this device belongs to ("" = none) and
        # the join protocol it speaks (0 = a MindFlock from before fleets — the
        # UI says "update MindFlock there" instead of offering to join it). The
        # id is not a secret: it names the group, the key is what admits.
        "fleet": _fleet_id(),
        "fleet_proto": FLEET_PROTO,
        # Whether THIS device runs PR review + issue handling for the group.
        "automation": _automation_here(),
    }


def _automation_here() -> bool:
    try:
        from backend.web.core import settings_hooks as _hooks

        fn = getattr(_hooks, "automation_here", None)
        return bool(fn()) if fn is not None else True
    except Exception:  # noqa: BLE001 — the hello must never fail
        return False


#: The "Your devices" join protocol this build speaks (``hello.fleet_proto``).
FLEET_PROTO = 1


def _fleet():
    """:mod:`backend.web.core.fleet`, imported lazily — it calls back into this
    module, and discovery must work even before (or without) a fleet store."""
    from backend.web.core import fleet as _fleet_mod

    return _fleet_mod


def _fleet_id() -> str:
    """This device's fleet id (``""`` when not in one, or the store is broken)."""
    try:
        return str(_fleet().fleet_id() or "")
    except Exception:  # noqa: BLE001 — discovery must never break on the fleet store
        return ""


def _is_member(key: str) -> bool:
    """Whether device ``key`` is a live member of this device's fleet."""
    try:
        return bool(key) and bool(_fleet().is_member(key))
    except Exception:  # noqa: BLE001
        return False


def _fleet_key() -> str:
    try:
        return str(_fleet().fleet_key() or "")
    except Exception:  # noqa: BLE001
        return ""


def _member_device(dev: Optional[dict]) -> bool:
    """Whether the discovered ``dev`` is a live member under the full MagicDNS
    name the roster recorded for it (not merely the same first label)."""
    try:
        return bool(dev) and bool(_fleet().member_device(dev))
    except Exception:  # noqa: BLE001
        return False


def _fleet_key_for(device: str) -> str:
    """The fleet key — when (and only when) it may go to ``device``: a live
    member under its recorded MagicDNS name, whose hello names this fleet,
    and that isn't known to be on another key epoch (then its pasted token,
    if any, is what still works). ``""`` otherwise."""
    dev = _DEVICES.get(device)
    if not dev or not _member_device(dev) or not _same_fleet(dev):
        return ""
    try:
        if _fleet().peer_on_other_epoch(device):
            return ""
    except Exception:  # noqa: BLE001
        pass
    return _fleet_key()


def _shared_link_name() -> str:
    """The service this device answers the shared phone link on (``""``)."""
    from backend.web.core import shared_link as _shared_link

    return _shared_link.configured_name()


def _shared_link_live() -> bool:
    try:
        from backend.web.core import shared_link as _shared_link

        return bool(_shared_link.advertised_url())
    except Exception:  # noqa: BLE001 — the hello must never fail
        return False


def listening() -> str:
    """Where this server listens, as far as other devices are concerned:
    ``"local"`` (bound to 127.0.0.1 — none of them can reach it), ``"tailnet"``
    (any other mode the launcher exported), ``""`` when unknown (a bare
    uvicorn run or a test, which export nothing)."""
    mode = (os.environ.get("CS_WEB_MODE") or "").strip().lower()
    if not mode:
        return ""
    return "local" if mode in ("local", "localhost") else "tailnet"


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


def _headers_for(
    device: str, *, auth: bool = True, bearer: Optional[str] = None
) -> dict:
    """Headers for a request to ``device``: always the remote marker, plus the
    credential — an explicit ``bearer`` when given (the fleet's own routes
    pass the key they mean: gossip, rekey under the OLD key, token rotation;
    a join presenting the pasted token), else the fleet key when it may go
    to ``device`` (:func:`_fleet_key_for`: a member under its recorded
    MagicDNS name that says it is in this fleet), else the pasted token for
    it. ``auth=False`` sends no credential at all (the public join routes: a
    device that isn't a member yet must not spray its pasted token at an
    endpoint that doesn't need it)."""
    headers = {REMOTE_HEADER: self_identity()["key"]}
    tok = bearer
    if tok is None and auth:
        tok = _fleet_key_for(device) or token_for(device)
    if tok:
        headers["Authorization"] = "Bearer " + tok
    return headers


def _candidate_bases(peer: dict) -> List[str]:
    """Base URLs worth probing for a peer's MindFlock server, best first:
    the port this server runs on (fleets usually share a config), the default
    web port, then HTTPS via MagicDNS (a peer fronted by ``tailscale serve``)."""
    bases = []
    for port in dict.fromkeys((_SERVER_PORT, 8765)) if peer.get("ip") else ():
        bases.append("http://%s:%d" % (peer["ip"], port))
    if peer.get("dns"):
        bases.append("https://%s" % peer["dns"])
    return bases


#: What one probe of a peer found (``probe_outcome``), most telling first:
#: a peer that answered something beats one we couldn't reach at all.
_OUTCOME_RANK = {"ok": 9, "not_mindflock": 3, "tls": 2, "refused": 1, "timeout": 1}

#: Plain words for a connection error (remote errors shown to people used to
#: be raw aiohttp strings: "Cannot connect to host 100.x:8765 ssl:default
#: [Connect call failed ('100.x', 8765)]").
OUTCOME_TEXT = {
    "refused": "connection refused",
    "timeout": "timed out",
    "tls": "the HTTPS connection failed",
    "not_mindflock": "something else answers there",
    "unreachable": "unreachable",
}


def probe_outcome(err: BaseException) -> str:
    """Classify a failed request to another device: ``refused`` (nothing
    listens on that address — MindFlock isn't running, or it is local-only
    there), ``timeout`` (packets dropped — usually a tailnet policy that
    doesn't open the port), ``tls`` (the HTTPS front failed), or
    ``unreachable``. Never raises."""
    import errno as _errno
    import ssl as _ssl

    try:
        if aiohttp is not None:
            if isinstance(err, aiohttp.ClientSSLError):
                return "tls"
            if isinstance(err, aiohttp.ContentTypeError):
                return "not_mindflock"
        if isinstance(err, _ssl.SSLError):
            return "tls"
        if isinstance(err, (asyncio.TimeoutError, TimeoutError, socket.timeout)):
            return "timeout"
        os_err = getattr(err, "os_error", None) or err
        if isinstance(os_err, ConnectionRefusedError):
            return "refused"
        code = getattr(os_err, "errno", None)
        if code == _errno.ECONNREFUSED:
            return "refused"
        if code == _errno.ETIMEDOUT or isinstance(os_err, TimeoutError):
            return "timeout"
        if isinstance(err, ValueError):  # not JSON
            return "not_mindflock"
    except Exception:  # noqa: BLE001
        pass
    return "unreachable"


def plain_error(err: BaseException) -> str:
    """A request error as a few plain words (see :data:`OUTCOME_TEXT`)."""
    return OUTCOME_TEXT.get(probe_outcome(err), "unreachable")


def _better(cur: str, new: str) -> str:
    """Keep the first candidate's outcome (the tailnet IP and port — the
    address that says the most) unless a later one is more telling."""
    if not cur:
        return new

    def rank(o):
        return 3 if o.startswith("http_") else _OUTCOME_RANK.get(o, 0)

    return new if rank(new) > rank(cur) else cur


async def _probe_peer(peer: dict) -> Optional[Tuple[str, dict]]:
    """``(base_url, hello_dict)`` for the first candidate that answers, else
    None. What every candidate found is left on ``peer["probe"]`` (``ok``,
    ``refused``, ``timeout``, ``tls``, ``not_mindflock``, ``http_<n>``,
    ``unreachable``) — how Settings → Devices says why a device isn't
    listed."""
    session = await _http_session()
    outcome = ""
    for base in _candidate_bases(peer):
        try:
            async with session.get(
                base + HELLO_PATH,
                timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    outcome = _better(outcome, "http_%d" % resp.status)
                    continue
                hello = await resp.json(content_type=None)
                if isinstance(hello, dict) and hello.get("app") == "mindflock":
                    peer["probe"] = "ok"
                    return base, hello
                outcome = _better(outcome, "not_mindflock")
        except Exception as err:  # noqa: BLE001 — closed port, timeout, TLS, not-JSON …
            outcome = _better(outcome, probe_outcome(err))
            continue
    peer["probe"] = outcome or "unreachable"
    return None


def _device_state(key: str) -> dict:
    return _DEVICES.setdefault(
        key,
        {
            "key": key,
            "host": "",
            "os": "",
            "ip": "",
            "dns": "",
            "base_url": "",
            "reachable": False,
            "remote_control": False,
            "auth": False,
            "version": "",
            # Its installed commit, install kind and desktop app version
            # (hello; "" from a MindFlock too old to say).
            "commit": "",
            "install": "",
            "shell_version": "",
            "shared_link": "",
            "shared_link_live": None,
            # What the last probe found ("ok", "refused", "timeout", … — see
            # _probe_peer) and its ACL tags.
            "probe": "",
            "tags": [],
            # Its hello's fleet id ("" = in none) and join protocol (0 = too
            # old to join) — what "Your devices" lists it by.
            "fleet": "",
            "fleet_proto": 0,
            "ips": [],
            # Its hello says it runs PR review + issue handling (None: its
            # MindFlock is too old to say).
            "automation": None,
            "last_seen": 0.0,
            "instances": [],
            "instances_ok": False,
            "error": "",
        },
    )


def _apply_probe(dev: dict, hit: Optional[Tuple[str, dict]], now: float) -> None:
    """Fold one :func:`_probe_peer` answer into ``dev`` (a miss only clears
    ``reachable`` — the rest is kept for the stale grace period)."""
    if not hit:
        dev["reachable"] = False
        return
    base, hello = hit
    dev["probe"] = "ok"
    try:
        proto = int(hello.get("fleet_proto") or 0)
    except (TypeError, ValueError):
        proto = 0
    dev.update(
        base_url=base,
        reachable=True,
        last_seen=now,
        remote_control=bool(hello.get("remote_control")),
        auth=bool(hello.get("auth")),
        version=str(hello.get("version") or ""),
        commit=str(hello.get("commit") or "")[:64],
        install=str(hello.get("install") or "")[:16],
        shell_version=str(hello.get("shell_version") or "")[:32],
        shared_link=str(hello.get("shared_link") or ""),
        # None: a MindFlock too old to say.
        shared_link_live=(
            hello["shared_link_live"]
            if isinstance(hello.get("shared_link_live"), bool)
            else None
        ),
        fleet=str(hello.get("fleet") or ""),
        fleet_proto=proto,
        automation=(
            hello["automation"] if isinstance(hello.get("automation"), bool) else None
        ),
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
        dev.update(
            host=peer["host"],
            os=peer["os"],
            ip=peer["ip"],
            dns=peer.get("dns", ""),
            ips=list(peer.get("ips") or []),
            tags=list(peer.get("tags") or []),
        )
        _apply_probe(dev, hit, now)
        if not hit:
            dev["probe"] = str(peer.get("probe") or "unreachable")
        _note_probe(peer, dev["probe"], now)
    for key, peer in list(_OFFLINE.items()):
        if key != self_identity()["key"]:
            _note_probe(peer, "asleep", now)
    # Drop devices that left the tailnet / stopped answering for a while.
    for key in list(_DEVICES):
        dev = _DEVICES[key]
        if key not in seen and now - dev.get("last_seen", 0) > _STALE_AFTER:
            _SEEN[key] = max(_SEEN.get(key, 0.0), float(dev.get("last_seen") or 0.0))
            del _DEVICES[key]
    for key in list(_PROBES):
        if key not in seen and key not in _OFFLINE:
            del _PROBES[key]  # left the tailnet


#: Every non-mobile tailnet peer the last sweep looked at (online and
#: offline), with what its probe found — the "why isn't it listed" answer
#: Settings → Devices and ``mindflock devices list`` give per peer.
_PROBES: Dict[str, dict] = {}
#: When MindFlock last answered on a device that discovery has since dropped
#: (key -> epoch s): kept so a member row can still say "last seen 3 h ago".
_SEEN: Dict[str, float] = {}


def _note_probe(peer: dict, outcome: str, now: float) -> None:
    key = peer.get("key") or ""
    if not key:
        return
    _PROBES[key] = {
        "device": key,
        "host": peer.get("host") or key,
        "dns": peer.get("dns") or "",
        "os": peer.get("os") or "",
        "online": bool(peer.get("online")),
        "tags": list(peer.get("tags") or []),
        "ts_last_seen": float(peer.get("ts_last_seen") or 0.0),
        "outcome": outcome,
        "at": now,
    }


def probes() -> List[dict]:
    """Snapshots of :data:`_PROBES`, tagged peers first, then by name."""
    return sorted(
        (dict(p) for p in _PROBES.values()),
        key=lambda p: (not p["tags"], p["host"].lower()),
    )


def last_seen(key: str) -> float:
    """When MindFlock last answered on ``key`` (epoch s, 0.0 never)."""
    dev = _DEVICES.get(key) or {}
    return max(float(dev.get("last_seen") or 0.0), _SEEN.get(key, 0.0))


def _connected(dev: dict) -> bool:
    """Can we actually drive this device right now? Reachable, remote control
    on there, and — when its gate is on — a credential for it: a pasted token,
    or the fleet key (it opens every member, see :func:`_fleet_key_for`)."""
    return bool(
        dev.get("reachable")
        and dev.get("remote_control")
        and (not dev.get("auth") or token_for(dev["key"]) or _fleet_key_for(dev["key"]))
    )


def connected_devices() -> List[dict]:
    """Snapshots of the devices this server can drive right now."""
    return [dict(d) for d in _DEVICES.values() if _connected(d)]


def _same_fleet(dev: dict) -> bool:
    """Whether ``dev``'s hello names this device's fleet (both non-empty)."""
    mine = _fleet_id()
    return bool(mine) and str(dev.get("fleet") or "") == mine


def fleet_devices() -> List[dict]:
    """Snapshots of the reachable devices that are members of this device's
    fleet AND say so themselves (their hello carries the same fleet id).

    The ONLY peers anything that adopts data from another device may talk to
    (settings sync, the fleet's roster gossip). Unlike :func:`connected_devices`
    this is about identity, not drivability: a gate-off tailnet node counts as
    "connected" with no credential at all, which is exactly who must never be
    able to push settings here. Remote control is deliberately not required —
    a member that turned it off answers 403, which surfaces as its error."""
    if not _fleet_id():
        return []
    return [
        dict(d)
        for d in _DEVICES.values()
        if d.get("reachable") and _member_device(d) and _same_fleet(d)
    ]


async def _read_json(resp) -> object:
    """``resp``'s body as JSON, or None when it isn't any."""
    try:
        return await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — empty / HTML error page / truncated
        return None


async def get_json(
    dev: dict,
    path: str,
    timeout: float = 3.0,
    *,
    auth: bool = True,
    bearer: Optional[str] = None,
) -> Tuple[int, object]:
    """``(status, body)`` for ``GET <path>`` on a device, with this device's
    credential for it (see :func:`_headers_for` for ``auth``/``bearer``);
    ``(0, None)`` when it can't be reached. The body is parsed for a 200 only.
    The fan-out primitive for features that ask every device something
    (:mod:`backend.web.core.fleet_claims`, settings sync, the fleet)."""
    if aiohttp is None or not dev.get("base_url"):
        return 0, None
    session = await _http_session()
    try:
        async with session.get(
            dev["base_url"] + path,
            headers=_headers_for(dev["key"], auth=auth, bearer=bearer),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.json(content_type=None) if resp.status == 200 else None
            return resp.status, body
    except Exception:  # noqa: BLE001 — unreachable / timeout / not JSON
        return 0, None


async def post_json(
    dev: dict,
    path: str,
    body: object,
    timeout: float = 10.0,
    *,
    auth: bool = True,
    bearer: Optional[str] = None,
) -> Tuple[int, object]:
    """``(status, body)`` for ``POST <path>`` with a JSON ``body`` on a device
    (credentials as :func:`get_json`); ``(0, None)`` when it can't be reached.

    The response is parsed as JSON whatever the status, so a refusal carries
    its ``{"error": …}`` back to the user ("that code is wrong or expired")
    instead of a bare number."""
    if aiohttp is None or not dev.get("base_url"):
        return 0, None
    session = await _http_session()
    try:
        async with session.post(
            dev["base_url"] + path,
            json=body,
            headers=_headers_for(dev["key"], auth=auth, bearer=bearer),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return resp.status, await _read_json(resp)
    except Exception:  # noqa: BLE001 — unreachable / timeout
        return 0, None


async def refresh_device(key: str) -> Optional[dict]:
    """Re-probe ``key``'s hello NOW (instead of on the next sweep) and return
    its snapshot; None for a device discovery never found.

    After a join the joined device's hello names the new fleet at once, and
    :func:`fleet_devices` — which requires that — would otherwise not include
    it for up to a discovery interval."""
    dev = _DEVICES.get(key)
    if dev is None:
        return None
    if aiohttp is not None and (dev.get("ip") or dev.get("dns")):
        peer = {"ip": dev.get("ip", ""), "dns": dev.get("dns", "")}
        hit = await _probe_peer(peer)
        _apply_probe(dev, hit, time.time())
        if not hit and peer.get("probe"):
            dev["probe"] = peer["probe"]
    return dict(dev)


async def discover_now() -> None:
    """One tailnet sweep right now (Settings → Devices' Refresh button)."""
    if aiohttp is None:
        return
    await _discover_once()


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
        dev.update(instances_ok=False, error=plain_error(err))


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
        member = _member_device(dev)
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
                # A fleet member is opened by the fleet key — no token to paste.
                "needs_token": bool(
                    dev["reachable"]
                    and dev["remote_control"]
                    and dev["auth"]
                    and not token_for(dev["key"])
                    and not _fleet_key_for(dev["key"])
                ),
                "connected": _connected(dev) and bool(dev.get("instances_ok")),
                "error": dev.get("error", ""),
                "sessions": len(dev.get("instances") or []) if _connected(dev) else 0,
                # "Your devices": on this device's roster; the join protocol it
                # speaks; whether it is in ANY fleet; whether in this one.
                "member": member,
                "fleet_proto": int(dev.get("fleet_proto") or 0),
                "in_fleet": bool(dev.get("fleet")),
                "same_fleet": _same_fleet(dev),
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
        return False, plain_error(err)
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


_ENCODED_DOT_OR_SLASH = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)


def _unsafe_tail(tail: str) -> bool:
    """Whether a forwarded path tail could climb out of the route it names:
    a ``.``/``..`` segment (the HTTP client collapses them, so
    ``<title>/../../fleet/rekey`` would land on ``/api/fleet/rekey``), a
    backslash, or a dot/slash still percent-encoded after the server decoded
    the path once (double encoding — never legitimate here)."""
    if "\\" in tail or _ENCODED_DOT_OR_SLASH.search(tail):
        return True
    return any(seg in (".", "..") for seg in tail.split("/"))


def _unsafe_proxy_path(path: str) -> bool:
    """:func:`_unsafe_tail` for a namespaced-session or ``fwd/`` path (the
    title itself may not be ``.``/``..`` either)."""
    if path.startswith(_INSTANCES_PREFIX):
        seg, _, tail = path[len(_INSTANCES_PREFIX) :].partition("/")
        bare = seg.partition(NS)[2]
        return bare in (".", "..") or _unsafe_tail(tail)
    hit = _split_fwd_path(path)
    return bool(hit) and _unsafe_tail(hit[1])


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


async def _may_lend_credential(scope) -> bool:
    """Whether a proxied request may carry THIS device's credential for the
    target (the fleet key, or the token pasted for it): when this device's
    own gate is on (the caller already proved itself to get here), or the
    caller is :func:`~backend.web.core.auth.privileged` — this machine, a
    credential holder such as the phone with the fleet-key cookie, a trusted
    Tailscale account. Otherwise — an anonymous caller of a gate-off device —
    the request goes out bare, and the target's own gate decides: holding
    the fleet key must not let this device launder a stranger into a gated
    one. Never raises (fails closed)."""
    try:
        from backend.web.core import auth as _auth

        if _auth.auth_enabled():
            return True
        return bool(await _auth.privileged(scope))
    except Exception:  # noqa: BLE001
        return False


class RemoteProxyMiddleware:
    """Forward ``/api/instances/<device>::<title>/…`` (HTTP + websocket) to the
    device that owns the session. Mounted INSIDE the auth gate, so the local
    token is checked first; the target's token is attached on the way out —
    only for a caller this device vouches for (:func:`_may_lend_credential`)."""

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
        if _unsafe_proxy_path(path):
            # The tail is forwarded verbatim: a dot segment would let a caller
            # of this device reach ANY route on the target (fleet roster, key
            # changes, settings sync) with this device's credential attached.
            await self._reject(scope, receive, send, "bad path", 400)
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
        lend = await _may_lend_credential(scope)
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
                lend=lend,
            )
        else:
            await self._proxy_ws(receive, send, dev, url, lend=lend)

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
        lend: bool = False,
    ) -> None:
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = _headers_for(dev["key"], auth=lend)
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

    async def _proxy_ws(
        self, receive, send, dev: dict, url: str, lend: bool = False
    ) -> None:
        msg = await receive()
        if msg["type"] != "websocket.connect":
            return
        scheme = "wss" if url.startswith("https") else "ws"
        ws_url = scheme + url[url.index("://") :]
        session = await _http_session()
        try:
            async with session.ws_connect(
                ws_url,
                headers=_headers_for(dev["key"], auth=lend),
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
