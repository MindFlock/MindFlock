"""Shared files across the person's own devices (docs/web-ui.md "Shared
files"; docs/devices.md "Shared files on your other devices").

**On demand, not a sync.** Each device keeps its own store
(:mod:`backend.web.core.hub`). The drawer, the phone sheet and the agents'
``hub`` tool also LIST what the person's other devices offer right now
(:func:`list_all`), each item marked with its device; a device that can't be
asked is named with the reason (offline, needs an update, remote control
off, not offering). USING one of those items (send it, save a copy, open
it, expand its code) FETCHES it (:func:`fetch`): its bytes come over, are
checked against the checksum the listing named, and land in THIS device's
store as a normal item that remembers where it came from (``origin``) — a
second use finds that copy and does not fetch again.

**The one transport.** Every request to another device rides the existing
forwarding path ``/api/devices/<device>/fwd/…`` — run in-process through
:class:`backend.web.core.remote.RemoteProxyMiddleware` with the CALLER's own
request scope (:func:`_fwd_get`), so its allow-list, one-hop rule, path
checks, device resolution and the rule on when this device lends its
credential all apply exactly as they do to the browser. The routes it may
reach there are the three GETs of ``routes/hub.py``'s "offered" half:
the list, one item's bytes (a range of at most :data:`CHUNK` per request —
each request stays inside the forwarding path's own timeout) and one
picture's thumbnail. Nothing here writes to, removes from or pins anything
on another device.

**Hostile input, even from the person's own device.** A listing is capped
(:data:`LIST_BODY_MAX` bytes, :data:`LIST_ITEMS_MAX` items) and every field
is checked and cleaned (:func:`clean_item`): ids, a SHA-256, a size, names
through :func:`hub.clean_name`, notes and previews as plain text. The kind a
device declares is never trusted — the receiving add sniffs the bytes. A
fetch is the store's own add (:class:`hub._Add`): this device's limits and
quota, reserved before a byte arrives, the declared size enforced while it
streams (an overrun is refused and cleaned up), the checksum compared before
the item appears (a mismatch keeps nothing). A name that looks like a secret
(``.env``, keys, credentials) is refused unless the person confirms it for
that one item (never an agent). Only devices that are the person's own
(:func:`remote.is_linked` — a "Your devices" member or one paired by token)
are listed or fetched from; a stranger MindFlock on the tailnet never is.

**Codes.** Ids are made per device, so ``file:k7m2`` could name an item on
two devices. A new id avoids every id the person's reachable devices hold
(:data:`hub.REMOTE_IDS`), a fetched copy keeps its code when it is free
here, and when two devices DO hold one code the list shows the device form,
``file@mac-mini:k7m2`` (:func:`hub.device_code`). Resolving a typed code
(:func:`prepare_codes`): this device's own item wins; then a copy already
fetched; then the one reachable device holding it is asked; two holders, or
none that can be asked, is a plain sentence — never another item, never
nothing said.

**When the origin changes.** A fetched copy is a snapshot: removing the
item on its device, or that device going away, leaves the copy here (it
expires on this device's own clock, like any item). An item's bytes never
change on its device; if one id there later names different bytes (an id
re-used after its 30-day tombstone), the checksum no longer matches the
copy, so the next use fetches the new item as a separate copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from backend import log
from backend.web.core import hub as _hub
from backend.web.core import send_files as _send_files
from backend.web.core.send_session import Refusal, clean_line, clean_text

#: A listing body at most this big is read; more is "too long".
LIST_BODY_MAX = 4 * 1024 * 1024
#: At most this many items of one device are listed.
LIST_ITEMS_MAX = 1000
#: A listing is asked again after this long (the drawer, codes, agents).
LIST_TTL_S = 15.0
#: Bytes per request when fetching an item (each within the forwarding
#: path's own timeout).
CHUNK = 4 * 1024 * 1024
#: A picture at most this big is its own thumbnail (there is no image
#: library to make a smaller one); a bigger one shows no thumbnail until
#: it is fetched.
THUMB_MAX = 512 * 1024
#: Thumbnails kept in memory (most recent), and their total bytes.
THUMB_CACHE_N = 128
THUMB_CACHE_BYTES = 24 * 1024 * 1024
#: At most this many codes in one text are looked up on other devices.
CODES_MAX = 20
OFFER_V = 1

_ROUTE_LIST = "/api/hub/offer"
_ROUTE_RAW = "/api/hub/offer/%s/raw"
_ROUTE_THUMB = "/api/hub/offer/%s/thumb"

#: device key -> (time, listing entry)
_LISTINGS: Dict[str, Tuple[float, dict]] = {}
#: (device, id, sha256) -> the fetch in flight (one fetch per item at once).
_INFLIGHT: Dict[Tuple[str, str, str], "asyncio.Future"] = {}
_THUMBS: "OrderedDict[Tuple[str, str, str], Tuple[str, bytes]]" = OrderedDict()


def _info(fmt: str, *args) -> None:
    try:
        if log.InfoLog is not None:
            log.InfoLog.Printf(fmt, *args)
    except Exception:  # noqa: BLE001 — logging never breaks a fetch
        pass


# --------------------------------------------------------------------------- #
# the forwarding path, in-process
# --------------------------------------------------------------------------- #
class _Overrun(BaseException):
    """Raised from the collecting ``send`` when a body passes its cap: a
    BaseException, so it ends the proxied request (and closes its
    connection) instead of being swallowed as a network error."""


async def _not_proxied(scope, receive, send) -> None:  # pragma: no cover
    """The app behind the middleware: a path it doesn't proxy is never ours."""
    raise Refusal(500, "not_forwarded", "not a forwarded path")


