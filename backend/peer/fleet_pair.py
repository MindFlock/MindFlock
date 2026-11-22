"""Pairing a NEW device through the device tunnel — no Tailscale on either side.

"Reach my devices from anywhere" (``docs/devices-anywhere.md`` §7) connects
devices that already share the group key. This module lets a computer that
has nothing yet — no key, no GitHub sign-in, no Tailscale — join the group
through the inviter's tunnel, with one long code:

``mfd1:<base32 payload>-<checksum>`` (unpadded lowercase RFC 4648 base32;
the checksum is ``base32(sha256(payload)[:3])[:4]``, as peer invites), where::

    payload = ver(1)=1 | kind(1) | where | path(16) | fp(16) | invite_id(8) | secret(20)
    kind 1 (a Cloudflare quick tunnel): where = len(1) | label   → wss://<label>.trycloudflare.com/<path>
    kind 2 (the loopback TEST carrier):  where = port(2, BE)     → wss://127.0.0.1:<port>/<path>

``path`` is the ingress's 128-bit path token, ``fp`` the inviter's identity
fingerprint (pinned before the joiner sends a byte), ``secret`` 160 bits that
never travel: the joiner proves it saw the code. Kind 2 parses only when the
caller says the loopback carrier is in use (tests).

The exchange rides the device tunnel's pinned TLS 1.3 (``fleet_tunnel``) and
replaces its ``dev`` frame — it never reaches the web server::

    S→C {"t":"hello","v":1,"nonce":sn}
    C→S {"t":"pair","v":1,"nonce":cn,"invite":<id hex>,"device","host","dns","runs",
         "proof":HMAC(secret, "mfpair-client-v1" | T)}
    S→C {"t":"ok","v":1,"from":<inviter key>,"bundle":{…},
         "proof":HMAC(secret, "mfpair-server-v1" | T | inviter)}      or {"t":"denied"}
    C→S {"t":"confirm","proof":HMAC(K_auth(bundle), "mfpair-confirm-v1" | T | inviter)}
         or {"t":"abort"} (the joiner refused the bundle)
    S→C {"t":"added"}                                                 then close

    T = server_pub | sn | cn | invite_id | device | host | dns | runs   (length-prefixed)

* The server checks the client's proof (constant time, against a dummy key
  for an unknown id) before it sends anything but the hello; a wrong, unknown,
  expired, used or rate-limited invite all get the same ``denied``. A right
  proof CONSUMES the invite (single use); the 5th wrong one destroys it; a
  global rate limit caps attempts across all invites (:class:`PairBook`).
* Both proofs bind the TLS key the client actually saw (``server_pub``) and
  fresh nonces from both ends: a relaying middlebox fails the pin, a replayed
  frame fails the nonce.
* The bundle (the group key and roster, exactly what a redeemed 8-character
  code hands out) is sent only after the client's proof, inside the pinned
  TLS. The inviter puts the joiner on ITS roster only after a ``confirm``
  MAC'd with the group key the bundle carried — the joiner adopted it — so a
  join that fails or is refused leaves no ghost member.

Nothing here logs, reprs or echoes a secret, a code or a bundle.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from backend.peer import wire

__all__ = [
    "CODE_PREFIX",
    "PairBook",
    "PairCode",
    "PairError",
    "PairRefused",
    "code_from_text",
    "encode_code",
    "pair",
    "parse_code",
    "serve_pair",
]

CODE_PREFIX = "mfd1:"
_VERSION = 1
KIND_TUNNEL = 1
KIND_LOOPBACK = 2
PATH_LEN = 16
FP_LEN = 16
INVITE_ID_LEN = 8
SECRET_LEN = 20
TTL_S = 600.0
MAX_FAILURES = 5
#: Proof attempts across every invite, per window (as peer invites).
RATE_LIMIT = 10
RATE_WINDOW_S = 60.0
#: Live codes at once: making a new one cancels the older (one secret out).
MAX_ACTIVE = 1
#: The whole exchange after the hello: bundle, the joiner's adopt, confirm,
#: the inviter's admit.
PAIR_TIMEOUT = 60.0
#: How long the inviter waits for the confirm after sending the bundle.
CONFIRM_TIMEOUT = 30.0
#: The ``ok`` frame (it carries the roster) is never bigger than this.
BUNDLE_MAX_FRAME = 256 * 1024
_NONCE_LEN = 32

CLIENT_LABEL = b"mfpair-client-v1"
SERVER_LABEL = b"mfpair-server-v1"
CONFIRM_LABEL = b"mfpair-confirm-v1"
_CONFIRM_SALT = b"mindflock-devices-pair-confirm-v1"

_B32_RE = re.compile(r"^[a-z2-7]+\Z")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_BAD_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CODE_IN_TEXT_RE = re.compile(r"mfd1:[a-z2-7]+-[a-z2-7]{4}", re.I)
_MAX_TEXT = 255


class PairError(ConnectionError):
    """The pairing didn't happen. ``reason`` is one word, never content:
    ``pin`` (the far end's TLS key isn't the one the code named — a man in
    the middle, or an old code), ``denied`` (wrong, expired or used code —
    the server doesn't say which), ``same_name`` (this computer has the
    inviter's name), ``proof`` (the far end didn't prove the code),
    ``protocol``, ``refused`` (this side refused the bundle; ``text`` says
    why) or ``unconfirmed`` (the inviter never acknowledged the confirm)."""

    def __init__(self, reason: str, text: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.text = text


class PairRefused(ValueError):
    """Raised by the joiner's ``adopt`` callback: the bundle can't be taken
    (the message is shown to the person)."""


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #
def _b32enc(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def _b32dec(text: str) -> bytes:
    if not _B32_RE.match(text) or len(text) % 8 in (1, 3, 6):
        raise ValueError
    data = base64.b32decode(text.upper() + "=" * (-len(text) % 8))
    if _b32enc(data) != text:  # non-canonical trailing bits
        raise ValueError
    return data


def _checksum(payload: bytes) -> str:
    return _b32enc(hashlib.sha256(payload).digest()[:3])[:4]


@dataclass(frozen=True)
class PairCode:
    """A parsed ``mfd1:`` code. ``url`` is where the inviter's tunnel is."""

    url: str
    fp: bytes = field(repr=False)
    invite_id: str  # 16 lowercase hex
    secret: bytes = field(repr=False)


