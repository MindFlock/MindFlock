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

**Push frames** (``{"t":"push","op":…,"p":{…}}``) carry a shared session's
terminal both ways and "my share list changed" — one-way, never answered, so
a live terminal is not a request round trip per keystroke. A peer one
release older closes the connection on an unknown frame type, so a push only
ever goes to a peer that proved it speaks them: it asked us for ``sessions``
or opened a terminal (docs/peer-link.md, "Shared sessions — CONTRACT"). A
well-formed push for an op this version doesn't know raises
:class:`UnsupportedPush`, which the transport drops without closing.
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
    "UnsupportedPush",
    "PUSH_OPS",
    "CAPS",
    "peer_info",
    "encode",
    "decode",
    "read_frame",
    "validate_request",
    "validate_response",
    "validate_push",
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
OPS = (
    "msg",
    "diff",
    "read_file",
    "list_files",
    "status",
    "screen",
    "mirror_pull",
    "joint_propose",
    "joint_accept",
    "joint_cancel",
    "joint_contract",
    "joint_proposal",
    "joint_state",
    "joint_check",
    # Shared sessions (docs/peer-link.md "Shared sessions — CONTRACT").
    "sessions",
    "term_open",
    "term_close",
    "session_focus",
    # An agent's message to a shared session (docs/peer-link.md "Agent
    # messages across the link").
    "agent_msg",
    # A person's line in a shared session's chat (docs/peer-link.md "Shared
    # sessions — CONTRACT"; backend.peer.chat_bridge).
    "chat_msg",
)
#: One-way frames (``t: "push"``): the sharer's terminal output and its end,
#: "my share list changed", a line of a shared session's chat — and the
#: opener's keystrokes and resizes.
PUSH_OPS = (
    "term_out",
    "term_end",
    "term_in",
    "term_resize",
    "sessions_changed",
    "chat_out",
)
#: What this version can do beyond the original ops, for the handshake's
#: ``caps`` (sent by the transport; a peer advertising ``sessions`` speaks
#: the shared-session ops and push frames, ``agent_msg`` takes an agent's
#: message to a session it shares, ``chat_msg`` talks in a shared session's
#: chat and takes its ``chat_out`` lines).
CAPS = ("sessions", "agent_msg", "chat_msg")