async def _fwd_get(
    scope: dict, device: str, target: str, query: str = "", cap: int = CHUNK
) -> Tuple[int, str, bytes]:
    """``(status, content type, body)`` of ``GET <target>?<query>`` on
    ``device`` through ``/api/devices/<device>/fwd`` — exactly the request
    the caller's browser could make (its scope: the same lend rule, the same
    one-hop refusal of a relayed caller). A body over ``cap`` is a
    :class:`Refusal` (``overrun``) and nothing of it is kept."""
    from backend.web.core import remote as _remote

    path = "/api/devices/%s/fwd%s" % (quote(device, safe=""), target)
    sc = dict(scope or {})
    sc.update(
        type="http",
        method="GET",
        path=path,
        raw_path=path.encode("latin-1", "replace"),
        query_string=query.encode("latin-1", "replace"),
        root_path="",
    )
    sc.setdefault("headers", [])
    sc.setdefault("http_version", "1.1")
    state = {"status": 0, "ctype": ""}
    body = bytearray()
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict) -> None:
        if msg.get("type") == "http.response.start":
            state["status"] = int(msg.get("status") or 0)
            for k, v in msg.get("headers") or []:
                if k.lower() == b"content-type":
                    state["ctype"] = v.decode("latin-1", "replace")
        elif msg.get("type") == "http.response.body":
            chunk = msg.get("body") or b""
            if len(body) + len(chunk) > cap:
                raise _Overrun()
            body.extend(chunk)

    try:
        await _remote.RemoteProxyMiddleware(_not_proxied)(sc, receive, send)
    except _Overrun:
        raise Refusal(
            502,
            "overrun",
            "%s sent more than it said it would — nothing was kept"
            % clean_line(device, 64),
        ) from None
    return state["status"], state["ctype"], bytes(body)


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# which devices
# --------------------------------------------------------------------------- #
def devices() -> List[dict]:
    """The person's OTHER devices: ``[{device, name, reachable, connected,
    remote_control, version}]`` — "Your devices" members (on the roster
    whether or not they are up) and devices paired by a pasted token. Never
    a MindFlock that merely shares the tailnet."""
    from backend.web.core import remote as _remote

    out: Dict[str, dict] = {}
    try:
        seen = _remote.devices_json()
    except Exception:  # noqa: BLE001
        seen = {"self": {}, "devices": []}
    me = str((seen.get("self") or {}).get("device") or "")
    for d in seen.get("devices") or []:
        key = str(d.get("device") or "")
        if not key or key == me:
            continue
        if not ((d.get("member") and d.get("same_fleet")) or d.get("has_token")):
            continue
        out[key] = {
            "device": key,
            "name": clean_line(d.get("host") or key, 64),
            "reachable": bool(d.get("reachable")),
            "remote_control": bool(d.get("remote_control")),
            "connected": bool(d.get("reachable"))
            and bool(d.get("remote_control"))
            and not d.get("needs_token"),
            "version": clean_line(d.get("version") or "", 32),
        }
    try:
        from backend.web.core import fleet as _fleet

        members = _fleet.live_members() if _fleet.in_fleet() else {}
    except Exception:  # noqa: BLE001
        members = {}
    for key, m in members.items():
        if key == me or key in out:
            continue
        out[key] = {
            "device": key,
            "name": clean_line((m or {}).get("host") or key, 64),
            "reachable": False,
            "remote_control": False,
            "connected": False,
            "version": "",
        }
    return sorted(out.values(), key=lambda d: d["name"].lower())


