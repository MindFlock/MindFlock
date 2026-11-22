"""Reach my devices from anywhere — your own computers without Tailscale.

The design and threat model are ``docs/devices-anywhere.md``; in short:

* **This device, reachable** — with ``general.devices_anywhere`` on (off by
  default, per device, never synced) and this device in a group
  (:mod:`backend.web.core.fleet`): a loopback relay ingress
  (:class:`backend.peer.relay.RelayIngress`, a fresh 128-bit path token per
  start) behind a Cloudflare quick tunnel
  (:class:`backend.peer.tunnel.QuickTunnel`), serving
  :class:`backend.peer.fleet_tunnel.TunnelServer` — pinned TLS 1.3 and a
  mutual fleet-key proof before a byte reaches the web server.
* **Its address, published** — sealed with a fleet-derived key
  (:mod:`backend.peer.fleet_record`) into the user's own private GitHub repo
  (:mod:`backend.web.core.github_records`): when the tunnel (re)starts, when
  the group's key changes, and every :data:`fleet_record.REFRESH_S`.
* **The others, found** — their records are read at start, again when a
  member answers on no path (at most once a :data:`READ_MIN` s, backing off
  to :data:`READ_MAX`), and every :data:`SLOW_REFRESH_S` while one is
  unreachable — never while every member answers. Each address gets a local
  :class:`backend.peer.fleet_tunnel.DeviceProxy`, and discovery
  (:func:`backend.web.core.remote._discover_once`) probes it only for a
  member the tailnet didn't answer for: Tailscale/direct first, the tunnel
  last. Everything that talks to a device's ``base_url`` (gossip, settings
  sync, session lists, ``/api/devices/<device>/fwd/…``) then works unchanged.

An address found in GitHub only says where to dial; it never grants trust
(the pin and the proof do), and the web server treats what comes through the
tunnel as a remote caller with the gate on (``auth``, ``tunnel_peers``).

* **A new device, paired** — the tunnel server also answers a pairing code
  (:mod:`backend.web.core.devices_pair`, :mod:`backend.peer.fleet_pair`);
  the joiner gets the inviter's address from the code itself
  (:func:`seed`), so it reaches the inviter before it can read GitHub.

Turning it off stops the tunnel, the ingress and every proxy, and deletes
this device's record. Nothing here logs the fleet key, a derived key, a
record's content or the tunnel address.

Test carriers (never in production use): ``MINDFLOCK_DEVICES_ANYWHERE_LOOPBACK=1``
serves the ingress itself as the address (``wss://127.0.0.1:<port>/<token>``,
plain WebSocket, no cloudflared); ``MINDFLOCK_DEVICES_ANYWHERE_STORE=<dir>``
keeps the records in a local folder instead of GitHub.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from typing import Dict, List, Optional

from backend.peer import fleet_record as _rec

log = logging.getLogger(__name__)

LOOPBACK_ENV = "MINDFLOCK_DEVICES_ANYWHERE_LOOPBACK"
STORE_ENV = "MINDFLOCK_DEVICES_ANYWHERE_STORE"

#: The background loop's tick.
TICK_S = 10.0
#: A tunnel that died (or wouldn't start) is retried after this, doubling.
RESTART_MIN, RESTART_MAX = 5.0, 300.0
#: A failure-triggered read waits at least this, doubling while nothing new
#: turns up.
READ_MIN, READ_MAX = 60.0, 900.0
#: While some member is unreachable, the records are read again this often.
SLOW_REFRESH_S = 600.0
#: How long the hello probe through the tunnel may take (Cloudflare + TLS).
PROBE_TIMEOUT = 10.0

TUNNEL_TEXT = "On · reachable through a private tunnel"
STARTING_TEXT = "Starting the private tunnel…"
NOT_INSTALLED = "cloudflared isn't installed on this computer"
NO_GROUP_TEXT = (
    "On · waiting until this computer is in a group of your devices "
    "(Add a device above)"
)


class _State:
    def __init__(self) -> None:
        self.ingress = None
        self.tunnel = None
        self.server = None
        self.url = ""  # the published address (never logged)
        self.tunnel_state = "off"  # off | starting | up | failed | not_installed
        self.tunnel_error = ""
        self.tunnel_reason = ""
        self.start_task: Optional[asyncio.Task] = None
        self.restart_at = 0.0
        self.restart_delay = RESTART_MIN
        self.died = False
        self.store = None
        self.github = {"state": "unknown", "text": "", "retry_at": 0.0}
        self.addrs: Dict[str, _rec.Record] = {}
        self.file_sha: Dict[str, str] = {}
        self.proxies: Dict[str, object] = {}
        self.dial: Dict[str, str] = {}  # device -> last dial failure reason
        self.read_at = 0.0  # last read
        self.read_wanted = True
        self.read_delay = READ_MIN
        self.unreached: set = set()
        self.via_tunnel: set = set()
        self.publish_error = ""
        # A failed publish / delete is tried again no sooner than this.
        self.write_retry_at = 0.0
        self.write_delay = READ_MIN
        self.lock: Optional[asyncio.Lock] = None
        # (fleet id, epoch) seen last tick: a change ends old streams.
        self.keys_tag = None
        # device -> until (epoch s): an address that came in a pairing code
        # (seed), kept while GitHub has no record for that device yet.
        self.seeded: Dict[str, float] = {}


_S = _State()


def _reset_for_tests() -> None:
    global _S
    _S = _State()


# --------------------------------------------------------------------------- #
# Setting and environment
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    try:
        from backend.config.settings import load_settings

        return bool(getattr(load_settings().general, "devices_anywhere", False))
    except Exception:  # noqa: BLE001
        return False


def _loopback() -> bool:
    return (os.environ.get(LOOPBACK_ENV) or "").strip() in ("1", "true", "yes")


def _root() -> str:
    from backend import config

    return os.path.join(config.GetConfigDir(), "devices_anywhere")


def _dir(*parts: str) -> str:
    """The state folder (0700), created on demand — only by a writer: a
    device that never turned the switch on gets no folder at all."""
    from backend.peer import paths

    d = paths.ensure_dir(_root())
    if parts:
        d = paths.ensure_dir(os.path.join(d, *parts))
    return d


def _state_path() -> str:
    return os.path.join(_root(), "state.json")


def _status_path() -> str:
    return os.path.join(_root(), "status.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(doc: dict) -> None:
    from backend.fsutil import atomic_write_json

    try:
        _dir()
        atomic_write_json(_state_path(), doc, mode=0o600)
    except OSError:
        log.debug("devices anywhere: state not saved", exc_info=True)


def _binary() -> Optional[str]:
    from backend.peer.service import CLOUDFLARED_ENV
    from backend.peer.tunnel import find_cloudflared

    return find_cloudflared(os.environ.get(CLOUDFLARED_ENV, ""))


def installed() -> bool:
    if _loopback():
        return True
    try:
        return bool(_binary())
    except Exception:  # noqa: BLE001
        return False


def wanted() -> bool:
    """Whether the doctor should offer cloudflared (the switch is on)."""
    return enabled()


def _store():
    if _S.store is None:
        folder = (os.environ.get(STORE_ENV) or "").strip()
        from backend.web.core import github_records as _gr

        if folder:
            _S.store = _gr.DirStore(folder)
        else:
            from backend.web.core import github_pr

            _S.store = _gr.GitHubStore(github_pr.api_token)
    return _S.store


def _set_store_for_tests(store) -> None:
    _S.store = store


# --------------------------------------------------------------------------- #
# Fleet facts
# --------------------------------------------------------------------------- #
def _fleet():
    from backend.web.core import fleet as _f

    return _f


def _self_key() -> str:
    from backend.web.core import remote as _remote

    return str(_remote.self_identity().get("key") or "")


def _keys() -> Optional[_rec.Keys]:
    """The current derived keys, None outside a group."""
    try:
        f = _fleet()
        doc = f.state()
        if not (doc.get("id") and doc.get("key")):
            return None
        return _rec.derive(doc["key"], doc["id"], int(doc.get("epoch") or 0))
    except Exception:  # noqa: BLE001
        return None


def _members() -> set:
    try:
        return set(_fleet().live_members())
    except Exception:  # noqa: BLE001
        return set()


#: This server's own port, as the server passed it at start (:func:`run_loop`,
#: the way discovery gets it) — None until then, and None when the process
#: couldn't tell. Never guessed: a wrong guess would pipe another device's
#: requests into whatever else listens on that port.
_SERVER_PORT: Optional[int] = None
_PORT_OK: Dict[int, bool] = {}

PORT_UNKNOWN_TEXT = (
    "this MindFlock server couldn't tell which port it listens on — start it "
    "with `mindflock serve` (or --port)"
)


def _listening_here(port: int) -> bool:
    """Whether THIS process listens on TCP ``port`` (cached per port)."""
    if port in _PORT_OK:
        return _PORT_OK[port]
    try:
        import psutil

        conns = psutil.Process().net_connections(kind="tcp")
    except Exception:  # noqa: BLE001 — can't look: don't trust it
        return False
    ok = any(
        c.status == "LISTEN" and c.laddr and int(c.laddr[1]) == int(port) for c in conns
    )
    if ok:
        _PORT_OK[port] = True
    return ok


def _origin_port() -> int:
    """This device's web server port, or 0 when it isn't known for certain
    (the port the server was started with, and this process listens on it)
    — the tunnel then refuses to start, and a stream is never piped."""
    port = _SERVER_PORT
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return 0
    return port if _listening_here(port) else 0


def _edge_ssl():
    if _loopback():
        return None
    from backend.peer.relay import edge_ssl_context

    return edge_ssl_context()


# --------------------------------------------------------------------------- #
# This device: ingress + tunnel + record
# --------------------------------------------------------------------------- #
def _make_server():
    """The pinned TLS server + fleet handshake (tests may swap it)."""
    from backend.peer import identity as _identity
    from backend.peer.fleet_tunnel import TunnelServer
    from backend.peer.transport import server_ssl_context

    ident = _identity.load_or_create()
    return (
        TunnelServer(
            server_ssl_context(ident),
            ident.pub,
            keys=_keys,
            self_key=_self_key,
            members=_members,
            origin_port=_origin_port,
            on_member=_on_incoming,
            pair=_pair_handler,
        ),
        ident.fingerprint().hex(),
    )


async def _pair_handler(
    reader, writer, server_pub: bytes, sn: bytes, msg: dict
) -> None:
    """A new device with a pairing code came in through the tunnel."""
    from backend.web.core import devices_pair

    await devices_pair.serve(reader, writer, server_pub, sn, msg)


def _make_tunnel(binary: str, port: int):
    """The quick tunnel (tests swap this for a fake)."""
    from backend.peer.tunnel import QuickTunnel

    return QuickTunnel(binary, port, workdir=_dir("tunnel"), on_exit=_exited)


def _exited() -> None:
    """cloudflared died on its own: the address is gone; the loop starts a
    fresh tunnel (new name, new token) after the backoff and republishes."""
    _S.died = True
    _S.url = ""
    _S.tunnel_state = "failed"
    _S.tunnel_error = "the private tunnel dropped — starting a new one"
    _S.tunnel_reason = "exited"
    _S.restart_at = time.time() + _S.restart_delay


def _new_token() -> str:
    """128 random bits, base32 — the ingress path (as peer relay invites)."""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


_FP = ""


async def _start() -> None:
    """Bring the ingress and the tunnel up, then publish. Never raises."""
    global _FP
    from backend.peer.relay import RelayIngress

    _S.tunnel_state, _S.tunnel_error, _S.tunnel_reason = "starting", "", ""
    if not await asyncio.to_thread(_origin_port):
        _S.tunnel_state, _S.tunnel_error, _S.tunnel_reason = (
            "failed",
            PORT_UNKNOWN_TEXT,
            "port_unknown",
        )
        _S.restart_at = time.time() + _S.restart_delay
        _S.restart_delay = min(RESTART_MAX, _S.restart_delay * 2)
        log.warning("devices anywhere: own port unknown; tunnel not started")
        return
    binary = None
    if not _loopback():
        binary = _binary()
        if not binary:
            _S.tunnel_state, _S.tunnel_error, _S.tunnel_reason = (
                "not_installed",
                NOT_INSTALLED,
                "not_installed",
            )
            return
    await _stop_tunnel()
    try:
        if _S.server is None:
            _S.server, _FP = _make_server()
        token = _new_token()
        ingress = RelayIngress(_S.server, "/" + token, trust_cf_ip=not _loopback())
        port = await ingress.start()
        _S.ingress = ingress
        if _loopback():
            url = "wss://127.0.0.1:%d/%s" % (port, token)
        else:
            _S.tunnel = _make_tunnel(binary, port)
            host = await _S.tunnel.start()
            url = "wss://%s/%s" % (host, token)
    except Exception as e:  # noqa: BLE001 — TunnelError or a surprise
        from backend.peer.tunnel import FAILURE_TEXT

        reason = getattr(e, "reason", "failed")
        _S.tunnel_state = "failed"
        _S.tunnel_reason = reason
        _S.tunnel_error = FAILURE_TEXT.get(reason) or "the private tunnel didn't start"
        _S.restart_at = time.time() + _S.restart_delay
        _S.restart_delay = min(RESTART_MAX, _S.restart_delay * 2)
        log.warning("devices anywhere: tunnel start failed (%s)", reason)
        await _stop_tunnel()
        return
    _S.url = url
    _S.died = False
    _S.tunnel_state, _S.tunnel_error, _S.tunnel_reason = "up", "", ""
    try:  # a pairing code names the old address: it can't be used any more
        from backend.web.core import devices_pair

        devices_pair.tunnel_changed(url)
    except Exception:  # noqa: BLE001
        pass
    _S.restart_delay = RESTART_MIN
    log.info("devices anywhere: private tunnel up")
    await publish()


async def _stop_tunnel() -> None:
    tunnel, _S.tunnel = _S.tunnel, None
    ingress, _S.ingress = _S.ingress, None
    _S.url = ""
    if tunnel is not None:
        try:
            await tunnel.stop()
        except Exception:  # noqa: BLE001
            pass
    if ingress is not None:
        try:
            await ingress.stop()
        except Exception:  # noqa: BLE001
            pass


def _note_github(err=None) -> None:
    if err is None:
        _S.github = {"state": "ok", "text": "", "retry_at": 0.0}
        return
    _S.github = {
        "state": getattr(err, "kind", "error"),
        "text": getattr(err, "text", "") or str(err),
        "retry_at": float(getattr(err, "retry_at", 0.0) or 0.0),
    }


def _github_blocked() -> bool:
    return _S.github.get("state") == "rate_limited" and time.time() < float(
        _S.github.get("retry_at") or 0
    )


def _write_failed() -> None:
    _S.write_retry_at = time.time() + _S.write_delay
    _S.write_delay = min(READ_MAX, _S.write_delay * 2)


def _write_ok() -> None:
    _S.write_retry_at = 0.0
    _S.write_delay = READ_MIN


async def publish(force: bool = False) -> bool:
    """Write this device's record (its current address). True on success."""
    from backend.web.core.github_records import StoreError

    keys = _keys()
    me = _self_key()
    if keys is None or not me or not _S.url or not _FP:
        return False
    if _github_blocked():
        return False
    doc = _load_state()
    pub = doc.get("published") if isinstance(doc.get("published"), dict) else {}
    name = _rec.file_name(keys, me)
    ctr = _rec.next_ctr(int(doc.get("ctr") or 0))
    text = _rec.seal(
        keys, _rec.Record(device=me, url=_S.url, fp=_FP, ctr=ctr, ts=time.time())
    )
    store = _store()
    try:
        if pub.get("name") and pub.get("name") != name:
            # The group's key changed (or this device's name): the old file
            # is unreadable to the others now — remove it.
            await store.delete(pub["name"])
        sha = await store.write(
            name, text, pub.get("sha", "") if pub.get("name") == name else ""
        )
    except StoreError as e:
        _note_github(e)
        _S.publish_error = e.text
        _write_failed()
        return False
    except Exception as e:  # noqa: BLE001
        _S.publish_error = "couldn't save this device's address"
        log.warning("devices anywhere: publish failed (%s)", type(e).__name__)
        _write_failed()
        return False
    _note_github()
    _write_ok()
    _S.publish_error = ""
    doc["ctr"] = ctr
    doc["published"] = {
        "name": name,
        "sha": sha,
        "at": time.time(),
        "fleet": keys.fleet_id,
        "epoch": keys.epoch,
        "url_id": _url_id(_S.url),
    }
    _save_state(doc)
    _write_status()
    return True