MAX_NAME = 128  # on the wire; stores sanitize further to 32
MAX_MSG_TEXT = 20_000
MAX_ERR = 300
MAX_BYE = 200
MAX_PATH = 1024
MIN_DIFF_CHARS = 1000
MAX_DIFF_CHARS = 200_000
MAX_LIST_ITEMS = 20_000
MAX_STAT_STR = 4096
MAX_SCREEN_CHARS = 200_000  # backend.peer.screen.MAX_SCREEN_CHARS
MAX_SCREEN_DIM = 1000
MAX_MIRROR_BYTES = 64 * 1024 * 1024  # backend.peer.share.MIRROR_MAX_BYTES
MAX_MIRROR_CHUNK = 960_000  # base64 of backend.peer.mirror.CHUNK raw bytes
# Joint tasks (backend.peer.joint): one per link, sized so a whole joint_state
# answer stays far under MAX_FRAME even at 4 bytes per character.
MAX_JOINT_BRIEF = 4000
MAX_JOINT_CONTRACT = 50_000
MAX_JOINT_PROPOSAL = 10_000
MAX_JOINT_PROPOSALS = 10
MAX_JOINT_REASON = 200
MAX_JOINT_REV = 2**31
# The joint check (backend.peer.joint_check): capped so a joint_state answer
# carrying a full checklist still fits one frame next to a full contract.
MAX_CHECK_STEPS = 25
MAX_CHECK_TEXT = 500
MAX_CHECK_NOTE = 300
CHECK_ACTORS = ("agent", "human")
CHECK_SIDES = ("mine", "theirs")
CHECK_RESULTS = ("pass", "fail", "blocked", "")
# Shared sessions (backend.peer.sessions_bridge): a terminal chunk is at most
# MAX_TERM_CHUNK raw bytes, base64 on the wire; a share list is bounded.
MAX_TERM_CHUNK = 32 * 1024
MAX_TERM_B64 = 4 * ((MAX_TERM_CHUNK + 2) // 3)
MAX_TERM_DIM = 1000
MAX_SESSION_TITLE = 200
MAX_SESSIONS = 50
MAX_ACTIVITY = 200
SESSION_MODES = ("gated", "full")
#: ``agent_msg``'s reply-chain depth: the mailbox holds far shallower chains
#: (backend.web.core.mailbox.max_hops); this only bounds the number.
MAX_AGENT_HOP = 1000
#: A chat line, in characters (backend.web.core.chat.MAX_TEXT), and the
#: name shown on it.
MAX_CHAT_TEXT = 2000
MAX_CHAT_NAME = 64
MAX_CHAT_FROM = 128


class ProtocolError(Exception):
    """The peer broke the protocol; close the connection."""


class ConnectionClosed(ProtocolError):
    """The connection ended (EOF at a frame boundary, reset, TLS error)."""


class UnsupportedPush(ProtocolError):
    """A well-formed push for an op this version doesn't implement (a newer
    peer). Not fatal: nobody waits for an answer, so it is dropped."""

    def __init__(self, op: str):
        super().__init__("unsupported push")
        self.op = op


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
_CHAT_ID_RE = re.compile(r"^c[0-9]{1,15}\Z")
_B64_32_RE = re.compile(r"^[A-Za-z0-9+/]{43}=\Z")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}\Z")
_SAS_RE = re.compile(r"^[0-9]{3}-[0-9]{3}-[0-9]{3}-[0-9]\Z")
_OP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}\Z")
_JOINT_ID_RE = re.compile(r"^jt_[0-9a-f]{12}\Z")
_PROPOSAL_ID_RE = re.compile(r"^cp_[0-9a-f]{8}\Z")
_ISO_RE = re.compile(r"^[0-9][0-9T:.+Z-]{0,39}\Z")
_STEP_ID_RE = re.compile(r"^s[0-9]{1,3}\Z")
_TERM_ID_RE = re.compile(r"^[0-9a-f]{16}\Z")
JOINT_STATES = ("proposed", "active", "done", "cancelled")
PROPOSAL_STATES = ("open", "accepted", "rejected")
JOINT_BYS = ("me", "peer")
MAX_APP = 64
MAX_CAPS = 32


# A git object id: SHA-1 (40 hex) or SHA-256 (64 hex).
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


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


def _req_mirror_pull(p, strict) -> dict:
    p = _keys(p, ("have", "offset"), (), strict)
    if not (
        p["have"] == "" or (isinstance(p["have"], str) and _SHA_RE.match(p["have"]))
    ):
        _fail("bad have")
    if not _int(p["offset"], 0, MAX_MIRROR_BYTES):
        _fail("bad offset")
    return p


# -- joint tasks (docs/peer-link.md "Joint tasks — CONTRACT") ------------------


def _joint_id(v) -> None:
    if not isinstance(v, str) or not _JOINT_ID_RE.match(v):
        _fail("bad joint id")


def _iso(v, what: str) -> None:
    if not isinstance(v, str) or not _ISO_RE.match(v):
        _fail("bad " + what)


def _joint_half(h, strict, with_shared: bool = False) -> dict:
    """One side's half as the wire carries it: ``{repo_name, branch,
    program}`` (+ ``shared`` inside a ``joint_state`` answer). Never a path."""
    req = ("repo_name", "branch", "program") + (("shared",) if with_shared else ())
    h = _keys(h, req, (), strict)
    if not _str(h["repo_name"], 0, MAX_NAME):
        _fail("bad repo_name")
    if not _str(h["branch"], 0, 200):
        _fail("bad branch")
    if not _str(h["program"], 0, 64):
        _fail("bad program")
    if with_shared and not isinstance(h["shared"], bool):
        _fail("bad shared")
    return h