def _device(name: str) -> dict:
    """The person's device called ``name`` (its key, host or tailnet name),
    or a :class:`Refusal`."""
    from backend.web.core import remote as _remote

    want = _remote.name_key(name)
    mine = devices()
    for d in mine:
        if want and want in (
            _remote.name_key(d["device"]),
            _remote.name_key(d["name"]),
        ):
            return d
    hit = _remote.find_device(name)
    if hit is not None:
        for d in mine:
            if d["device"] == hit.get("key"):
                return d
    raise Refusal(
        404,
        "no_device",
        "%s is not one of your devices — see Settings → Your devices"
        % clean_line(name, 64),
    )


# --------------------------------------------------------------------------- #
# a listing, cleaned
# --------------------------------------------------------------------------- #
_BY = ("you", "phone", "session", "device")


def _last_part(raw: str) -> str:
    import re

    return re.split(r"[/\\]", raw)[-1]


def clean_item(raw) -> Optional[dict]:
    """One listed item, every field checked and cleaned — or None when it
    can't be one (no valid id, checksum or size). Nothing a device sends is
    passed on as it came."""
    if not isinstance(raw, dict):
        return None
    iid = raw.get("id")
    if not isinstance(iid, str) or not _hub._ID_RE.match(iid):
        return None
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _hub._SHA_RE.match(sha):
        return None
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 1 << 40:
        return None
    rawname = raw.get("name") if isinstance(raw.get("name"), str) else ""
    rawname = rawname[:1024]
    kind = raw.get("kind") if raw.get("kind") in ("image", "text", "file") else "file"
    name = _hub.clean_name(
        rawname, {"image": "image", "text": "snippet.txt"}.get(kind, "file")
    )
    # Secret: what the device says (the person confirmed it there) OR what
    # either name looks like here — a device can't talk its way out of it.
    secret = raw.get("secret") is True or any(
        _send_files.classify(n) in ("secret", "never")
        for n in {_last_part(rawname).strip(), name}
        if n
    )
    mime = raw.get("mime") if raw.get("mime") in _hub.IMAGE_TYPES.values() else ""
    added = raw.get("added")
    now = time.time()
    added = (
        min(max(float(added), 0.0), now + 86400)
        if isinstance(added, (int, float))
        and not isinstance(added, bool)
        and added == added  # not NaN
        else 0.0
    )
    textual = raw.get("textual") is True
    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    preview = raw.get("preview") if isinstance(raw.get("preview"), str) else ""
    by = raw.get("by") if raw.get("by") in _BY else "you"
    by_name = raw.get("by_name") if isinstance(raw.get("by_name"), str) else ""
    return {
        "id": iid,
        "name": name,
        "kind": kind,
        "mime": mime if kind == "image" else "",
        "size": size,
        "sha256": sha,
        "added": added,
        "note": clean_text(note[: 4 * _hub.NOTE_MAX], _hub.NOTE_MAX) if note else "",
        "textual": textual,
        "preview": (
            clean_text(preview[: 4 * _hub.PREVIEW_CHARS], _hub.PREVIEW_CHARS)
            if textual and preview and not secret
            else ""
        ),
        "by": by,
        "by_name": clean_line(by_name[:500], 120) if by_name else "",
        "secret": secret,
    }


