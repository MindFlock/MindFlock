"""The session chat across a peer link (docs/people.md "A small chat";
backend.peer.chat_bridge): the sharer takes the peer's line into its chat
and pushes every line of a shared session's chat to the links it is shared
on; the opener keeps a copy under the ``<peer>::<title>`` row, sends over
the link, and answers ``/api/instances/<peer>::<title>/chat``."""

from __future__ import annotations

import asyncio

import pytest

from backend.peer import chat_bridge, transport
from backend.peer.service import PeerServiceError
from backend.web.core import chat
from tests.unit.peer.test_integration_routes import link
from tests.unit.test_peer_sessions import (  # noqa: F401 — fixtures
    LID,
    bus,
    client,
    engine,
    item,
    make_svc,
    peer_home,
    run,
    svc,
    theirs,
)

OTHER = "cd" * 16
ME = {"id": "me-1", "name": "Alice"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    chat._reset()
    chat_bridge._reset()
    monkeypatch.setattr(chat, "owner_identity", lambda: dict(ME))
    yield
    chat._reset()
    chat_bridge._reset()


def speaks_chat(svc, lids=(LID,)):
    svc.transport.peer_info = lambda lid: (
        {"app": "mindflock", "caps": ["sessions", "chat_msg"]} if lid in lids else {}
    )


def chat_events(bus):
    return [(kw["session"], kw["data"]) for kw in bus.named("session.chat")]


# --------------------------------------------------------------------------- #
# The sharer
# --------------------------------------------------------------------------- #
def test_inbound_chat_posts_as_the_peer(svc, engine, bus):
    svc.shared_sessions.add(LID, "api-fix", "full")
    lnk = svc.store.get(LID)
    out = chat_bridge.inbound_chat(
        svc, lnk, {"title": "api-fix", "text": "hi there", "from_name": "Pat"}
    )
    assert out == {"ok": True, "id": "c1"}
    [m] = chat.messages("api-fix")
    assert m["from"] == "peer:" + LID and m["name"] == "Pat" and m["text"] == "hi there"
    assert chat_events(bus) == [("api-fix", m)]
    # No name of its own: the link's name for the peer.
    chat_bridge.inbound_chat(
        svc, lnk, {"title": "api-fix", "text": "x", "from_name": ""}
    )
    assert chat.messages("api-fix")[-1]["name"] == "Bob"


def test_inbound_chat_refuses_what_is_not_shared(svc, engine, bus):
    lnk = svc.store.get(LID)
    with pytest.raises(transport.PeerOpError, match="not shared"):
        chat_bridge.inbound_chat(
            svc, lnk, {"title": "api-fix", "text": "hi", "from_name": ""}
        )
    svc.shared_sessions.add(LID, "api-fix", "full")
    with pytest.raises(transport.PeerOpError, match="not shared"):
        chat_bridge.inbound_chat(
            svc, lnk, {"title": "docs", "text": "hi", "from_name": ""}
        )
    assert chat.messages("api-fix") == [] and chat.messages("docs") == []


def test_fan_out_reaches_the_links_it_is_shared_on_only(monkeypatch, engine, bus):
    third = "ef" * 16
    svc = make_svc(
        monkeypatch, link(), link(OTHER, peer_name="Cat"), link(third, peer_name="Dan")
    )
    svc.shared_sessions.add(LID, "api-fix", "full")
    svc.shared_sessions.add(third, "api-fix", "full")
    svc.shared_sessions.add(OTHER, "docs", "full")
    speaks_chat(svc, (LID, OTHER))  # Dan's MindFlock is older: no chat_out
    msg = chat.post("api-fix", {"id": "peer:" + LID, "name": "Bob"}, "hello")
    assert run(chat_bridge.fan_out(svc, "api-fix", msg)) == 1
    [(lid, op, p)] = svc.transport.pushes
    assert (lid, op, p["title"]) == (LID, "chat_out", "api-fix")
    # Bob's own line goes back to Bob as "you" (link ids are per side).
    assert p["msg"] == {**msg, "from": "you"}

    svc.transport.pushes.clear()
    mine = chat.post("docs", ME, "for Cat")
    run(chat_bridge.fan_out(svc, "docs", mine))
    assert [(lid, p["msg"]) for lid, _o, p in svc.transport.pushes] == [(OTHER, mine)]
    # Nobody gets a line while disconnected; a peer row's copy never fans out.
    svc.transport.pushes.clear()
    svc.transport.connected = False
    assert run(chat_bridge.fan_out(svc, "api-fix", msg)) == 0
    svc.transport.connected = True
    assert run(chat_bridge.fan_out(svc, "Bob::api-fix", msg)) == 0
    assert svc.transport.pushes == []


def test_install_fans_out_from_the_bus(svc, engine, monkeypatch):
    from backend.web.core import events

    monkeypatch.setattr(events, "BUS", events.EventBus())
    svc.shared_sessions.add(LID, "api-fix", "full")
    speaks_chat(svc)

    async def main():
        chat_bridge.install(svc)
        chat_bridge.install(svc)  # idempotent: one subscription
        chat.post("api-fix", ME, "from the bus")
        for _ in range(5):
            await asyncio.sleep(0)

    run(main())
    assert [(op, p["msg"]["text"]) for _l, op, p in svc.transport.pushes] == [
        ("chat_out", "from the bus")
    ]


# --------------------------------------------------------------------------- #
# The opener
# --------------------------------------------------------------------------- #
def line(**kw):
    base = {"id": "c7", "from": "owner", "name": "Bob", "text": "hey", "ts": 12.5}
    base.update(kw)
    return base


def test_chat_out_becomes_a_session_chat_on_the_row(theirs, bus):
    out = run(chat_bridge.inbound_out(theirs, LID, {"title": "api-fix", "msg": line()}))
    assert out is not None
    [(session, data)] = chat_events(bus)
    assert session == "Bob::api-fix"
    # As sent, except whose line it is: theirs, in this side's terms.
    assert data == {**line(), "from": "peer:%s:owner" % LID}
    # My own line echoed back is mine here.
    run(
        chat_bridge.inbound_out(
            theirs, LID, {"title": "api-fix", "msg": line(id="c8", **{"from": "you"})}
        )
    )
    assert chat.messages("Bob::api-fix")[-1]["from"] == ME["id"]
    # The same line again (a re-push) is no new event.
    run(chat_bridge.inbound_out(theirs, LID, {"title": "api-fix", "msg": line()}))
    assert len(chat_events(bus)) == 2


def test_chat_out_for_a_session_not_shared_with_me_is_dropped(theirs, bus):
    run(chat_bridge.inbound_out(theirs, LID, {"title": "nope", "msg": line()}))
    run(chat_bridge.inbound_out(theirs, OTHER, {"title": "api-fix", "msg": line()}))
    assert chat_events(bus) == []


def test_lines_arrive_in_id_order(theirs, bus):
    for i in (3, 1, 2):
        run(
            chat_bridge.inbound_out(
                theirs, LID, {"title": "api-fix", "msg": line(id="c%d" % i)}
            )
        )
    rows = chat.messages("Bob::api-fix")
    assert [m["id"] for m in rows] == ["c1", "c2", "c3"]
    assert [m["id"] for m in chat.messages("Bob::api-fix", "c1")] == ["c2", "c3"]


def test_send_goes_over_the_link_and_is_kept(theirs, bus):
    theirs.transport.answers["chat_msg"] = {"ok": True, "id": "c9"}
    msg = run(chat_bridge.send(theirs, LID, "api-fix", "  hello Bob \n"))
    assert msg["id"] == "c9" and msg["from"] == ME["id"] and msg["text"] == "hello Bob"
    p = [p for _l, op, p in theirs.transport.requests if op == "chat_msg"][-1]
    assert p == {"title": "api-fix", "text": "hello Bob", "from_name": "Alice"}
    assert chat.messages("Bob::api-fix") == [msg]
    # Its echo is the same line: kept once, no second event.
    run(
        chat_bridge.inbound_out(
            theirs,
            LID,
            {"title": "api-fix", "msg": {**msg, "from": "you", "name": "Alice"}},
        )
    )
    assert len(chat.messages("Bob::api-fix")) == 1
    assert len(chat_events(bus)) == 1


def test_send_refusals(theirs):
    with pytest.raises(PeerServiceError) as exc:
        run(chat_bridge.send(theirs, LID, "api-fix", "  "))
    assert exc.value.status == 400
    with pytest.raises(PeerServiceError) as exc:
        run(chat_bridge.send(theirs, LID, "nope", "hi"))
    assert exc.value.status == 409 and "doesn't share" in exc.value.message
    theirs.transport.answers["chat_msg"] = transport.PeerOpError("not shared")
    with pytest.raises(PeerServiceError) as exc:
        run(chat_bridge.send(theirs, LID, "api-fix", "hi"))
    assert exc.value.status == 409 and "stopped sharing" in exc.value.message
    theirs.transport.answers["chat_msg"] = transport.PeerOpError("unsupported op")
    with pytest.raises(PeerServiceError) as exc:
        run(chat_bridge.send(theirs, LID, "api-fix", "hi"))
    assert exc.value.status == 409 and "update" in exc.value.message
    theirs.transport.connected = False
    with pytest.raises(PeerServiceError) as exc:
        run(chat_bridge.send(theirs, LID, "api-fix", "hi"))
    assert exc.value.status == 503
    assert chat.messages("Bob::api-fix") == []


def test_copies_are_forgotten_when_the_link_drops(theirs, bus):
    run(chat_bridge.inbound_out(theirs, LID, {"title": "api-fix", "msg": line()}))
    chat_bridge._on_event(
        {"event": "peer.state", "data": {"link_id": LID, "connected": True}}
    )
    assert chat.messages("Bob::api-fix")
    chat_bridge._on_event(
        {"event": "peer.state", "data": {"link_id": LID, "connected": False}}
    )
    assert chat.messages("Bob::api-fix") == []
    # …and when they stop sharing that one session.
    run(chat_bridge.inbound_out(theirs, LID, {"title": "api-fix", "msg": line()}))
    chat_bridge._on_event(
        {
            "event": "peer.session_unshared",
            "data": {"link_id": LID, "title": "api-fix", "theirs": True},
        }
    )
    assert chat.messages("Bob::api-fix") == []


# --------------------------------------------------------------------------- #
# Routes: /api/instances/<peer>::<title>/chat
# --------------------------------------------------------------------------- #
def test_get_chat_on_a_peer_row(theirs, client, bus):
    run(chat_bridge.inbound_out(theirs, LID, {"title": "api-fix", "msg": line()}))
    r = client.get("/api/instances/Bob::api-fix/chat")
    assert r.status_code == 200
    body = r.json()
    assert body["me"] == ME and [m["id"] for m in body["messages"]] == ["c7"]
    r = client.get("/api/instances/Bob::api-fix/chat?since=c7")
    assert r.json()["messages"] == []
    assert client.get("/api/instances/Bob::nope/chat").status_code == 409


def test_post_chat_on_a_peer_row(theirs, client, bus):
    theirs.transport.answers["chat_msg"] = {"ok": True, "id": "c3"}
    r = client.post("/api/instances/Bob::api-fix/chat", json={"text": "hi Bob"})
    assert r.status_code == 202
    assert r.json()["id"] == "c3" and r.json()["from"] == ME["id"]
    assert (
        client.post("/api/instances/Bob::api-fix/chat", json={"text": " "}).status_code
        == 400
    )
    r = client.post("/api/instances/Bob::nope/chat", json={"text": "x"})
    assert r.status_code == 409


def test_chat_on_a_peer_row_offline(theirs, client, monkeypatch):
    theirs.transport.connected = False
    monkeypatch.setattr(theirs, "row_prefixes", lambda: {"Bob": LID})
    r = client.post("/api/instances/Bob::api-fix/chat", json={"text": "x"})
    assert r.status_code == 503 and "isn't connected" in r.json()["error"]
    assert client.get("/api/instances/Bob::api-fix/chat").status_code == 503