def _split_url(url: str, loopback: bool) -> tuple:
    """``wss://<label>.trycloudflare.com/<token>`` → ``(KIND_TUNNEL, label,
    path bytes)``; the loopback test address → ``(KIND_LOOPBACK, port,
    path)``. Raises ValueError."""
    from backend.peer.addr import parse_addr

    addr = parse_addr(url)
    if not addr.is_relay:
        raise ValueError("not a tunnel address")
    path = _b32dec(addr.path.lstrip("/"))
    if len(path) != PATH_LEN:
        raise ValueError("bad tunnel path")
    host = addr.host.lower()
    if loopback and host == "127.0.0.1":
        return KIND_LOOPBACK, addr.port, path
    suffix = ".trycloudflare.com"
    label = host[: -len(suffix)] if host.endswith(suffix) else ""
    if addr.port != 443 or not _LABEL_RE.match(label):
        raise ValueError("not a quick-tunnel address")
    return KIND_TUNNEL, label, path


def encode_code(
    url: str,
    fp: bytes,
    invite_id: bytes,
    secret: bytes,
    *,
    loopback: bool = False,
) -> str:
    """The ``mfd1:`` code for an invite on the tunnel at ``url``."""
    if (
        len(fp) != FP_LEN
        or len(invite_id) != INVITE_ID_LEN
        or len(secret) != SECRET_LEN
    ):
        raise ValueError("bad invite material")
    kind, where, path = _split_url(url, loopback)
    if kind == KIND_TUNNEL:
        w = where.encode("ascii")
        loc = bytes([len(w)]) + w
    else:
        loc = struct.pack(">H", int(where))
    payload = bytes([_VERSION, kind]) + loc + path + fp + invite_id + secret
    return CODE_PREFIX + _b32enc(payload) + "-" + _checksum(payload)