def _url_id(url: str) -> str:
    """A non-reversible tag of the published address (to notice a change
    without storing it)."""
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] if url else ""


async def delete_record() -> bool:
    """Remove this device's record (switch off / left the group)."""
    from backend.web.core.github_records import StoreError

    doc = _load_state()
    pub = doc.get("published") if isinstance(doc.get("published"), dict) else {}
    if not pub.get("name"):
        return True
    try:
        await _store().delete(pub["name"], pub.get("sha", ""))
    except StoreError as e:
        _note_github(e)
        _S.publish_error = "couldn't remove this device's address: " + e.text
        _write_failed()
        return False
    except Exception as e:  # noqa: BLE001
        _S.publish_error = "couldn't remove this device's address"
        log.warning("devices anywhere: delete failed (%s)", type(e).__name__)
        _write_failed()
        return False
    _write_ok()
    _S.publish_error = ""
    doc.pop("published", None)
    _save_state(doc)
    _write_status()
    return True


# --------------------------------------------------------------------------- #
# The others: read records, run local proxies
# --------------------------------------------------------------------------- #
def want_read() -> None:
    _S.read_wanted = True


def _read_due(now: float) -> bool:
    if _github_blocked():
        return False
    since = now - _S.read_at
    if _S.read_at == 0.0:
        return True
    if _S.read_wanted and since >= _S.read_delay:
        return True
    return bool(_S.unreached) and since >= SLOW_REFRESH_S


