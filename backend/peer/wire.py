"""Peer wire format: length-prefixed JSON frames and strict validators.

A frame is a 4-byte big-endian length followed by a UTF-8 JSON **object** of
at most :data:`MAX_FRAME` bytes. Parsing rejects: length 0 or over the cap,
invalid UTF-8, nesting deeper than :data:`MAX_DEPTH`, duplicate keys,
``NaN``/``Infinity`` and non-object top levels. Every error is a
:class:`ProtocolError`, and the caller closes the connection.

Everything a peer sends is validated against its schema before anyone looks
at it: unknown frame types are rejected, ``bool`` is never accepted as
``int``, strings have length caps, lone surrogates are refused everywhere, and
strings that end up in a terminal or a UI (names, message text, error strings)
refuse control characters.

**Tolerant inbound, strict outbound** (so the protocol can grow without
dropping links between versions): what a peer sends may carry keys this
version doesn't know — they are dropped before anything reads the frame — and
a well-formed request for an op we don't have raises :class:`UnsupportedOp`,
which the transport answers ``ok:false, err:"unsupported"`` without closing.
Handshake frames may carry an optional ``app`` (the peer's MindFlock version)
and ``caps`` (capability names), validated when present. What WE send is
always checked against the exact schema (``strict=True``), because a peer one
release older still rejects anything extra.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl

__all__ = [
    "MAX_FRAME",
    "HANDSHAKE_MAX_FRAME",
    "MAX_DEPTH",
    "MAX_ID",
    "OPS",
    "ProtocolError",
    "ConnectionClosed",
    "UnsupportedOp",
    "peer_info",
    "encode",
    "decode",
    "read_frame",
    "validate_request",
    "validate_response",
    "validate_message",
    "validate_hello",
    "validate_pair",
    "validate_auth",
    "validate_welcome",
    "validate_denied",
    "clip_text",
]

MAX_FRAME = 1_048_576
HANDSHAKE_MAX_FRAME = 4096
MAX_DEPTH = 16
MAX_ID = 2**53
PROTOCOL_VERSION = 1
OPS = ("msg", "diff", "read_file", "list_files", "status")

MAX_NAME = 128  # on the wire; stores sanitize further to 32
MAX_MSG_TEXT = 20_000
MAX_ERR = 300
MAX_BYE = 200
MAX_PATH = 1024
MIN_DIFF_CHARS = 1000
MAX_DIFF_CHARS = 200_000
MAX_LIST_ITEMS = 20_000
MAX_STAT_STR = 4096


class ProtocolError(Exception):
    """The peer broke the protocol; close the connection."""


class ConnectionClosed(ProtocolError):
    """The connection ended (EOF at a frame boundary, reset, TLS error)."""


class UnsupportedOp(ProtocolError):
    """A well-formed request for an op this version doesn't implement (a
    newer peer). Not fatal: answer ``err:"unsupported"`` and carry on."""

    def __init__(self, rid: int, op: str):
        super().__init__("unsupported op")
        self.rid = rid
        self.op = op


# -- strings -----------------------------------------------------------------

# Surrogates can't be encoded as UTF-8; "line" strings also refuse every C0/C1
# control and DEL; "text" strings allow tab, newline and carriage return.
_BAD_ANY = re.compile(r"[\ud800-\udfff]")
_BAD_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_BAD_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_BAD_PATH = re.compile(r"[\x00\ud800-\udfff]")
# Anchored with \Z, never $ ($ also matches before a trailing newline), and
# [0-9] rather than \d (which matches every Unicode digit).
_MSG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_B64_32_RE = re.compile(r"^[A-Za-z0-9+/]{43}=\Z")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}\Z")
_SAS_RE = re.compile(r"^[0-9]{3}-[0-9]{3}-[0-9]{3}-[0-9]\Z")
_OP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}\Z")
MAX_APP = 64
MAX_CAPS = 32


def _hex_re(n: int) -> re.Pattern:
    return re.compile(rf"^[0-9a-f]{{{n}}}\Z")


_HEX16, _HEX32, _HEX64, _HEX128 = _hex_re(16), _hex_re(32), _hex_re(64), _hex_re(128)


def _fail(what: str):
    raise ProtocolError(what)


def _str(v, lo: int, hi: int, bad: re.Pattern = _BAD_LINE) -> bool:
    return isinstance(v, str) and lo <= len(v) <= hi and not bad.search(v)


def _int(v, lo: int, hi: int) -> bool:
    return type(v) is int and lo <= v <= hi  # bool is a subclass of int


def _keys(d, required, optional=(), strict: bool = True) -> dict:
    """Check ``d``'s keys. ``strict`` refuses any key outside required +
    optional; otherwise those are dropped. Returns ``d`` (strict) or the
    copy with only the known keys."""
    if not isinstance(d, dict):
        _fail("expected an object")
    keys = set(d)
    if not keys.issuperset(required):
        _fail("unexpected or missing keys")
    extra = keys - set(required) - set(optional)
    if not extra:
        return d
    if strict:
        _fail("unexpected or missing keys")
    return {k: v for k, v in d.items() if k not in extra}


def clip_text(s, limit: int) -> str:
    """Make ``s`` a valid wire text string of at most ``limit`` chars (for
    error and bye strings we originate)."""
    text = _BAD_TEXT.sub("?", s if isinstance(s, str) else str(s))
    return text[:limit]


# -- codec -------------------------------------------------------------------


def _check_depth(obj) -> None:
    # Iterative, so it can't recurse. The C parser itself bounds recursion
    # (RecursionError, caught in decode) for anything absurdly deep.
    stack = [(obj, 1)]
    while stack:
        o, depth = stack.pop()
        if depth > MAX_DEPTH:
            _fail("frame nested too deeply")
        children = o.values() if isinstance(o, dict) else o
        stack.extend((c, depth + 1) for c in children if isinstance(c, (dict, list)))


def _no_dups(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            _fail("duplicate key")
        d[k] = v
    return d


def _no_constants(name):
    _fail("non-finite number")


def _finite_float(s: str) -> float:
    f = float(s)
    if f in (float("inf"), float("-inf")):
        _fail("non-finite number")
    return f


def decode(body: bytes) -> dict:
    """Parse one frame body (without the length prefix)."""
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        _fail("frame is not UTF-8")
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_no_dups,
            parse_constant=_no_constants,
            parse_float=_finite_float,
        )
    except ProtocolError:
        raise
    except (ValueError, RecursionError):
        _fail("frame is not valid JSON or too deeply nested")
    if not isinstance(obj, dict):
        _fail("frame is not a JSON object")
    _check_depth(obj)
    return obj


def encode(obj: dict) -> bytes:
    if not isinstance(obj, dict):
        raise ProtocolError("frames are JSON objects")
    try:
        body = json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ProtocolError("frame is not encodable") from None
    if not 0 < len(body) <= MAX_FRAME:
        raise ProtocolError("frame too large")
    return len(body).to_bytes(4, "big") + body


async def read_frame(reader: asyncio.StreamReader, max_size: int = MAX_FRAME) -> dict:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            raise ConnectionClosed("connection closed") from None
        raise ProtocolError("truncated frame header") from None
    except (ConnectionError, ssl.SSLError, OSError) as e:
        raise ConnectionClosed(type(e).__name__) from None
    n = int.from_bytes(header, "big")
    if n == 0 or n > max_size:
        _fail("bad frame length")
    try:
        body = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        raise ProtocolError("truncated frame") from None
    except (ConnectionError, ssl.SSLError, OSError) as e:
        raise ConnectionClosed(type(e).__name__) from None
    return decode(body)


# -- requests ----------------------------------------------------------------


def _req_msg(p, strict) -> dict:
    p = _keys(p, ("msg_id", "text"), ("reply_to",), strict)
    if not _str(p["msg_id"], 1, 64) or not _MSG_ID_RE.match(p["msg_id"]):
        _fail("bad msg_id")
    if not _str(p["text"], 1, MAX_MSG_TEXT, _BAD_TEXT):
        _fail("bad text")
    rt = p.get("reply_to")
    if rt is not None and (not _str(rt, 1, 64) or not _MSG_ID_RE.match(rt)):
        _fail("bad reply_to")
    return p


def _req_diff(p, strict) -> dict:
    p = _keys(p, ("max_chars",), (), strict)
    if not _int(p["max_chars"], MIN_DIFF_CHARS, MAX_DIFF_CHARS):
        _fail("bad max_chars")
    return p


def _req_read_file(p, strict) -> dict:
    p = _keys(p, ("path",), (), strict)
    if not _str(p["path"], 1, MAX_PATH):
        _fail("bad path")
    return p


def _req_empty(p, strict) -> dict:
    return _keys(p, (), (), strict)


_REQUESTS = {
    "msg": _req_msg,
    "diff": _req_diff,
    "read_file": _req_read_file,
    "list_files": _req_empty,
    "status": _req_empty,
}


def validate_request(op, p, strict: bool = True) -> dict:
    """Raise ProtocolError unless ``(op, p)`` is an allowed request; returns
    ``p`` (with unknown keys dropped when not ``strict``)."""
    check = _REQUESTS.get(op) if isinstance(op, str) else None
    if check is None:
        _fail("unknown op")
    return check(p, strict)


# -- responses ---------------------------------------------------------------


def _stat_item(v) -> bool:
    if isinstance(v, str):
        return _str(v, 0, MAX_STAT_STR, _BAD_ANY)
    if not isinstance(v, dict) or len(v) > 8:
        return False
    for k, x in v.items():
        if not _str(k, 1, 32):
            return False
        if x is None or isinstance(x, bool) or _int(x, -MAX_ID, MAX_ID):
            continue
        if not _str(x, 0, MAX_STAT_STR, _BAD_ANY):
            return False
    return True


def _res_msg(p, req, strict) -> dict:
    p = _keys(p, ("accepted",), (), strict)
    if not isinstance(p["accepted"], bool):
        _fail("bad accepted")
    return p


def _res_diff(p, req, strict) -> dict:
    p = _keys(p, ("stat", "diff", "truncated"), (), strict)
    cap = MAX_DIFF_CHARS
    if isinstance(req, dict) and _int(
        req.get("max_chars"), MIN_DIFF_CHARS, MAX_DIFF_CHARS
    ):
        cap = req["max_chars"]
    stat = p["stat"]
    if (
        not isinstance(stat, list)
        or len(stat) > MAX_LIST_ITEMS
        or not all(map(_stat_item, stat))
    ):
        _fail("bad stat")
    if not _str(p["diff"], 0, cap, _BAD_ANY):
        _fail("bad diff")
    if not isinstance(p["truncated"], bool):
        _fail("bad truncated")
    return p


def _res_read_file(p, req, strict) -> dict:
    p = _keys(p, ("path", "size", "encoding", "content", "truncated"), (), strict)
    if not _str(p["path"], 1, MAX_PATH, _BAD_PATH):
        _fail("bad path")
    if not _int(p["size"], 0, MAX_ID):
        _fail("bad size")
    enc, content = p["encoding"], p["content"]
    if enc == "utf-8":
        ok = _str(content, 0, MAX_FRAME, _BAD_ANY)
    elif enc == "base64":
        ok = (
            isinstance(content, str)
            and len(content) <= MAX_FRAME
            and len(content) % 4 == 0
            and bool(_B64_RE.match(content))
        )
    else:
        ok = False
    if not ok:
        _fail("bad content")
    if not isinstance(p["truncated"], bool):
        _fail("bad truncated")
    return p


def _res_list_files(p, req, strict) -> dict:
    p = _keys(p, ("files", "truncated"), (), strict)
    files = p["files"]
    if (
        not isinstance(files, list)
        or len(files) > MAX_LIST_ITEMS
        or not all(_str(f, 1, MAX_PATH, _BAD_PATH) for f in files)
    ):
        _fail("bad files")
    if not isinstance(p["truncated"], bool):
        _fail("bad truncated")
    return p


def _res_status(p, req, strict) -> dict:
    p = _keys(p, ("shared", "agent", "name"), (), strict)
    if not isinstance(p["shared"], bool):
        _fail("bad shared")
    if p["agent"] not in ("running", "stopped", "none"):
        _fail("bad agent")
    if not _str(p["name"], 0, MAX_NAME):
        _fail("bad name")
    return p


_RESPONSES = {
    "msg": _res_msg,
    "diff": _res_diff,
    "read_file": _res_read_file,
    "list_files": _res_list_files,
    "status": _res_status,
}


def validate_response(op, p, req: dict | None = None, strict: bool = True) -> dict:
    """Raise ProtocolError unless ``p`` is a valid ``ok:true`` payload for
    ``op`` (``req`` is the request payload, used to bound ``diff``); returns
    ``p`` (unknown keys dropped when not ``strict``)."""
    check = _RESPONSES.get(op) if isinstance(op, str) else None
    if check is None:
        _fail("unknown op")
    return check(p, req, strict)


# -- frames after the handshake ---------------------------------------------


def validate_message(obj) -> str:
    """Validate a post-handshake frame and return its type. ``req`` frames
    are validated down to their payload; ``res`` payloads need the request's
    op, so the caller checks them with :func:`validate_response`."""
    if not isinstance(obj, dict):
        _fail("expected an object")
    t = obj.get("t")
    if t == "req":
        _keys(obj, ("t", "id", "op", "p"), (), strict=False)
        if not _int(obj["id"], 1, MAX_ID):
            _fail("bad id")
        op = obj["op"]
        if not isinstance(op, str) or not _OP_NAME_RE.match(op):
            _fail("bad op")
        if op not in _REQUESTS:
            raise UnsupportedOp(obj["id"], op)
        obj["p"] = validate_request(op, obj["p"], strict=False)
    elif t == "res":
        if obj.get("ok") is True:
            _keys(obj, ("t", "id", "ok", "p"), (), strict=False)
            if not isinstance(obj["p"], dict):
                _fail("bad p")
        elif obj.get("ok") is False:
            _keys(obj, ("t", "id", "ok", "err"), (), strict=False)
            if not _str(obj["err"], 0, MAX_ERR, _BAD_TEXT):
                _fail("bad err")
        else:
            _fail("bad ok")
        if not _int(obj["id"], 1, MAX_ID):
            _fail("bad id")
    elif t in ("ping", "pong"):
        _keys(obj, ("t",), (), strict=False)
    elif t == "bye":
        _keys(obj, ("t", "reason"), (), strict=False)
        if not _str(obj["reason"], 0, MAX_BYE, _BAD_TEXT):
            _fail("bad reason")
    else:
        _fail("unknown frame type")
    return t


# -- handshake frames ---------------------------------------------------------


def _version(obj) -> None:
    if not _int(obj.get("v"), PROTOCOL_VERSION, PROTOCOL_VERSION):
        _fail("unsupported protocol version")


def _peer_extras(obj) -> None:
    """The optional ``app`` / ``caps`` a newer peer sends in a handshake
    frame: validated when present (a malformed one is a protocol error)."""
    if "app" in obj and not _str(obj["app"], 1, MAX_APP):
        _fail("bad app")
    if "caps" in obj:
        caps = obj["caps"]
        if (
            not isinstance(caps, list)
            or len(caps) > MAX_CAPS
            or not all(isinstance(c, str) and _CAP_RE.match(c) for c in caps)
        ):
            _fail("bad caps")


def peer_info(obj) -> dict:
    """``{"app": str|None, "caps": [str]}`` from a validated handshake frame
    — what the peer said about itself (nothing, from this release's peers)."""
    return {
        "app": obj.get("app") if isinstance(obj.get("app"), str) else None,
        "caps": list(obj.get("caps") or []),
    }


def validate_hello(obj) -> bytes:
    """Returns the 32-byte nonce."""
    _keys(obj, ("t", "v", "nonce", "name"), (), strict=False)
    _peer_extras(obj)
    if obj["t"] != "hello":
        _fail("expected hello")
    _version(obj)
    if not _str(obj["nonce"], 44, 44) or not _B64_32_RE.match(obj["nonce"]):
        _fail("bad nonce")
    nonce = base64.b64decode(obj["nonce"], validate=True)
    if len(nonce) != 32 or base64.b64encode(nonce).decode("ascii") != obj["nonce"]:
        _fail("bad nonce")
    if not _str(obj["name"], 1, MAX_NAME):
        _fail("bad name")
    return nonce


def validate_pair(obj) -> None:
    _keys(obj, ("t", "v", "invite_id", "pub", "name", "proof", "sig"), (), strict=False)
    _peer_extras(obj)
    if obj["t"] != "pair":
        _fail("expected pair")
    _version(obj)
    for key, rx in (
        ("invite_id", _HEX16),
        ("pub", _HEX64),
        ("proof", _HEX64),
        ("sig", _HEX128),
    ):
        if not isinstance(obj[key], str) or not rx.match(obj[key]):
            _fail(f"bad {key}")
    if not _str(obj["name"], 1, MAX_NAME):
        _fail("bad name")


def validate_auth(obj) -> None:
    _keys(obj, ("t", "v", "link_id", "sig"), (), strict=False)
    _peer_extras(obj)
    if obj["t"] != "auth":
        _fail("expected auth")
    _version(obj)
    if not isinstance(obj["link_id"], str) or not _HEX32.match(obj["link_id"]):
        _fail("bad link_id")
    if not isinstance(obj["sig"], str) or not _HEX128.match(obj["sig"]):
        _fail("bad sig")


def validate_welcome(obj) -> None:
    _keys(obj, ("t", "link_id", "name", "sas"), (), strict=False)
    _peer_extras(obj)
    if obj["t"] != "welcome":
        _fail("expected welcome")
    if not isinstance(obj["link_id"], str) or not _HEX32.match(obj["link_id"]):
        _fail("bad link_id")
    if not _str(obj["name"], 1, MAX_NAME):
        _fail("bad name")
    if not isinstance(obj["sas"], str) or not _SAS_RE.match(obj["sas"]):
        _fail("bad sas")


def validate_denied(obj) -> None:
    _keys(obj, ("t",))
    if obj["t"] != "denied":
        _fail("expected denied")