def parse_code(text, *, loopback: bool = False) -> PairCode:
    """Parse an ``mfd1:`` code (surrounding text and case forgiven). Raises
    ValueError with a plain message; never echoes the code."""
    code = code_from_text(text)
    if not code:
        raise ValueError("that isn't a device code — copy the whole code again")
    body, _, check = code[len(CODE_PREFIX) :].rpartition("-")
    try:
        payload = _b32dec(body)
    except ValueError:
        raise ValueError("that code is damaged — copy it again") from None
    if not hmac.compare_digest(_checksum(payload), check):
        raise ValueError("that code is damaged — copy it again")
    try:
        ver, kind = payload[0], payload[1]
        rest = payload[2:]
        if ver != _VERSION:
            raise ValueError("this code is from a newer MindFlock — update this one")
        if kind == KIND_TUNNEL:
            n = rest[0]
            label = rest[1 : 1 + n].decode("ascii")
            rest = rest[1 + n :]
            if not _LABEL_RE.match(label):
                raise ValueError
            host_port = "%s.trycloudflare.com" % label
        elif kind == KIND_LOOPBACK and loopback:
            (port,) = struct.unpack(">H", rest[:2])
            rest = rest[2:]
            if not 0 < port < 65536:
                raise ValueError
            host_port = "127.0.0.1:%d" % port
        else:
            raise ValueError
        if len(rest) != PATH_LEN + FP_LEN + INVITE_ID_LEN + SECRET_LEN:
            raise ValueError
        path, rest = rest[:PATH_LEN], rest[PATH_LEN:]
        fp, rest = rest[:FP_LEN], rest[FP_LEN:]
        iid, secret = rest[:INVITE_ID_LEN], rest[INVITE_ID_LEN:]
    except ValueError as err:
        if str(err):
            raise
        raise ValueError("that code is damaged — copy it again") from None
    except (IndexError, UnicodeDecodeError, struct.error):
        raise ValueError("that code is damaged — copy it again") from None
    return PairCode(
        url="wss://%s/%s" % (host_port, _b32enc(path)),
        fp=bytes(fp),
        invite_id=bytes(iid).hex(),
        secret=bytes(secret),
    )


def code_from_text(text) -> str:
    """The ``mfd1:`` code inside pasted text (lowercased), or ""."""
    if not isinstance(text, str):
        return ""
    m = _CODE_IN_TEXT_RE.search(text[:4096])
    return m.group(0).lower() if m else ""


# --------------------------------------------------------------------------- #
# The inviter's book
# --------------------------------------------------------------------------- #
@dataclass
class _Entry:
    secret: bytes = field(repr=False)
    code: str = field(repr=False)
    url: str = field(repr=False)
    expires_at: float  # monotonic
    expires_wall: float
    failures: int = 0