async def read_records() -> int:
    """Read every member's record → how many addresses changed. Keeps what
    was known when GitHub can't be reached. Never raises."""
    from backend.web.core.github_records import StoreError

    now = time.time()
    _S.read_at = now
    _S.read_wanted = False
    keys = _keys()
    me = _self_key()
    if keys is None:
        return 0
    members = _members() - {me}
    by_name = {_rec.file_name(keys, d): d for d in members}
    doc = _load_state()
    seen = doc.get("seen") if isinstance(doc.get("seen"), dict) else {}
    changed = 0
    try:
        files = await _store().list()
        for name, sha in files.items():
            device = by_name.get(name)
            if device is None:
                continue  # another group's, an old key's, or a removed device's
            if _S.file_sha.get(name) == sha and device in _S.addrs:
                continue
            text, sha2 = await _store().read(name)
            try:
                rec = _rec.open_record(
                    keys,
                    name,
                    text,
                    members=members,
                    self_key=me,
                    last_ctr=int(seen.get(device) or 0),
                    loopback=_loopback(),
                )
            except _rec.RecordError as e:
                log.info("devices anywhere: a record was ignored (%s)", e.reason)
                _S.file_sha[name] = sha2 or sha
                continue
            _S.file_sha[name] = sha2 or sha
            _S.seeded.pop(device, None)  # its own record now says where it is
            old = _S.addrs.get(device)
            if old is None or old.url != rec.url or old.fp != rec.fp:
                changed += 1
                _S.dial.pop(device, None)
            _S.addrs[device] = rec
            seen[device] = rec.ctr
        # A device whose record is gone (switched off, left) has no address —
        # unless the address came in the pairing code and that device hasn't
        # published yet (no GitHub sign-in there, or not yet).
        for device in list(_S.addrs):
            if device in members and _seed_live(device, now):
                continue
            if device not in members or _rec.file_name(keys, device) not in files:
                _S.addrs.pop(device, None)
                changed += 1
        _note_github()
    except StoreError as e:
        _note_github(e)
    except Exception as e:  # noqa: BLE001
        log.warning("devices anywhere: reading addresses failed (%s)", type(e).__name__)
    doc = _load_state()
    doc["seen"] = seen
    _save_state(doc)
    _S.read_delay = READ_MIN if changed else min(READ_MAX, _S.read_delay * 2)
    await _sync_proxies()
    _write_status()
    return changed


