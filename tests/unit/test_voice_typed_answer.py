"""Answers typed straight into the pane, and voice dictation kept out of dialogs.

Voice cycle 5. Two server-side halves of the voice answer guard:

1. Keys typed into a session's pane (its terminal websocket, a ``/send``)
   while it is on a dialog answer that dialog as surely as ``/answer`` does,
   so they stamp ``voice_actions.answered_at`` — a same-id re-ask slipping
   between polls is never answered by a phrase begun before them. Only keys
   that SETTLE a dialog count (Enter, a digit, y/n, Escape): the mouse wheel,
   clicks, focus reports and arrow navigation never stamp.
2. ``/send {"voice": true}``: a voice client's dictation is never typed into
   a dialog (409 ``in_dialog``, nothing typed); a person's ``/send`` is
   unchanged.
"""

from __future__ import annotations

import socket
import time

import pytest
from fastapi.testclient import TestClient

from backend.session import mux
from backend.web import server
from backend.web.core import agent_io
from backend.web.core import agent_state
from backend.web.core import prompt_queue as pq
from backend.web.core import voice_actions
from tests.unit.test_prompt_queue import client, qfile  # noqa: F401
from tests.unit.test_typing_guard import DIALOG, PLAIN, screens  # noqa: F401

WHEEL_UP_SGR = b"\x1b[<64;40;12M"
WHEEL_DOWN_SGR = b"\x1b[<65;40;12M"
#: X10 wheel report whose column/row bytes are DIGITS ('1' = column 17).
WHEEL_X10 = b"\x1b[M`12"
CLICK_SGR = b"\x1b[<0;5;9M\x1b[<0;5;9m"
FOCUS = (b"\x1b[I", b"\x1b[O")
ARROWS = (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB", b"\x1b[1;5C")
PAGING = (b"\x1b[5~", b"\x1b[6~", b"\x1b[H", b"\x1b[F")
REPLIES = (b"\x1b[3;1R", b"\x1b[?1;2c", b"\x1b]11;rgb:0000/0000/0000\x07")


# --------------------------------------------------------------------------- #
# agent_io.settling_input — which bytes answer a dialog                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "data",
    [
        WHEEL_UP_SGR,
        WHEEL_DOWN_SGR,
        WHEEL_UP_SGR * 6,
        WHEEL_X10,
        WHEEL_X10 * 3,
        CLICK_SGR,
        *FOCUS,
        *ARROWS,
        *PAGING,
        *REPLIES,
        b"\x1b1",  # Alt+1: a meta chord, not Escape then 1
        b"\x1b[200~",  # a paste's opening bracket alone
        b"",
        b"0",
        b"hello",
        b" ",
        "\x1b[<64;3;4M",  # a text frame
    ],
)
def test_navigation_and_scroll_bytes_never_settle(data):
    assert agent_io.settling_input(data) is False


@pytest.mark.parametrize(
    "data",
    [
        b"\r",
        b"\n",
        b"1",
        b"9",
        b"y",
        b"n",
        b"Y",
        b"\x1b",  # a bare Escape: cancel
        b"\x1b\x1b",
        b"\x1b[B\r",  # arrow down, then Enter
        WHEEL_UP_SGR + b"2",
        b"\x1b[200~go on\x1b[201~",  # a paste that types an n
        "1",
        "\r",
    ],
)
def test_keys_that_answer_a_dialog_settle(data):
    assert agent_io.settling_input(data) is True


# --------------------------------------------------------------------------- #
# server._note_pane_input — stamped only while a dialog is up                  #
# --------------------------------------------------------------------------- #
class _Inst:
    Program = "claude"


@pytest.fixture
def pane(monkeypatch):
    """A session ``w`` whose last activity reading the test sets, and no
    probe screen unless a test gives one."""
    voice_actions._reset_for_tests()
    monkeypatch.setattr(agent_state, "_ACTIVITY_CACHE", {})
    monkeypatch.setattr(agent_state, "_LAST_SCREEN", {})
    monkeypatch.setattr(server, "_PANE_DIALOG_MEMO", {})

    def reading(activity):
        agent_state._ACTIVITY_CACHE["w"] = {"reported": activity}

    yield reading
    voice_actions._reset_for_tests()


STAMP_TABLE = [
    # (last activity reading, bytes, stamped?)
    ("clarify", b"\r", True),
    ("clarify", b"1", True),
    ("clarify", b"2\r", True),
    ("clarify", b"y", True),
    ("clarify", b"n", True),
    ("clarify", b"\x1b", True),
    ("limit", b"1", True),
    ("limit", b"\r", True),
    ("clarify", WHEEL_UP_SGR, False),
    ("clarify", WHEEL_DOWN_SGR * 4, False),
    ("clarify", WHEEL_X10, False),
    ("clarify", CLICK_SGR, False),
    ("clarify", FOCUS[0], False),
    ("clarify", FOCUS[1], False),
    ("clarify", b"\x1b[A", False),
    ("clarify", b"\x1b[B", False),
    ("clarify", b"\x1bOB", False),
    ("clarify", b"\x1b[5~", False),
    ("clarify", b"\x1b[3;1R", False),  # a terminal reply to tmux
    ("working", b"\r", False),
    ("working", b"1", False),
    ("idle", b"\r", False),
    ("idle", b"y", False),
    ("offline", b"\r", False),
    (None, b"\r", False),  # never probed
]


@pytest.mark.parametrize("activity,data,stamped", STAMP_TABLE)
def test_pane_input_stamps_only_settling_keys_on_a_dialog(
    pane, activity, data, stamped
):
    if activity is not None:
        pane(activity)
    before = time.time()
    server._note_pane_input(_Inst(), "w", data)
    at = voice_actions.answered_at("w")
    if stamped:
        assert at >= before
    else:
        assert at == 0.0


def test_a_sub_agent_dialog_on_the_probe_screen_counts_while_working(pane):
    """The row reads working (a background sub-agent's hook events) while
    its permission prompt is on screen: Enter there answers it."""
    pane("working")
    agent_state.note_screen("w", DIALOG)
    server._note_pane_input(_Inst(), "w", b"\r")
    assert voice_actions.answered_at("w") > 0


def test_a_plain_probe_screen_while_working_does_not_stamp(pane):
    pane("working")
    agent_state.note_screen("w", PLAIN)
    server._note_pane_input(_Inst(), "w", b"\r")
    assert voice_actions.answered_at("w") == 0.0


def test_a_stale_probe_screen_is_no_evidence(pane, monkeypatch):
    pane("working")
    agent_state._LAST_SCREEN["w"] = (time.time() - 3600, DIALOG)
    server._note_pane_input(_Inst(), "w", b"\r")
    assert voice_actions.answered_at("w") == 0.0


def test_the_screen_check_runs_once_per_distinct_screen(pane, monkeypatch):
    pane("working")
    agent_state.note_screen("w", DIALOG)
    calls = []
    real = server.providers.resolve

    def resolve(program):
        calls.append(program)
        return real(program)

    monkeypatch.setattr(server.providers, "resolve", resolve)
    for _ in range(5):
        server._note_pane_input(_Inst(), "w", b"1")
    assert len(calls) == 1


def test_a_failing_read_never_raises_nor_stamps(pane, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(agent_state, "last_screen", boom)
    pane("working")
    server._note_pane_input(_Inst(), "w", b"\r")
    assert voice_actions.answered_at("w") == 0.0


def test_another_sessions_keys_do_not_stamp_this_one(pane):
    pane("clarify")
    agent_state._ACTIVITY_CACHE["other"] = {"reported": "clarify"}
    server._note_pane_input(_Inst(), "other", b"\r")
    assert voice_actions.answered_at("w") == 0.0
    assert voice_actions.answered_at("other") > 0


# --------------------------------------------------------------------------- #
# The terminal websocket: keys typed in the pane                               #
# --------------------------------------------------------------------------- #
class _FakeProc:
    """A PTY stand-in: the server writes ``fd``; the test reads ``peer``."""

    def __init__(self) -> None:
        self.mine, self.peer = socket.socketpair()
        self.fd = self.mine.fileno()
        self.peer.setblocking(False)

    def setwinsize(self, rows, cols):
        pass

    def terminate(self, force=False):
        pass

    def received(self, wait=0.4) -> bytes:
        end = time.time() + wait
        got = b""
        while time.time() < end:
            try:
                got += self.peer.recv(65536)
            except BlockingIOError:
                time.sleep(0.02)
        return got


@pytest.fixture
def term(pane, monkeypatch):
    procs: list = []

    def spawn(*a, **kw):
        procs.append(_FakeProc())
        return procs[-1]

    inst = _Inst()
    monkeypatch.setattr(server.ENGINE, "instances", {"w": inst})
    monkeypatch.setattr(server, "ensure_terminal_session", lambda i, t: ("mf_w", None))
    monkeypatch.setattr(mux, "spawn_pty", spawn)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_note_human_input", lambda *a, **k: None)
    return procs


def _type(data: bytes, procs) -> bytes:
    with TestClient(server.app).websocket_connect("/api/instances/w/terminal") as ws:
        ws.receive_text()  # the keyboard-owner frame
        ws.send_bytes(data)
        got = procs[-1].received()
    return got


@pytest.mark.parametrize(
    "activity,data,stamped",
    [
        ("clarify", b"\r", True),
        ("clarify", b"1", True),
        ("clarify", b"\x1b", True),
        ("clarify", WHEEL_UP_SGR, False),
        ("clarify", FOCUS[0], False),
        ("clarify", b"\x1b[B", False),
        ("working", b"\r", False),
    ],
)
def test_terminal_socket_keys_stamp_on_a_dialog(term, pane, activity, data, stamped):
    pane(activity)
    got = _type(data, term)
    assert got == data  # the keys still reach the pane, stamp or not
    assert (voice_actions.answered_at("w") > 0) is stamped


def test_a_budget_locked_keystroke_never_lands_nor_stamps(term, pane, monkeypatch):
    monkeypatch.setattr(server, "_budget_locked", lambda t: True)
    pane("clarify")
    assert _type(b"\r", term) == b""
    assert voice_actions.answered_at("w") == 0.0


# --------------------------------------------------------------------------- #
# /send — a person's send into a dialog stamps; voice dictation is gated       #
# --------------------------------------------------------------------------- #
@pytest.fixture
def stamps():
    voice_actions._reset_for_tests()
    yield
    voice_actions._reset_for_tests()


def test_a_persons_send_into_a_dialog_still_types_and_stamps(
    client, screens, stamps, monkeypatch
):
    monkeypatch.setitem(agent_state._ACTIVITY_CACHE, "t1", {"reported": "clarify"})
    screens["t1"] = DIALOG
    r = client.post("/api/instances/t1/send", json={"text": "1"})
    assert r.json() == {"sent": True, "submitted": True}
    assert client._sent == [("agent_t1", "1", True)]
    assert voice_actions.answered_at("t1") > 0


def test_a_persons_send_while_working_does_not_stamp(
    client, screens, stamps, monkeypatch
):
    monkeypatch.setitem(agent_state._ACTIVITY_CACHE, "t1", {"reported": "working"})
    r = client.post("/api/instances/t1/send", json={"text": "go on"})
    assert r.json() == {"sent": True, "submitted": True}
    assert voice_actions.answered_at("t1") == 0.0


@pytest.mark.parametrize(
    "activity,screen",
    [
        ("clarify", PLAIN),  # the reading says dialog
        ("limit", PLAIN),  # the usage-limit menu
        ("working", DIALOG),  # a sub-agent's prompt: the screen says dialog
        ("idle", DIALOG),
        ("unknown", PLAIN),  # an unreadable activity: don't type blind
    ],
)
@pytest.mark.parametrize("text", ["yes and also ", " ", "2"])
def test_voice_dictation_into_a_dialog_is_409_with_nothing_typed(
    client, screens, stamps, monkeypatch, activity, screen, text
):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity)
    screens["t1"] = screen
    r = client.post(
        "/api/instances/t1/send",
        json={"text": text, "submit": False, "voice": True},
    )
    assert r.status_code == 409, r.text
    assert r.json()["in_dialog"] is True
    assert client._sent == []
    assert pq.list_queue("t1") == []
    assert voice_actions.answered_at("t1") == 0.0


def test_voice_dictation_types_when_no_dialog_is_up(client, screens, monkeypatch):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    r = client.post(
        "/api/instances/t1/send",
        json={"text": "add a test ", "submit": False, "voice": True},
    )
    assert r.json() == {"sent": True, "submitted": False}
    assert client._sent == [("agent_t1", "add a test ", False)]


def test_voice_dictation_may_be_a_lone_space(client, screens, monkeypatch):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    r = client.post(
        "/api/instances/t1/send", json={"text": " ", "submit": False, "voice": True}
    )
    assert r.json() == {"sent": True, "submitted": False}
    assert client._sent == [("agent_t1", " ", False)]


def test_a_submitted_voice_message_queues_behind_a_dialog(client, screens, monkeypatch):
    """Like any dialog-safe send: queued for the drain, never typed."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    screens["t1"] = DIALOG
    r = client.post("/api/instances/t1/send", json={"text": "rerun it", "voice": True})
    assert r.json() == {"sent": False, "queued": True, "submitted": False}
    assert client._sent == []
    assert [i["text"] for i in pq.list_queue("t1")] == ["rerun it"]


@pytest.mark.parametrize("body", [{"text": "2", "submit": False}, {"text": "2"}])
def test_a_human_send_without_the_voice_flag_is_unaffected(
    client, screens, monkeypatch, body
):
    """A person at the send box types into the dialog they are looking at."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "clarify")
    screens["t1"] = DIALOG
    r = client.post("/api/instances/t1/send", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert client._sent == [("agent_t1", "2", body.get("submit", True))]


@pytest.mark.parametrize(
    "body",
    [
        {"text": " ", "voice": True},  # submitted blank: still empty
        {"text": " ", "submit": False},  # not voice: still empty
        {"text": "", "submit": False, "voice": True},
    ],
)
def test_blank_text_is_still_400_outside_voice_live_typing(client, body):
    r = client.post("/api/instances/t1/send", json=body)
    assert r.status_code == 400
    assert client._sent == []


def test_voice_flag_must_be_true_not_truthy(client, screens, monkeypatch):
    """Only ``true`` opts in: a stray ``"voice": "yes"`` is a plain send."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "clarify")
    screens["t1"] = DIALOG
    r = client.post(
        "/api/instances/t1/send", json={"text": "2", "submit": False, "voice": "yes"}
    )
    assert r.json() == {"sent": True, "submitted": False}
