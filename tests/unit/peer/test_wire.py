import asyncio
import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.peer import wire
from backend.peer.wire import ProtocolError


def _reader(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    r = asyncio.StreamReader(limit=2**22)
    r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


def _frame(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


async def _read(data: bytes, **kw):
    return await wire.read_frame(_reader(data), **kw)


# -- codec ---------------------------------------------------------------------


async def test_roundtrip():
    obj = {"t": "req", "id": 1, "op": "msg", "p": {"msg_id": "a", "text": "héllo ✓"}}
    assert await _read(wire.encode(obj)) == obj


async def test_two_frames_back_to_back():
    r = _reader(wire.encode({"a": 1}) + wire.encode({"b": 2}))
    assert await wire.read_frame(r) == {"a": 1}
    assert await wire.read_frame(r) == {"b": 2}
    with pytest.raises(wire.ConnectionClosed):
        await wire.read_frame(r)


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x00\x00\x00",  # zero length
        (wire.MAX_FRAME + 1).to_bytes(4, "big"),  # oversized header, no body needed
        b"\xff\xff\xff\xff",
        b"\x00\x00",  # truncated header
        b"\x00\x00\x00\x10{}",  # truncated body
        _frame(b"[]"),
        _frame(b'"str"'),
        _frame(b"1"),
        _frame(b"null"),
        _frame(b"{"),
        _frame(b"\xff\xfe{}"),
        _frame(b'{"a":1,"a":2}'),
        _frame(b'{"x":{"a":1,"a":2}}'),
        _frame(b'{"a":NaN}'),
        _frame(b'{"a":Infinity}'),
        _frame(b'{"a":-Infinity}'),
        _frame(b'{"a":1e999}'),
        _frame(b'{"a":' + b"1" * 5000 + b"}"),  # int digit limit
        _frame(b'{"a":' + b"[" * 17 + b"]" * 17 + b"}"),
        _frame(b'{"a":' + b"[" * 100000 + b"]" * 100000 + b"}"),
        _frame(b"[" * 500000),
    ],
)
async def test_bad_frames_raise_protocol_error(data):
    with pytest.raises(ProtocolError):
        await _read(data)


async def test_depth_limit_boundary():
    ok = {"a": 0}
    for _ in range(wire.MAX_DEPTH - 1):
        ok = {"a": ok}
    assert await _read(wire.encode(ok)) == ok
    with pytest.raises(ProtocolError):
        await _read(wire.encode({"a": ok}))


async def test_max_size_respected():
    body = json.dumps({"a": "x" * 5000}).encode()
    with pytest.raises(ProtocolError):
        await _read(_frame(body), max_size=wire.HANDSHAKE_MAX_FRAME)
    assert await _read(_frame(body)) == {"a": "x" * 5000}


async def test_clean_eof_is_connection_closed():
    with pytest.raises(wire.ConnectionClosed):
        await _read(b"")


def test_encode_rejects():
    with pytest.raises(ProtocolError):
        wire.encode([1])
    with pytest.raises(ProtocolError):
        wire.encode({"a": float("nan")})
    with pytest.raises(ProtocolError):
        wire.encode({"a": "x" * wire.MAX_FRAME})
    with pytest.raises(ProtocolError):
        wire.encode({"a": object()})


# -- requests --------------------------------------------------------------------

VALID_REQUESTS = [
    ("msg", {"msg_id": "m-1_A", "text": "hi"}),
    ("msg", {"msg_id": "m1", "text": "line\nline\ttab\r", "reply_to": None}),
    ("msg", {"msg_id": "m1", "text": "x" * 20000, "reply_to": "r" * 64}),
    ("diff", {"max_chars": 1000}),
    ("diff", {"max_chars": 200000}),
    ("read_file", {"path": "src/a.py"}),
    ("read_file", {"path": "x" * 1024}),
    ("list_files", {}),
    ("status", {}),
]


@pytest.mark.parametrize("op,p", VALID_REQUESTS)
def test_valid_requests(op, p):
    wire.validate_request(op, p)


