"""Shared sessions on the wire (docs/peer-link.md "Shared sessions —
CONTRACT"): the request/response validators, the push frame, and the
tolerant-inbound / strict-outbound rules for both."""

from __future__ import annotations

import base64

import pytest

from backend.peer import wire

TID = "0123456789abcdef"
B64 = base64.b64encode(b"hello").decode()


def item(**kw):
    base = {
        "title": "api-fix",
        "name": "API fix",
        "status": "running",
        "activity": "working",
        "stage": "agent",
        "branch": "feature/api",
        "folder": "app",
        "mode": "full",
        "program": "claude",
    }
    base.update(kw)
    return base


def test_new_ops_are_on_the_allow_list():
    for op in ("sessions", "term_open", "term_close", "session_focus"):
        assert op in wire.OPS
    assert set(wire.PUSH_OPS) == {
        "term_out",
        "term_end",
        "term_in",
        "term_resize",
        "sessions_changed",
    }
    assert "sessions" in wire.CAPS


@pytest.mark.parametrize(
    "op,p",
    [
        ("sessions", {}),
        ("term_open", {"title": "api-fix", "cols": 80, "rows": 24}),
        ("term_close", {"term_id": TID}),
        ("session_focus", {"title": "api-fix"}),
        ("session_focus", {"title": ""}),  # their page moved off
    ],
)
def test_requests_accepted(op, p):
    assert wire.validate_request(op, dict(p)) == p


@pytest.mark.parametrize(
    "op,p",
    [
        ("term_open", {"title": "", "cols": 80, "rows": 24}),
        ("term_open", {"title": "x" * 201, "cols": 80, "rows": 24}),
        ("term_open", {"title": "a\nb", "cols": 80, "rows": 24}),
        ("term_open", {"title": "a", "cols": 0, "rows": 24}),
        ("term_open", {"title": "a", "cols": 80, "rows": 1001}),
        ("term_open", {"title": "a", "cols": True, "rows": 24}),
        ("term_open", {"title": "a", "cols": 80}),
        ("term_close", {"term_id": "XYZ"}),
        ("term_close", {"term_id": TID.upper()}),
        ("session_focus", {"title": "\x1b[2J"}),
        ("sessions", {"extra": 1}),
    ],
)
def test_requests_refused(op, p):
    with pytest.raises(wire.ProtocolError):
        wire.validate_request(op, p)


def test_sessions_response():
    p = {"sessions": [item(), item(title="b", mode="gated")]}
    assert wire.validate_response("sessions", p)["sessions"][1]["mode"] == "gated"


@pytest.mark.parametrize(
    "bad",
    [
        item(mode="root"),
        item(title=""),
        item(folder="/home/me/app\x07"),
        item(activity="x" * 201),
        item(status=7),
    ],
)
def test_sessions_response_refuses_bad_items(bad):
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("sessions", {"sessions": [bad]})


def test_sessions_response_bounds_and_dupes():
    with pytest.raises(wire.ProtocolError):
        wire.validate_response(
            "sessions", {"sessions": [item(title=str(i)) for i in range(51)]}
        )
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("sessions", {"sessions": [item(), item()]})


def test_sessions_response_tolerant_inbound_strict_outbound():
    newer = {"sessions": [item(colour="blue")]}
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("sessions", newer)
    got = wire.validate_response("sessions", newer, strict=False)
    assert "colour" not in got["sessions"][0]
    # mode / program are optional: a lean sharer may leave them out.
    lean = item()
    del lean["mode"], lean["program"]
    assert wire.validate_response("sessions", {"sessions": [lean]})


def test_term_open_response():
    assert wire.validate_response("term_open", {"ok": True, "term_id": TID})
    for bad in ({"ok": False, "term_id": TID}, {"ok": True, "term_id": "nope"}):
        with pytest.raises(wire.ProtocolError):
            wire.validate_response("term_open", bad)


@pytest.mark.parametrize(
    "op,p",
    [
        ("term_out", {"term_id": TID, "data": B64}),
        ("term_in", {"term_id": TID, "data": B64}),
        ("term_end", {"term_id": TID, "reason": "withdrawn"}),
        ("term_resize", {"term_id": TID, "cols": 120, "rows": 40}),
        ("sessions_changed", {}),
    ],
)
def test_push_frames(op, p):
    frame = {"t": "push", "op": op, "p": dict(p)}
    assert wire.validate_message(frame) == "push"
    assert frame["p"] == p
    assert wire.validate_push(op, dict(p)) == p


@pytest.mark.parametrize(
    "p",
    [
        {"term_id": TID, "data": ""},
        {"term_id": TID, "data": "not base64!"},
        {"term_id": TID, "data": "abc"},  # not a multiple of 4
        {"term_id": TID, "data": "A" * (wire.MAX_TERM_B64 + 4)},
        {"term_id": "x", "data": B64},
    ],
)
def test_push_data_refused(p):
    with pytest.raises(wire.ProtocolError):
        wire.validate_message({"t": "push", "op": "term_out", "p": p})


def test_full_chunk_fits():
    data = base64.b64encode(b"\x00" * wire.MAX_TERM_CHUNK).decode()
    assert len(data) <= wire.MAX_TERM_B64
    wire.validate_push("term_out", {"term_id": TID, "data": data})


def test_unknown_push_is_not_fatal_but_bad_names_are():
    with pytest.raises(wire.UnsupportedPush) as exc:
        wire.validate_message({"t": "push", "op": "term_paste", "p": {}})
    assert exc.value.op == "term_paste"
    with pytest.raises(wire.ProtocolError) as exc:
        wire.validate_message({"t": "push", "op": "Bad Op", "p": {}})
    assert not isinstance(exc.value, wire.UnsupportedPush)


def test_push_tolerant_inbound_strict_outbound():
    frame = {
        "t": "push",
        "op": "term_in",
        "p": {"term_id": TID, "data": B64, "seq": 4},
        "x": 1,
    }
    assert wire.validate_message(frame) == "push"
    assert frame["p"] == {"term_id": TID, "data": B64}
    with pytest.raises(wire.ProtocolError):
        wire.validate_push("term_in", {"term_id": TID, "data": B64, "seq": 4})


def test_encode_round_trip():
    frame = {"t": "push", "op": "term_out", "p": {"term_id": TID, "data": B64}}
    body = wire.encode(frame)[4:]
    assert wire.decode(body) == frame