#: How long an address from a pairing code is used while GitHub has no record
#: for that device (it is only where to dial: the pin and the proof decide).
SEED_TTL = 86400.0


def _seed_live(device: str, now: float) -> bool:
    until = _S.seeded.get(device)
    if until is None:
        return False
    if now >= until:
        _S.seeded.pop(device, None)
        return False
    return True


def seed(device: str, url: str, fp_hex: str) -> None:
    """Use ``url`` (pinned to ``fp_hex``) for ``device`` — the address a
    pairing code carried, which the joiner checked (pin + the inviter's
    proof) — until GitHub has that device's own record, at most
    :data:`SEED_TTL`. Raises ValueError for a malformed one."""
    _rec.check_url(url, loopback=_loopback())
    if not _rec._FP_RE.match(fp_hex or "") or not _rec._DEVICE_RE.match(device or ""):
        raise ValueError("bad address")
    now = time.time()
    _S.addrs[device] = _rec.Record(device=device, url=url, fp=fp_hex, ctr=0, ts=now)
    _S.seeded[device] = now + SEED_TTL
    _S.dial.pop(device, None)


def current_url() -> str:
    """This device's tunnel address while it is up ("" otherwise) — the
    members' path. Never logged, never put in a pairing code (that gets its
    own path, :func:`open_pair_path`)."""
    return _S.url if _S.tunnel_state == "up" else ""


