"""Owner, driver and viewers (:mod:`backend.web.core.ownership`,
docs/people.md "Owner, driver, viewers").

The store and its rules (take / release / idle release / last-terminal
release), the read-only terminal websocket (input from a non-driver dropped
and answered with a notice, terminal replies always passing), the 409 on the
typed-into-terminal HTTP routes and its ``take`` override, the driver and
owner routes, presence rows and their events, and the MCP row / whoami
fields.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import events as events_mod
from backend.web.core import ownership as own
from backend.web.core import person_sessions
from backend.web.core.terminal import is_terminal_reply
from tests.unit.test_mailbox_routes import _mk_inst

#: The module's own naming, before the fixtures stub it with NAMES.
_NAME_OF = own.name_of

A = "00000000-0000-4000-8000-00000000000a"
B = "ffffffff-ffff-4fff-8fff-fffffffffffb"
NAMES = {A: "Alice", B: "Bob"}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clk(monkeypatch, tmp_path):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    c = Clock()
    own._reset_for_tests()
    monkeypatch.setattr(own, "clock", c)
    monkeypatch.setattr(own, "_path", lambda: str(tmp_path / "owners.json"))
    monkeypatch.setattr(own, "name_of", lambda pid: NAMES.get(pid, ""))
    monkeypatch.setattr(person_sessions, "_path", lambda: str(tmp_path / "sp.json"))
    monkeypatch.setattr(person_sessions, "_PERSON", None)
    yield c
    own._reset_for_tests()
    monkeypatch.setattr(person_sessions, "_PERSON", None)


@pytest.fixture
def seen(clk):
    got = []
    unsub = events_mod.BUS.subscribe(got.append)
    yield got
    unsub()


def _names(got, event):
    return [e for e in got if e["event"] == event]


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_owner_defaults_to_the_person_stamp_and_only_an_override_changes_it(clk):
    person_sessions.set_person("s", A)
    assert own.owner("s") == A
    own.set_owner("s", B)
    assert own.owner("s") == B
    # persisted: a fresh load reads the override back
    own._reset_for_tests()
    assert own.owner("s") == B
    with pytest.raises(ValueError):
        own.set_owner("s", "not-an-id")
    own.drop("s")
    assert own.owner("s") == A  # back to the stamp


def test_first_attach_takes_the_keyboard_and_the_second_person_watches(clk, seen):
    c1 = own.attach("s", A, "laptop")
    assert own.driver("s") == A
    c2 = own.attach("s", B, "desk")
    assert own.driver("s") == A
    assert own.may_type(c1) == (True, "")
    ok, notice = own.may_type(c2)
    assert not ok and notice == "Alice is driving — take control to type"
    # the notice is rate-limited per terminal
    assert own.may_type(c2) == (False, "")
    clk.t += own.NOTICE_EVERY_S + 0.1
    assert own.may_type(c2)[1]
    changed = _names(seen, "session.driver_changed")
    assert [(e["old"], e["new"], e["data"]["reason"]) for e in changed] == [
        ("", A, "attach")
    ]


def test_take_tells_the_previous_driver_and_release_frees_it(clk, seen):
    frames = []
    own.attach("s", A, "laptop", notify=frames.append)
    c2 = own.attach("s", B, "desk")
    assert own.take("s", B, "desk") == A
    assert own.driver("s") == B
    assert own.may_type(c2) == (True, "")
    ev = _names(seen, "session.driver_changed")[-1]
    assert (ev["old"], ev["new"], ev["data"]["reason"]) == (A, B, "take")
    assert ev["data"]["name"] == "Bob" and ev["data"]["previous_name"] == "Alice"
    # Alice's terminal was told it no longer drives
    assert frames[-1] == {
        "type": "driver",
        "driving": False,
        "driver": B,
        "driver_name": "Bob",
    }
    assert own.release("s", A) is False  # not hers to release
    assert own.release("s", B) is True
    assert own.driver("s") == ""


def test_a_peer_attaches_under_the_name_it_was_given(clk, seen, monkeypatch):
    """A peer typing through a shared session (peer.sessions_bridge) is the
    person ``peer:<link>``, named after the peer while its terminal is open —
    as driver, as viewer, and in the events that tell the previous driver."""
    monkeypatch.setattr(own, "_own_id", lambda: A)
    monkeypatch.setattr(own, "name_of", _NAME_OF)
    peer = "peer:" + "ab" * 16
    c = own.attach("s", peer, "Mauricio", name="Mauricio")
    assert own.name_of(peer) == "Mauricio"
    assert own.mode("s", peer) == {
        "type": "driver",
        "driving": True,
        "driver": peer,
        "driver_name": "Mauricio",
    }
    assert own.viewers("s")[peer]["name"] == "Mauricio"
    mine = own.attach("s", B, "desk")
    own.take("s", B, "desk")
    ev = _names(seen, "session.driver_changed")[-1]
    assert ev["data"]["previous_name"] == "Mauricio"
    assert own.may_type(c)[0] is False
    own.detach(c)
    assert own.name_of(peer) == ""  # gone with its last terminal
    assert set(own.viewers("s")) == {B}
    own.detach(mine)
    # a name is never taken from a person attaching without one
    own.attach("s", B, "desk", name="")
    assert own.viewers("s")[B]["name"] == ""


def test_the_lock_lapses_after_ten_idle_minutes_and_input_refreshes_it(clk, seen):
    own.attach("s", A, "laptop")
    clk.t += own.IDLE_RELEASE_S - 1
    own.note_input("s", A, "laptop")
    clk.t += own.IDLE_RELEASE_S - 1
    assert own.driver("s") == A
    clk.t += 2
    assert own.driver("s") == ""
    assert _names(seen, "session.driver_changed")[-1]["data"]["reason"] == "idle"


def test_input_while_nobody_drives_takes_the_keyboard_back(clk):
    """A lock that lapsed for idleness must not leave its holder read-only."""
    c = own.attach("s", A, "laptop")
    clk.t += own.IDLE_RELEASE_S + 1
    assert own.driver("s") == ""
    assert own.may_type(c) == (True, "")
    assert own.driver("s") == A


def test_the_drivers_last_terminal_closing_releases_it(clk, seen):
    c1 = own.attach("s", A, "laptop")
    c2 = own.attach("s", A, "desk")  # same person, another device
    own.attach("s", B, "phone")
    own.detach(c1)
    assert own.driver("s") == A  # still has a terminal open
    own.detach(c2)
    assert own.driver("s") == ""
    assert _names(seen, "session.driver_changed")[-1]["data"]["reason"] == "left"


def test_viewers_are_one_entry_per_person_with_their_devices(clk):
    own.attach("s", A, "laptop")
    clk.t += 1
    own.attach("s", A, "desk")
    own.attach("s", B, "phone")
    own.note_input("s", A, "desk")
    v = own.viewers("s")
    assert set(v) == {A, B}
    assert v[A]["name"] == "Alice"
    assert v[A]["device"] == "desk"  # the one used last
    assert v[A]["devices"] == ["laptop", "desk"]
    assert v[A]["since"] == 1000.0
    assert v[A]["typing"] is True and v[B]["typing"] is False
    clk.t += own.TYPING_S + 1
    assert own.viewers("s")[A]["typing"] is False


def test_presence_changes_emit_the_row_fields(clk, seen):
    person_sessions.set_person("s", A)
    c = own.attach("s", B, "desk")
    ev = _names(seen, "session.presence_changed")[-1]
    assert ev["session"] == "s"
    assert ev["data"]["owner"] == A
    assert ev["data"]["driver"] == B
    assert ev["data"]["driver_name"] == "Bob"
    assert set(ev["data"]["viewers"]) == {B}
    n = len(_names(seen, "session.presence_changed"))
    own.note_input("s", B, "desk")  # starts typing: announced once
    own.note_input("s", B, "desk")
    assert len(_names(seen, "session.presence_changed")) == n + 1
    own.detach(c)
    assert _names(seen, "session.presence_changed")[-1]["data"]["viewers"] == {}


def test_overlay_refreshes_local_rows_and_leaves_remote_rows_alone(clk):
    own.attach("s", A, "laptop")
    remote = {
        "title": "mini::t",
        "device": "mini",
        "driver": B,
        "viewers": {B: {"name": "Bob"}},
    }
    rows = [{"title": "s", "driver": "stale"}, remote]
    own.overlay(rows)
    assert rows[0]["driver"] == A and set(rows[0]["viewers"]) == {A}
    assert rows[0]["driver_name"] == "Alice"
    assert rows[1] == remote  # passes through the gateway unchanged


def test_caller_reads_the_person_and_the_relaying_device():
    scope = {
        "type": "http",
        "headers": [(b"x-mindflock-remote", b"Mini")],
        "state": {"person": A},
    }
    assert own.caller(scope) == (A, "mini")


def test_terminal_replies_are_not_keystrokes():
    assert is_terminal_reply(b"\x1b[12;40R")
    assert is_terminal_reply(b"\x1b[?62;22c\x1b[>0;95;0c")
    assert is_terminal_reply(b"\x1b]11;rgb:0000/0000/0000\x07")
    assert is_terminal_reply("\x1b[I")
    assert not is_terminal_reply(b"ls\r")
    assert not is_terminal_reply(b"\x1b[A")  # an arrow key
    assert not is_terminal_reply(b"")


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(clk, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    monkeypatch.setattr(server.ENGINE, "instances", {})
    server.ENGINE.instances["s"] = _mk_inst("s", str(tmp_path / "s"))
    who = {"person": A}
    monkeypatch.setattr(
        own,
        "caller",
        lambda scope: (
            _hdr(scope, b"x-test-person") or who["person"],
            _hdr(scope, b"x-test-device") or "laptop",
        ),
    )
    sent = []

    def deliver(inst, title, text, **kw):
        sent.append(text)
        return "sent", None

    monkeypatch.setattr(server, "_deliver_to_agent", deliver)
    monkeypatch.setattr(server, "_budget_locked", lambda title: False)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda title: None)
    c = TestClient(server.app)
    c.sent = sent
    return c


def _hdr(scope, name):
    s = getattr(scope, "scope", scope)
    for k, v in s.get("headers") or []:
        if k == name:
            return v.decode()
    return ""


BOB = {"x-test-person": B, "x-test-device": "desk"}


def test_send_from_a_non_driver_is_409_until_they_take(app):
    own.take("s", A, "laptop")
    r = app.post("/api/instances/s/send", json={"text": "hi"}, headers=BOB)
    assert r.status_code == 409
    assert r.json() == {
        "error": "driven by Alice",
        "driver": {"person": A, "name": "Alice", "device": "laptop"},
    }
    assert app.sent == []
    r = app.post(
        "/api/instances/s/send", json={"text": "hi", "take": True}, headers=BOB
    )
    assert r.status_code == 200 and app.sent == ["hi"]
    assert own.driver("s") == B
    # and now Alice is the one refused
    assert app.post("/api/instances/s/send", json={"text": "x"}).status_code == 409


def test_send_passes_for_the_driver_and_while_nobody_drives(app):
    assert app.post("/api/instances/s/send", json={"text": "a"}).status_code == 200
    assert own.driver("s") == ""  # sending takes nothing
    own.take("s", A)
    assert app.post("/api/instances/s/send", json={"text": "b"}).status_code == 200
    assert app.sent == ["a", "b"]


def test_the_prompt_queue_and_answer_are_gated_too(app):
    own.take("s", A)
    r = app.post("/api/instances/s/queue", json={"text": "later"}, headers=BOB)
    assert r.status_code == 409 and r.json()["error"] == "driven by Alice"
    r = app.post("/api/instances/s/answer", json={"keys": ["Enter"]}, headers=BOB)
    assert r.status_code == 409 and r.json()["driver"]["person"] == A
    assert app.post("/api/instances/s/queue", json={"text": "ok"}).status_code == 200


def test_driver_route_takes_and_releases(app, seen):
    own.take("s", A)
    r = app.post("/api/instances/s/driver", json={"take": True}, headers=BOB)
    assert r.status_code == 200
    body = r.json()
    assert body["driver"] == B and body["previous"] == A
    assert body["driver_name"] == "Bob"
    ev = _names(seen, "session.driver_changed")[-1]
    assert (ev["old"], ev["new"]) == (A, B)
    # Alice can't release Bob's lock
    r = app.post("/api/instances/s/driver", json={"release": True})
    assert r.status_code == 409 and r.json()["error"] == "driven by Bob"
    r = app.post("/api/instances/s/driver", json={"release": True}, headers=BOB)
    assert r.status_code == 200 and r.json()["driver"] == ""
    assert app.post("/api/instances/s/driver", json={}).status_code == 400
    assert (
        app.post("/api/instances/nope/driver", json={"take": True}).status_code == 404
    )


def test_owner_route_is_the_device_owners_only(app, monkeypatch):
    r = app.post("/api/instances/s/owner", json={"person": B})
    assert r.status_code == 200 and r.json()["owner"] == B
    assert app.post("/api/instances/s/owner", json={"person": "x"}).status_code == 400

    async def no(scope):
        return False

    monkeypatch.setattr(server._auth, "may_configure", no)
    assert app.post("/api/instances/s/owner", json={"person": A}).status_code == 403
    assert own.owner("s") == B


# --------------------------------------------------------------------------- #
# the terminal websocket
# --------------------------------------------------------------------------- #
class _FakeProc:
    """A PTY stand-in: the server reads/writes ``fd``; the test holds
    ``peer`` (what the server writes is what the pane would receive)."""

    def __init__(self) -> None:
        self.mine, self.peer = socket.socketpair()
        self.fd = self.mine.fileno()
        self.peer.setblocking(False)

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        pass

    def received(self, wait=0.5) -> bytes:
        end = time.time() + wait
        got = b""
        while time.time() < end:
            try:
                got += self.peer.recv(65536)
            except BlockingIOError:
                time.sleep(0.02)
        return got


@pytest.fixture
def term(app, monkeypatch):
    procs = []

    def spawn(*a, **kw):
        procs.append(_FakeProc())
        return procs[-1]

    monkeypatch.setattr(server, "_ensure_agent_session", lambda inst, t: ("s", None))
    monkeypatch.setattr(server.ptyprocess.PtyProcess, "spawn", spawn)
    monkeypatch.setattr(server, "_ATTACH_PROBE_GRACE_S", 0.0)
    return procs


def _frame(ws):
    return json.loads(ws.receive_text())


def test_terminal_attach_is_read_only_for_a_watcher(app, term):
    with app.websocket_connect("/api/instances/s/terminal") as alice:
        assert _frame(alice) == {
            "type": "driver",
            "driving": True,
            "driver": A,
            "driver_name": "Alice",
        }
        with app.websocket_connect("/api/instances/s/terminal", headers=BOB) as bob:
            assert _frame(bob)["driving"] is False
            assert set(own.viewers("s")) == {A, B}
            bob.send_bytes(b"rm -rf\r")
            assert _frame(bob) == {
                "type": "readonly",
                "message": "Alice is driving — take control to type",
            }
            bob.send_bytes(b"\x1b[3;1R")  # a terminal reply always passes
            alice.send_bytes(b"ls\r")
            time.sleep(0.2)
            assert term[1].received() == b"\x1b[3;1R"
            assert term[0].received() == b"ls\r"
            # Bob takes control over HTTP: his socket is told, and types
            r = app.post("/api/instances/s/driver", json={"take": True}, headers=BOB)
            assert r.status_code == 200
            assert _frame(bob)["driving"] is True
            assert _frame(alice)["driving"] is False
            bob.send_bytes(b"pwd\r")
            time.sleep(0.2)
            assert term[1].received() == b"pwd\r"
        # Bob's last terminal closed: the keyboard is free again
        deadline = time.time() + 2
        while own.driver("s") and time.time() < deadline:
            time.sleep(0.02)
        assert own.driver("s") == ""
        assert set(own.viewers("s")) == {A}


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def test_mcp_rows_carry_owner_driver_viewers_and_whoami_says_who_drives():
    from tests.unit._mcp_fakes import FakeCtx, make_box, row

    viewers = {B: {"name": "", "device": "desk", "since": 1.0, "typing": True}}
    box, api, clock = make_box(
        [
            row("orch", owner=A, driver=A),
            row("w1", parent="orch", owner=A, driver=B, viewers=viewers),
            row("idle"),
        ]
    )
    api.top_routes[("GET", "/api/person")] = {"id": A, "name": "Ada"}
    assert box.whoami({}, FakeCtx(clock))["you_drive"] is True
    rows = box.list_sessions({"filter": "all"}, FakeCtx(clock))["sessions"]
    by = {r["title"]: r for r in rows}
    assert by["w1"]["owner"] == A and by["w1"]["driver"] == B
    assert by["w1"]["viewers"] == viewers
    assert "driver" not in by["idle"] and "viewers" not in by["idle"]
    got = box.get_session({"title": "w1"}, FakeCtx(clock))["session"]
    assert got["driver"] == B

    box, api, clock = make_box([row("orch", owner=A, driver=B)])
    api.top_routes[("GET", "/api/person")] = {"id": A, "name": "Ada"}
    assert box.whoami({}, FakeCtx(clock))["you_drive"] is False
    box, api, clock = make_box([row("orch")])
    api.top_routes[("GET", "/api/person")] = {"id": A, "name": "Ada"}
    assert box.whoami({}, FakeCtx(clock))["you_drive"] is None  # nobody drives


def test_mcp_answer_prompt_explains_a_session_someone_else_drives():
    from backend import client
    from backend.mcp.tools import ToolError
    from tests.unit._mcp_fakes import FakeCtx, make_box, row

    box, api, clock = make_box([row("orch"), row("w1", parent="orch")])
    api.errors[("POST", "/api/instances/w1/answer")] = client.ApiError(
        409, "driven by Bob"
    )
    with pytest.raises(ToolError, match="driven by Bob — a person holds its keyboard"):
        box.answer_prompt({"title": "w1", "keys": ["Enter"]}, FakeCtx(clock))