def clean_listing(body) -> Tuple[List[dict], int, bool]:
    """``(items, dropped, truncated)`` of a device's listing: at most
    :data:`LIST_ITEMS_MAX` valid items, an id once (the first)."""
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return [], 0, False
    out: List[dict] = []
    seen = set()
    dropped = 0
    truncated = False
    for raw in items:
        if len(out) >= LIST_ITEMS_MAX:
            truncated = True
            break
        it = clean_item(raw)
        if it is None or it["id"] in seen:
            dropped += 1
            continue
        seen.add(it["id"])
        out.append(it)
    return out, dropped, truncated


_STATES = {
    "offline": "%s is offline — its shared files show here when it is back",
    "old": "%s runs an older MindFlock — update it to see its shared files here",
    "off": "Allow remote control is off on %s — turn it on there (Settings → "
    "Your devices → Advanced) to see its shared files",
    "private": "%s keeps its shared files to itself (its Settings → Shared "
    "files → Offer to my other devices)",
    "not_linked": "%s did not accept this device's credential — pair them "
    "again in Settings → Your devices",
    "refused": "%s refused: %s",
    "error": "%s answered something unexpected — try again",
}


def _state_from(status: int, body: bytes) -> Tuple[str, str]:
    """``(state, detail)`` for a listing that is not a good answer."""
    data = _json(body)
    code = str(data.get("code") or "") if isinstance(data, dict) else ""
    err = str(data.get("error") or "") if isinstance(data, dict) else ""
    if status in (0, 502, 503, 504):
        return "offline", err
    if status == 404 and code != "not_offered":
        return "old", ""
    if status == 403 and code in ("not_offered",):
        return "private", ""
    if status == 403 and (code == "remote_control_off" or "remote control" in err):
        return "off", ""
    if status == 401 or code == "not_linked":
        return "not_linked", ""
    if status == 403:
        return "refused", clean_line(err or "HTTP 403", 160)
    if status == 405:
        return "old", ""
    return "error", "HTTP %d" % status


async def list_device(scope: dict, dev: dict, *, fresh: bool = False) -> dict:
    """One device's entry: ``{device, name, state, note, items, dropped,
    truncated, at}`` — ``state`` ``ok`` or why not (``offline`` / ``old`` /
    ``off`` / ``private`` / ``not_linked`` / ``refused`` / ``error``), and
    ``note`` the sentence to show. Cached :data:`LIST_TTL_S`."""
    key = dev["device"]
    hit = _LISTINGS.get(key)
    now = time.time()
    if hit and not fresh and now - hit[0] < LIST_TTL_S:
        return hit[1]
    entry = {
        "device": key,
        "name": dev["name"],
        "state": "ok",
        "note": "",
        "items": [],
        "dropped": 0,
        "truncated": False,
        "at": now,
    }
    if not dev.get("reachable"):
        entry["state"] = "offline"
    elif not dev.get("remote_control"):
        entry["state"] = "off"
    else:
        status: Optional[int]
        try:
            status, _ctype, body = await _fwd_get(
                scope, key, _ROUTE_LIST, cap=LIST_BODY_MAX
            )
        except Refusal as r:
            status, body = 0, b""
            if r.code == "overrun":
                status = None
                entry["state"] = "error"
                entry["note"] = (
                    "%s sent a list longer than this device reads — nothing of "
                    "it is shown" % dev["name"]
                )
        except Exception:  # noqa: BLE001 — a device never breaks the list
            status, body = 0, b""
        if status == 200:
            data = _json(body)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                entry["state"] = "error"
            else:
                items, dropped, truncated = clean_listing(data)
                entry.update(items=items, dropped=dropped, truncated=truncated)
        elif status is not None:
            entry["state"], detail = _state_from(status, body)
            if entry["state"] == "refused":
                entry["note"] = _STATES["refused"] % (dev["name"], detail)
    if entry["state"] != "ok" and not entry["note"]:
        entry["note"] = _STATES.get(entry["state"], "%s") % dev["name"]
    _LISTINGS[key] = (now, entry)
    return entry