def _joint_proposal_obj(v, strict) -> dict:
    v = _keys(v, ("id", "by", "text", "at", "state"), (), strict)
    if not isinstance(v["id"], str) or not _PROPOSAL_ID_RE.match(v["id"]):
        _fail("bad proposal id")
    if v["by"] not in JOINT_BYS:
        _fail("bad by")
    if not _str(v["text"], 1, MAX_JOINT_PROPOSAL, _BAD_TEXT):
        _fail("bad proposal text")
    _iso(v["at"], "at")
    if v["state"] not in PROPOSAL_STATES:
        _fail("bad proposal state")
    return v


def _req_joint_propose(p, strict) -> dict:
    p = _keys(p, ("id", "brief", "contract_text", "mine"), (), strict)
    _joint_id(p["id"])
    if not _str(p["brief"], 1, MAX_JOINT_BRIEF, _BAD_TEXT):
        _fail("bad brief")
    if not _str(p["contract_text"], 0, MAX_JOINT_CONTRACT, _BAD_TEXT):
        _fail("bad contract_text")
    p["mine"] = _joint_half(p["mine"], strict)
    return p


def _req_joint_accept(p, strict) -> dict:
    p = _keys(p, ("id", "mine"), (), strict)
    _joint_id(p["id"])
    p["mine"] = _joint_half(p["mine"], strict)
    return p


def _req_joint_cancel(p, strict) -> dict:
    p = _keys(p, ("id", "reason"), (), strict)
    _joint_id(p["id"])
    if not _str(p["reason"], 0, MAX_JOINT_REASON):
        _fail("bad reason")
    return p


def _req_joint_contract(p, strict) -> dict:
    p = _keys(p, ("id", "rev", "text", "by"), (), strict)
    _joint_id(p["id"])
    if not _int(p["rev"], 1, MAX_JOINT_REV):
        _fail("bad rev")
    if not _str(p["text"], 0, MAX_JOINT_CONTRACT, _BAD_TEXT):
        _fail("bad text")
    if p["by"] not in JOINT_BYS:
        _fail("bad by")
    return p


def _req_joint_proposal(p, strict) -> dict:
    p = _keys(p, ("id", "proposal"), (), strict)
    _joint_id(p["id"])
    p["proposal"] = _joint_proposal_obj(p["proposal"], strict)
    return p


def _req_joint_state(p, strict) -> dict:
    p = _keys(p, ("id",), (), strict)
    _joint_id(p["id"])
    return p


def _iso_or_empty(v, what: str) -> None:
    if v != "":
        _iso(v, what)


def _check_step(v, strict) -> dict:
    """One joint-check step, ``side`` in the SENDER's terms."""
    v = _keys(v, ("id", "text", "expect", "actor", "side"), (), strict)
    if not isinstance(v["id"], str) or not _STEP_ID_RE.match(v["id"]):
        _fail("bad step id")
    if not _str(v["text"], 1, MAX_CHECK_TEXT, _BAD_TEXT):
        _fail("bad step text")
    if not _str(v["expect"], 0, MAX_CHECK_TEXT, _BAD_TEXT):
        _fail("bad step expect")
    if v["actor"] not in CHECK_ACTORS:
        _fail("bad step actor")
    if v["side"] not in CHECK_SIDES:
        _fail("bad step side")
    return v


def _check_steps(v, strict) -> list:
    if not isinstance(v, list) or len(v) > MAX_CHECK_STEPS:
        _fail("bad steps")
    steps = [_check_step(x, strict) for x in v]
    if len({s["id"] for s in steps}) != len(steps):
        _fail("duplicate step id")
    return steps


def _check_side_results(v, strict) -> dict:
    """The sender's own side: ``{results: {step_id: cell}, tested_sha, at}``."""
    v = _keys(v, ("results", "tested_sha", "at"), (), strict)
    res = v["results"]
    if not isinstance(res, dict) or len(res) > MAX_CHECK_STEPS:
        _fail("bad results")
    out = {}
    for sid, cell in res.items():
        if not isinstance(sid, str) or not _STEP_ID_RE.match(sid):
            _fail("bad step id")
        cell = _keys(cell, ("result", "note", "at", "by"), (), strict)
        if cell["result"] not in CHECK_RESULTS:
            _fail("bad result")
        if not _str(cell["note"], 0, MAX_CHECK_NOTE, _BAD_TEXT):
            _fail("bad note")
        _iso_or_empty(cell["at"], "at")
        if cell["by"] not in CHECK_ACTORS:
            _fail("bad by")
        out[sid] = cell
    v["results"] = out
    if v["tested_sha"] != "" and not (
        isinstance(v["tested_sha"], str) and _SHA_RE.match(v["tested_sha"])
    ):
        _fail("bad tested_sha")
    _iso_or_empty(v["at"], "at")
    return v