def open_pair_path(ttl_s: float) -> str:
    """A fresh pairing path on the live tunnel, for ``ttl_s`` seconds → its
    address (``wss://<tunnel host>/<new token>``), "" when the tunnel isn't
    up. It replaces any earlier pairing path at once (one code at a time),
    takes ONLY pairing frames, and dies with the tunnel."""
    url, ingress, server = current_url(), _S.ingress, _S.server
    if not url or ingress is None or server is None:
        return ""
    token = _new_token()

    async def on_pair(reader, writer, source: str = "") -> None:
        await server(reader, writer, source, pair_only=True)

    ingress.set_side_path("/" + token, on_pair, until=time.monotonic() + float(ttl_s))
    base = url.rsplit("/", 1)[0]
    return "%s/%s" % (base, token)


def close_pair_path() -> None:
    """Retire the pairing path (the code was cancelled, or no code is live)."""
    ingress = _S.ingress
    if ingress is not None:
        try:
            ingress.set_side_path(None)
        except Exception:  # noqa: BLE001
            pass


def fingerprint() -> str:
    """This device's identity fingerprint (hex) as the tunnel presents it."""
    return _FP


async def ensure_up(timeout: float = 45.0) -> bool:
    """Bring the tunnel up now if it isn't (the switch must be on and this
    device in a group) and wait for it → whether it is up."""
    if current_url():
        return True
    await apply()
    task = _S.start_task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, TimeoutError, Exception):  # noqa: BLE001
            pass
    return bool(current_url())