def forget(device: str = "") -> None:
    """Drop the cached listing of ``device`` (every one when empty)."""
    if device:
        _LISTINGS.pop(device, None)
    else:
        _LISTINGS.clear()


async def list_all(scope: dict, *, fresh: bool = False) -> dict:
    """Every device's entry (:func:`list_device`), its items marked with
    the code to type (``file:<id>``, or ``file@<device>:<id>`` when that id
    is held by more than one of the person's devices) and the local copy
    already fetched, if any. → ``{self, devices: [...]}``."""
    mine = devices()
    entries = await asyncio.gather(
        *(list_device(scope, d, fresh=fresh) for d in mine), return_exceptions=True
    )
    out = []
    for d, e in zip(mine, entries):
        if isinstance(e, BaseException):
            e = {
                "device": d["device"],
                "name": d["name"],
                "state": "error",
                "note": _STATES["error"] % d["name"],
                "items": [],
                "dropped": 0,
                "truncated": False,
                "at": time.time(),
            }
        out.append(e)
    me = _hub.self_device()
    try:
        doc = await asyncio.to_thread(_hub._load)
    except Refusal:
        doc = {"items": {}, "gone": {}}
    local_ids = {i for i, r in doc["items"].items() if not r.get("removed")}
    holders: Dict[str, int] = {}
    for e in out:
        for it in e["items"]:
            holders[it["id"]] = holders.get(it["id"], 0) + 1
    _hub.REMOTE_IDS.clear()
    _hub.REMOTE_IDS.update(holders)
    shaped = []
    for e in out:
        rows = []
        for it in e["items"]:
            amb = holders[it["id"]] > 1 or it["id"] in local_ids
            got = None
            for rec in _hub._live(doc):
                o = _hub.origin_of(rec)
                if (
                    o
                    and o["device"] == e["device"]
                    and o["id"] == it["id"]
                    and rec.get("sha256") == it["sha256"]
                ):
                    got = rec["id"]
                    break
            rows.append(
                {
                    **it,
                    "device": e["device"],
                    "device_name": e["name"],
                    "ref": (
                        _hub.device_code(e["device"], it["id"])
                        if amb
                        else _hub.PREFIX + it["id"]
                    ),
                    "copy": got,
                    "too_big": False,
                }
            )
        shaped.append({**e, "items": rows})
    lim = await asyncio.to_thread(_hub.limits)
    for e in shaped:
        for it in e["items"]:
            it["too_big"] = it["size"] > lim["item_bytes"]
    return {"self": me, "devices": shaped}


# --------------------------------------------------------------------------- #
# fetching one item
# --------------------------------------------------------------------------- #
def _refusal_from(dev: dict, iid: str, status: int, body: bytes) -> Refusal:
    data = _json(body)
    code = str(data.get("code") or "") if isinstance(data, dict) else ""
    if status in (0, 502, 503, 504):
        return Refusal(
            502,
            "device_offline",
            "%s can't be reached right now, so file:%s was not fetched — try "
            "again when it is back" % (dev["name"], iid),
        )
    if status == 404 and code == "not_offered":
        return Refusal(
            404,
            "not_offered",
            "file:%s is no longer offered by %s (removed there, or kept on that "
            "device only)" % (iid, dev["name"]),
        )
    if status in (404, 405):
        return Refusal(
            409,
            "device_old",
            "%s runs an older MindFlock — update it to fetch its shared files"
            % dev["name"],
        )
    if status == 409 and code == "changed":
        return Refusal(
            409,
            "changed",
            "file:%s changed on %s while it was being fetched — nothing was "
            "saved. Try again" % (iid, dev["name"]),
        )
    state, detail = _state_from(status, body)
    return Refusal(
        502,
        "device_refused",
        _STATES.get(state, "%s refused")
        % ((dev["name"], detail) if state == "refused" else dev["name"]),
    )