class PairBook:
    """In-memory single-use pairing invites (a restart kills them all)."""

    def __init__(
        self,
        *,
        clock=time.monotonic,
        wall=time.time,
        max_failures: int = MAX_FAILURES,
        rate: int = RATE_LIMIT,
        window_s: float = RATE_WINDOW_S,
        max_active: int = MAX_ACTIVE,
    ):
        self._clock, self._wall = clock, wall
        self._max_failures = max_failures
        self._rate, self._window = rate, window_s
        self._max_active = max_active
        self._entries: dict = {}
        self._attempts: deque = deque()
        self._lock = threading.Lock()
        self._dummy = secrets.token_bytes(SECRET_LEN)
        self.stats = {"ok": 0, "wrong": 0, "unknown": 0, "limited": 0, "killed": 0}

    def _purge(self, now: float) -> None:
        for iid in [i for i, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[iid]

    def create(
        self, url: str, fp: bytes, *, loopback: bool = False, ttl_s: float = TTL_S
    ) -> dict:
        """A new invite for the tunnel at ``url`` → ``{invite_id, code,
        expires_at (epoch s)}``. Older invites beyond :data:`MAX_ACTIVE` go."""
        if not 0 < float(ttl_s) <= TTL_S:
            raise ValueError("bad ttl")
        iid, secret = secrets.token_bytes(INVITE_ID_LEN), secrets.token_bytes(
            SECRET_LEN
        )
        code = encode_code(url, fp, iid, secret, loopback=loopback)
        with self._lock:
            now = self._clock()
            self._purge(now)
            while len(self._entries) >= self._max_active:
                self._entries.pop(next(iter(self._entries)))
            entry = _Entry(
                secret=secret,
                code=code,
                url=url,
                expires_at=now + float(ttl_s),
                expires_wall=self._wall() + float(ttl_s),
            )
            self._entries[iid.hex()] = entry
        return {"invite_id": iid.hex(), "code": code, "expires_at": entry.expires_wall}

    def active(self) -> list:
        """Live invites, newest last: ``{invite_id, code, url, expires_at}``
        (for this device's own screen only)."""
        with self._lock:
            self._purge(self._clock())
            return [
                {
                    "invite_id": iid,
                    "code": e.code,
                    "url": e.url,
                    "expires_at": e.expires_wall,
                }
                for iid, e in self._entries.items()
            ]

    def revoke(self, invite_id: str = "") -> int:
        """Drop one invite (or all with ``""``) → how many went."""
        with self._lock:
            if invite_id:
                return 1 if self._entries.pop(invite_id, None) is not None else 0
            n = len(self._entries)
            self._entries.clear()
            return n

    def check(self, invite_id, proof, transcript: bytes) -> Optional[bytes]:
        """``proof == HMAC-SHA256(secret, transcript)`` (hex), in constant
        time → the invite's secret (the invite is CONSUMED), else None. Each
        wrong proof counts and the 5th destroys the invite; unknown, expired,
        used and rate-limited attempts all return None the same way."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            while self._attempts and self._attempts[0] <= now - self._window:
                self._attempts.popleft()
            if len(self._attempts) >= self._rate:
                self.stats["limited"] += 1
                return None
            self._attempts.append(now)
            entry = None
            if isinstance(invite_id, str) and _HEX16_RE.match(invite_id):
                entry = self._entries.get(invite_id)
            key = entry.secret if entry is not None else self._dummy
            want = hmac.new(key, bytes(transcript), hashlib.sha256).hexdigest()
            ok = isinstance(proof, str) and hmac.compare_digest(want, proof)
            if entry is None:
                self.stats["unknown"] += 1
                return None
            if ok:
                del self._entries[invite_id]
                self.stats["ok"] += 1
                return entry.secret
            entry.failures += 1
            self.stats["wrong"] += 1
            if entry.failures >= self._max_failures:
                del self._entries[invite_id]
                self.stats["killed"] += 1
            return None


# --------------------------------------------------------------------------- #
# Transcript and proofs
# --------------------------------------------------------------------------- #
def _field(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


def transcript(
    server_pub: bytes,
    sn: bytes,
    cn: bytes,
    invite_id: str,
    device: str,
    host: str,
    dns: str,
    runs: bool,
    inviter: str = "",
) -> bytes:
    """Every field either side acts on, length-prefixed (no field can bleed
    into the next)."""
    parts = [
        server_pub,
        sn,
        cn,
        bytes.fromhex(invite_id),
        device.encode("utf-8"),
        host.encode("utf-8"),
        dns.encode("utf-8"),
        b"\x01" if runs else b"\x00",
    ]
    if inviter:
        parts.append(inviter.encode("utf-8"))
    return b"".join(_field(p) for p in parts)


def _mac(key: bytes, label: bytes, t: bytes) -> str:
    return hmac.new(key, label + b"|" + t, hashlib.sha256).hexdigest()


def confirm_key(bundle: dict) -> bytes:
    """The key the joiner's confirm is MAC'd with: derived from the group
    key the bundle carried (so a valid confirm says "I hold — and took —
    that key"). Raises ValueError for a bundle without one."""
    from backend.peer.fleet_record import _hkdf

    try:
        key, fid, epoch = bundle["key"], bundle["id"], int(bundle["epoch"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("bundle has no key") from None
    if not (isinstance(key, str) and key and isinstance(fid, str) and fid):
        raise ValueError("bundle has no key")
    return _hkdf(key, "pair-confirm", fid, epoch)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s) -> bytes:
    if not isinstance(s, str) or len(s) > 4 * ((_NONCE_LEN + 2) // 3):
        raise wire.ProtocolError("bad nonce")
    try:
        raw = base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise wire.ProtocolError("bad nonce") from None
    if len(raw) != _NONCE_LEN:
        raise wire.ProtocolError("bad nonce")
    return raw


def _text_ok(v, lo: int = 0) -> bool:
    return isinstance(v, str) and lo <= len(v) <= _MAX_TEXT and not _BAD_TEXT.search(v)


_PAIR_KEYS = {"t", "v", "nonce", "invite", "device", "host", "dns", "runs", "proof"}


def _parse_pair(msg) -> tuple:
    """The joiner's ``pair`` frame → ``(cn, invite, device, host, dns, runs,
    proof)``; :class:`wire.ProtocolError` when malformed."""
    if not isinstance(msg, dict) or set(msg) != _PAIR_KEYS:
        raise wire.ProtocolError("bad frame")
    if msg["t"] != "pair" or msg["v"] != 1:
        raise wire.ProtocolError("bad frame")
    cn = _unb64(msg["nonce"])
    invite, device, host, dns = msg["invite"], msg["device"], msg["host"], msg["dns"]
    runs, got = msg["runs"], msg["proof"]
    if not (
        isinstance(invite, str)
        and _HEX16_RE.match(invite)
        and isinstance(device, str)
        and _DEVICE_RE.match(device)
        and _text_ok(host, 1)
        and _text_ok(dns)
        and isinstance(runs, bool)
        and isinstance(got, str)
        and _HEX64_RE.match(got)
    ):
        raise wire.ProtocolError("bad frame")
    return cn, invite, device, host, dns, runs, got


async def _send(writer, obj: dict) -> None:
    writer.write(wire.encode(obj))
    await writer.drain()


async def _deny(writer, reason: str = "") -> None:
    with contextlib.suppress(Exception):
        await _send(
            writer, {"t": "denied", "reason": reason} if reason else {"t": "denied"}
        )


# --------------------------------------------------------------------------- #
# Server side (the inviter) — called by fleet_tunnel.TunnelServer
# --------------------------------------------------------------------------- #
async def serve_pair(
    reader,
    writer,
    *,
    server_pub: bytes,
    sn: bytes,
    msg: dict,
    book: PairBook,
    self_key: Callable[[], str],
    make_bundle: Callable[[str, str, str], dict],
    commit: Callable[[str, str, str, bool], Awaitable[None]],
    confirm_timeout: float = CONFIRM_TIMEOUT,
) -> str:
    """Answer one ``pair`` frame (``msg``, after our hello carried ``sn``).

    ``make_bundle(device, host, dns)`` → what the joiner adopts (the group's
    key and roster plus a live entry for it — the inviter's own roster is
    NOT touched; :class:`PairRefused` when that name is taken); ``commit(device, host, dns, runs)`` puts the joiner on the
    inviter's roster — called only after a valid confirm. → the outcome word
    (``denied``, ``same_name``, ``aborted``, ``unconfirmed``, ``added``,
    ``error``); never raises for anything the far end does."""
    try:
        cn, invite, device, host, dns, runs, got = _parse_pair(msg)
    except wire.ProtocolError:
        await _deny(writer)
        return "denied"
    t = transcript(server_pub, sn, cn, invite, device, host, dns, runs)
    secret = book.check(invite, got, CLIENT_LABEL + b"|" + t)
    if secret is None:
        await _deny(writer)
        return "denied"
    me = self_key()
    if not me or device == me:
        # The proof was right (the joiner holds the code), so saying why is
        # safe — and the only way the person learns to rename one of them.
        await _deny(writer, "same_name")
        return "same_name"
    try:
        bundle = make_bundle(device, host, dns)
        ckey = confirm_key(bundle)
    except PairRefused:  # its name is taken in this group
        await _deny(writer, "same_name")
        return "same_name"
    except Exception:  # noqa: BLE001 — not in a group any more, a bad roster
        await _deny(writer)
        return "error"
    await _send(
        writer,
        {
            "t": "ok",
            "v": 1,
            "from": me,
            "proof": _mac(secret, SERVER_LABEL, t + _field(me.encode("utf-8"))),
            "bundle": bundle,
        },
    )
    del bundle
    try:
        ans = await asyncio.wait_for(
            wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME), confirm_timeout
        )
    except (
        wire.ProtocolError,
        OSError,
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
    ):
        return "unconfirmed"
    if ans == {"t": "abort"}:
        return "aborted"
    want = _mac(ckey, CONFIRM_LABEL, t + _field(me.encode("utf-8")))
    proof = ans.get("proof") if isinstance(ans, dict) else None
    if not (
        isinstance(ans, dict)
        and set(ans) == {"t", "proof"}
        and ans.get("t") == "confirm"
        and isinstance(proof, str)
        and hmac.compare_digest(want, proof)
    ):
        return "unconfirmed"
    try:
        await commit(device, host, dns, runs)
    except Exception:  # noqa: BLE001 — never a half-told joiner: no "added"
        return "error"
    with contextlib.suppress(Exception):
        await _send(writer, {"t": "added"})
    return "added"


# --------------------------------------------------------------------------- #
# Client side (the new device)
# --------------------------------------------------------------------------- #
async def pair(
    code: PairCode,
    *,
    device: str,
    host: str,
    dns: str,
    runs: bool,
    adopt: Callable[[str, dict], None],
    edge_ssl,
    tls_ctx=None,
    timeout: float = PAIR_TIMEOUT,
) -> tuple:
    """Join through the inviter's tunnel with ``code`` → ``(inviter key,
    bundle)`` once the inviter said ``added``.

    ``adopt(inviter, bundle)`` takes the bundle (raise :class:`PairRefused`
    to refuse it: the inviter is told ``abort`` and adds nothing). It runs
    BEFORE the confirm: if anything fails after it ran (no ``added`` —
    :class:`PairError` ``unconfirmed`` —, a timeout, a cancel) the caller
    undoes the adopt.
    Raises :class:`PairError` for everything else (see its reasons), or
    ``OSError``/``TimeoutError`` when the tunnel can't be reached."""
    from backend.peer import relay
    from backend.peer.addr import parse_addr
    from backend.peer.identity import fingerprint_of, pub_from_cert_der
    from backend.peer.transport import client_ssl_context

    device = str(device or "")
    host = str(host or device)[:_MAX_TEXT]
    dns = str(dns or "")[:_MAX_TEXT]
    if not _DEVICE_RE.match(device):
        raise PairError(
            "refused", "this computer's name can't be used as a device name"
        )
    addr = parse_addr(code.url)
    async with asyncio.timeout(timeout):
        reader, writer = await relay.dial(
            addr, tls_ctx or client_ssl_context(), edge_ssl
        )
        try:
            sslobj = writer.get_extra_info("ssl_object")
            if sslobj is None or sslobj.version() != "TLSv1.3":
                raise PairError("protocol")
            pub = pub_from_cert_der(sslobj.getpeercert(binary_form=True))
            # The pin, before anything is sent.
            if pub is None or not hmac.compare_digest(fingerprint_of(pub), code.fp):
                raise PairError("pin")
            try:
                hello = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
                if (
                    set(hello) != {"t", "v", "nonce"}
                    or hello["t"] != "hello"
                    or hello["v"] != 1
                ):
                    raise PairError("protocol")
                sn = _unb64(hello["nonce"])
            except wire.ProtocolError:
                raise PairError("protocol") from None
            cn = secrets.token_bytes(_NONCE_LEN)
            t = transcript(pub, sn, cn, code.invite_id, device, host, dns, runs)
            await _send(
                writer,
                {
                    "t": "pair",
                    "v": 1,
                    "nonce": _b64(cn),
                    "invite": code.invite_id,
                    "device": device,
                    "host": host,
                    "dns": dns,
                    "runs": bool(runs),
                    "proof": _mac(code.secret, CLIENT_LABEL, t),
                },
            )
            try:
                ans = await wire.read_frame(reader, BUNDLE_MAX_FRAME)
            except wire.ConnectionClosed:
                raise PairError("denied") from None
            except wire.ProtocolError:
                raise PairError("protocol") from None
            if ans.get("t") == "denied":
                raise PairError(
                    "same_name" if ans.get("reason") == "same_name" else "denied"
                )
            inviter, bundle, got = ans.get("from"), ans.get("bundle"), ans.get("proof")
            if (
                set(ans) != {"t", "v", "from", "proof", "bundle"}
                or ans.get("t") != "ok"
                or not isinstance(inviter, str)
                or not _DEVICE_RE.match(inviter)
                or not isinstance(got, str)
                or not isinstance(bundle, dict)
            ):
                raise PairError("protocol")
            tt = t + _field(inviter.encode("utf-8"))
            if not hmac.compare_digest(_mac(code.secret, SERVER_LABEL, tt), got):
                raise PairError("proof")
            if inviter == device:
                raise PairError("same_name")
            try:
                ckey = confirm_key(bundle)
                adopt(inviter, bundle)
            except (PairRefused, ValueError) as err:
                with contextlib.suppress(Exception):
                    await _send(writer, {"t": "abort"})
                raise PairError("refused", str(err)) from None
            await _send(
                writer, {"t": "confirm", "proof": _mac(ckey, CONFIRM_LABEL, tt)}
            )
            try:
                done = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
            except (wire.ProtocolError, OSError, ConnectionError):
                done = None
            if done != {"t": "added"}:
                raise PairError("unconfirmed")
        except BaseException:
            with contextlib.suppress(Exception):
                writer.transport.abort()
            raise
    with contextlib.suppress(Exception):
        writer.close()
    return inviter, bundle
