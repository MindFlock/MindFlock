"""Your devices — the owner's computers as one group (internally: the fleet).

A fleet is ONE shared secret, the fleet key, held by every member, plus a
roster of members. Holding the key == being one of the owner's devices, so it
is a full credential on every member (what a pasted access token already is)
and it only ever leaves a member inside an approved join. Settings sync and
anything else that ADOPTS data from another device talks only to members,
authenticated with the key — a tailnet node whose gate is off is reachable,
but it is not one of yours.

**Identity** is the device key (the MagicDNS label, see
:func:`backend.web.core.remote.self_identity`) bound to the member's FULL
MagicDNS name once one is known (``dns`` on its roster entry): the key is only
ever sent to a discovered device whose name matches (:func:`member_device`),
never to whatever node happens to hold the same first label. Discovery stays
Tailscale's job (:mod:`backend.web.core.remote`); this module only decides who
belongs.

**The roster** lives in ``fleet.json`` beside ``settings.json``::

    {"id": "<16 hex>", "key": "<token_urlsafe(32)>", "epoch": 3,
     "members": {"<key>": {"host", "added_at", "added_by", "dns"}},
     "removed": {"<key>": {"at": <ts>, "by": "<device that removed it>"}},
     "prev_keys": {"<epoch>": {"key", "at"}},
     "admits": {"<key>": <ts>}}

Members and removals are both grow-only maps merged by max timestamp: a device
is a member while its ``added_at`` is newer than its tombstone. Tombstones
never shrink, and gossip alone never brings a device back that this device
knows was REMOVED by another device — every member holds the same key, so a
roster entry can't prove who wrote it, and the removed device still holds the
old one. Re-adding it takes a fresh join through a device that knows of the
removal (``admits``: the joiner's entry it handed out, see :func:`bundle_for`),
or the person allowing it here once another member let it back in
(:func:`allow`; status ``readmitted_elsewhere``). A device that LEFT on its
own (its tombstone's ``by`` is itself) is not held off that way: a later
admit, learned by gossip like any other, brings it back.
Rosters are only merged between devices on the SAME key epoch: whatever a
device learned while it held an older key could have been written by anyone
else who held that key — including the device the newer key was made to lock
out. A key change is taken only one epoch at a time (:func:`apply_rekey`) and
REPLACES the roster rather than merging into it. ``by`` on a tombstone is
what the device that removed it said about itself: "rig removed laptop" is how
a forged removal shows.

**Joining** — three ways in, one outcome (the joiner receives the bundle: id,
key, epoch, roster with a live entry for the joiner):

* an 8-character code made on a member (:func:`create_invite`), typed on the
  new device (:func:`join_with_code` → the member's public ``redeem``);
* the new device asks (:func:`request_join` → the member's public
  ``requests``) and a person approves on the member; both screens show the
  same 6-digit code so they can tell it is THIS request;
* one click for a device this one already holds a pasted access token for
  (:func:`add_paired`) — the token already proves the owner controls it.

The admitting device does NOT put the joiner on its own roster: the joiner
does that itself once it holds the key (its announce, then gossip). A join
that fails, is cancelled or is abandoned therefore leaves no ghost member.

**Removing** a device rotates the key (:func:`remove`) and, by default, every
remaining member's own access token too (pasted tokens the removed device
held, or read while it was a member, stop working). Members that can be
reached get the new key under the old one; a member that was offline keeps
the old key until it is seen again — the gossip loop then hands it the new
key under its old one (``prev_keys``), one epoch at a time. The removed device
is left holding a key that 401s on every member that has heard.

What one shared key can't do is tell a removed device from a member that
never heard of the removal: until a member that was offline is handed the new
key, the removed device can still talk to it with the old one. So a lost or
stolen device must ALSO be removed from the tailnet (the Tailscale admin
console) — that cuts it off everywhere at once, offline members included
(:data:`TAILNET_ADVICE`).

Invites, incoming requests and the outgoing join are memory only: a restart
cancels them, which is the safe direction.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from backend import log

#: Seconds between gossip passes (pull every reachable member's roster).
INTERVAL = 30.0
#: The fleet protocol this build speaks — advertised in the remote hello.
FLEET_PROTO = 1

#: An invite code lives this long, and at most this many are live at once.
INVITE_TTL = 600.0
MAX_INVITES = 3
#: Brute-force guard on redeem: this many wrong codes from ONE caller inside
#: the window lock that caller out for the lockout, doubled on each repeat
#: lockout up to the cap (its misses never cost anyone else their code) …
_FAIL_LIMIT = 5
_FAIL_WINDOW = 600.0
_LOCKOUT = 60.0
_MAX_LOCKOUT = 3600.0
#: … and this many from everyone together, from at least this many different
#: addresses (guessing from many machines), burn every live invite.
_GLOBAL_FAIL_LIMIT = 20
_GLOBAL_FAIL_IPS = 3

#: What removal can't do by itself (see the module docstring) — shown with
#: every removal.
TAILNET_ADVICE = (
    "If it was lost or stolen, also remove it from your tailnet in the "
    "Tailscale admin console — that cuts it off everywhere at once, even from "
    "devices that are offline now"
)
#: A re-admission of a removed device (:func:`bundle_for`) is honoured this
#: long — the joiner announces itself at once, gossip catches a lost announce.
ADMIT_TTL = 86400.0

#: Clock skew tolerated on a timestamp another device sends (roster entries,
#: tombstones): anything later than now + this is pulled back to it, so a
#: far-future ``added_at`` can't outrank every tombstone forever.
_MAX_SKEW = 300.0
#: Old keys are kept this long (or until every member is seen on the new
#: epoch) so a member that was offline at a removal can still be handed the
#: new key under the one it holds.
PREV_KEY_TTL = 30 * 86400.0

#: An incoming join request waits this long; at most this many wait at once.
REQUEST_TTL = 600.0
MAX_REQUESTS = 5
#: How often a device waiting for approval asks again.
POLL_INTERVAL = 2.0

#: Public endpoints (redeem / requests) per client IP: this many per window.
PUBLIC_LIMIT = 20
PUBLIC_WINDOW = 60.0
#: The joiner's poll has its own, roomier bucket: it asks every
#: POLL_INTERVAL s for up to REQUEST_TTL (30 a minute — over PUBLIC_LIMIT, so
#: sharing the bucket would cut every request-to-join off after ~40 s), and it
#: needs an unguessable id AND a 24-byte secret, so there is nothing to guess.
POLL_LIMIT = 90

#: Crockford base32 — no I, L, O, U, so a code read aloud can't be mistyped
#: into another valid code (see :func:`normalize_code`).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret
_CODE_LEN = 8

#: A device key is a MagicDNS label.
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_MAX_HOST = 255
_MAX_CODE = 64
_JOIN_PREFIX = ("mindflock", "devices", "join")

_LOCK = threading.RLock()


class TooManyAttempts(PermissionError):
    """Redeem is locked out after repeated wrong codes (the route's 429)."""


def _now() -> float:
    # One seam for the clock, so tests can age invites and requests.
    return time.time()


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
# (path, mtime_ns, size) -> parsed doc. key_valid() runs on every request once
# auth accepts the fleet key, so the file is re-read only when it changed.
_CACHE: Dict[str, object] = {"sig": None, "doc": None}


def _path() -> Path:
    from backend.config import settings as _settings

    return _settings.settings_path().parent / "fleet.json"


def _empty() -> dict:
    return {
        "id": "",
        "key": "",
        "epoch": 0,
        "members": {},
        "removed": {},
        "prev_keys": {},
        "admits": {},
    }


def _ts(v, clamp: bool) -> Optional[float]:
    """A timestamp from JSON, or None for junk. ``json.loads`` accepts
    ``Infinity``/``NaN``: an infinite tombstone would make a re-add impossible
    and an infinite ``added_at`` would outrank every removal — both dropped.
    ``clamp`` (data from ANOTHER device) pulls a future time back to
    now + :data:`_MAX_SKEW`."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if clamp:
        f = min(f, _now() + _MAX_SKEW)
    return f


def clean_dns(dns, key: str) -> str:
    """A member's full MagicDNS name (lower-case, no trailing dot) — or ""
    when ``dns`` isn't a name whose first label is ``key``."""
    name = str(dns or "").strip().rstrip(".").lower()[:_MAX_HOST]
    if not name or name.split(".")[0] != key:
        return ""
    return name


def _clean_members(raw, clamp: bool = False) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or not DEVICE_RE.match(k) or not isinstance(v, dict):
            continue
        added_at = _ts(v.get("added_at") or 0, clamp)
        if added_at is None:
            continue
        out[k] = {
            "host": str(v.get("host") or "")[:_MAX_HOST],
            "added_at": added_at,
            "added_by": str(v.get("added_by") or "")[:_MAX_HOST],
            "dns": clean_dns(v.get("dns"), k),
        }
    return out


def _clean_removed(raw, clamp: bool = False) -> Dict[str, dict]:
    """Tombstones as ``{key: {"at", "by"}}`` — the bare-timestamp form an
    earlier build wrote is read as one with no ``by``."""
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or not DEVICE_RE.match(k):
            continue
        by = ""
        if isinstance(v, dict):
            by = v.get("by")
            by = by if isinstance(by, str) and DEVICE_RE.match(by) else ""
            v = v.get("at")
        ts = _ts(v, clamp)
        if ts is not None:
            out[k] = {"at": ts, "by": by}
    return out


def _clean_admits(raw) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        ts = _ts(v, False)
        if isinstance(k, str) and DEVICE_RE.match(k) and ts is not None:
            out[k] = ts
    return out


def _clean_prev_keys(raw) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(v, dict) or not str(k).isdigit():
            continue
        key, at = v.get("key"), _ts(v.get("at"), False)
        if isinstance(key, str) and _KEY_RE.match(key) and at is not None:
            out[str(int(k))] = {"key": key, "at": at}
    return out


def _normalize(data) -> dict:
    if not isinstance(data, dict):
        return _empty()
    try:
        epoch = int(data.get("epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    doc = {
        "id": str(data.get("id") or ""),
        "key": str(data.get("key") or ""),
        "epoch": max(epoch, 0),
        "members": _clean_members(data.get("members")),
        "removed": _clean_removed(data.get("removed")),
        "prev_keys": _clean_prev_keys(data.get("prev_keys")),
        "admits": _clean_admits(data.get("admits")),
    }
    if not doc["id"] or not doc["key"]:
        doc["id"], doc["key"], doc["prev_keys"], doc["admits"] = "", "", {}, {}
    return doc


def _load() -> dict:
    """The persisted doc (a private copy the caller may mutate)."""
    path = _path()
    try:
        st = path.stat()
        sig = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _empty()
    with _LOCK:
        if _CACHE["sig"] != sig:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            _CACHE["sig"], _CACHE["doc"] = sig, _normalize(raw)
        return copy.deepcopy(_CACHE["doc"])


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fleet.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    with _LOCK:
        _CACHE["sig"], _CACHE["doc"] = None, None


def _self_ident() -> dict:
    from backend.web.core import remote as _remote

    return _remote.self_identity()


def _self_key() -> str:
    return _self_ident()["key"]


def _self_host() -> str:
    ident = _self_ident()
    return ident.get("host") or ident["key"]


def _self_dns() -> str:
    ident = _self_ident()
    return clean_dns(ident.get("dns"), ident["key"])


def _emit(event: str, **data) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(event, data=data)
    except Exception:  # noqa: BLE001 — an event must never break a join
        pass


def _gone_at(doc: dict, key: str) -> Optional[float]:
    """When ``key`` was removed (its tombstone), or None."""
    t = doc["removed"].get(key)
    return None if t is None else t["at"]


def _live(doc: dict, key: str) -> bool:
    m = doc["members"].get(key)
    if m is None:
        return False
    gone = _gone_at(doc, key)
    return gone is None or gone < m["added_at"]


def _left(doc: dict, key: str) -> bool:
    """``key``'s tombstone is its own leave (``by`` is itself), not a removal
    by another device."""
    t = doc["removed"].get(key)
    return bool(t) and t.get("by") == key


def _dead_here(doc: dict) -> Set[str]:
    """Devices this device knows were REMOVED by another device (tombstoned,
    not by themselves, and not re-added here since): no roster from
    elsewhere brings them back. A device that left on its own isn't one —
    a later admit anywhere revives it (max timestamp, like any entry)."""
    return {k for k in doc["removed"] if not _live(doc, k) and not _left(doc, k)}


#: Devices removed here (see :func:`_dead_here`) that another member's roster
#: says were let back in: ``{key: {"host", "by", "added_at", "dns"}}``. Memory
#: only — gossip refills it every pass. The person decides (:func:`allow`).
_READMITTED: Dict[str, dict] = {}


def _note_readmitted(doc: dict, other: dict, dead: Set[str]) -> None:
    """Remember every device in ``dead`` that ``other``'s roster has live
    again under an add newer than this device's tombstone for it."""
    for k in dead:
        m = other["members"].get(k)
        if m is None or _live(doc, k) or not _live(other, k):
            continue
        if m["added_at"] <= (_gone_at(doc, k) or 0.0):
            continue  # an entry from before the removal, not a re-admission
        _READMITTED[k] = {
            "host": m.get("host") or k,
            "by": m.get("added_by") or "",
            "added_at": m["added_at"],
            "dns": m.get("dns") or "",
        }


def _tombstone(doc: dict, key: str, by: str) -> None:
    """Remove ``key`` now — never earlier than the add it cancels (a clock
    behind the adder's)."""
    m = doc["members"].get(key)
    at = max(_now(), m["added_at"] if m else 0.0)
    doc["removed"][key] = {"at": at, "by": str(by or "")}


def _merge_tombstone(key: str, mine: Optional[dict], t: dict) -> dict:
    """The one tombstone for ``key`` out of ``mine`` (may be None) and ``t``.

    A removal by ANOTHER device outranks the device's own leave (a
    self-tombstone, ``by == key``) whatever their order: it stays sticky and
    keeps its remover, taking the later ``at`` so a re-admission between the
    two is still cancelled by the leave. Two of a kind: the later one wins.
    Same answer whichever side each arrives from, so every member converges."""
    if mine is None:
        return dict(t)
    t_removal = t.get("by") != key
    mine_removal = mine.get("by") != key
    if t_removal == mine_removal:
        return dict(t) if t["at"] > mine["at"] else mine
    removal = t if t_removal else mine
    return {"at": max(t["at"], mine["at"]), "by": removal.get("by") or ""}


def _union_removed(doc: dict, other: Dict[str, dict]) -> bool:
    """Fold ``other``'s tombstones into ``doc``'s (:func:`_merge_tombstone`
    per device); none is ever dropped."""
    changed = False
    for k, t in other.items():
        mine = doc["removed"].get(k)
        merged = _merge_tombstone(k, mine, t)
        if merged != mine:
            doc["removed"][k] = merged
            changed = True
    return changed


# --------------------------------------------------------------------------- #
# store API
# --------------------------------------------------------------------------- #
def state() -> dict:
    """A deep copy of the persisted doc."""
    return _load()


def in_fleet() -> bool:
    doc = _load()
    return bool(doc["id"] and doc["key"])


def fleet_id() -> str:
    return _load()["id"]


def fleet_key() -> str:
    return _load()["key"]


def is_member(key: str) -> bool:
    return _live(_load(), key)


def live_members() -> Dict[str, dict]:
    doc = _load()
    return {k: v for k, v in doc["members"].items() if _live(doc, k)}


def _dns_matches(doc: dict, key: str, dev: Optional[dict]) -> bool:
    recorded = (doc["members"].get(key) or {}).get("dns") or ""
    if not recorded:
        return True  # nothing recorded yet (an older member): the label decides
    seen = str((dev or {}).get("dns") or "").strip().rstrip(".").lower()
    return seen == recorded


def member_device(dev: Optional[dict]) -> bool:
    """Whether the DISCOVERED device ``dev`` (a :mod:`remote` record) is a live
    member: its key is on the roster AND, when the roster recorded the
    member's full MagicDNS name, ``dev`` carries that same name. A node that
    merely shares the first label (another tailnet's ``ethans-mac-mini``, a
    re-registered name) is not the member and never gets the key."""
    if not dev or not dev.get("key"):
        return False
    doc = _load()
    return _live(doc, dev["key"]) and _dns_matches(doc, dev["key"], dev)


def key_valid(candidate: Optional[str]) -> bool:
    """Constant-time compare ``candidate`` against the fleet key (``False``
    outside a fleet — an empty key must never match an empty candidate)."""
    if not candidate:
        return False
    key = _load()["key"]
    if not key:
        return False
    return hmac.compare_digest(str(candidate), key)


def create() -> dict:
    """Start a fleet of one (this device). Idempotent."""
    with _LOCK:
        doc = _load()
        me = _self_key()
        if doc["id"] and doc["key"]:
            if not _live(doc, me):
                _put_member(doc, me, _self_host(), me, _self_dns())
                _save(doc)
            return _load()
        doc = _empty()
        doc.update(id=secrets.token_hex(8), key=secrets.token_urlsafe(32), epoch=1)
        _put_member(doc, me, _self_host(), me, _self_dns())
        _save(doc)
        return _load()


def bundle() -> dict:
    """Everything a joiner receives — INCLUDING the key."""
    doc = _load()
    return copy.deepcopy(
        {k: doc[k] for k in ("id", "key", "epoch", "members", "removed")}
    )


def bundle_for(device: str, host: str, dns: str = "") -> dict:
    """The :func:`bundle` handed to a joiner: the roster plus a live entry for
    the joiner itself (added now, by this device). This device's OWN roster is
    left alone — the joiner puts itself on it once it actually holds the key
    (its announce and gossip), so a join that never completes leaves no ghost
    member behind."""
    out = bundle()
    tmp = {"members": out["members"], "removed": out["removed"]}
    _put_member(tmp, device, host, _self_key(), dns)
    _note_admit(device, out["members"][device]["added_at"])
    return out


def _note_admit(device: str, added_at: float) -> None:
    """This device is letting ``device`` in. When it is one this device knows
    was removed, remember the admission: the joiner's announce (and gossip)
    then brings it back HERE — the one way past a tombstone (see
    :func:`_union`)."""
    with _LOCK:
        doc = _load()
        if not doc["id"] or device not in _dead_here(doc):
            return
        cut = _now() - ADMIT_TTL
        doc["admits"] = {k: t for k, t in doc["admits"].items() if t > cut}
        doc["admits"][device] = added_at
        _save(doc)


def roster() -> dict:
    """The roster members gossip (no key)."""
    doc = _load()
    return copy.deepcopy({k: doc[k] for k in ("id", "epoch", "members", "removed")})


def _put_member(doc: dict, key: str, host: str, by: str, dns: str = "") -> None:
    # added_at must outrank any tombstone, even one stamped this same instant
    # (or by a device whose clock runs ahead), or the re-add would not take.
    now = _now()
    gone = _gone_at(doc, key)
    if gone is not None and gone >= now:
        now = gone + 0.001
    doc["members"][key] = {
        "host": str(host or key)[:_MAX_HOST],
        "added_at": now,
        "added_by": str(by or "")[:_MAX_HOST],
        "dns": clean_dns(dns, key),
    }


def _validate_bundle(b) -> dict:
    if not isinstance(b, dict):
        raise ValueError("not a device-group bundle")
    fid, key = b.get("id"), b.get("key")
    if not isinstance(fid, str) or not _ID_RE.match(fid):
        raise ValueError("bundle has no valid id")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError("bundle has no valid key")
    try:
        epoch = int(b.get("epoch"))
    except (TypeError, ValueError):
        raise ValueError("bundle has no valid epoch") from None
    if epoch < 1:
        raise ValueError("bundle has no valid epoch")
    if not isinstance(b.get("members"), dict):
        raise ValueError("bundle has no members")
    return {
        "id": fid,
        "key": key,
        "epoch": epoch,
        "members": _clean_members(b.get("members"), clamp=True),
        "removed": _clean_removed(b.get("removed") or {}, clamp=True),
        "prev_keys": {},
    }


class OlderBundle(ValueError):
    """The device handed over a key OLDER than the one this device holds for
    the same group (it missed a key change) — nothing was adopted."""


def _keep_prev_key(doc: dict, epoch: int, key: str) -> None:
    """Remember the key of ``epoch`` (see :func:`deliver_rekey`)."""
    if key and epoch > 0:
        doc.setdefault("prev_keys", {})[str(int(epoch))] = {"key": key, "at": _now()}


def _prune_prev_keys(doc: dict) -> None:
    cut = _now() - PREV_KEY_TTL
    keys = doc.get("prev_keys") or {}
    for ep in list(keys):
        if keys[ep]["at"] < cut or int(ep) >= doc["epoch"]:
            del keys[ep]


def adopt_bundle(b: dict) -> List[str]:
    """Become a member of the fleet ``b`` describes (a :func:`bundle`).

    * A DIFFERENT fleet replaces this device's only when that one has no
      other live members (else ValueError — leaving it would orphan them;
      the person leaves it first).
    * The SAME fleet (a rejoin): a bundle on a NEWER key epoch replaces the
      local members outright — whatever this device merged while it held the
      old key could have been written by anyone holding that key, the device
      the new key was made to lock out included. The same epoch (an explicit
      rejoin: the admitter's key is taken) unions; an OLDER one is refused
      (:class:`OlderBundle`) and nothing changes. Either way this device's
      own tombstones are kept: a device it knows was removed stays removed.
    * A bundle that removes this device and has no live entry for it is
      refused: that is a removal, not an invitation.

    Returns the devices this one knows were removed that the bundle has LIVE
    (``exposed``: the admitter may have handed them the key it just gave us
    — a key-conflict or missed-change rejoin). They are tombstoned again
    here, later than the bundle's add, and the caller replaces the key once
    the join completes (:func:`_finish_join`, :func:`adopt_from_peer`)."""
    new = _validate_bundle(b)
    exposed: List[str] = []
    with _LOCK:
        doc = _load()
        me = _self_key()
        if me in new["removed"] and not _live(new, me):
            raise ValueError("this device was removed from that group")
        if doc["id"] and doc["id"] != new["id"]:
            live = [k for k in doc["members"] if _live(doc, k)]
            if any(k != me for k in live):
                raise ValueError(
                    "this device is already one of %d devices — leave that group first"
                    % len(live)
                )
        if doc["id"] == new["id"]:
            if new["epoch"] < doc["epoch"]:
                raise OlderBundle(
                    "that device has an older key for your devices than this one"
                )
            dead = _dead_here(doc) - {me}
            exposed = sorted(k for k in dead if _live(new, k))
            cut = {
                k: (doc["removed"][k], new["members"][k]["added_at"]) for k in exposed
            }
            if new["epoch"] > doc["epoch"]:
                old = doc
                doc = new
                doc["prev_keys"] = dict(old.get("prev_keys") or {})
                doc["admits"] = dict(old.get("admits") or {})
                _keep_prev_key(doc, old["epoch"], old["key"])
                _prune_prev_keys(doc)
                _union_removed(doc, old["removed"])
                _keep_dead(doc, dead, old["members"])
            else:
                doc["key"] = new["key"]
                _union(doc, new, dead)
            for k, (t, added_at) in cut.items():
                # Later than the add the bundle carries, so the admitter (who
                # has it live) takes the removal when it next hears from us.
                at = max(_now(), t["at"], added_at + 0.001)
                doc["removed"][k] = {"at": at, "by": t.get("by") or me}
        else:
            doc = new
        if not _live(doc, me):
            _put_member(doc, me, _self_host(), me, _self_dns())
        elif not doc["members"][me].get("dns") and _self_dns():
            doc["members"][me]["dns"] = _self_dns()
        _save(doc)
    return exposed


def add_member(key: str, host: str, by: str, dns: str = "") -> None:
    """Add (or re-add — a fresh ``added_at`` outranks an older tombstone)."""
    if not DEVICE_RE.match(key or ""):
        raise ValueError("not a device name: %r" % key)
    with _LOCK:
        doc = _load()
        _put_member(doc, key, host, by, dns)
        _save(doc)


def remove_member(key: str) -> None:
    """Tombstone ``key``, recording this device as the one that removed it."""
    with _LOCK:
        doc = _load()
        _tombstone(doc, key, _self_key())
        _save(doc)


def allow(key: str) -> dict:
    """The person lets ``key`` back in HERE (Settings → Devices, "Allow it
    here" on a device another member re-admitted — status
    ``readmitted_elsewhere``): this device's tombstone for it goes and it is
    a member again, added now by this device, so the tombstones the other
    members still carry are older than the add. Raises ``KeyError`` when
    ``key`` isn't a device removed here."""
    with _LOCK:
        doc = _load()
        if not doc["id"] or not DEVICE_RE.match(key or ""):
            raise KeyError(key)
        if key not in _dead_here(doc) or key == _self_key():
            raise KeyError(key)
        seen = _READMITTED.get(key) or {}
        old = doc["members"].get(key) or {}
        host = seen.get("host") or old.get("host") or key
        doc["removed"].pop(key, None)
        _put_member(
            doc, key, host, _self_key(), seen.get("dns") or old.get("dns") or ""
        )
        _save(doc)
        _READMITTED.pop(key, None)
    _emit(
        "device.joined",
        device=key,
        host=host,
        detail="%s is one of your devices here again" % host,
    )
    return {"ok": True, "device": key, "host": host}


def _keep_dead(doc: dict, dead: Set[str], old_members: Dict[str, dict]) -> None:
    """After ``doc``'s members were replaced: every device in ``dead`` (known
    removed here before) keeps the entry it had here — its tombstone, kept
    too, outranks that — whatever newer ``added_at`` the new roster claims."""
    for k in dead:
        if k in old_members:
            doc["members"][k] = dict(old_members[k])
        else:
            doc["members"].pop(k, None)


def _union(doc: dict, other: dict, dead: Set[str] = frozenset()) -> bool:
    """Fold ``other``'s roster into ``doc``'s (max timestamp per entry). A
    device in ``dead`` (known removed here) is never brought back by it —
    unless this device admitted it again itself (``admits``)."""
    changed = False
    cut = _now() - ADMIT_TTL
    admits = doc.setdefault("admits", {})
    for k, m in other["members"].items():
        mine = doc["members"].get(k)
        if k in dead:
            at = admits.get(k)
            if at is None or at <= cut:
                continue
            if m["added_at"] > (_gone_at(doc, k) or 0.0):
                del admits[k]  # used: the device is back
                changed = True
        if mine is None or m["added_at"] > mine["added_at"]:
            doc["members"][k] = dict(m)
            changed = True
        elif m["added_at"] == mine["added_at"] and m.get("dns") and not mine.get("dns"):
            # The same add, now with the member's full name (it filled its
            # own in after joining): take it without a new added_at.
            mine["dns"] = m["dns"]
            changed = True
    if _union_removed(doc, other["removed"]):
        changed = True
    return changed


def _epoch_of(body) -> Optional[int]:
    try:
        return int(body.get("epoch"))
    except (TypeError, ValueError, AttributeError):
        return None


def merge_roster(remote: dict) -> bool:
    """Fold another member's :func:`roster` into ours. Returns whether
    anything changed. Only a roster of the same fleet AND the same key epoch
    is taken (one learned under another key is not trusted — see
    :func:`adopt_bundle`). A member that wasn't live before is announced
    (``device.joined``: the joiner itself confirms, so the admitting screen
    updates when the join really happened). A roster that removes THIS
    device makes it leave. A device this one knows was removed stays removed
    whatever ``added_at`` the roster claims (see :func:`_union`)."""
    if not isinstance(remote, dict):
        return False
    other = {
        "members": _clean_members(remote.get("members"), clamp=True),
        "removed": _clean_removed(remote.get("removed"), clamp=True),
    }
    with _LOCK:
        doc = _load()
        if not doc["id"] or remote.get("id") != doc["id"]:
            return False
        if _epoch_of(remote) != doc["epoch"]:
            return False
        me = _self_key()
        before = {k for k in doc["members"] if _live(doc, k)}
        dead = _dead_here(doc) - {me}
        changed = _union(doc, other, dead)
        _note_readmitted(doc, other, dead)
        if changed:
            _save(doc)
        joined = [
            (k, doc["members"][k].get("host") or k)
            for k in sorted(doc["members"])
            if k != me and k not in before and _live(doc, k)
        ]
        gone_here = me in doc["removed"] and not _live(doc, me)
    if gone_here:
        _removed_here(_removed_by(doc, me))
        return changed
    for key, host in joined:
        _emit(
            "device.joined",
            device=key,
            host=host,
            detail="%s joined your devices" % host,
        )
    return changed


def rekey() -> str:
    """Rotate the fleet key (epoch + 1); returns the new key. The old one is
    kept (``prev_keys``) to hand the new key to members that were away."""
    with _LOCK:
        doc = _load()
        if not doc["id"]:
            raise ValueError("not in a device group")
        _keep_prev_key(doc, doc["epoch"], doc["key"])
        doc["key"] = secrets.token_urlsafe(32)
        doc["epoch"] += 1
        _prune_prev_keys(doc)
        _save(doc)
        return doc["key"]


def apply_rekey(body: dict) -> bool:
    """Take the next key a member sent (under the current one): the group's
    next epoch ONLY — never a jump, so a removed device that still holds an
    old key can't push a member onto a key "newer" than the real one.

    The body's roster REPLACES ours (one learned under the old key could have
    been written by anyone holding it), except that tombstones only ever grow
    (ours ∪ the body's), a device this one knows was removed stays removed,
    and this device keeps its own entry — unless the body removes it, which
    makes it leave."""
    if not isinstance(body, dict):
        return False
    key = body.get("key")
    epoch = _epoch_of(body)
    if epoch is None:
        return False
    if not isinstance(key, str) or not _KEY_RE.match(key):
        return False
    members = _clean_members(body.get("members"), clamp=True)
    removed = _clean_removed(body.get("removed"), clamp=True)
    with _LOCK:
        doc = _load()
        if not doc["id"] or body.get("id") != doc["id"]:
            return False
        if epoch != doc["epoch"] + 1:
            return False
        me = _self_key()
        dead = _dead_here(doc) - {me}
        before = {k for k in doc["members"] if _live(doc, k)}
        old_epoch = doc["epoch"]
        new = {
            "id": doc["id"],
            "key": key,
            "epoch": epoch,
            "members": members,
            "removed": dict(doc["removed"]),
            "prev_keys": dict(doc.get("prev_keys") or {}),
            "admits": dict(doc.get("admits") or {}),
        }
        _union_removed(new, removed)
        _keep_dead(new, dead, doc["members"])
        _note_readmitted(new, {"members": members, "removed": removed}, dead)
        mine = doc["members"].get(me)
        if mine and mine["added_at"] > (new["members"].get(me) or {}).get(
            "added_at", float("-inf")
        ):
            new["members"][me] = dict(mine)
        gone_here = me in new["removed"] and not _live(new, me)
        if not gone_here and not _live(new, me):
            _put_member(new, me, _self_host(), me, _self_dns())
        _keep_prev_key(new, old_epoch, doc["key"])
        _prune_prev_keys(new)
        _save(new)
        joined = [
            (k, new["members"][k].get("host") or k)
            for k in sorted(new["members"])
            if k != me and k not in before and _live(new, k)
        ]
    # Whoever was on our old key is on this one now as far as we know (the
    # sender is): keep sending them the key until gossip says otherwise.
    for p in _PEERS.values():
        if p.get("epoch") == old_epoch:
            p.update(epoch=None, conflict=False)
    if gone_here:
        _removed_here(_removed_by(new, me))
        return True
    for k, host in joined:
        _emit(
            "device.joined", device=k, host=host, detail="%s joined your devices" % host
        )
    return True


def prev_key(epoch) -> str:
    """The key this device held at ``epoch`` ("" when it never did, or it
    was pruned)."""
    try:
        ep = str(int(epoch))
    except (TypeError, ValueError):
        return ""
    return ((_load().get("prev_keys") or {}).get(ep) or {}).get("key", "")


def _forget_prev_keys() -> None:
    with _LOCK:
        doc = _load()
        if doc.get("prev_keys"):
            doc["prev_keys"] = {}
            _save(doc)


def rekey_body() -> dict:
    """What a member is handed with the current key (:func:`remove`,
    :func:`deliver_rekey`): id, epoch, key and the roster."""
    doc = _load()
    return copy.deepcopy(
        {k: doc[k] for k in ("id", "epoch", "key", "members", "removed")}
    )


def key_fp(key: str) -> str:
    """A non-secret fingerprint of a fleet key (the same on every device):
    two members on the same epoch whose fingerprints differ hold different
    keys — a conflict, not one of them being behind."""
    if not key:
        return ""
    return hashlib.sha256(("mindflock-fleet-fp:" + key).encode("utf-8")).hexdigest()[
        :16
    ]


def unauthorized_body() -> dict:
    """The 401 a member route answers with: no secret, but enough for the
    caller to tell "I am behind" (my epoch is higher) from "it is behind",
    and — same epoch, different ``kfp`` — "we hold different keys"."""
    doc = _load()
    return {
        "error": "not one of this device's devices",
        "id": doc["id"],
        "epoch": doc["epoch"],
        "kfp": key_fp(doc["key"]),
    }


def leave() -> None:
    """Forget the fleet locally (no network)."""
    with _LOCK:
        _save(_empty())
        _INVITES.clear()
        _STALE.clear()
        _PEERS.clear()
        _READMITTED.clear()


def _removed_by(doc: dict, key: str) -> str:
    return (doc["removed"].get(key) or {}).get("by") or ""


def _removed_here(by: str = "") -> None:
    """Another member's roster says this device was removed: leave locally
    and stop syncing settings with devices that no longer trust us."""
    me = _self_key()
    leave()
    try:
        from backend.web.core import settings_sync

        settings_sync.disable()
    except Exception:  # noqa: BLE001
        pass
    if log.InfoLog is not None:
        log.InfoLog.Printf(
            "fleet: this device was removed from your devices (by %s)", by or "?"
        )
    _emit(
        "device.removed",
        device=me,
        host=_self_host(),
        by=by,
        detail=(
            "this device was removed from your devices by %s" % by
            if by and by != me
            else "this device was removed from your devices"
        ),
    )


# --------------------------------------------------------------------------- #
# codes
# --------------------------------------------------------------------------- #
_CODE_MAP = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def normalize_code(s: str) -> str:
    """Uppercase, drop spaces/dashes, and fold the look-alikes Crockford
    leaves out (I/L→1, O→0, U→V) so a code read aloud still matches."""
    s = re.sub(r"[\s-]+", "", str(s or "")).upper()
    return s.translate(_CODE_MAP)


def _format_code(code: str) -> str:
    return code[:4] + "-" + code[4:]


def parse_join_string(s: str) -> Tuple[str, str]:
    """``(device | "", code)`` from what a person pasted: the whole command
    (``mindflock devices join <dev> <code>``), ``<dev> <code>``, or a bare code
    (which may itself be typed with a space: ``ABCD EFGH``)."""
    tokens = str(s or "").split()
    if [t.lower() for t in tokens[:3]] == list(_JOIN_PREFIX):
        tokens = tokens[3:]
    if len(tokens) >= 2 and DEVICE_RE.match(tokens[0].lower()):
        rest = normalize_code("".join(tokens[1:]))
        if len(rest) == _CODE_LEN:
            return tokens[0].lower(), rest
    return "", normalize_code("".join(tokens))


# --------------------------------------------------------------------------- #
# invites (a code made here, typed on the new device)
# --------------------------------------------------------------------------- #
_INVITES: List[dict] = []  # {"code": normalized, "created_at", "expires_at"}
_FAILS: Dict[str, List[float]] = {}  # caller IP -> its wrong-redeem timestamps
_ALL_FAILS: List[Tuple[float, str]] = []  # (when, caller IP) — the backstop
_LOCKED_UNTIL: Dict[str, float] = {}  # caller IP -> locked out until
_LOCKOUTS: Dict[str, Tuple[int, float]] = {}  # caller IP -> (lockouts, last until)


def _prune_invites(now: float) -> None:
    _INVITES[:] = [i for i in _INVITES if i["expires_at"] > now]


def _invite_row(inv: dict) -> dict:
    code = _format_code(inv["code"])
    return {
        "code": code,
        "expires_at": inv["expires_at"],
        "device": _self_key(),
        "command": "mindflock devices join %s %s" % (_self_key(), code),
    }


def create_invite() -> dict:
    """A fresh single-use code (starts the fleet when there is none)."""
    if not in_fleet():
        create()
    with _LOCK:
        now = _now()
        _prune_invites(now)
        code = "".join(secrets.choice(ALPHABET) for _ in range(_CODE_LEN))
        inv = {"code": code, "created_at": now, "expires_at": now + INVITE_TTL}
        _INVITES.append(inv)
        del _INVITES[:-MAX_INVITES]  # oldest dropped
        return _invite_row(inv)


def invites() -> List[dict]:
    with _LOCK:
        _prune_invites(_now())
        return [_invite_row(i) for i in _INVITES]


def cancel_invites() -> None:
    with _LOCK:
        _INVITES.clear()


def _claim_matches_peer(device: str, ip: str) -> bool:
    """Whether a public request claiming to be ``device`` may come from
    ``ip``: when ``ip`` is a tailnet address and discovery knows ``device``'s
    addresses, it must be one of them. Anything we can't check (a caller
    behind a proxy we can't vouch for, a device discovery hasn't seen) passes
    — the person still compares the code on both screens."""
    if not ip:
        return True
    try:
        from backend.web.core import tailnet_trust as _tt

        if not _tt.is_tailnet_ip(ip):
            return True
    except Exception:  # noqa: BLE001
        return True
    dev = _device(device) or {}
    known = {a for a in [dev.get("ip")] + list(dev.get("ips") or []) if a}
    return not known or ip in known


def _joiner_dns(device: str, claimed: str) -> str:
    """The joiner's full MagicDNS name for its roster entry: what discovery
    saw for it when it has one (Tailscale's word), else what it claims."""
    seen = clean_dns((_device(device) or {}).get("dns"), device)
    return seen or clean_dns(claimed, device)


def _note_failure(ip: str, now: float) -> None:
    """A wrong code from ``ip``: lock that caller out after
    :data:`_FAIL_LIMIT` misses — :data:`_LOCKOUT`, doubled on every repeat
    lockout up to :data:`_MAX_LOCKOUT` (one caller's guesses never burn
    anyone's code, however long it keeps at it) — and burn every live invite
    only once :data:`_GLOBAL_FAIL_LIMIT` misses pile up from at least
    :data:`_GLOBAL_FAIL_IPS` different addresses."""
    if len(_FAILS) > 1024:  # a bound, not a policy
        for k in [k for k, v in _FAILS.items() if not v or v[-1] <= now - _FAIL_WINDOW]:
            del _FAILS[k]
    if len(_LOCKOUTS) > 1024:
        for k in [k for k, v in _LOCKOUTS.items() if v[1] <= now - _MAX_LOCKOUT]:
            del _LOCKOUTS[k]
    mine = [t for t in _FAILS.get(ip, []) if t > now - _FAIL_WINDOW] + [now]
    _FAILS[ip] = mine
    if len(mine) >= _FAIL_LIMIT:
        _FAILS.pop(ip, None)
        n, last = _LOCKOUTS.get(ip, (0, 0.0))
        if last <= now - _MAX_LOCKOUT:
            n = 0  # quiet for an hour: start over
        span = min(_LOCKOUT * (2**n), _MAX_LOCKOUT)
        _LOCKED_UNTIL[ip] = now + span
        _LOCKOUTS[ip] = (n + 1, now + span)
        if log.InfoLog is not None:
            log.InfoLog.Printf(
                "fleet: %d wrong device codes from %s — locked out for %d s",
                _FAIL_LIMIT,
                ip or "?",
                int(span),
            )
    _ALL_FAILS[:] = [f for f in _ALL_FAILS if f[0] > now - _FAIL_WINDOW] + [(now, ip)]
    if (
        len(_ALL_FAILS) >= _GLOBAL_FAIL_LIMIT
        and len({a for _, a in _ALL_FAILS}) >= _GLOBAL_FAIL_IPS
    ):
        # Guessing from many addresses: no code made before this moment works.
        _INVITES.clear()
        _ALL_FAILS.clear()
        if log.InfoLog is not None:
            log.InfoLog.Printf(
                "fleet: %d wrong device codes — invites cancelled", _GLOBAL_FAIL_LIMIT
            )


def redeem(code: str, device: str, host: str, ip: str = "", dns: str = "") -> dict:
    """Trade a live invite code for the bundle (single use). Raises
    ``PermissionError`` on a wrong/expired code or a caller whose tailnet
    address isn't ``device``'s, :class:`TooManyAttempts` while that caller is
    locked out.

    The joiner is NOT added to this device's roster here (see
    :func:`bundle_for`): it adds itself once it holds the key."""
    if not DEVICE_RE.match(device or ""):
        raise ValueError("not a device name")
    want = normalize_code(code)[:_MAX_CODE]
    with _LOCK:
        now = _now()
        until = _LOCKED_UNTIL.get(ip, 0.0)
        if now < until:
            mins = int((until - now + 59) // 60)
            raise TooManyAttempts(
                "too many wrong codes — wait %s"
                % ("a minute" if mins <= 1 else "%d minutes" % mins)
            )
        _LOCKED_UNTIL.pop(ip, None)
        if not _claim_matches_peer(device, ip):
            raise PermissionError("that request didn't come from %s" % device)
        _prune_invites(now)
        hit = None
        # Compare against EVERY live invite (no early exit): the time taken
        # says nothing about which, or whether, one matched.
        for inv in _INVITES:
            if hmac.compare_digest(want.encode(), inv["code"].encode()):
                hit = inv
        if hit is None:
            _note_failure(ip, now)
            raise PermissionError("that code is wrong or expired")
        _INVITES.remove(hit)
        if not in_fleet():
            create()
        host = str(host or device)[:_MAX_HOST]
        return bundle_for(device, host, _joiner_dns(device, dns))


# --------------------------------------------------------------------------- #
# incoming requests (the new device asks, a person approves here)
# --------------------------------------------------------------------------- #
_REQUESTS: Dict[str, dict] = {}


def _six_digits() -> str:
    n = "%06d" % secrets.randbelow(10**6)
    return n[:3] + " " + n[3:]


def _prune_requests(now: float) -> None:
    for rid, req in list(_REQUESTS.items()):
        if req["state"] == "pending" and req["expires_at"] <= now:
            req["state"] = "expired"
        # Settled requests linger one more TTL so the asker's poll can still
        # read "denied"/"expired" — then they go.
        if req["expires_at"] + REQUEST_TTL <= now:
            del _REQUESTS[rid]


def open_request(
    device: str,
    host: str,
    secret_hash: str,
    ip: str = "",
    dns: str = "",
    runs_automation: bool = False,
) -> dict:
    """Record a join request; returns ``{id, code}`` (code shown on both
    screens). One per device — asking again (from the same address) replaces
    the earlier one. Raises ``PermissionError`` when the caller's tailnet
    address isn't the one discovery knows for ``device``, ``ValueError`` when
    another address already has a request waiting under that name."""
    if not DEVICE_RE.match(device or ""):
        raise ValueError("not a device name")
    if not isinstance(secret_hash, str) or not _HASH_RE.match(secret_hash):
        raise ValueError("secret_hash must be 64 hex characters")
    if not _claim_matches_peer(device, ip):
        raise PermissionError("that request didn't come from %s" % device)
    host = str(host or device)[:_MAX_HOST]
    with _LOCK:
        now = _now()
        _prune_requests(now)
        for rid, req in list(_REQUESTS.items()):
            if req["device"] != device:
                continue
            if req["state"] == "pending" and req.get("ip") and ip and req["ip"] != ip:
                raise ValueError("a request from %s is already waiting" % device)
            del _REQUESTS[rid]
        pending = sorted(
            (r for r in _REQUESTS.values() if r["state"] == "pending"),
            key=lambda r: r["created_at"],
        )
        for old in pending[: max(0, len(pending) - MAX_REQUESTS + 1)]:
            del _REQUESTS[old["id"]]
        rid = secrets.token_hex(8)
        req = {
            "id": rid,
            "device": device,
            "host": host,
            "dns": _joiner_dns(device, dns),
            "ip": ip or "",
            "code": _six_digits(),
            "secret_hash": secret_hash,
            "created_at": now,
            "expires_at": now + REQUEST_TTL,
            "state": "pending",
            # It runs PR review / issue handling now (see runs_automation).
            "runs_automation": bool(runs_automation),
        }
        _REQUESTS[rid] = req
    _emit(
        "device.join_requested",
        device=device,
        host=host,
        code=req["code"],
        id=rid,
        detail="%s · code %s" % (host, req["code"]),
    )
    return {"id": rid, "code": req["code"]}


def _check_secret(req: dict, secret: str) -> None:
    got = hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()
    if not hmac.compare_digest(got, req["secret_hash"]):
        raise PermissionError("wrong secret")


def request_state(rid: str, secret: str) -> dict:
    """What the asker polls. Raises ``KeyError`` (unknown id) or
    ``PermissionError`` (wrong secret). The bundle is served ONCE — and not
    at all when the device was removed after the approval (``denied``)."""
    with _LOCK:
        _prune_requests(_now())
        req = _REQUESTS.get(rid)
        if req is None:
            raise KeyError(rid)
        _check_secret(req, secret)
        if req["state"] == "approved":
            del _REQUESTS[rid]
            gone = _gone_at(_load(), req["device"])
            if not in_fleet() or (
                gone is not None and gone >= req.get("approved_at", 0.0)
            ):
                return {"state": "denied"}
            return {
                "state": "approved",
                "bundle": bundle_for(req["device"], req["host"], req.get("dns", "")),
            }
        return {"state": req["state"]}


def withdraw_request(rid: str, secret: str) -> dict:
    """The asker gave up (Cancel / Ctrl-C): drop its request while it is
    still pending, so nobody can approve a device that stopped listening.
    Raises ``KeyError`` / ``PermissionError`` like :func:`request_state`."""
    with _LOCK:
        _prune_requests(_now())
        req = _REQUESTS.get(rid)
        if req is None:
            raise KeyError(rid)
        _check_secret(req, secret)
        if req["state"] != "pending":
            return {"ok": False, "state": req["state"]}
        del _REQUESTS[rid]
        return {"ok": True, "state": "withdrawn"}


def _pending(rid: str) -> dict:
    _prune_requests(_now())
    req = _REQUESTS.get(rid)
    if req is None or req["state"] != "pending":
        raise KeyError(rid)
    return req


def approve(rid: str) -> dict:
    """Let the asking device in (starts the fleet when there is none). Its
    bundle goes out on its next poll; it becomes a member here when it
    announces itself with the key (:func:`merge_roster`)."""
    with _LOCK:
        req = _pending(rid)
        if not in_fleet():
            create()
        req["state"] = "approved"
        req["approved_at"] = _now()
        device, host = req["device"], req["host"]
        runs = bool(req.get("runs_automation"))
    return {"ok": True, "device": device, "host": host, "runs_automation": runs}


def deny(rid: str) -> dict:
    with _LOCK:
        req = _pending(rid)
        req["state"] = "denied"
        return {"ok": True, "device": req["device"], "host": req["host"]}


def pending_requests() -> List[dict]:
    with _LOCK:
        _prune_requests(_now())
        return [
            {
                k: r.get(k, "")
                for k in (
                    "id",
                    "device",
                    "host",
                    "code",
                    "created_at",
                    "expires_at",
                    "ip",
                )
            }
            for r in sorted(_REQUESTS.values(), key=lambda r: r["created_at"])
            if r["state"] == "pending"
        ]


# --------------------------------------------------------------------------- #
# public-endpoint rate limit
# --------------------------------------------------------------------------- #
_HITS: Dict[str, List[float]] = {}


def allow_public(ip: str, kind: str = "join") -> bool:
    """Count one hit on a public fleet endpoint from ``ip``; False once it is
    over :data:`PUBLIC_LIMIT` (``kind="poll"``: :data:`POLL_LIMIT`) in
    :data:`PUBLIC_WINDOW` s."""
    limit = POLL_LIMIT if kind == "poll" else PUBLIC_LIMIT
    ip = "%s:%s" % (kind, ip)
    with _LOCK:
        now = _now()
        if len(_HITS) > 1024:  # a bound, not a policy
            for k in [
                k for k, v in _HITS.items() if not v or v[-1] <= now - PUBLIC_WINDOW
            ]:
                del _HITS[k]
        hits = [t for t in _HITS.get(ip, []) if t > now - PUBLIC_WINDOW]
        if len(hits) >= limit:
            _HITS[ip] = hits
            return False
        hits.append(now)
        _HITS[ip] = hits
        return True


# --------------------------------------------------------------------------- #
# talking to other devices
# --------------------------------------------------------------------------- #
_BG: Set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """A fire-and-forget task that is kept alive until it finishes."""
    task = asyncio.ensure_future(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


def _known_devices() -> List[dict]:
    """Snapshots of every device discovery knows (reachable or not)."""
    from backend.web.core import remote as _remote

    return [dict(d) for d in list(_remote._DEVICES.values())]


def _device(key: str) -> Optional[dict]:
    from backend.web.core import remote as _remote

    d = _remote._DEVICES.get(key)
    return dict(d) if d else None


def _label(dev: Optional[dict], key: str = "") -> str:
    return (dev or {}).get("host") or (dev or {}).get("key") or key


async def _refresh(key: str) -> Optional[dict]:
    """Re-probe ``key``'s hello now (fresh ``fleet`` / ``fleet_proto``)."""
    from backend.web.core import remote as _remote

    fn = getattr(_remote, "refresh_device", None)
    if fn is None:
        return _device(key)
    try:
        snap = await fn(key)
    except Exception:  # noqa: BLE001
        snap = None
    return dict(snap) if isinstance(snap, dict) else _device(key)


async def _target(key: str) -> dict:
    """The device record to join through, re-probed once when the cached one
    looks unusable (offline, or an older hello without ``fleet_proto``)."""
    if not DEVICE_RE.match(key or ""):
        raise ValueError("not a device name: %r" % key)
    if key == _self_key():
        raise ValueError("that is this device")
    dev = _device(key)
    if not dev or not dev.get("reachable") or int(dev.get("fleet_proto") or 0) < 1:
        dev = await _refresh(key) or dev
    if not dev or not dev.get("reachable"):
        raise ValueError("%s isn't reachable right now" % _label(dev, key))
    if int(dev.get("fleet_proto") or 0) < 1:
        raise ValueError("update MindFlock on %s first" % _label(dev, key))
    return dev


def _visible_members(exclude: Tuple[str, ...] = ()) -> List[dict]:
    """Reachable devices that are live members — whatever their hello says
    about the fleet (a just-joined or just-rekeyed one may lag), but always
    under the member's recorded MagicDNS name (:func:`member_device`)."""
    me = _self_key()
    return [
        d
        for d in _known_devices()
        if d.get("reachable")
        and d["key"] != me
        and d["key"] not in exclude
        and member_device(d)
    ]


def _err_text(status: int, body, dev: Optional[dict]) -> str:
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    if not status:
        return "%s didn't answer" % _label(dev)
    if status == 404:
        return "update MindFlock on %s first" % _label(dev)
    return "%s answered HTTP %s" % (_label(dev), status)


async def _announce(targets: List[dict], body: dict, bearer: str) -> None:
    from backend.web.core import remote as _remote

    async def one(dev):
        try:
            await _remote.post_json(dev, "/api/fleet/roster", body, bearer=bearer)
        except Exception:  # noqa: BLE001 — gossip catches up later
            pass

    if targets:
        await asyncio.gather(*(one(d) for d in targets))


def _key_at(doc: dict, epoch: int) -> str:
    """The key this device held (or holds) at ``epoch``, "" when unknown."""
    if epoch == doc["epoch"]:
        return doc["key"]
    return ((doc.get("prev_keys") or {}).get(str(epoch)) or {}).get("key", "")


async def deliver_rekey(dev: dict, their: int, bearer: Optional[str] = None) -> bool:
    """Bring ``dev``, on key epoch ``their``, up to the CURRENT key and
    roster — what :func:`remove` does for every member it can reach, and what
    gossip does later for one that was away. A member takes a key change only
    one epoch at a time (:func:`apply_rekey`), so one that missed several gets
    each in turn, each sent under the key before it (``prev_keys``).
    ``bearer`` overrides the key the FIRST step is sent under (the one ``dev``
    itself handed over, when it is the device being joined through)."""
    from backend.web.core import remote as _remote

    doc = _load()
    try:
        their = int(their)
    except (TypeError, ValueError):
        return False
    if not doc["id"] or their < 1 or their >= doc["epoch"]:
        return False
    base = rekey_body()
    for ep in range(their + 1, doc["epoch"] + 1):
        under = (bearer if ep == their + 1 and bearer else "") or _key_at(doc, ep - 1)
        key = _key_at(doc, ep)
        if not under or not key:
            return False
        try:
            status, resp = await _remote.post_json(
                dev, "/api/fleet/rekey", dict(base, epoch=ep, key=key), bearer=under
            )
        except Exception:  # noqa: BLE001
            return False
        if not (status == 200 and isinstance(resp, dict) and resp.get("ok")):
            return False
    return True


def _on_epoch(key: str, epoch: int) -> None:
    """Record that member ``key`` just took key ``epoch`` from us — so the
    fleet key keeps going to it (:func:`peer_on_other_epoch`) before the
    next gossip pass confirms it."""
    _PEERS.setdefault(key, {"at": _now(), "status": 200}).update(
        epoch=epoch, error="", ahead=False, conflict=False, loser=False
    )


def _enable_remote_control() -> None:
    """Joining IS the permission: members drive and sync each other.

    A settings.json that exists but can't be read refuses the save
    (:class:`~backend.config.settings.SettingsUnreadable` — saving would
    replace it with defaults): that is logged, never a failed join; the
    person fixes the file and turns remote control on by hand."""
    from backend.config import settings as _settings

    try:
        _settings.update_settings(general={"remote_control": "on"})
    except _settings.SettingsUnreadable as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "fleet: couldn't turn remote control on (settings.json "
                "unreadable): %v",
                err,
            )


def runs_automation() -> bool:
    """Whether THIS device is actually running PR review or issue handling
    right now: it runs them here (:func:`_automation_here`) AND one of them is
    set up (PR review not switched off with repos chosen, or issue handling on
    with issue repos). What a joiner tells the device it joins through, so
    the automation stays where its history (the processed-PR / issue ledgers)
    is. Never raises."""
    try:
        from backend.config import settings as _settings

        gh = _settings.load_settings().github
        prs = gh.enabled is not False and bool(gh.repos)
        issues = bool(gh.issues_enabled) and bool(gh.issue_repos)
    except Exception:  # noqa: BLE001
        return False
    return (prs or issues) and _automation_here()


def _joiner_runs(body) -> bool:
    return isinstance(body, dict) and body.get("runs_automation") is True


def _automation_device() -> str:
    """``github.automation_device`` — the device key the group chose to run
    PR review and issue handling (synced; "" when nobody chose). Never
    raises."""
    try:
        from backend.config import settings as _settings

        gh = getattr(_settings.load_settings(), "github", None)
        return str(getattr(gh, "automation_device", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _set_automation_device(key: str, nudge: bool = True) -> None:
    """Save ``github.automation_device`` = ``key`` and stamp it as an edit
    made here, so settings sync spreads it (last edit wins; every member
    computes the same choice anyway). ``nudge`` False: stamp only — the
    caller tells the other devices itself (:func:`leave_fleet`). Never
    raises: an unreadable settings.json is logged, never a failed join."""
    from backend.config import settings as _settings

    if not key or key == _automation_device():
        return
    try:
        _settings.update_settings(github={"automation_device": key})
    except Exception as err:  # noqa: BLE001 — SettingsUnreadable too
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("fleet: couldn't set github.automation_device: %v", err)
        return
    try:
        from backend.web.core import settings_sync

        if nudge:
            settings_sync.local_change()
        else:
            settings_sync.scan_local()
    except Exception:  # noqa: BLE001 — the setting is saved either way
        pass


def _settle_automation(joiner: str, joiner_runs: bool, admitter: str) -> None:
    """A join completed (either side calls this, and both decide the same):
    when the group hasn't chosen where PR review and issue handling run
    (``github.automation_device`` unset, or naming a device that isn't a
    live member — the joiner aside: the admitter doesn't list it yet), they
    run on the joiner when it already runs them (``joiner_runs``, see
    :func:`runs_automation` — its processed-PR / issue history is there),
    else on the admitter. A choice the group already made stands."""
    cur = _automation_device()
    if cur and (cur == joiner or cur in live_members()):
        return
    _set_automation_device(joiner if joiner_runs else admitter)


async def after_admit(joiner_runs: Optional[bool] = False, joiner: str = "") -> str:
    """This device just let another one in (a redeemed code, an approved
    request, a one-click add): make the membership real on THIS side too.

    The newcomer's first acts are a settings pull and roster gossip from
    here. Both arrive as relayed requests (``X-MindFlock-Remote``), which the
    auth middleware refuses while remote control is off — so admitting turns
    it on, exactly as joining does on the other side. And a device that isn't
    syncing exports ``enabled: false``: the joiner would copy its settings
    once and never hear from it again, so sync starts here — SEEDED
    (``enable(seed=True)``): this device's values spread only where nobody
    else has one, and any real edit elsewhere in the group (a rotated token,
    a deleted source) still wins over them. Turning sync on stamped "now"
    is reserved for the person choosing "this device leads". Then, unless
    the group already chose, picks where PR review / issue handling run:
    on ``joiner`` when it says it runs them, else here (see
    :func:`_settle_automation`; ``joiner_runs`` None: the caller decides
    that later). Returns the sync error ("" when fine); never raises — the
    admission stands either way."""
    from backend.web.core import remote as _remote

    try:
        if not _remote.remote_control_enabled():
            _enable_remote_control()
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("fleet: couldn't turn remote control on: %v", err)
    sync_error = ""
    try:
        from backend.web.core import settings_sync

        if not settings_sync.enabled():
            await settings_sync.enable("", seed=True)
    except Exception as err:  # noqa: BLE001
        sync_error = str(err) or "settings sync didn't start"
    # After sync is on: the choice is stamped as an edit and spreads.
    if joiner_runs is not None and DEVICE_RE.match(joiner or ""):
        _settle_automation(joiner, bool(joiner_runs), _self_key())
    return sync_error


# --------------------------------------------------------------------------- #
# outgoing join (this device joining another's group)
# --------------------------------------------------------------------------- #
def _idle_join() -> dict:
    return {
        "state": "idle",
        "device": "",
        "host": "",
        "code": "",
        "error": "",
        "id": "",
    }


_JOIN: dict = _idle_join()
_JOIN_TASK: List[Optional[asyncio.Task]] = [None]
#: What withdrawing the outgoing request needs (never in join_status()):
#: ``(device record, request id, secret)``.
_JOIN_SECRET: List[Optional[tuple]] = [None]
_BUSY = ("waiting", "joining")


def join_status() -> dict:
    return dict(_JOIN)


def _set_join(**kw) -> None:
    _JOIN.update(kw)


def _start_join(dev: dict, **kw) -> None:
    if _JOIN["state"] in _BUSY:
        raise ValueError(
            "already joining %s — cancel that first"
            % (_JOIN["host"] or _JOIN["device"])
        )
    _JOIN.clear()
    _JOIN.update(_idle_join(), device=dev["key"], host=_label(dev), **kw)


async def _preflight(dev: dict) -> bool:
    """Before anything goes to ``dev``: refuse when this device is already
    in a group with OTHER live members that ``dev`` isn't one of — the other
    side would hand out its key for nothing (the adopt would refuse), so
    say so here. Returns True when ``dev`` is already a member of this
    device's group that accepts this device's key (nothing to do: already
    joined); a member that refuses it is a rejoin, which goes ahead."""
    from backend.web.core import remote as _remote

    doc = _load()
    me = _self_key()
    others = [k for k in doc["members"] if k != me and _live(doc, k)]
    if not doc["id"] or not others:
        return False
    if dev["key"] not in others:
        raise ValueError(
            "this device is already one of %d devices — leave your current "
            "group first" % (len(others) + 1)
        )
    if str(dev.get("fleet") or "") != doc["id"]:
        return False
    try:
        status, _ = await _remote.get_json(dev, "/api/fleet/roster", bearer=doc["key"])
    except Exception:  # noqa: BLE001
        status = 0
    return status == 200


async def join_with_code(device: str, code: str) -> dict:
    """Join ``device``'s group with a code made there. Returns
    :func:`join_status` (state ``joined`` or ``error``)."""
    from backend.web.core import remote as _remote

    want = normalize_code(code)
    if not want or len(want) > _MAX_CODE:
        raise ValueError("enter the code shown on the other device")
    dev = await _target(device)
    if await _preflight(dev):
        _start_join(dev, state="joined")
        return join_status()
    _start_join(dev, state="joining")
    runs = runs_automation()
    try:
        status, body = await _remote.post_json(
            dev,
            "/api/fleet/redeem",
            {
                "code": want,
                "device": _self_key(),
                "host": _self_host(),
                "dns": _self_dns(),
                "fleet_proto": FLEET_PROTO,
                "runs_automation": runs,
            },
            auth=False,
        )
    except Exception as err:  # noqa: BLE001
        status, body = 0, {"error": str(err) or ""}
    if status == 200 and isinstance(body, dict):
        await _finish_join(dev, body, runs)
    else:
        _set_join(state="error", error=_err_text(status, body, dev))
    return join_status()


async def request_join(device: str) -> dict:
    """Ask ``device`` to let this one in; a person approves there. Polls in
    the background; returns :func:`join_status` (state ``waiting``)."""
    from backend.web.core import remote as _remote

    dev = await _target(device)
    if await _preflight(dev):
        _start_join(dev, state="joined")
        return join_status()
    _start_join(dev, state="joining")
    secret = secrets.token_urlsafe(24)
    runs = runs_automation()
    try:
        status, body = await _remote.post_json(
            dev,
            "/api/fleet/requests",
            {
                "device": _self_key(),
                "host": _self_host(),
                "dns": _self_dns(),
                "secret_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                "fleet_proto": FLEET_PROTO,
                "runs_automation": runs,
            },
            auth=False,
        )
    except Exception as err:  # noqa: BLE001
        status, body = 0, {"error": str(err) or ""}
    if (
        status != 200
        or not isinstance(body, dict)
        or not _ID_RE.match(str(body.get("id") or ""))
    ):
        _set_join(state="error", error=_err_text(status, body, dev))
        return join_status()
    _set_join(state="waiting", id=body["id"], code=str(body.get("code") or ""))
    _JOIN_SECRET[0] = (dev, body["id"], secret)
    _JOIN_TASK[0] = _spawn(_poll_request(dev, body["id"], secret, runs))
    return join_status()


async def _poll_request(dev: dict, rid: str, secret: str, runs: bool = False) -> None:
    from backend.web.core import remote as _remote

    deadline = _now() + REQUEST_TTL
    path = "/api/fleet/requests/%s?secret=%s" % (rid, quote(secret, safe=""))
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if _JOIN.get("id") != rid:
                return  # cancelled / replaced
            if _now() > deadline:
                _set_join(state="expired", error="nobody approved in time")
                return
            try:
                status, body = await _remote.get_json(dev, path, auth=False)
            except Exception:  # noqa: BLE001
                status, body = 0, None
            if status == 200 and isinstance(body, dict):
                st = body.get("state")
                if st == "approved" and isinstance(body.get("bundle"), dict):
                    _set_join(state="joining")
                    _JOIN_SECRET[0] = None
                    # Once the bundle is in hand the join is committed: a
                    # cancel (or shutdown) must not cut it off half-way.
                    await asyncio.shield(_finish_join(dev, body["bundle"], runs))
                    return
                if st == "denied":
                    _set_join(state="denied", error="%s said no" % _label(dev))
                    return
                if st == "expired":
                    _set_join(state="expired", error="nobody approved in time")
                    return
            elif status == 404:
                # Restarted, or the request was replaced/dropped over there.
                _set_join(state="expired", error="the request was lost — ask again")
                return
            elif status in (401, 403):
                _set_join(state="error", error=_err_text(status, body, dev))
                return
            # 0 / 5xx: keep asking until the deadline.
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        _set_join(state="error", error=str(err) or "join failed")


async def _withdraw(dev: dict, rid: str, secret: str) -> None:
    """Best-effort: tell ``dev`` to drop our pending request (the request's
    TTL still covers a device that can't be reached)."""
    from backend.web.core import remote as _remote

    try:
        await _remote.post_json(
            dev,
            "/api/fleet/requests/%s/cancel" % rid,
            {"secret": secret},
            auth=False,
        )
    except Exception:  # noqa: BLE001
        pass


def cancel_join() -> dict:
    """Stop the outgoing join. While ``waiting`` the request is also
    withdrawn on the other device (so it can't be approved after all).
    Refused once ``joining``: the bundle is in hand (or the code was
    sent) and the join completes — the state is returned unchanged."""
    if _JOIN["state"] == "joining":
        return join_status()
    task = _JOIN_TASK[0]
    if task is not None and not task.done():
        task.cancel()
    _JOIN_TASK[0] = None
    pending = _JOIN_SECRET[0]
    _JOIN_SECRET[0] = None
    if pending is not None and _JOIN["state"] == "waiting":
        dev, rid, secret = pending
        if rid == _JOIN.get("id"):
            try:
                _spawn(_withdraw(dev, rid, secret))
            except RuntimeError:  # no running loop (a sync caller)
                pass
    _JOIN.clear()
    _JOIN.update(_idle_join())
    return join_status()


async def _finish_join(dev: dict, b: dict, runs: Optional[bool] = None) -> None:
    """Adopt the bundle ``dev`` handed over, then make the membership real:
    remote control on, ``dev`` re-probed (its hello now names our fleet), the
    other members told (this is what puts us on THEIR rosters), and settings
    sync started with ``dev`` leading where it has values. ``runs``: whether
    this device ran PR review / issue handling when it asked (what it told
    ``dev``); None looks now.

    Devices this one knows were removed that ``dev`` still had live
    (``exposed``, see :func:`adopt_bundle`) may hold the key just adopted:
    once the others have heard the join, the key is replaced (and the
    members' own tokens with it, :func:`rotate_key`) without them."""
    if runs is None:
        runs = runs_automation()
    try:
        exposed = adopt_bundle(b)
    except OlderBundle as err:
        # Same group, but ``dev`` missed a key change: hand it the current
        # key under the one it just gave us, rather than taking its old one.
        if await deliver_rekey(dev, _epoch_of(b) or 0, str(b.get("key") or "")):
            _set_join(state="joined", error="")
        else:
            _set_join(
                state="error",
                error="%s — it needs the current key (try again from there)" % err,
            )
        return
    except ValueError as err:
        _set_join(state="error", error=str(err))
        return
    try:
        _enable_remote_control()
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("fleet: couldn't turn remote control on: %v", err)
    await _refresh(dev["key"])
    await _announce(_visible_members(), roster(), fleet_key())
    if exposed:
        await _rotate_exposed(exposed, dev)
    sync_error = ""
    try:
        from backend.web.core import settings_sync

        await settings_sync.enable(start_from=dev["key"])
    except Exception as err:  # noqa: BLE001 — the join stands without sync
        sync_error = "joined, but settings sync didn't start: %s" % (err or "error")
    # After ``dev``'s settings arrived: a choice the group already made
    # stands (what ``dev`` decides in after_admit too).
    _settle_automation(_self_key(), bool(runs), dev["key"])
    _set_join(state="joined", error=sync_error)
    _emit(
        "device.joined",
        device=_self_key(),
        host=_self_host(),
        via=dev["key"],
        detail="joined %s's devices" % _label(dev),
    )


async def add_paired(device: str) -> dict:
    """Add a device this one holds a pasted access token for — the token
    already proves the owner controls it, so no code is needed. ``device``
    joins THIS device's group, so it takes this device's shared settings
    where this one has them (``direction``: ``theirs_take_mine``); PR review
    and issue handling stay wherever they run now (the adopt's answer says
    whether ``device`` runs them)."""
    from backend.web.core import remote as _remote

    tok = _remote.token_for(device)
    if not tok:
        raise ValueError("pair with %s first (paste its access token)" % device)
    dev = await _target(device)
    create()
    me = _self_key()
    # BEFORE the adopt: the adopted device's first act is a relayed settings
    # pull from here (see after_admit) — it must not race our remote control.
    sync_error = await after_admit(None)
    with _LOCK:
        prev = _load()["members"].get(device)
        add_member(device, _label(dev), by=me, dns=_joiner_dns(device, ""))
    try:
        status, body = await _remote.post_json(
            dev,
            "/api/fleet/adopt",
            {
                "bundle": bundle(),
                "from": {"key": me, "host": _self_host(), "dns": _self_dns()},
            },
            bearer=tok,
        )
    except Exception as err:  # noqa: BLE001
        status, body = 0, {"error": str(err) or ""}
    if status != 200:
        with _LOCK:
            doc = _load()
            if prev is None:
                doc["members"].pop(device, None)
            else:
                doc["members"][device] = prev
            _save(doc)
        raise RuntimeError(_err_text(status, body, dev))
    _settle_automation(device, _joiner_runs(body), me)
    await _refresh(device)
    _emit(
        "device.joined",
        device=device,
        host=_label(dev),
        detail="%s joined your devices" % _label(dev),
    )
    return {
        "ok": True,
        "device": device,
        "host": _label(dev),
        "sync_error": sync_error,
        "direction": "theirs_take_mine",
    }


#: adopt_from_peer's settings pull: tries, and the pause between them.
ADOPT_SYNC_TRIES = 3
ADOPT_SYNC_DELAY = 2.0


async def adopt_from_peer(body: dict, presented_own_token: bool) -> dict:
    """The receiving side of :func:`add_paired`. Only this device's OWN
    access token authorizes it — never the fleet key, never tailnet trust."""
    if not presented_own_token:
        raise PermissionError("needs this device's own access token")
    if not isinstance(body, dict):
        raise ValueError("bad body")
    frm = body.get("from") if isinstance(body.get("from"), dict) else {}
    src = str(frm.get("key") or "")
    if not DEVICE_RE.match(src):
        raise ValueError("bad 'from' device")
    runs = runs_automation()  # before the group (and its settings) arrive
    exposed = adopt_bundle(body.get("bundle"))
    try:
        _enable_remote_control()
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("fleet: couldn't turn remote control on: %v", err)

    async def follow_up():
        await _refresh(src)
        if exposed:
            await _rotate_exposed(exposed, {"key": src, "host": src})
        from backend.web.core import settings_sync

        try:
            for attempt in range(ADOPT_SYNC_TRIES):
                try:
                    await settings_sync.enable(start_from=src)
                    return
                except Exception as err:  # noqa: BLE001
                    if attempt + 1 >= ADOPT_SYNC_TRIES:
                        if log.ErrorLog is not None:
                            log.ErrorLog.Printf(
                                "fleet: settings sync didn't start: %v", err
                            )
                        return
                await asyncio.sleep(ADOPT_SYNC_DELAY)
        finally:
            # After ``src``'s settings arrived (or didn't): what ``src``
            # decides too (add_paired).
            _settle_automation(_self_key(), runs, src)

    _spawn(follow_up())
    host = str(frm.get("host") or src)[:_MAX_HOST]
    _emit(
        "device.joined",
        device=_self_key(),
        host=_self_host(),
        via=src,
        detail="added to your devices by %s" % host,
    )
    return {"ok": True, "runs_automation": runs}


# --------------------------------------------------------------------------- #
# removal / leave
# --------------------------------------------------------------------------- #
async def _rotate_key(
    exclude: Tuple[str, ...] = (),
) -> Tuple[List[dict], List[str], List[str]]:
    """Replace the fleet key and hand the new one to every member that can be
    reached now. Returns ``(targets, rekeyed, missed)``: the members tried,
    the keys of those that took it, and the live members that didn't (they
    get it from gossip when next seen — ``prev_keys``)."""
    old_epoch = _load()["epoch"]
    rekey()
    targets = _visible_members(exclude=exclude)
    results = (
        await asyncio.gather(*(deliver_rekey(d, old_epoch) for d in targets))
        if targets
        else []
    )
    rekeyed = sorted(d["key"] for d, ok in zip(targets, results) if ok)
    new_epoch = _load()["epoch"]
    for k in rekeyed:
        _on_epoch(k, new_epoch)
    me = _self_key()
    missed = sorted(
        k for k in live_members() if k != me and k not in rekeyed and k not in exclude
    )
    return targets, rekeyed, missed


async def _rotate_member_tokens(
    targets: List[dict], rekeyed: List[str], missed: List[str]
) -> Tuple[List[str], List[str]]:
    """Ask every member that took the new key (:func:`_rotate_key`) to
    replace its OWN access token too (``/api/fleet/rotate-token``, under the
    new key). Returns ``(rotated, rotate_failed)`` — the failed ones include
    ``missed``: they never got the new key to be asked with, so their
    tokens are rotated there by hand. This device's own token is the
    caller's business."""
    from backend.web.core import remote as _remote

    new = fleet_key()

    async def one(dev) -> bool:
        try:
            status, resp = await _remote.post_json(
                dev, "/api/fleet/rotate-token", {}, bearer=new
            )
        except Exception:  # noqa: BLE001
            return False
        return status == 200 and isinstance(resp, dict) and bool(resp.get("ok"))

    done = [d for d in targets if d["key"] in rekeyed]
    oks = await asyncio.gather(*(one(d) for d in done)) if done else []
    rotated = [d["key"] for d, ok in zip(done, oks) if ok]
    failed = [d["key"] for d, ok in zip(done, oks) if not ok]
    return sorted(rotated), sorted(set(failed) | set(missed))


async def rotate_key(exclude: Tuple[str, ...] = ()) -> dict:
    """Replace the devices' shared key (Security → Rotate token, in a group)
    AND every member's own access token: a lost phone holds both (the
    shared-link QR carries the paired devices' own tokens), so it is signed
    out of all of them. Members reached now take the key at once and replace
    their token; ``missed`` ones get the key when they are next seen, but
    their own token is only replaced by hand there (``rotate_failed``, which
    includes them). This device's own token is the caller's to rotate.
    ``exclude``: members never sent the new key (see :func:`_finish_join`).
    Returns ``{"rekeyed", "missed", "rotated", "rotate_failed"}`` (all
    empty outside a group)."""
    if not in_fleet():
        return {"rekeyed": [], "missed": [], "rotated": [], "rotate_failed": []}
    targets, rekeyed, missed = await _rotate_key(exclude=exclude)
    rotated, failed = await _rotate_member_tokens(targets, rekeyed, missed)
    return {
        "rekeyed": rekeyed,
        "missed": missed,
        "rotated": rotated,
        "rotate_failed": failed,
    }


async def _rotate_exposed(exposed: List[str], dev: dict) -> None:
    """The join just completed through ``dev``, which still had ``exposed``
    (devices removed here) live: it may have handed them the key this
    device took. Replace it without them (:func:`rotate_key`). Never
    raises — the join stands."""
    try:
        out = await rotate_key(exclude=tuple(exposed))
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "fleet: couldn't replace the key %v may hold: %v", exposed, err
            )
        return
    if log.InfoLog is not None:
        log.InfoLog.Printf(
            "fleet: %v were removed here but still on %v's roster — key "
            "replaced (rekeyed %v, missed %v)",
            exposed,
            _label(dev),
            out["rekeyed"],
            out["missed"],
        )


async def remove(device: str, rotate_tokens: bool = True) -> dict:
    """Remove a member and cut it off:

    * the fleet key rotates — every member that can be reached gets the new
      one under the old one now; one that is away gets it automatically from
      gossip the next time it is seen (``prev_keys``, :func:`gossip_once`);
    * with ``rotate_tokens`` (the default) every member that took the new key
      also replaces its OWN access token, and so does this device — tokens
      pasted on the removed device (or read through the fleet key while it
      was a member) stop working. A phone signed in with an old token needs
      to scan the QR again.

    What it can't take back: the removed device's Tailscale access, anything
    it already copied, and — until they hear — its old key on members that
    are offline now (``advice``: remove it from the tailnet too). Removing
    THIS device is leaving (``{"ok", "left": true}``). Returns ``{"rekeyed",
    "missed", "rotated", "rotate_failed", "advice"}``.

    When ``device`` is the one the group chose to run PR review and issue
    handling (``github.automation_device``), they move HERE — a lost device
    can't hand them over itself."""
    if device == _self_key():
        return {**(await leave_fleet()), "left": True}
    if not in_fleet() or not is_member(device):
        raise KeyError(device)
    host = (live_members().get(device) or {}).get("host") or device
    remove_member(device)
    targets, rekeyed, missed = await _rotate_key(exclude=(device,))
    with _LOCK:
        for rid, req in list(_REQUESTS.items()):
            if req["device"] == device:
                del _REQUESTS[rid]
    me = _self_key()
    if _automation_device() == device:
        _set_automation_device(me)
    rotated: List[str] = []
    rotate_failed: List[str] = []
    if rotate_tokens:
        rotated, rotate_failed = await _rotate_member_tokens(targets, rekeyed, missed)
        try:
            from backend.web.core import auth as _auth

            _auth.rotate_token()
            rotated.append(me)
        except Exception as err:  # noqa: BLE001 — env-pinned / store failure
            rotate_failed.append(me)
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("fleet: couldn't replace this token: %v", err)
    _emit(
        "device.removed",
        device=device,
        host=host,
        by=me,
        detail="%s was removed from your devices. %s." % (host, TAILNET_ADVICE),
    )
    return {
        "ok": True,
        "rekeyed": rekeyed,
        "missed": missed,
        "rotated": sorted(rotated),
        "rotate_failed": sorted(rotate_failed),
        "advice": TAILNET_ADVICE,
    }


async def leave_fleet() -> dict:
    """Leave the group: tell the members (a tombstone for us), then forget
    it here and stop syncing settings (alone, PR review and issue handling
    run here). When this device is the one the group chose to run them
    (``github.automation_device``), they go to the live member with the
    lowest device key first — what every other member's fallback picks too,
    should the nudge not reach it before we're gone."""
    if not in_fleet():
        return {"ok": True}
    me = _self_key()
    with _LOCK:
        doc = _load()
        others = sorted(k for k in doc["members"] if k != me and _live(doc, k))
    if others and _automation_device() == me:
        _set_automation_device(others[0], nudge=False)
        try:
            from backend.web.core import settings_sync

            await settings_sync.nudge_peers()  # while they still take our key
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        doc = _load()
        _tombstone(doc, me, me)
        _save(doc)
    await _announce(_visible_members(), roster(), fleet_key())
    leave()
    try:
        from backend.web.core import settings_sync

        settings_sync.disable()
    except Exception:  # noqa: BLE001
        pass
    _emit(
        "device.removed",
        device=me,
        host=_self_host(),
        by=me,
        detail="%s left your devices" % _self_host(),
    )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# gossip
# --------------------------------------------------------------------------- #
#: member key -> it rejects our key for a reason that is OURS to fix (its
#: key is newer, or — an older peer that doesn't say — every one rejects it).
_STALE: Dict[str, bool] = {}
#: member key -> {"at", "status", "error", "epoch", "ahead"} from the last pass.
_PEERS: Dict[str, dict] = {}


async def _deliver_any(dev: dict) -> bool:
    """``dev`` refused our key without saying which epoch it is on (its own
    gate answered before the fleet route could — an older build, or a
    middleware in the way): try handing it the current key under each old
    one this device kept, newest first."""
    doc = _load()
    for ep in sorted((int(e) for e in (doc.get("prev_keys") or {})), reverse=True):
        if await deliver_rekey(dev, ep):
            return True
    return False


async def gossip_once() -> bool:
    """Push-pull with every reachable same-fleet member: POST our roster,
    merge the roster it answers with. Returns whether ours changed.

    A member that refuses our key says which epoch it is on: a NEWER one
    means this device missed a key change (``stale_key``); an OLDER one means
    IT did — it is handed the current key under the old one it holds
    (:func:`deliver_rekey`), never mistaken for "this device was removed".
    The SAME epoch with a different key fingerprint is a conflict (two
    halves of the group each changed the key apart): the side with the lower
    fingerprint is the one asked to rejoin, and neither sends the key to the
    other meanwhile."""
    from backend.web.core import remote as _remote

    if not in_fleet():
        _STALE.clear()
        return False
    me = _self_key()
    fn = getattr(_remote, "fleet_devices", None)
    devs = [d for d in (fn() if fn else []) if d.get("key") != me]
    polled = set()
    changed = False
    mine, key = roster(), fleet_key()

    async def one(dev):
        try:
            return await _remote.post_json(dev, "/api/fleet/roster", mine, bearer=key)
        except Exception:  # noqa: BLE001
            return 0, None

    results = await asyncio.gather(*(one(d) for d in devs)) if devs else []
    for dev, (status, body) in zip(devs, results):
        if not in_fleet():
            break  # a roster removed us
        k = dev["key"]
        polled.add(k)
        doc = _load()
        my_epoch, my_fp = doc["epoch"], key_fp(doc["key"])
        err, their, ahead, conflict, loser = "", None, False, False, False
        if status == 200 and isinstance(body, dict):
            _STALE[k] = False
            their = _epoch_of(body)
            if merge_roster(body):
                changed = True
        elif status == 401:
            info = body if isinstance(body, dict) else {}
            their = _epoch_of(info)
            same_id = bool(info.get("id")) and info.get("id") == doc["id"]
            their_fp = str(info.get("kfp") or "")
            if their is None:
                if await _deliver_any(dev):
                    _STALE.pop(k, None)
                    their = my_epoch
                else:
                    _STALE[k] = True
                    err = "doesn't accept this device's key"
            elif same_id and their > my_epoch:
                _STALE[k] = True
                ahead = True
                err = "has a newer key — this device missed a change"
            elif same_id and their < my_epoch:
                _STALE.pop(k, None)
                if await deliver_rekey(dev, their):
                    their = my_epoch
                else:
                    err = "missed a key change — ask it to rejoin"
            elif same_id and their_fp and their_fp != my_fp:
                conflict = True
                loser = my_fp < their_fp
                if loser:
                    _STALE[k] = True
                else:
                    _STALE.pop(k, None)
                err = "has a different key for your devices — rejoin one from the other"
            elif info.get("id"):
                _STALE.pop(k, None)
                err = "is in another group now"
            else:
                _STALE[k] = True
                err = "doesn't accept this device's key"
        else:
            _STALE.pop(k, None)
            err = _err_text(status, body, dev)
        _PEERS[k] = {
            "at": _now(),
            "status": status,
            "error": err,
            "epoch": their,
            "ahead": ahead,
            "conflict": conflict,
            "loser": loser,
        }
    for k in list(_STALE):
        if k not in polled:
            del _STALE[k]
    _maybe_forget_prev_keys()
    return changed


def _maybe_forget_prev_keys() -> None:
    """Once every live member has been seen on the current epoch (with the
    same key — not one in conflict), nobody needs an old key any more."""
    doc = _load()
    if not doc.get("prev_keys"):
        return
    me = _self_key()
    others = [k for k in doc["members"] if k != me and _live(doc, k)]
    if all(
        (_PEERS.get(k) or {}).get("epoch") == doc["epoch"]
        and not (_PEERS.get(k) or {}).get("conflict")
        for k in others
    ):
        _forget_prev_keys()


def peer_on_other_epoch(key: str) -> bool:
    """The last gossip pass found member ``key`` on another key epoch (one of
    us missed a change), or on ours with a different key (a conflict): the
    fleet key can't open it until that heals."""
    peer = _PEERS.get(key) or {}
    if peer.get("conflict"):
        return True
    return peer.get("epoch") is not None and peer["epoch"] != _load()["epoch"]


def peer_conflict(key: str) -> bool:
    """Member ``key`` holds a different key on our epoch (see gossip_once)."""
    return bool((_PEERS.get(key) or {}).get("conflict"))


def stale_key() -> bool:
    """This device's key is out of date: a member answered with a NEWER
    epoch, or (members too old to say) every reachable one rejects it — it
    was removed, or the key rotated while it was away. Never while a member
    accepted our key in the last pass — except that in a key conflict (same
    epoch, different keys) the side with the lower fingerprint is the one
    asked to rejoin."""
    if any((_PEERS.get(k) or {}).get("loser") for k, v in _STALE.items() if v):
        return True
    if any(v is False for v in _STALE.values()):
        return False
    if any((_PEERS.get(k) or {}).get("ahead") for k, v in _STALE.items() if v):
        return True
    return bool(_STALE) and all(_STALE.values())


async def fleet_loop() -> None:
    while True:
        try:
            await gossip_once()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must never die
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("fleet gossip failed: %v", err)
        await asyncio.sleep(INTERVAL)


# --------------------------------------------------------------------------- #
# the GET /api/fleet payload
# --------------------------------------------------------------------------- #
#: Whether ``tailscale serve`` fronts this server's port (it shells out, so
#: it is checked off the event loop and remembered for a minute).
SERVE_CHECK_TTL = 60.0
_SERVE: Dict[str, float] = {"at": 0.0, "exposed": 0.0}


def _check_serve() -> bool:
    try:
        from backend.web.core import mobile_access as _ma
        from backend.web.core import shared_link as _sl

        if _sl.advertised_url():
            return True
        return bool(_ma._tailscale_serves_port(_ma._server_port()))
    except Exception:  # noqa: BLE001
        return False


async def refresh_exposure() -> None:
    """Re-check (in a thread) whether ``tailscale serve`` exposes this server,
    when the remembered answer is older than :data:`SERVE_CHECK_TTL`."""
    if time.monotonic() - _SERVE["at"] < SERVE_CHECK_TTL and _SERVE["at"]:
        return
    exposed = await asyncio.to_thread(_check_serve)
    _SERVE.update(at=time.monotonic(), exposed=1.0 if exposed else 0.0)


def _gate_warning() -> bool:
    """The gate is off and the server is reachable beyond this machine — a
    non-local bind, or local mode fronted on the tailnet by ``tailscale
    serve`` / the shared link. Any tailnet node can drive it then, and
    through it the owner's other devices."""
    try:
        from backend.web.core import auth as _auth

        if _auth.auth_enabled():
            return False
    except Exception:  # noqa: BLE001
        return False
    mode = (os.environ.get("CS_WEB_MODE") or "").strip().lower()
    if mode not in ("local", "localhost"):
        return True
    return bool(_SERVE["exposed"])


def _automation_here() -> bool:
    try:
        from backend.web.core import settings_hooks as _hooks

        fn = getattr(_hooks, "automation_here", None)
        return bool(fn()) if fn is not None else True
    except Exception:  # noqa: BLE001
        return False


def _automation_runner() -> str:
    """The member that runs PR review + issue handling for the group
    (``settings_hooks.automation_runner``), "" outside a group of two or
    more. Never raises."""
    try:
        from backend.web.core import settings_hooks as _hooks

        fn = getattr(_hooks, "automation_runner", None)
        return str(fn() or "") if fn is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def status(privileged: bool) -> dict:
    """Settings → Devices. Without ``privileged`` (a relayed or untrusted
    caller) nothing that lets someone in is included: no invite codes, no
    pending requests, no join code."""
    from backend import __version__
    from backend.web.core import remote as _remote

    doc = _load()
    me = _self_key()
    my_id = doc["id"]
    known = {d["key"]: d for d in _known_devices()}
    runner = _automation_runner()
    members = []
    for key, m in sorted(doc["members"].items()):
        if not _live(doc, key):
            continue
        dev = known.get(key) or {}
        mine = key == me
        members.append(
            {
                "key": key,
                "host": (_self_host() if mine else dev.get("host")) or m["host"] or key,
                "added_at": m["added_at"],
                "self": mine,
                "reachable": True if mine else bool(dev.get("reachable")),
                "version": __version__ if mine else str(dev.get("version") or ""),
                # Update all my devices: the build it runs, whether it can be
                # updated from here (``editable``/``other`` can't), and its
                # desktop app's version ("" when none reported).
                **_update_facts(mine, dev),
                "same_fleet": (
                    True if mine else bool(my_id and dev.get("fleet") == my_id)
                ),
                "error": "" if mine else (_PEERS.get(key) or {}).get("error", ""),
                # Same group and epoch, a different key: one of the two has
                # to rejoin the other (see gossip_once).
                "key_conflict": False if mine else peer_conflict(key),
                # Runs PR review + issue handling (one device of the group,
                # github.automation_device — derived the same on every one).
                "automation": (
                    key == runner
                    if runner
                    else (
                        _automation_here()
                        if mine
                        else (
                            dev["automation"]
                            if isinstance(dev.get("automation"), bool)
                            else None
                        )
                    )
                ),
            }
        )
    candidates = []
    for key, dev in sorted(known.items()):
        if key == me or (not dev.get("reachable") and not dev.get("last_seen")):
            continue
        their = str(dev.get("fleet") or "")
        same = bool(my_id and their == my_id)
        member = _live(doc, key)
        if member and same:
            continue
        candidates.append(
            {
                "device": key,
                "host": dev.get("host") or key,
                "version": str(dev.get("version") or ""),
                "fleet_proto": int(dev.get("fleet_proto") or 0),
                "reachable": bool(dev.get("reachable")),
                "member": member,
                "in_fleet": bool(their),
                "same_fleet": same,
                "has_token": bool(_remote.token_for(key)),
            }
        )
    removed = []
    for key, t in sorted(doc["removed"].items(), key=lambda kv: -kv[1]["at"]):
        if _live(doc, key):
            continue
        by = t.get("by") or ""
        m = doc["members"].get(key) or {}
        removed.append(
            {
                "key": key,
                "host": (known.get(key) or {}).get("host") or m.get("host") or key,
                "removed_at": t["at"],
                "removed_by": by,
                "removed_by_host": _host_of(doc, known, by),
                # It left on its own (not a removal by another device).
                "left": by == key,
            }
        )
    dead = _dead_here(doc)
    readmitted = [
        {
            "key": key,
            "host": (known.get(key) or {}).get("host") or seen.get("host") or key,
            "by": seen.get("by") or "",
            "by_host": _host_of(doc, known, seen.get("by") or ""),
        }
        for key, seen in sorted(_READMITTED.items())
        if key in dead
    ]
    join = join_status()
    if not privileged:
        join["code"] = ""
    return {
        "in_fleet": bool(doc["id"] and doc["key"]),
        "id": my_id,
        "epoch": doc["epoch"],
        "self": {"key": me, "host": _self_host()},
        "members": members,
        "invites": invites() if privileged else [],
        "requests": pending_requests() if privileged else [],
        "join": join,
        "stale_key": stale_key(),
        "gate_warning": _gate_warning(),
        "candidates": candidates,
        # Tombstones, newest first, with who removed each: "rig removed
        # laptop at 10:32" is how a forged removal shows.
        "removed": removed,
        # Removed here, but another member let it back in: the person
        # decides here (POST /api/fleet/members/<key>/allow).
        "readmitted_elsewhere": readmitted,
    }


def _update_facts(mine: bool, dev: dict) -> dict:
    """``{commit, install, shell_version}`` for a member row: this device's
    own, or what the member's hello reported. Never raises."""
    if not mine:
        return {
            "commit": str(dev.get("commit") or ""),
            "install": str(dev.get("install") or ""),
            "shell_version": str(dev.get("shell_version") or ""),
        }
    try:
        from backend.web.core import remote as _remote
        from backend.web.core import self_update as _su

        return {
            "commit": _su.installed_commit(),
            "install": _su.install_kind(),
            "shell_version": _remote._SHELL["version"],
        }
    except Exception:  # noqa: BLE001
        return {"commit": "", "install": "", "shell_version": ""}


def _host_of(doc: dict, known: Dict[str, dict], key: str) -> str:
    """A display name for device ``key`` (status rows)."""
    if not key:
        return ""
    return (
        (_self_host() if key == _self_key() else "")
        or (known.get(key) or {}).get("host")
        or (doc["members"].get(key) or {}).get("host")
        or key
    )