async def _entry_for(scope: dict, dev: dict, iid: str, fresh: bool) -> dict:
    entry = await list_device(scope, dev, fresh=fresh)
    if entry["state"] != "ok":
        raise Refusal(
            502 if entry["state"] == "offline" else 409,
            "device_" + entry["state"],
            entry["note"] or _STATES.get(entry["state"], "%s") % dev["name"],
        )
    for it in entry["items"]:
        if it["id"] == iid:
            return it
    raise Refusal(
        404,
        "not_found",
        "%s has no file:%s in its shared files (removed there, kept on that "
        "device only, or the code is wrong)" % (dev["name"], iid),
    )


async def fetch(
    scope: dict,
    device: str,
    raw_id,
    *,
    session: str = "",
    secret_ok: bool = False,
) -> dict:
    """Fetch item ``raw_id`` of ``device`` into THIS device's shared files
    (or find the copy already here) → its public record plus ``already``
    (True when no bytes moved) and ``from`` (the device's name). ``session``
    names the agent's session that asked (logged); a session never passes
    ``secret_ok``. Every refusal is a :class:`Refusal` with a plain next
    step; a failed fetch leaves nothing behind."""
    iid = _hub.check_id(raw_id)
    dev = _device(device)
    if dev["device"] == _hub.self_device():
        rec = await asyncio.to_thread(_hub.get, iid)
        return {**rec, "already": True, "from": dev["name"]}
    it = await _entry_for(scope, dev, iid, fresh=False)
    have = await asyncio.to_thread(_hub.copy_of, dev["device"], iid, it["sha256"])
    if have is None:
        # The cached listing may be stale: ask once more before moving bytes.
        it = await _entry_for(scope, dev, iid, fresh=True)
        have = await asyncio.to_thread(_hub.copy_of, dev["device"], iid, it["sha256"])
    if have is not None:
        await asyncio.to_thread(_hub.touch, [have["id"]])
        return {**have, "already": True, "from": dev["name"]}
    if it["secret"] and (session or not secret_ok):
        raise Refusal(
            403,
            "secret",
            "%s on %s looks like a secret (an env file, key or credential) — it is "
            "not brought to this device%s"
            % (
                it["name"],
                dev["name"],
                (
                    " by an agent; ask your user"
                    if session
                    else ". If you really mean to, confirm it for this one item"
                ),
            ),
            can_confirm=not session,
        )
    key = (dev["device"], iid, it["sha256"])
    running = _INFLIGHT.get(key)
    if running is not None:
        rec = await asyncio.shield(running)
        return {**rec, "already": True, "from": dev["name"]}
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = fut
    try:
        rec = await _pull(scope, dev, it, session=session)
        fut.set_result(rec)
        return {**rec, "already": False, "from": dev["name"]}
    except BaseException as err:
        if not fut.done():
            fut.set_exception(
                err
                if isinstance(err, Exception)
                else Refusal(500, "cancelled", "the fetch was cancelled")
            )
            fut.exception()  # retrieved: no "never retrieved" warning
        raise
    finally:
        _INFLIGHT.pop(key, None)