async def publish_soon() -> None:
    """Something that publishing needs may have just arrived (settings sync
    brought the GitHub sign-in): try GitHub again now instead of after the
    backoff. Never raises."""
    _S.write_retry_at = 0.0
    _S.read_at = 0.0
    if _S.github.get("state") != "rate_limited":
        _S.github = {"state": "unknown", "text": "", "retry_at": 0.0}
    if not os.environ.get(STORE_ENV):
        _S.store = None
    await tick()


async def github_ready() -> bool:
    """Whether this device has a GitHub sign-in the address book can use
    (a token at all — the repo checks come at the first write). The folder
    test store always counts. Never raises."""
    if (os.environ.get(STORE_ENV) or "").strip():
        return True
    try:
        from backend.web.core import github_pr

        return bool(await github_pr.api_token())
    except Exception:  # noqa: BLE001
        return False


def save_switch(on: bool) -> None:
    """Save the switch (this device only; never synced). Raises on a
    settings file that can't be saved."""
    from backend.config import settings as settings_store
    from backend.web.core import settings_sync as _sync

    settings_store.update_settings(general={"devices_anywhere": bool(on)})
    _sync.local_change()


def _route(device: str):
    rec = _S.addrs.get(device)
    keys = _keys()
    me = _self_key()
    if rec is None or keys is None or not me:
        return None
    return rec.url, rec.fp, keys, me, device


def _on_incoming(device: str) -> None:
    """A member just came in through our tunnel (it proved the group key):
    it is up. If we can't reach it ourselves, look for its address soon —
    still at most one read a :data:`READ_MIN`."""
    if device not in _S.addrs or device in _S.dial:
        _S.read_wanted = True
        _S.read_delay = READ_MIN


async def _on_dial(device: str, ok: bool, reason: str) -> None:
    if ok:
        _S.dial.pop(device, None)
        return
    _S.dial[device] = reason
    want_read()  # the address may have moved: look again (rate-limited)


async def _sync_proxies() -> None:
    from backend.peer.fleet_tunnel import DeviceProxy

    # A proxy is never stopped while the switch is on, even when its device's
    # address goes away (it then refuses every connection): discovery may
    # still name its port as that device's base_url, and a freed loopback
    # port could be taken by another local process — which would then be
    # handed the fleet key as a bearer.
    for device in _S.addrs:
        if device in _S.proxies:
            continue
        proxy = DeviceProxy(
            device,
            lambda d=device: _route(d),
            edge_ssl=_edge_ssl(),
            on_result=lambda ok, reason, d=device: _on_dial(d, ok, reason),
        )
        try:
            await proxy.start()
        except OSError:
            continue
        _S.proxies[device] = proxy


