"""Session chat (:mod:`backend.web.core.chat`, docs/people.md "A small chat"):
the store (ids, since, caps, stripping, forget on delete), the
``session.chat`` event, and ``/api/instances/{title}/chat`` for the owner
(a remote row 409, an unknown session 404). The guest's side is in
test_guest_links.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import chat


@pytest.fixture(autouse=True)
def _fresh():
    chat._reset()
    yield
    chat._reset()


def _events(name: str):
    seen: list = []
    unsub = server._events.BUS.subscribe(
        lambda e: seen.append(e) if e["event"] == name else None
    )
    return seen, unsub


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_post_and_messages_since():
    a = chat.post("alpha", {"id": "p1", "name": "Erik"}, "hi")
    b = chat.post("beta", {"id": "p1", "name": "Erik"}, "elsewhere")
    c = chat.post("alpha", {"id": "guest:g", "name": "Pat"}, "hello back")
    assert (a["id"], b["id"], c["id"]) == ("c1", "c2", "c3")
    assert a["from"] == "p1" and a["name"] == "Erik" and a["text"] == "hi"
    assert isinstance(a["ts"], float)
    assert [m["text"] for m in chat.messages("alpha")] == ["hi", "hello back"]
    assert [m["id"] for m in chat.messages("alpha", "c1")] == ["c3"]
    assert chat.messages("alpha", "c3") == []
    assert chat.messages("nobody") == []


def test_text_is_cleaned_capped_and_blank_refused():
    m = chat.post("t", {"id": "x", "name": ""}, "  a\x00b\x1b[31m\r\nline\x7f two\t \n")
    assert m["text"] == "ab[31m\nline two"
    assert m["name"] == "Someone"
    assert len(chat.post("t", {"id": "x"}, "y" * 5000)["text"]) == chat.MAX_TEXT
    for blank in ("", "   ", "\x00\x01\n", None):
        with pytest.raises(ValueError):
            chat.post("t", {"id": "x"}, blank)


def test_keeps_the_last_200():
    for i in range(chat.KEEP + 5):
        chat.post("t", {"id": "x"}, str(i))
    rows = chat.messages("t")
    assert len(rows) == chat.KEEP and rows[0]["text"] == "5"


def test_post_emits_session_chat():
    seen, unsub = _events("session.chat")
    try:
        m = chat.post("alpha", {"id": "peer:L1", "name": "Mauricio"}, "yo")
    finally:
        unsub()
    assert len(seen) == 1
    assert seen[0]["session"] == "alpha" and seen[0]["data"] == m


def test_a_deleted_session_forgets_its_chat():
    chat.subscribe()
    chat.post("alpha", {"id": "x"}, "one")
    chat.post("beta", {"id": "x"}, "two")
    server._events.BUS.emit("session.deleted", session="alpha")
    assert chat.messages("alpha") == []
    assert len(chat.messages("beta")) == 1
    chat.forget()
    assert chat.messages("beta") == []


def test_owner_identity_falls_back_to_the_device_then_owner(monkeypatch):
    from backend.web.core import person, remote

    monkeypatch.setattr(person, "current", lambda: {"id": "pid", "name": "Erik"})
    assert chat.owner_identity() == {"id": "pid", "name": "Erik"}
    monkeypatch.setattr(person, "current", lambda: {"id": "pid", "name": ""})
    monkeypatch.setattr(remote, "self_identity", lambda: {"host": "laptop"})
    assert chat.owner_identity() == {"id": "pid", "name": "laptop"}
    monkeypatch.setattr(remote, "self_identity", lambda: {"host": ""})
    assert chat.owner_identity() == {"id": "pid", "name": "Owner"}


# --------------------------------------------------------------------------- #
# the routes (owner)
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    from backend.web.core import person

    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setitem(server.ENGINE.instances, "alpha", object())
    monkeypatch.setattr(person, "current", lambda: {"id": "pid", "name": "Erik"})
    return TestClient(server.app)


def test_owner_reads_and_writes(client):
    r = client.get("/api/instances/alpha/chat")
    assert r.status_code == 200
    assert r.json() == {"messages": [], "me": {"id": "pid", "name": "Erik"}}
    seen, unsub = _events("session.chat")
    try:
        r = client.post("/api/instances/alpha/chat", json={"text": "hello"})
    finally:
        unsub()
    assert r.status_code == 201
    msg = r.json()
    assert msg["from"] == "pid" and msg["name"] == "Erik" and msg["text"] == "hello"
    assert [e["data"]["id"] for e in seen] == [msg["id"]]
    assert client.get("/api/instances/alpha/chat").json()["messages"] == [msg]
    assert (
        client.get(f"/api/instances/alpha/chat?since={msg['id']}").json()["messages"]
        == []
    )


def test_blank_is_400(client):
    r = client.post("/api/instances/alpha/chat", json={"text": "  "})
    assert r.status_code == 400
    assert client.post("/api/instances/alpha/chat", json={}).status_code == 400


def test_unknown_session_is_404_and_remote_row_409(client):
    from backend.web.routes import chat as routes

    assert client.get("/api/instances/ghost/chat").status_code == 404
    assert (
        client.post("/api/instances/ghost/chat", json={"text": "x"}).status_code == 404
    )
    # A <device>::<title> row is the device's: the remote middleware proxies
    # it there (502 while that device isn't connected) and the route itself
    # refuses one that slips through — a chat is never kept for it here.
    r = client.post("/api/instances/mac::alpha/chat", json={"text": "x"})
    assert r.status_code not in (200, 201)
    assert routes._refusal("mac::alpha").status_code == 409
    assert chat.messages("mac::alpha") == []
