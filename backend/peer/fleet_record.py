"""The encrypted address record one of your devices leaves in GitHub.

"Reach my devices from anywhere" (``docs/devices-anywhere.md``): each device
writes where its tunnel is RIGHT NOW into a private repo of the user's own
GitHub account, and the other devices read it from there. This module is the
record alone — keys, slot names, sealing and checking — with no I/O, so its
rules are testable on their own.

* Keys are HKDF-SHA256 over the fleet key, salted and bound to the fleet id
  and key epoch, one per purpose (``record``, ``slot``, ``auth``). A key
  change (a removed device) makes every older record unreadable.
* A record is AES-256-GCM (``cryptography`` — already a dependency for peer
  identities) over a small JSON body, behind a version byte, with the file's
  slot name in the associated data: a valid record copied into another
  device's file fails to open.
* :func:`open_record` rejects, with a reason word and never the content:
  an unknown version, a bad tag (tampered / another fleet / another epoch),
  a body naming another fleet or epoch, a slot that isn't the named device's,
  a timestamp from the future or older than :data:`MAX_AGE`, a counter below
  the highest one already accepted for that device (a replay of old repo
  history), and an address that isn't a quick-tunnel relay URL.

Nothing in a record is readable without the fleet key: no hostname, device
name or address in clear, and the slot name is an HMAC under a fleet-derived
key (not a hash of anything public).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from backend.peer.addr import parse_addr

#: Format version (the first byte of the sealed record).
VERSION = 1
#: Text prefix of a record file's content.
PREFIX = "mfd1."
#: HKDF salt — fixed per protocol version.
_SALT = b"mindflock-devices-anywhere-v1"
_AAD = b"mindflock-devices-anywhere-record|"
_NONCE = 12
#: A record whose ``ts`` is older than this is stale (a publishing device
#: refreshes its record every :data:`REFRESH_S`).
MAX_AGE = 3 * 86400.0
REFRESH_S = 12 * 3600.0
#: Clock skew tolerated on ``ts``.
MAX_SKEW = 300.0
#: A record file is never bigger than this (base64 of a ~300-byte body).
MAX_FILE = 4096

SLOT_RE = re.compile(r"^[0-9a-f]{32}\Z")
_FP_RE = re.compile(r"^[0-9a-f]{32}\Z")
_DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_TUNNEL_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com\Z"
)


class RecordError(ValueError):
    """A record that must be ignored. ``reason`` is one word (never content):
    ``format``, ``version``, ``tag``, ``body``, ``fleet``, ``epoch``, ``slot``,
    ``member``, ``future``, ``stale``, ``replay``, ``address``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Keys:
    """The keys derived from one fleet key at one epoch."""

    fleet_id: str
    epoch: int
    record: bytes
    slot: bytes
    auth: bytes


@dataclass(frozen=True)
class Record:
    device: str
    url: str
    fp: str  # hex sha256(identity pub)[:16]
    ctr: int
    ts: float


def _hkdf(fleet_key: str, purpose: str, fleet_id: str, epoch: int) -> bytes:
    info = ("%s|%s|%d" % (purpose, fleet_id, int(epoch))).encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=info).derive(
        str(fleet_key).encode("utf-8")
    )


def derive(fleet_key: str, fleet_id: str, epoch: int) -> Keys:
    """Every key for ``(fleet_key, fleet_id, epoch)``. Raises ``ValueError``
    outside a fleet (an empty key must never derive anything usable)."""
    if not fleet_key or not fleet_id:
        raise ValueError("not in a group of devices")
    epoch = int(epoch)
    return Keys(
        fleet_id=str(fleet_id),
        epoch=epoch,
        record=_hkdf(fleet_key, "record", fleet_id, epoch),
        slot=_hkdf(fleet_key, "slot", fleet_id, epoch),
        auth=_hkdf(fleet_key, "auth", fleet_id, epoch),
    )


def slot_for(keys: Keys, device: str) -> str:
    """The file name stem a device's record lives under (32 hex)."""
    return hmac.new(keys.slot, str(device).encode("utf-8"), hashlib.sha256).hexdigest()[
        :32
    ]


def file_name(keys: Keys, device: str) -> str:
    return slot_for(keys, device) + ".rec"


def slot_of(file_name_: str) -> str:
    """The slot of a repo file name, ``""`` when it isn't a record file."""
    name = str(file_name_ or "")
    if not name.endswith(".rec"):
        return ""
    stem = name[: -len(".rec")]
    return stem if SLOT_RE.match(stem) else ""