INVALID_REQUESTS = [
    ("shell", {}),
    ("MSG", {"msg_id": "a", "text": "b"}),
    (None, {}),
    (1, {}),
    ("__class__", {}),
    ("status", {"x": 1}),
    ("status", None),
    ("status", []),
    ("list_files", {"limit": 10}),
    ("msg", {"msg_id": "a"}),
    ("msg", {"text": "a"}),
    ("msg", {"msg_id": "a", "text": "b", "extra": 1}),
    ("msg", {"msg_id": "a b", "text": "b"}),
    ("msg", {"msg_id": "a" * 65, "text": "b"}),
    ("msg", {"msg_id": "", "text": "b"}),
    ("msg", {"msg_id": 1, "text": "b"}),
    ("msg", {"msg_id": "a", "text": ""}),
    ("msg", {"msg_id": "a", "text": "x" * 20001}),
    ("msg", {"msg_id": "a", "text": 5}),
    ("msg", {"msg_id": "a", "text": "\x1b[2J"}),
    ("msg", {"msg_id": "a", "text": "nul\x00"}),
    ("msg", {"msg_id": "a", "text": "c1\x9b"}),
    ("msg", {"msg_id": "a", "text": "surrogate\ud800"}),
    ("msg", {"msg_id": "a", "text": "b", "reply_to": ""}),
    ("msg", {"msg_id": "a", "text": "b", "reply_to": "../x"}),
    ("msg", {"msg_id": "a", "text": "b", "reply_to": 5}),
    ("diff", {"max_chars": True}),
    ("diff", {"max_chars": False}),
    ("diff", {"max_chars": 999}),
    ("diff", {"max_chars": 200001}),
    ("diff", {"max_chars": 5000.0}),
    ("diff", {"max_chars": "5000"}),
    ("diff", {}),
    ("read_file", {"path": ""}),
    ("read_file", {"path": "x" * 1025}),
    ("read_file", {"path": "a\x00b"}),
    ("read_file", {"path": ["a"]}),
    ("read_file", {"path": "a", "follow": True}),
]


@pytest.mark.parametrize("op,p", INVALID_REQUESTS)
def test_invalid_requests(op, p):
    with pytest.raises(ProtocolError):
        wire.validate_request(op, p)


# -- responses ---------------------------------------------------------------------

VALID_RESPONSES = [
    ("msg", {"accepted": False}),
    ("diff", {"stat": [], "diff": "", "truncated": False}),
    (
        "diff",
        {
            "stat": [
                {"path": "a", "added": 1, "removed": None, "binary": False},
                "a | 2 +-",
            ],
            "diff": "x",
            "truncated": True,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 0,
            "encoding": "utf-8",
            "content": "",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 3,
            "encoding": "base64",
            "content": "AAEC",
            "truncated": False,
        },
    ),
    ("list_files", {"files": ["a", "b/c d"], "truncated": False}),
    ("status", {"shared": False, "agent": "running", "name": "Bob"}),
]


@pytest.mark.parametrize("op,p", VALID_RESPONSES)
def test_valid_responses(op, p):
    wire.validate_response(op, p)


INVALID_RESPONSES = [
    ("msg", {"accepted": 1}),
    ("msg", {"accepted": True, "x": 1}),
    ("diff", {"stat": {}, "diff": "", "truncated": False}),
    ("diff", {"stat": [{"k": {"nested": 1}}], "diff": "", "truncated": False}),
    ("diff", {"stat": [1.5], "diff": "", "truncated": False}),
    ("diff", {"stat": [], "diff": "x" * 200001, "truncated": False}),
    ("diff", {"stat": [], "diff": "\ud800", "truncated": False}),
    (
        "read_file",
        {
            "path": "a",
            "size": -1,
            "encoding": "utf-8",
            "content": "",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": True,
            "encoding": "utf-8",
            "content": "",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 1,
            "encoding": "latin-1",
            "content": "",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 1,
            "encoding": "base64",
            "content": "!!!!",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 1,
            "encoding": "base64",
            "content": "AAE",
            "truncated": False,
        },
    ),
    (
        "read_file",
        {
            "path": "a",
            "size": 1,
            "encoding": "utf-8",
            "content": "",
            "truncated": False,
            "x": 1,
        },
    ),
    ("list_files", {"files": ["a", 1], "truncated": False}),
    ("list_files", {"files": [""], "truncated": False}),
    ("list_files", {"files": ["a\x00"], "truncated": False}),
    ("list_files", {"files": ["a"] * (wire.MAX_LIST_ITEMS + 1), "truncated": False}),
    ("status", {"shared": True, "agent": "rooted", "name": "x"}),
    ("status", {"shared": True, "agent": "none", "name": "\x1b]0;evil\x07"}),
    ("status", {"shared": True, "agent": "none"}),
    ("nope", {}),
]