def _req_joint_check(p, strict) -> dict:
    """``{id, rev, side_results, steps?, by?}`` — the sender's results for its
    own side; the whole step list (and who wrote it) when it changed."""
    p = _keys(p, ("id", "rev", "side_results"), ("steps", "by"), strict)
    _joint_id(p["id"])
    if not _int(p["rev"], 1, MAX_JOINT_REV):
        _fail("bad rev")
    p["side_results"] = _check_side_results(p["side_results"], strict)
    if ("steps" in p) != ("by" in p):
        _fail("steps and by go together")
    if "steps" in p:
        p["steps"] = _check_steps(p["steps"], strict)
        if p["by"] not in JOINT_BYS:
            _fail("bad by")
    return p


# -- shared sessions (docs/peer-link.md "Shared sessions — CONTRACT") ---------


def _session_title(v, lo: int = 1) -> None:
    if not _str(v, lo, MAX_SESSION_TITLE):
        _fail("bad title")


def _term_id(v) -> None:
    if not isinstance(v, str) or not _TERM_ID_RE.match(v):
        _fail("bad term_id")


def _term_size(p) -> None:
    if not _int(p["cols"], 1, MAX_TERM_DIM) or not _int(p["rows"], 1, MAX_TERM_DIM):
        _fail("bad size")


def _term_data(v) -> None:
    if (
        not isinstance(v, str)
        or not 0 < len(v) <= MAX_TERM_B64
        or len(v) % 4
        or not _B64_RE.match(v)
    ):
        _fail("bad data")


def _req_term_open(p, strict) -> dict:
    p = _keys(p, ("title", "cols", "rows"), (), strict)
    _session_title(p["title"])
    _term_size(p)
    return p


def _req_term_close(p, strict) -> dict:
    p = _keys(p, ("term_id",), (), strict)
    _term_id(p["term_id"])
    return p


def _req_session_focus(p, strict) -> dict:
    """``{title}`` — the row the opener's page selected; ``""`` = it left."""
    p = _keys(p, ("title",), (), strict)
    _session_title(p["title"], 0)
    return p


def _req_agent_msg(p, strict) -> dict:
    """``{title, text, from_title, from_name, msg_id, reply_to?, hop?}`` — an
    agent in the sender's session ``from_title`` messaging the shared session
    ``title``; ``reply_to`` is the id of the message it answers (one the
    receiver sent), ``hop`` how deep that reply chain is."""
    p = _keys(
        p,
        ("title", "text", "from_title", "from_name", "msg_id"),
        ("reply_to", "hop"),
        strict,
    )
    _session_title(p["title"])
    _session_title(p["from_title"])
    if not _str(p["from_name"], 0, MAX_SESSION_TITLE):
        _fail("bad from_name")
    if not _str(p["msg_id"], 1, 64) or not _MSG_ID_RE.match(p["msg_id"]):
        _fail("bad msg_id")
    if not _str(p["text"], 1, MAX_MSG_TEXT, _BAD_TEXT):
        _fail("bad text")
    rt = p.get("reply_to")
    if rt is not None and (not _str(rt, 1, 64) or not _MSG_ID_RE.match(rt)):
        _fail("bad reply_to")
    if "hop" in p and not _int(p["hop"], 0, MAX_AGENT_HOP):
        _fail("bad hop")
    return p


def _chat_text(v) -> None:
    if not _str(v, 1, MAX_CHAT_TEXT, _BAD_TEXT):
        _fail("bad text")


def _chat_id(v) -> None:
    if not isinstance(v, str) or not _CHAT_ID_RE.match(v):
        _fail("bad id")