def _aad(slot: str) -> bytes:
    return _AAD + bytes([VERSION]) + b"|" + slot.encode("ascii")


def check_url(url: str, *, loopback: bool = False) -> str:
    """``url`` if it is a relay address a record may carry: ``wss://<one
    label>.trycloudflare.com[:443]/<path>`` (``loopback``: also
    ``wss://127.0.0.1:<port>/<path>`` — the test carrier). Raises
    :class:`RecordError` (``address``)."""
    try:
        addr = parse_addr(url)
    except ValueError:
        raise RecordError("address") from None
    if not addr.is_relay:
        raise RecordError("address")
    if loopback and addr.host == "127.0.0.1":
        return url
    if addr.port != 443 or not _TUNNEL_HOST_RE.match(addr.host.lower()):
        raise RecordError("address")
    return url


def seal(keys: Keys, rec: Record) -> str:
    """The text a record file holds for ``rec``."""
    slot = slot_for(keys, rec.device)
    body = json.dumps(
        {
            "v": VERSION,
            "fleet": keys.fleet_id,
            "epoch": keys.epoch,
            "device": rec.device,
            "url": rec.url,
            "fp": rec.fp,
            "ctr": int(rec.ctr),
            "ts": float(rec.ts),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    nonce = os.urandom(_NONCE)
    ct = AESGCM(keys.record).encrypt(nonce, body, _aad(slot))
    blob = bytes([VERSION]) + nonce + ct
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def open_record(
    keys: Keys,
    file_name_: str,
    text: str,
    *,
    members=(),
    self_key: str = "",
    last_ctr: int = 0,
    now: float | None = None,
    loopback: bool = False,
) -> Record:
    """Open and check one record file (see the module docstring). ``members``:
    the live roster keys a record may name; ``last_ctr``: the highest counter
    already accepted for the device this file's slot belongs to. Raises
    :class:`RecordError`."""
    slot = slot_of(file_name_)
    if not slot:
        raise RecordError("format")
    if not isinstance(text, str) or len(text) > MAX_FILE:
        raise RecordError("format")
    text = text.strip()
    if not text.startswith(PREFIX):
        raise RecordError("format")
    raw = text[len(PREFIX) :]
    try:
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        raise RecordError("format") from None
    if len(blob) < 1 + _NONCE + 16:
        raise RecordError("format")
    if blob[0] != VERSION:
        raise RecordError("version")
    nonce, ct = blob[1 : 1 + _NONCE], blob[1 + _NONCE :]
    try:
        body = AESGCM(keys.record).decrypt(nonce, ct, _aad(slot))
    except InvalidTag:
        raise RecordError("tag") from None
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RecordError("body") from None
    if not isinstance(doc, dict) or doc.get("v") != VERSION:
        raise RecordError("body")
    if doc.get("fleet") != keys.fleet_id:
        raise RecordError("fleet")
    if doc.get("epoch") != keys.epoch:
        raise RecordError("epoch")
    device = doc.get("device")
    if not isinstance(device, str) or not _DEVICE_RE.match(device):
        raise RecordError("body")
    if not hmac.compare_digest(slot_for(keys, device), slot):
        raise RecordError("slot")
    if device == self_key or device not in set(members):
        raise RecordError("member")
    fp = doc.get("fp")
    ctr = doc.get("ctr")
    ts = doc.get("ts")
    if (
        not isinstance(fp, str)
        or not _FP_RE.match(fp)
        or not isinstance(ctr, int)
        or isinstance(ctr, bool)
        or ctr < 0
        or not isinstance(ts, (int, float))
        or isinstance(ts, bool)
        or ts != ts  # NaN
    ):
        raise RecordError("body")
    now = time.time() if now is None else now
    if ts > now + MAX_SKEW:
        raise RecordError("future")
    if ts < now - MAX_AGE:
        raise RecordError("stale")
    if ctr < int(last_ctr or 0):
        raise RecordError("replay")
    url = doc.get("url")
    if not isinstance(url, str):
        raise RecordError("address")
    check_url(url, loopback=loopback)
    return Record(device=device, url=url, fp=fp, ctr=ctr, ts=float(ts))


def next_ctr(last: int, now: float | None = None) -> int:
    """A counter above ``last`` that also tracks the clock, so it stays
    monotonic across a reinstall that lost ``last``."""
    now = time.time() if now is None else now
    return max(int(last or 0) + 1, int(now * 1000))