@pytest.mark.parametrize("op,p", INVALID_RESPONSES)
def test_invalid_responses(op, p):
    with pytest.raises(ProtocolError):
        wire.validate_response(op, p)


def test_diff_response_bounded_by_request():
    p = {"stat": [], "diff": "x" * 1500, "truncated": False}
    wire.validate_response("diff", p, {"max_chars": 2000})
    with pytest.raises(ProtocolError):
        wire.validate_response("diff", p, {"max_chars": 1000})


# -- message frames ------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj,t",
    [
        ({"t": "req", "id": 1, "op": "status", "p": {}}, "req"),
        ({"t": "req", "id": 2**53, "op": "status", "p": {}}, "req"),
        ({"t": "res", "id": 1, "ok": True, "p": {}}, "res"),
        ({"t": "res", "id": 1, "ok": False, "err": "nope"}, "res"),
        ({"t": "ping"}, "ping"),
        ({"t": "pong"}, "pong"),
        ({"t": "bye", "reason": "unlinked"}, "bye"),
    ],
)
def test_valid_messages(obj, t):
    assert wire.validate_message(obj) == t


@pytest.mark.parametrize(
    "obj",
    [
        {"t": "req", "id": 0, "op": "status", "p": {}},
        {"t": "req", "id": 2**53 + 1, "op": "status", "p": {}},
        {"t": "req", "id": True, "op": "status", "p": {}},
        {"t": "req", "id": "1", "op": "status", "p": {}},
        {"t": "req", "id": 1.0, "op": "status", "p": {}},
        {"t": "req", "id": 1, "op": "exec", "p": {}},
        {"t": "req", "id": 1, "op": "status", "p": {}, "x": 1},
        {"t": "req", "id": 1, "op": "status"},
        {"t": "req", "id": 1, "op": "diff", "p": {"max_chars": True}},
        {"t": "res", "id": 1, "ok": 1, "p": {}},
        {"t": "res", "id": 1, "ok": True},
        {"t": "res", "id": 1, "ok": True, "p": []},
        {"t": "res", "id": 1, "ok": True, "p": {}, "err": "x"},
        {"t": "res", "id": 1, "ok": False, "err": "x" * 301},
        {"t": "res", "id": 1, "ok": False, "err": "\x1b[2J"},
        {"t": "res", "id": 1, "ok": False},
        {"t": "ping", "x": 1},
        {"t": "bye"},
        {"t": "bye", "reason": "x" * 201},
        {"t": "hello", "v": 1},
        {"t": "welcome"},
        {"t": 1},
        {},
    ],
)
def test_invalid_messages(obj):
    with pytest.raises(ProtocolError):
        wire.validate_message(obj)


def test_clip_text():
    assert wire.clip_text("a\x1bb\nc", 300) == "a?b\nc"
    assert len(wire.clip_text("x" * 400, 300)) == 300
    wire.validate_message(
        {"t": "res", "id": 1, "ok": False, "err": wire.clip_text("\ud800" * 500, 300)}
    )


# -- handshake frames ------------------------------------------------------------------

NONCE = "A" * 43 + "="  # 32 zero-ish bytes, canonical