async def candidates(exclude=()) -> List[dict]:
    """Peer entries (shaped like ``remote._node_entry``) for discovery to
    probe through the tunnel: members with a known address that the tailnet
    didn't answer for (``exclude``). ``[]`` while off. Never raises."""
    if not enabled() or _keys() is None:
        return []
    try:
        await _sync_proxies()
        roster = _fleet().live_members()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for device, proxy in _S.proxies.items():
        if device in exclude or device not in _S.addrs or not proxy.base_url:
            continue
        entry = roster.get(device) or {}
        out.append(
            {
                "key": device,
                "host": str(entry.get("host") or device),
                # The roster's own name for it: the far end proved it holds
                # the group's key, so it IS that member (fleet.member_device).
                "dns": str(entry.get("dns") or ""),
                "ip": "",
                "ips": [],
                "os": "",
                "online": True,
                "tags": [],
                "base_url": proxy.base_url,
                "via": "tunnel",
                "timeout": PROBE_TIMEOUT,
            }
        )
    return out


def note_sweep(reached: set, via_tunnel: set) -> None:
    """Discovery's verdict: which members answered (any path) and which of
    them through the tunnel. A member nobody reached asks for a re-read."""
    if not enabled():
        return
    members = _members() - {_self_key()}
    _S.unreached = members - set(reached)
    _S.via_tunnel = set(via_tunnel)
    if _S.unreached:
        want_read()  # rate-limited by _read_due


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def _lock() -> asyncio.Lock:
    if _S.lock is None:
        _S.lock = asyncio.Lock()
    return _S.lock


async def tick() -> None:
    """One pass: bring things in line with the switch. Never raises."""
    async with _lock():
        try:
            await _tick()
        except Exception as e:  # noqa: BLE001
            log.warning("devices anywhere: tick failed (%s)", type(e).__name__)


def _revoke_stale_streams(keys) -> None:
    """Membership and keys are checked at each handshake; an open stream
    outlives both unless ended here. Incoming streams whose member left or
    whose keys are no longer current end (:meth:`TunnelServer.sweep`); on a
    key change every stream through our proxies ends too (they were
    authenticated under the old key)."""
    if _S.server is not None:
        try:
            if _S.server.sweep():
                log.info("devices anywhere: ended streams of a past key/member")
        except Exception:  # noqa: BLE001
            pass
    tag = (keys.fleet_id, keys.epoch) if keys is not None else None
    if _S.keys_tag is not None and tag != _S.keys_tag:
        for proxy in list(_S.proxies.values()):
            try:
                proxy.drop_streams()
            except Exception:  # noqa: BLE001
                pass
    _S.keys_tag = tag


_LOOP: Optional[asyncio.AbstractEventLoop] = None


def fleet_changed() -> None:
    """The group's state was just saved (a removal, a key rotation, a
    join): end what the change revoked NOW rather than at the next tick.
    Callable from any thread; never raises."""
    loop = _LOOP
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(lambda: _revoke_stale_streams(_keys()))
    except Exception:  # noqa: BLE001
        pass


async def _tick() -> None:
    on = enabled()
    keys = _keys()
    _revoke_stale_streams(keys)
    if not on or keys is None:
        if _S.ingress is not None or _S.tunnel is not None or _S.proxies:
            await _shutdown()
        if (
            _load_state().get("published")
            and time.time() >= _S.write_retry_at
            and not _github_blocked()
        ):
            await delete_record()
        return
    now = time.time()
    starting = _S.start_task is not None and not _S.start_task.done()
    if not starting and not _S.url and now >= _S.restart_at:
        _S.start_task = asyncio.get_running_loop().create_task(
            _start(), name="devices-anywhere-start"
        )
        starting = True
    if _S.url and not starting:
        pub = _load_state().get("published") or {}
        if (
            (
                pub.get("fleet") != keys.fleet_id
                or pub.get("epoch") != keys.epoch
                or pub.get("url_id") != _url_id(_S.url)
                or now - float(pub.get("at") or 0) >= _rec.REFRESH_S
                or _S.publish_error
            )
            and now >= _S.write_retry_at
            and not _github_blocked()
        ):
            await publish()
    if _read_due(now):
        await read_records()


async def apply() -> dict:
    """Make the switch take effect now (the route calls this)."""
    _S.restart_at = 0.0
    _S.read_at = 0.0
    _S.write_retry_at = 0.0
    await tick()
    return status()


async def retry() -> dict:
    _S.restart_at = 0.0
    _S.restart_delay = RESTART_MIN
    _S.read_at = 0.0
    _S.write_retry_at = 0.0
    _S.github = {"state": "unknown", "text": "", "retry_at": 0.0}
    _S.store = None if not os.environ.get(STORE_ENV) else _S.store
    await tick()
    return status()