async def _pull(scope: dict, dev: dict, it: dict, *, session: str) -> dict:
    """Stream the item's bytes into a new add (:class:`hub._Add` — this
    device's limits, quota and the declared size enforced as it arrives),
    then commit it only when the checksum matches."""
    iid, size, sha = it["id"], it["size"], it["sha256"]
    lim = await asyncio.to_thread(_hub.limits)
    if size > lim["item_bytes"]:
        raise Refusal(
            413,
            "too_large",
            "%s on %s is %s — one shared file here can be at most %s (hub.item_mb "
            "in this device's ~/.mindflock/settings.json)"
            % (
                it["name"],
                dev["name"],
                _hub.human(size),
                _hub.human(lim["item_bytes"]),
            ),
        )
    add = await asyncio.to_thread(_hub._Add, size, lim)
    started = time.time()
    try:
        off = 0
        while off < size:
            n = min(CHUNK, size - off)
            status, _ctype, data = await _fwd_get(
                scope,
                dev["device"],
                _ROUTE_RAW % iid,
                urlencode({"offset": off, "length": n, "sha256": sha}),
                cap=n,
            )
            if status != 200:
                raise _refusal_from(dev, iid, status, data)
            if len(data) != n:
                raise Refusal(
                    502,
                    "incomplete",
                    "the transfer of %s from %s stopped after %s of %s — nothing "
                    "was saved. Try again"
                    % (
                        it["name"],
                        dev["name"],
                        _hub.human(off + len(data)),
                        _hub.human(size),
                    ),
                )
            try:
                await asyncio.to_thread(add.write, data)
            except OSError as err:
                raise Refusal(
                    507,
                    "disk_full",
                    "this device's disk could not take %s (%s) — free some space "
                    "and try again" % (it["name"], clean_line(str(err), 120)),
                ) from None
            off += n
        rec = await asyncio.to_thread(
            lambda: add.commit(
                it["name"],
                by="device",
                by_name=dev["name"],
                note=it["note"],
                snippet=it["kind"] == "text",
                origin={
                    "device": dev["device"],
                    "name": dev["name"],
                    "id": iid,
                    "sha256": sha,
                    "fetched": time.time(),
                },
                want_id=iid,
                sha256=sha,
            )
        )
    except BaseException:
        await asyncio.to_thread(add.abort)
        raise
    _info(
        "shared files: fetched file:%s (%d bytes) from %s as file:%s for %s in %.1fs",
        iid,
        size,
        dev["device"],
        rec["id"],
        ('session "%s"' % clean_line(session, 80)) if session else "the app",
        time.time() - started,
    )
    return rec


async def thumb(scope: dict, device: str, raw_id) -> Tuple[str, bytes]:
    """``(mime, bytes)`` of a picture another device holds, for the list —
    only a picture at most :data:`THUMB_MAX` (it is its own thumbnail), its
    type sniffed here from the bytes and the bytes checked against the
    listed checksum; cached in memory. A :class:`Refusal` (404
    ``no_thumb``) for anything else: the drawer draws an icon then."""
    iid = _hub.check_id(raw_id)
    dev = _device(device)
    it = await _entry_for(scope, dev, iid, fresh=False)
    if it["kind"] != "image" or it["size"] > THUMB_MAX:
        raise Refusal(404, "no_thumb", "no thumbnail for that item")
    key = (dev["device"], iid, it["sha256"])
    hit = _THUMBS.get(key)
    if hit is not None:
        _THUMBS.move_to_end(key)
        return hit
    status, _ctype, data = await _fwd_get(
        scope, dev["device"], _ROUTE_THUMB % iid, cap=THUMB_MAX
    )
    if status != 200:
        raise Refusal(404, "no_thumb", "no thumbnail for that item")
    mime = _hub._sniff_image(data[:16])
    if not mime or hashlib.sha256(data).hexdigest() != it["sha256"]:
        raise Refusal(404, "no_thumb", "no thumbnail for that item")
    _THUMBS[key] = (mime, data)
    while len(_THUMBS) > THUMB_CACHE_N or (
        sum(len(v[1]) for v in _THUMBS.values()) > THUMB_CACHE_BYTES
    ):
        _THUMBS.popitem(last=False)
    return mime, data


