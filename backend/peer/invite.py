"""One-time pairing codes.

A code is ``mfp1:<base32 payload>-<checksum>`` where the payload is
``host_len(1) | host | port(2, BE) | invite_id(8) | secret(20) | server_fp(16)``
(unpadded lowercase RFC 4648 base32) and the checksum is
``base32(sha256(payload)[:3])[:4]``. The fingerprint pins the inviter's key, so
even the first connection cannot be MITM'd; the 160-bit secret proves the
joiner saw the code.

:class:`InviteBook` lives in memory only: restarting the server kills every
invite. Each invite is single use, expires on ``time.monotonic``, and dies
after 5 wrong proofs; a global rate limit caps proof attempts across all
invites. Nothing here ever logs, reprs or echoes a secret or a code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

__all__ = [
    "CODE_PREFIX",
    "CodeInfo",
    "Invite",
    "InviteBook",
    "encode_code",
    "parse_code",
]

CODE_PREFIX = "mfp1:"
INVITE_ID_LEN = 8
SECRET_LEN = 20
FP_LEN = 16
DEFAULT_TTL_S = 600.0
MAX_TTL_S = 600.0
MAX_FAILURES = 5
PAIR_RATE_LIMIT = 10
PAIR_RATE_WINDOW_S = 60.0
MAX_ACTIVE = 32

_MAX_PAYLOAD = 1 + 253 + 2 + INVITE_ID_LEN + SECRET_LEN + FP_LEN
_B32_RE = re.compile(r"^[a-z2-7]+\Z")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}\Z")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\Z")


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


def _check_host(host: str) -> str:
    if not isinstance(host, str) or not host:
        raise ValueError("bad host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError("bad host: not a reachable address")
        return host
    if len(host) > 253 or not _HOSTNAME_RE.match(host):
        raise ValueError("bad host")
    return host


def _check_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("bad port")
    return port


@dataclass(frozen=True)
class CodeInfo:
    """A parsed pairing code."""

    host: str
    port: int
    invite_id: str  # 16 lowercase hex chars
    secret: bytes = field(repr=False)
    server_fp: bytes = field(repr=False)


def encode_code(
    host: str, port: int, invite_id: bytes, secret: bytes, server_fp: bytes
) -> str:
    host = _check_host(host)
    port = _check_port(port)
    hb = host.encode("utf-8")
    if (
        len(invite_id) != INVITE_ID_LEN
        or len(secret) != SECRET_LEN
        or len(server_fp) != FP_LEN
    ):
        raise ValueError("bad code field length")
    payload = (
        bytes([len(hb)]) + hb + port.to_bytes(2, "big") + invite_id + secret + server_fp
    )
    return f"{CODE_PREFIX}{_b32enc(payload)}-{_checksum(payload)}"


def parse_code(code: str) -> CodeInfo:
    """Parse a pairing code. Raises ``ValueError`` (whose message never
    contains the code) for anything malformed. Surrounding whitespace and
    upper case are tolerated."""
    if not isinstance(code, str):
        raise ValueError("pairing code must be a string")
    text = code.strip().lower()
    if not text.startswith(CODE_PREFIX):
        raise ValueError("not a MindFlock pairing code (expected mfp1:…)")
    body, sep, cs = text[len(CODE_PREFIX) :].rpartition("-")
    # cs must be base32 before compare_digest, which raises TypeError on non-ASCII.
    if (
        not sep
        or len(cs) != 4
        or not _B32_RE.match(cs)
        or not body
        or len(body) > (_MAX_PAYLOAD * 8 + 4) // 5
    ):
        raise ValueError("malformed pairing code")
    try:
        payload = _b32dec(body)
    except ValueError:
        raise ValueError("malformed pairing code") from None
    if not hmac.compare_digest(_checksum(payload), cs):
        raise ValueError("pairing code checksum mismatch (typo?)")
    host_len = payload[0]
    if (
        host_len == 0
        or len(payload) != 1 + host_len + 2 + INVITE_ID_LEN + SECRET_LEN + FP_LEN
    ):
        raise ValueError("malformed pairing code")
    pos = 1
    try:
        host = payload[pos : pos + host_len].decode("utf-8")
        _check_host(host)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("malformed pairing code: bad host") from None
    pos += host_len
    port = int.from_bytes(payload[pos : pos + 2], "big")
    if port == 0:
        raise ValueError("malformed pairing code: bad port")
    pos += 2
    invite_id = payload[pos : pos + INVITE_ID_LEN].hex()
    pos += INVITE_ID_LEN
    secret = payload[pos : pos + SECRET_LEN]
    pos += SECRET_LEN
    server_fp = payload[pos : pos + FP_LEN]
    return CodeInfo(
        host=host, port=port, invite_id=invite_id, secret=secret, server_fp=server_fp
    )


@dataclass(frozen=True)
class Invite:
    invite_id: str
    code: str = field(repr=False)
    expires_at: float  # time.monotonic() deadline (the book's clock)


@dataclass
class _Entry:
    secret: bytes = field(repr=False)
    expires_at: float
    failures: int = 0


class InviteBook:
    """In-memory single-use invites for this instance (``server_fp`` is our
    own key fingerprint, embedded in every code)."""

    def __init__(
        self,
        server_fp: bytes,
        *,
        clock=time.monotonic,
        max_failures: int = MAX_FAILURES,
        pair_rate: int = PAIR_RATE_LIMIT,
        pair_window_s: float = PAIR_RATE_WINDOW_S,
        max_active: int = MAX_ACTIVE,
    ):
        if len(server_fp) != FP_LEN:
            raise ValueError("bad server fingerprint")
        self._fp = bytes(server_fp)
        self._clock = clock
        self._max_failures = max_failures
        self._pair_rate = pair_rate
        self._pair_window = pair_window_s
        self._max_active = max_active
        self._entries: dict[str, _Entry] = {}
        self._attempts: deque[float] = deque()
        self._lock = threading.Lock()
        self._dummy_key = secrets.token_bytes(SECRET_LEN)

    def _purge(self, now: float) -> None:
        for iid in [i for i, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[iid]

    def create(self, host: str, port: int, ttl_s: float = DEFAULT_TTL_S) -> Invite:
        if (
            isinstance(ttl_s, bool)
            or not isinstance(ttl_s, (int, float))
            or not 0 < ttl_s <= MAX_TTL_S
        ):
            raise ValueError(f"ttl_s must be in (0, {int(MAX_TTL_S)}]")
        with self._lock:
            now = self._clock()
            self._purge(now)
            if len(self._entries) >= self._max_active:
                raise ValueError("too many active invites; revoke one first")
            iid = secrets.token_bytes(INVITE_ID_LEN)
            secret = secrets.token_bytes(SECRET_LEN)
            code = encode_code(host, port, iid, secret, self._fp)
            entry = _Entry(secret=secret, expires_at=now + float(ttl_s))
            self._entries[iid.hex()] = entry
            return Invite(invite_id=iid.hex(), code=code, expires_at=entry.expires_at)

    def revoke(self, invite_id: str) -> bool:
        with self._lock:
            return self._entries.pop(invite_id, None) is not None

    def active(self) -> list[dict]:
        """Live invites: id and expiry only, never the secret or code."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            return [
                {
                    "invite_id": iid,
                    "expires_at": e.expires_at,
                    "expires_in": max(0.0, e.expires_at - now),
                }
                for iid, e in self._entries.items()
            ]

    def has_active(self) -> bool:
        return bool(self.active())

    def check(self, invite_id, proof, transcript: bytes) -> bool:
        """Verify ``proof == HMAC-SHA256(secret, transcript)`` in constant time.
        Success consumes the invite; each failure counts and the 5th destroys
        it. Unknown, expired and rate-limited attempts all return False."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            while self._attempts and self._attempts[0] <= now - self._pair_window:
                self._attempts.popleft()
            if len(self._attempts) >= self._pair_rate:
                return False
            self._attempts.append(now)

            entry = None
            if isinstance(invite_id, str) and _HEX16_RE.match(invite_id):
                entry = self._entries.get(invite_id)
            key = entry.secret if entry is not None else self._dummy_key
            expected = hmac.new(key, bytes(transcript), hashlib.sha256).digest()
            ok = isinstance(proof, (bytes, bytearray)) and hmac.compare_digest(
                expected, bytes(proof)
            )
            if entry is None:
                return False
            if ok:
                del self._entries[invite_id]
                return True
            entry.failures += 1
            if entry.failures >= self._max_failures:
                del self._entries[invite_id]
            return False
