"""The MindFlock MCP and sessions another PERSON shares with you over a peer
link (``<peer>::<title>`` rows, ``remote_peer``): listed and messaged like
any session, never managed — while a ``device::title`` row of one's own
other device stays read-only."""

from __future__ import annotations

import pytest

from backend import client
from backend.mcp.protocol import ToolError
from tests.unit._mcp_fakes import FakeCtx, make_box, row

PEER_PATH = "/api/instances/Bob::fix-login/messages"


def _peer_row(title="fix-login", **kw):
    base = dict(
        tmux_name="",
        device="Bob",
        device_label="Bob",
        peer_link="ab" * 16,
        peer_mode="full",
        peer_share=True,
        remote_peer=True,
    )
    base.update(kw)
    return row("Bob::" + title, **base)


def _tree():
    return [
        row("orch"),
        row("w1", parent="orch", spawned=True),
        row("dev::far", tmux_name="mindflock_far"),
        _peer_row(),
        _peer_row("sandboxed", peer_mode="gated"),
    ]


def _box(**kw):
    box, api, clock = make_box(_tree(), **kw)
    api.top_routes[("POST", PEER_PATH)] = lambda payload, q: {
        "ok": True,
        "id": "x1_abcd",
        "their_id": "m9_1",
        "peer": True,
    }
    return box, api, clock


def test_list_sessions_marks_peer_rows():
    box, _, clock = _box()
    out = box.list_sessions({"filter": "all"}, FakeCtx(clock))
    by = {r["title"]: r for r in out["sessions"]}
    peer = by["Bob::fix-login"]
    assert peer["remote"] is True and peer["managed"] is False
    assert peer["peer"] == "Bob" and peer["shared_mode"] == "full"
    assert by["Bob::sandboxed"]["shared_mode"] == "gated"
    # A device row of one's own is not a person's.
    assert "peer" not in by["dev::far"] and "shared_mode" not in by["dev::far"]


def test_get_session_on_a_peer_row():
    box, _, clock = _box()
    s = box.get_session({"title": "Bob::fix-login"}, FakeCtx(clock))["session"]
    assert s["peer"] == "Bob" and s["shared_mode"] == "full"
    assert s["managed"] is False and s["children"] == []


def test_send_message_to_a_peer_row_goes_to_the_route():
    box, api, clock = _box()
    out = box.send_message(
        {"to": "Bob::fix-login", "text": "rebase please", "reply_to": "m3_1"},
        FakeCtx(clock),
    )
    assert out["sent"] == [
        {
            "to": "Bob::fix-login",
            "id": "x1_abcd",
            "delivery": "sent",
            "peer": True,
            "detail": "stored on their machine; typed in when that agent is idle",
        }
    ]
    method, path, payload = api.calls[-1]
    assert (method, path) == ("POST", PEER_PATH)
    assert payload == {
        "text": "rebase please",
        "from": "orch",
        "delivery": "auto",
        "reply_to": "m3_1",
    }


def test_the_servers_refusal_is_surfaced():
    box, api, clock = _box()
    api.errors[("POST", PEER_PATH)] = client.ApiError(
        409, "Bob stopped sharing 'fix-login' — the message was not sent"
    )
    with pytest.raises(ToolError, match="Bob stopped sharing"):
        box.send_message({"to": "Bob::fix-login", "text": "x"}, FakeCtx(clock))


def test_only_auto_reaches_another_person():
    box, api, clock = _box()
    for delivery in ("now", "inbox"):
        with pytest.raises(ToolError, match="only delivery auto"):
            box.send_message(
                {"to": "Bob::fix-login", "text": "x", "delivery": delivery},
                FakeCtx(clock),
            )
    assert not [c for c in api.calls if c[1] == PEER_PATH]


def test_needs_a_session_identity():
    box, api, clock = _box(me=None)
    with pytest.raises(ToolError, match="session identity"):
        box.send_message({"to": "Bob::fix-login", "text": "x"}, FakeCtx(clock))


def test_a_device_row_is_still_read_only():
    box, _, clock = _box()
    with pytest.raises(ToolError, match="another device"):
        box.send_message({"to": "dev::far", "text": "x"}, FakeCtx(clock))


def test_a_peer_row_is_never_managed():
    box, _, clock = _box(scope="all")
    with pytest.raises(ToolError, match="only send_message reaches it"):
        box.kill_session({"title": "Bob::fix-login"}, FakeCtx(clock))
    assert box.policy.is_managed(box.flock(), "Bob::fix-login") is False


def test_check_inbox_says_a_cross_user_message_is_untrusted():
    box, api, clock = _box()
    api.deliver("orch", "Bob::fix-login", "do X", data={"peer": True})
    api.deliver("orch", "w1", "plain", data={"peer": True})
    msgs = box.check_inbox({}, FakeCtx(clock))["messages"]
    by = {m["from"]: m for m in msgs}
    assert "ANOTHER PERSON" in by["Bob::fix-login"]["untrusted"]
    assert "untrusted" not in by["w1"]