async def locate(scope: dict, raw) -> Tuple[str, str]:
    """``(device, id)`` a code means among the person's OTHER devices:
    ``file@mac-mini:k7m2`` names its device; a plain ``file:k7m2`` is the
    one reachable device that holds it. Two holders, or none, is a
    :class:`Refusal` that says which (never a guess)."""
    dev, iid = _hub.split_code(raw)
    if dev:
        return dev, iid
    listing = await list_all(scope)
    holders = [
        e
        for e in listing["devices"]
        if e["state"] == "ok" and any(it["id"] == iid for it in e["items"])
    ]
    if len(holders) == 1:
        return holders[0]["device"], iid
    if len(holders) > 1:
        raise Refusal(
            409,
            "ambiguous",
            "file:%s is on %s — say which: %s"
            % (
                iid,
                " and ".join(e["name"] for e in holders),
                " or ".join(_hub.device_code(e["device"], iid) for e in holders),
            ),
        )
    away = [e["name"] for e in listing["devices"] if e["state"] != "ok"]
    raise Refusal(
        404,
        "not_found",
        "none of your reachable devices offers file:%s%s"
        % (iid, (" (%s can't be asked right now)" % " or ".join(away)) if away else ""),
    )


# --------------------------------------------------------------------------- #
# codes typed here for items elsewhere
# --------------------------------------------------------------------------- #
async def prepare_codes(scope: dict, text: str, *, session: str = "") -> Dict[str, str]:
    """Before ``text`` is expanded (:func:`hub.expand`): fetch every item a
    code in it names on another of the person's devices, so the expansion
    finds it here. → ``{code as typed: why it can't be}`` for the codes that
    name another device's item but could not be fetched, or are ambiguous —
    the sender is told; the code stays as typed (never another item)."""
    if not _hub.has_code(text):
        return {}
    why: Dict[str, str] = {}
    me = _hub.self_device()
    try:
        doc = await asyncio.to_thread(_hub._load)
    except Refusal:
        return {}
    looked = 0
    listing: Optional[dict] = None
    for m in _hub.HANDLE_RE.finditer(text):
        if not _hub._code_match(m):
            continue
        typed = m.group(0)
        if typed in why:
            continue
        dev = (m.group(2) or "").lower()
        iid = m.group(3).lower()
        if not _hub._ID_RE.match(iid):
            continue
        if _hub.resolve(doc, dev, iid, me) is not None:
            continue
        if dev == me:
            continue  # this device's own code to nothing: the usual note
        looked += 1
        if looked > CODES_MAX:
            why[typed] = "%s was not looked up (too many codes in one message)" % typed
            continue
        if dev:
            try:
                await fetch(scope, dev, iid, session=session)
            except Refusal as r:
                why[typed] = r.message
            continue
        froms = sorted(
            {
                _hub.origin_of(r)["name"]  # type: ignore[index]
                for r in _hub.copies(doc, iid)
            }
        )
        if len(froms) > 1:
            why[typed] = (
                "%s names copies from %s — say which, like file@<device>:%s"
                % (typed, " and ".join(froms), iid)
            )
            continue
        if listing is None:
            listing = await list_all(scope)
        holders = [
            e
            for e in listing["devices"]
            if e["state"] == "ok" and any(it["id"] == iid for it in e["items"])
        ]
        if len(holders) == 1:
            try:
                await fetch(scope, holders[0]["device"], iid, session=session)
            except Refusal as r:
                why[typed] = r.message
        elif len(holders) > 1:
            why[typed] = "%s is on %s — say which: %s" % (
                typed,
                " and ".join(e["name"] for e in holders),
                " or ".join(_hub.device_code(e["device"], iid) for e in holders),
            )
        else:
            away = [e["name"] for e in listing["devices"] if e["state"] != "ok"]
            if away:
                why[typed] = (
                    "%s is not in Shared files here, and %s can't be asked right "
                    "now — it was sent as typed" % (typed, " or ".join(away))
                )
        doc = await asyncio.to_thread(_hub._load)
    return why