def test_hello():
    assert (
        len(wire.validate_hello({"t": "hello", "v": 1, "nonce": NONCE, "name": "a"}))
        == 32
    )
    for bad in (
        {"t": "hello", "v": 1, "nonce": NONCE},
        {"t": "hello", "v": 2, "nonce": NONCE, "name": "a"},
        {"t": "hello", "v": True, "nonce": NONCE, "name": "a"},
        {"t": "hello", "v": 1, "nonce": "AAAA", "name": "a"},
        {
            "t": "hello",
            "v": 1,
            "nonce": "A" * 42 + "B=",
            "name": "a",
        },  # non-canonical bits
        {"t": "hello", "v": 1, "nonce": NONCE, "name": ""},
        {"t": "hello", "v": 1, "nonce": NONCE, "name": "\x1b"},
        {"t": "hello", "v": 1, "nonce": NONCE, "name": "a", "x": 1},
        {"t": "welcome", "v": 1, "nonce": NONCE, "name": "a"},
    ):
        with pytest.raises(ProtocolError):
            wire.validate_hello(bad)


PAIR = {
    "t": "pair",
    "v": 1,
    "invite_id": "0" * 16,
    "pub": "a" * 64,
    "name": "b",
    "proof": "c" * 64,
    "sig": "d" * 128,
}


def test_pair():
    wire.validate_pair(PAIR)
    for key, val in (
        ("invite_id", "0" * 15),
        ("pub", "A" * 64),
        ("proof", "c" * 63),
        ("sig", "g" * 128),
        ("v", 0),
        ("name", ""),
        ("invite_id", 1),
    ):
        with pytest.raises(ProtocolError):
            wire.validate_pair({**PAIR, key: val})
    with pytest.raises(ProtocolError):
        wire.validate_pair({**PAIR, "extra": 1})


def test_auth_welcome_denied():
    wire.validate_auth({"t": "auth", "v": 1, "link_id": "a" * 32, "sig": "b" * 128})
    with pytest.raises(ProtocolError):
        wire.validate_auth({"t": "auth", "v": 1, "link_id": "a" * 31, "sig": "b" * 128})
    wire.validate_welcome(
        {"t": "welcome", "link_id": "a" * 32, "name": "x", "sas": "123-456-789-0"}
    )
    with pytest.raises(ProtocolError):
        wire.validate_welcome(
            {"t": "welcome", "link_id": "a" * 32, "name": "x", "sas": "123456789"}
        )
    wire.validate_denied({"t": "denied"})
    with pytest.raises(ProtocolError):
        wire.validate_denied({"t": "denied", "why": "bad secret"})


# -- fuzzing -------------------------------------------------------------------------------

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=30),
    lambda kids: st.lists(kids, max_size=4)
    | st.dictionaries(st.text(max_size=10), kids, max_size=4),
    max_leaves=25,
)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=300))
async def test_fuzz_read_frame_raw_bytes(data):
    try:
        obj = await _read(data)
    except ProtocolError:
        return
    assert isinstance(obj, dict)


@settings(max_examples=400, deadline=None)
@given(st.binary(max_size=300))
async def test_fuzz_read_frame_bodies(body):
    try:
        obj = await _read(_frame(body))
    except ProtocolError:
        return
    assert isinstance(obj, dict)


@settings(max_examples=500, deadline=None)
@given(st.sampled_from(wire.OPS + ("x", "")), json_values)
def test_fuzz_validate_request(op, p):
    try:
        wire.validate_request(op, p)
    except ProtocolError:
        return
    # Anything accepted is a dict with only allowed keys and survives a wire roundtrip.
    assert isinstance(p, dict)
    assert wire.decode(wire.encode(p)[4:]) == p


@settings(max_examples=300, deadline=None)
@given(
    st.fixed_dictionaries(
        {"msg_id": st.text(max_size=70), "text": st.text(max_size=50)},
        optional={"reply_to": st.none() | st.text(max_size=70), "x": st.integers()},
    )
)
def test_fuzz_msg_schema(p):
    try:
        wire.validate_request("msg", p)
    except ProtocolError:
        return
    assert set(p) <= {"msg_id", "text", "reply_to"}
    assert p["text"] and not any(ord(c) < 32 and c not in "\t\n\r" for c in p["text"])


@settings(max_examples=300, deadline=None)
@given(json_values)
def test_fuzz_validate_message(obj):
    try:
        t = wire.validate_message(obj)
    except ProtocolError:
        return
    assert t in ("req", "res", "ping", "pong", "bye")