async def _shutdown() -> None:
    task, _S.start_task = _S.start_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await _stop_tunnel()
    # Discovery must stop using the proxies' ports BEFORE they are freed.
    try:
        from backend.web.core import remote as _remote

        _remote.forget_via("tunnel")
    except Exception:  # noqa: BLE001
        pass
    for device in list(_S.proxies):
        proxy = _S.proxies.pop(device)
        try:
            await proxy.stop()
        except Exception:  # noqa: BLE001
            pass
    _S.addrs.clear()
    _S.seeded.clear()
    _S.file_sha.clear()
    _S.dial.clear()
    _S.tunnel_state, _S.tunnel_error, _S.tunnel_reason = "off", "", ""
    _write_status()


async def stop() -> None:
    """Server shutdown: stop everything, keep the record (the next start
    publishes the new address over it)."""
    if (
        _S.ingress is None
        and _S.tunnel is None
        and not _S.proxies
        and _S.start_task is None
    ):
        return
    await _shutdown()


async def run_loop(server_port: Optional[int] = None) -> None:
    """The background loop (a no-op tick while the switch is off).

    ``server_port``: the port this server was started on (None when it
    can't tell; the tunnel then refuses to start). First, any cloudflared a
    crashed run of this state folder left behind is ended
    (:func:`backend.peer.tunnel.reap_stale`) — before this server opens a
    single loopback listener a stale tunnel could be pointing at."""
    global _SERVER_PORT, _LOOP
    _SERVER_PORT = server_port
    _LOOP = asyncio.get_running_loop()
    try:
        from backend.peer.tunnel import reap_stale

        await asyncio.to_thread(reap_stale)
    except Exception:  # noqa: BLE001
        pass
    while True:
        await tick()
        await asyncio.sleep(TICK_S)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def status() -> dict:
    """What Settings → Your devices shows under the switch (and the doctor)."""
    on = enabled()
    in_group = _keys() is not None
    pub = _load_state().get("published") or {}
    gh = dict(_S.github)
    tstate = _S.tunnel_state if on else "off"
    retry = False
    install = False
    if not on:
        state, text = "off", "Off"
    elif not in_group:
        state, text = "waiting", NO_GROUP_TEXT
    elif tstate == "not_installed":
        state, text, install = (
            "failed",
            "Couldn't start the tunnel — %s" % NOT_INSTALLED,
            True,
        )
    elif tstate == "failed":
        state, text, retry = (
            "failed",
            "Couldn't start the tunnel — %s" % (_S.tunnel_error or "it didn't start"),
            True,
        )
    elif gh.get("state") in (
        "permission",
        "no_token",
        "public_repo",
        "not_owned",
        "moved",
    ):
        state, text, retry = "needs_github", gh.get("text") or "", True
    elif tstate != "up":
        state, text = "starting", STARTING_TEXT
    else:
        state, text = "on", TUNNEL_TEXT
        if gh.get("state") in ("unreachable", "rate_limited", "error"):
            extra = gh.get("text") or "couldn't reach GitHub"
            text += " · %s — using the addresses found earlier; retrying" % extra
        elif _S.publish_error:
            text += " · %s" % _S.publish_error
    return {
        "on": on,
        "state": state,
        "text": text,
        "retry": retry,
        "install": install,
        "installed": installed(),
        "tunnel": tstate,
        "github": {"state": gh.get("state"), "text": gh.get("text") or ""},
        "published_at": float(pub.get("at") or 0.0) or None,
        "found": sorted(_S.addrs),
        "via_tunnel": sorted(_S.via_tunnel),
    }


def _write_status() -> None:
    """A no-secrets snapshot for ``mindflock doctor`` (another process)."""
    try:
        st = status()
        snap = {
            "at": time.time(),
            "pid": os.getpid(),
            "on": st["on"],
            "state": st["state"],
            "text": st["text"],
            "tunnel": st["tunnel"],
            "github": st["github"],
            "published_at": st["published_at"],
            "found": len(st["found"]),
        }
        from backend.fsutil import atomic_write_json

        if not st["on"] and not os.path.isdir(_root()):
            return  # never on here: leave no trace
        _dir()
        atomic_write_json(_status_path(), snap)
    except Exception:  # noqa: BLE001
        pass


def last_status() -> dict:
    """The snapshot the running server last wrote (``{}`` when none)."""
    try:
        with open(_status_path(), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}