def _req_chat_msg(p, strict) -> dict:
    """``{title, text, from_name}`` — the opener's line for the chat of
    ``title``, a session the receiver shares; ``from_name`` is what the
    opener calls itself (``""``: the receiver names it)."""
    p = _keys(p, ("title", "text", "from_name"), (), strict)
    _session_title(p["title"])
    _chat_text(p["text"])
    if not _str(p["from_name"], 0, MAX_CHAT_NAME):
        _fail("bad from_name")
    return p


_REQUESTS = {
    "msg": _req_msg,
    "diff": _req_diff,
    "read_file": _req_read_file,
    "list_files": _req_empty,
    "status": _req_empty,
    "screen": _req_empty,
    "mirror_pull": _req_mirror_pull,
    "joint_propose": _req_joint_propose,
    "joint_accept": _req_joint_accept,
    "joint_cancel": _req_joint_cancel,
    "joint_contract": _req_joint_contract,
    "joint_proposal": _req_joint_proposal,
    "joint_state": _req_joint_state,
    "joint_check": _req_joint_check,
    "sessions": _req_empty,
    "term_open": _req_term_open,
    "term_close": _req_term_close,
    "session_focus": _req_session_focus,
    "agent_msg": _req_agent_msg,
    "chat_msg": _req_chat_msg,
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
    # ``head`` (their share's HEAD sha) is newer: an older peer omits it.
    p = _keys(p, ("shared", "agent", "name"), ("head",), strict)
    if "head" in p and not (isinstance(p["head"], str) and _SHA_RE.match(p["head"])):
        _fail("bad head")
    if not isinstance(p["shared"], bool):
        _fail("bad shared")
    if p["agent"] not in ("running", "stopped", "none"):
        _fail("bad agent")
    if not _str(p["name"], 0, MAX_NAME):
        _fail("bad name")
    return p


def _res_screen(p, req, strict) -> dict:
    p = _keys(p, ("text", "cols", "rows", "agent"), (), strict)
    # Escapes ARE allowed here (it is a terminal screen); the viewer's side
    # strips all but colours before a browser sees it (backend.peer.screen).
    if not _str(p["text"], 0, MAX_SCREEN_CHARS, _BAD_ANY):
        _fail("bad text")
    if not _int(p["cols"], 0, MAX_SCREEN_DIM) or not _int(p["rows"], 0, MAX_SCREEN_DIM):
        _fail("bad size")
    if p["agent"] not in ("running", "stopped", "none"):
        _fail("bad agent")
    return p


def _res_mirror_pull(p, req, strict) -> dict:
    p = _keys(p, ("head", "tip", "size", "sha256", "offset", "data"), (), strict)
    for k in ("head", "tip"):
        if not isinstance(p[k], str) or not _SHA_RE.match(p[k]):
            _fail("bad " + k)
    if not _int(p["size"], 0, MAX_MIRROR_BYTES) or not _int(
        p["offset"], 0, MAX_MIRROR_BYTES
    ):
        _fail("bad size")
    sha = p["sha256"]
    if not isinstance(sha, str) or not (
        sha == "" if p["size"] == 0 else _HEX64.match(sha)
    ):
        _fail("bad sha256")
    data = p["data"]
    if (
        not isinstance(data, str)
        or len(data) > MAX_MIRROR_CHUNK
        or len(data) % 4
        or not _B64_RE.match(data)
    ):
        _fail("bad data")
    return p


def _res_joint_ok(p, req, strict) -> dict:
    p = _keys(p, ("ok",), (), strict)
    if not isinstance(p["ok"], bool):
        _fail("bad ok")
    return p


def _res_joint_contract(p, req, strict) -> dict:
    p = _keys(p, ("ok", "rev"), (), strict)
    if not isinstance(p["ok"], bool):
        _fail("bad ok")
    if not _int(p["rev"], 0, MAX_JOINT_REV):
        _fail("bad rev")
    return p


def _joint_wire_view(j, strict) -> dict:
    """The joint as one side sees it (``joint_state``): halves by name only,
    never a local path, share id or session title."""
    j = _keys(
        j,
        (
            "id",
            "brief",
            "proposed_by",
            "created",
            "state",
            "mine",
            "theirs",
            "contract",
            "proposals",
        ),
        ("check",),
        strict,
    )
    _joint_id(j["id"])
    if not _str(j["brief"], 1, MAX_JOINT_BRIEF, _BAD_TEXT):
        _fail("bad brief")
    if j["proposed_by"] not in JOINT_BYS:
        _fail("bad proposed_by")
    _iso(j["created"], "created")
    if j["state"] not in JOINT_STATES:
        _fail("bad state")
    for half in ("mine", "theirs"):
        j[half] = _joint_half(j[half], strict, with_shared=True)
    c = _keys(j["contract"], ("text", "rev", "updated", "by"), (), strict)
    if not _str(c["text"], 0, MAX_JOINT_CONTRACT, _BAD_TEXT):
        _fail("bad contract text")
    if not _int(c["rev"], 0, MAX_JOINT_REV):
        _fail("bad contract rev")
    _iso(c["updated"], "updated")
    if c["by"] not in JOINT_BYS:
        _fail("bad contract by")
    j["contract"] = c
    props = j["proposals"]
    if not isinstance(props, list) or len(props) > MAX_JOINT_PROPOSALS:
        _fail("bad proposals")
    j["proposals"] = [_joint_proposal_obj(x, strict) for x in props]
    if "check" in j and j["check"] is not None:
        j["check"] = _joint_check_view(j["check"], strict)
    return j


def _joint_check_view(v, strict) -> dict:
    """The joint check inside a ``joint_state`` answer (sender's terms):
    ``{rev, by, steps, side_results}`` — an older peer drops the key."""
    v = _keys(v, ("rev", "by", "steps", "side_results"), (), strict)
    if not _int(v["rev"], 1, MAX_JOINT_REV):
        _fail("bad check rev")
    if v["by"] not in JOINT_BYS:
        _fail("bad check by")
    v["steps"] = _check_steps(v["steps"], strict)
    v["side_results"] = _check_side_results(v["side_results"], strict)
    return v


def _res_joint_state(p, req, strict) -> dict:
    p = _keys(p, ("joint",), (), strict)
    if p["joint"] is not None:
        p["joint"] = _joint_wire_view(p["joint"], strict)
    return p


def _session_item(v, strict) -> dict:
    """One session the sender shares: names and states only — the folder's
    NAME, never a path."""
    v = _keys(
        v,
        ("title", "name", "status", "activity", "stage", "branch", "folder"),
        ("mode", "program"),
        strict,
    )
    _session_title(v["title"])
    if not _str(v["name"], 0, MAX_SESSION_TITLE):
        _fail("bad name")
    for k in ("status", "stage"):
        if not _str(v[k], 0, 32):
            _fail("bad " + k)
    if not _str(v["activity"], 0, MAX_ACTIVITY):
        _fail("bad activity")
    if not _str(v["branch"], 0, 200):
        _fail("bad branch")
    if not _str(v["folder"], 0, MAX_NAME):
        _fail("bad folder")
    if "mode" in v and v["mode"] not in SESSION_MODES:
        _fail("bad mode")
    if "program" in v and not _str(v["program"], 0, 64):
        _fail("bad program")
    return v


def _res_sessions(p, req, strict) -> dict:
    p = _keys(p, ("sessions",), (), strict)
    items = p["sessions"]
    if not isinstance(items, list) or len(items) > MAX_SESSIONS:
        _fail("bad sessions")
    p["sessions"] = [_session_item(x, strict) for x in items]
    if len({x["title"] for x in p["sessions"]}) != len(p["sessions"]):
        _fail("duplicate session title")
    return p


def _res_term_open(p, req, strict) -> dict:
    p = _keys(p, ("ok", "term_id"), (), strict)
    if p["ok"] is not True:
        _fail("bad ok")
    _term_id(p["term_id"])
    return p


def _res_agent_msg(p, req, strict) -> dict:
    """``{ok: true, id}`` — ``id`` is the message's id in the receiver's
    mailbox."""
    p = _keys(p, ("ok", "id"), (), strict)
    if p["ok"] is not True:
        _fail("bad ok")
    if not _str(p["id"], 1, 64) or not _MSG_ID_RE.match(p["id"]):
        _fail("bad id")
    return p


def _res_chat_msg(p, req, strict) -> dict:
    """``{ok: true, id}`` — ``id`` is the line's id in the receiver's chat
    (``c<seq>``; its ``chat_out`` echo carries the same one)."""
    p = _keys(p, ("ok", "id"), (), strict)
    if p["ok"] is not True:
        _fail("bad ok")
    _chat_id(p["id"])
    return p


_RESPONSES = {
    "msg": _res_msg,
    "diff": _res_diff,
    "read_file": _res_read_file,
    "list_files": _res_list_files,
    "status": _res_status,
    "screen": _res_screen,
    "mirror_pull": _res_mirror_pull,
    "joint_propose": _res_joint_ok,
    "joint_accept": _res_joint_ok,
    "joint_cancel": _res_joint_ok,
    "joint_contract": _res_joint_contract,
    "joint_proposal": _res_joint_ok,
    "joint_state": _res_joint_state,
    "joint_check": _res_joint_contract,
    "sessions": _res_sessions,
    "term_open": _res_term_open,
    "term_close": _res_joint_ok,
    "session_focus": _res_joint_ok,
    "agent_msg": _res_agent_msg,
    "chat_msg": _res_chat_msg,
}


def validate_response(op, p, req: dict | None = None, strict: bool = True) -> dict:
    """Raise ProtocolError unless ``p`` is a valid ``ok:true`` payload for
    ``op`` (``req`` is the request payload, used to bound ``diff``); returns
    ``p`` (unknown keys dropped when not ``strict``)."""
    check = _RESPONSES.get(op) if isinstance(op, str) else None
    if check is None:
        _fail("unknown op")
    return check(p, req, strict)


# -- push frames ----------------------------------------------------------------


def _push_term_data(p, strict) -> dict:
    p = _keys(p, ("term_id", "data"), (), strict)
    _term_id(p["term_id"])
    _term_data(p["data"])
    return p


def _push_term_end(p, strict) -> dict:
    p = _keys(p, ("term_id", "reason"), (), strict)
    _term_id(p["term_id"])
    if not _str(p["reason"], 0, MAX_BYE):
        _fail("bad reason")
    return p


def _push_term_resize(p, strict) -> dict:
    p = _keys(p, ("term_id", "cols", "rows"), (), strict)
    _term_id(p["term_id"])
    _term_size(p)
    return p


def _push_chat_out(p, strict) -> dict:
    """``{title, msg: {id, from, name, text, ts}}`` — one line of the
    sender's chat on ``title``, a session it shares with the receiver.
    ``from`` is ``"you"`` on the receiver's own lines."""
    p = _keys(p, ("title", "msg"), (), strict)
    _session_title(p["title"])
    m = _keys(p["msg"], ("id", "from", "name", "text", "ts"), (), strict)
    _chat_id(m["id"])
    if not _str(m["from"], 0, MAX_CHAT_FROM):
        _fail("bad from")
    if not _str(m["name"], 0, MAX_CHAT_NAME):
        _fail("bad name")
    _chat_text(m["text"])
    ts = m["ts"]
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not 0 <= ts < 1e11:
        _fail("bad ts")
    p["msg"] = m
    return p


_PUSHES = {
    "term_out": _push_term_data,
    "term_in": _push_term_data,
    "term_end": _push_term_end,
    "term_resize": _push_term_resize,
    "sessions_changed": _req_empty,
    "chat_out": _push_chat_out,
}


def validate_push(op, p, strict: bool = True) -> dict:
    """Raise ProtocolError unless ``(op, p)`` is an allowed push; returns
    ``p`` (with unknown keys dropped when not ``strict``)."""
    check = _PUSHES.get(op) if isinstance(op, str) else None
    if check is None:
        _fail("unknown push")
    return check(p, strict)


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
    elif t == "push":
        _keys(obj, ("t", "op", "p"), (), strict=False)
        op = obj["op"]
        if not isinstance(op, str) or not _OP_NAME_RE.match(op):
            _fail("bad op")
        if op not in _PUSHES:
            raise UnsupportedPush(op)
        obj["p"] = validate_push(op, obj["p"], strict=False)
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
