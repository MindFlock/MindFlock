"""The screen-evidence guard on every automated typing path (E2E defect F).

In the live MCP run (2026-10-05) the orchestrator's queued ``answer_prompt``
permission — raised by a background sub-agent at 10:04:24 — was approved at
10:07:17 with no ``/answer`` POST. The mailbox shows why: bye-worker's
``result`` was "delivered" to the orchestrator at 10:07:16.88, yet it never
became a user turn in the orchestrator's transcript. The orchestrator read
``idle`` from 10:07:00 (its main turn's Stop hook, the dialog still up), the
delivery lane typed the result line plus Enter into the dialog, and the Enter
picked the highlighted "1. Yes". Hypothesis confirmed: the mailbox lane.

Every automated typer now captures the pane right before typing and HOLDS
when the provider sees a live dialog at the bottom of the screen, whatever
the activity reading says. The screens are the run's real captures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.web.core import mailbox as mb
from backend.web.core import prompt_queue as pq
from tests.unit.test_mailbox_routes import (
    _post,
    _settle,
    _strip,
    env,
    lane,
)  # noqa: F401
from tests.unit.test_prompt_queue import client, drain, qfile  # noqa: F401

DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    return (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")


#: The orchestrator's screen at 10:07:16: a sub-agent's answer_prompt dialog.
DIALOG = _screen("claude2_answer_prompt_tab")
#: Claude's redraw glitch: the parser gives up, the waiting patterns do not.
GLITCH = _screen("claude2_redraw_glitch")
#: No dialog: the agent mid-turn.
PLAIN = _screen("claude2_working")


@pytest.fixture
def screens(monkeypatch):
    """What each tmux session's visible screen shows (by session title)."""
    from backend.web import server

    shown: dict = {}
    captured: list = []

    def capture(name):
        from backend.session import tmux

        captured.append(name)
        for key, screen in shown.items():
            if name in (key, "agent_" + key, tmux.to_mindflock_tmux_name(key)):
                return screen, None
        return PLAIN, None

    monkeypatch.setattr(server._agent_io, "capture_screen", capture)
    shown["_captured"] = captured
    # The queue tests' sessions run "bash"; these screens are Claude's.
    for title in ("d1", "t1"):
        inst = server.ENGINE.instances.get(title)
        if inst is not None:
            monkeypatch.setattr(inst, "Program", "claude")
    return shown


# --------------------------------------------------------------------------- #
# Mailbox delivery lane — the path that approved the dialog                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("screen", [DIALOG, GLITCH], ids=["parsed", "glitch"])
def test_replay_defect_f_the_lane_never_types_into_a_dialog_it_reads_idle(
    lane, screens, screen
):
    server, st = lane
    st.activity["orch"] = "idle"  # the main turn's Stop hook; the dialog is up
    screens["orch"] = screen
    m = mb.post(
        "orch",
        'Created bye.txt with content "Goodbye!" and committed it.',
        sender="w1",
        kind="result",
        data={"status": "done"},
        now=st.clock["now"],
    )
    for _ in range(3):
        _settle(server, st, "orch")
    assert st.typed == []
    assert mb.get("orch", m["id"])["state"] == "pending"  # held, not dropped
    assert screens["_captured"]  # the guard looked
    # The person answers the dialog; the screen is an ordinary one again.
    screens["orch"] = PLAIN
    _settle(server, st, "orch")
    ((_name, line, submit),) = st.typed
    assert submit is True and "Created bye.txt" in line
    assert mb.get("orch", m["id"])["state"] == "delivered"


def test_the_lane_still_types_when_the_screen_has_no_dialog(lane, screens):
    server, st = lane
    m = mb.post("w1", "hello", sender="orch", now=st.clock["now"])
    _settle(server, st, "w1")
    assert len(st.typed) == 1
    assert mb.get("w1", m["id"])["state"] == "delivered"


def test_delivery_now_holds_on_a_dialog_the_activity_missed(env, screens):
    _, st = env
    st.activity["w1"] = "working"  # sibling sub-agent tool events
    screens["w1"] = DIALOG
    body = _post(st, "w1", text="stop and commit", delivery="now").json()
    assert body["delivery"] == "pending"
    assert "waiting on a prompt" in body["detail"]
    assert st.typed == []
    assert mb.next_pending("w1")["id"] == body["message"]["id"]


# --------------------------------------------------------------------------- #
# Prompt-queue drain (the user's "When it's free")                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("screen", [DIALOG, GLITCH], ids=["parsed", "glitch"])
def test_the_drain_holds_a_queued_prompt_while_a_dialog_is_up(
    drain, screens, monkeypatch, screen
):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    screens["d1"] = screen
    pq.enqueue("d1", "e2e-human-note-ALPHA: summarise the merge")
    server._drain_one_queue("d1")
    assert sent == []
    assert [i["text"] for i in pq.list_queue("d1")] == [
        "e2e-human-note-ALPHA: summarise the merge"
    ]
    # Dialog answered: the next settled idle sends it.
    screens["d1"] = PLAIN
    server._QUEUE_STATE["d1"]["idle_since"] = 1.0
    server._drain_one_queue("d1")
    assert sent == ["e2e-human-note-ALPHA: summarise the merge"]


def test_the_drains_limit_resume_holds_while_a_dialog_is_up(
    drain, screens, monkeypatch
):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "limit")
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: True)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    screens["d1"] = DIALOG  # the Esc did not clear what is on screen
    pq.enqueue("d1", "next task")
    server._drain_one_queue("d1")
    assert sent == []
    assert [i["text"] for i in pq.list_queue("d1")] == ["next task"]


# --------------------------------------------------------------------------- #
# /send with dialog_safe (Send now, every paste — a playbook's included)       #
# --------------------------------------------------------------------------- #
def test_dialog_safe_send_queues_when_the_screen_shows_a_dialog(
    client, screens, monkeypatch
):
    from backend.web import server

    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    screens["t1"] = DIALOG
    r = client.post(
        "/api/instances/t1/send", json={"text": "rerun it", "dialog_safe": True}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": False, "queued": True, "submitted": False}
    assert client._sent == []
    assert [i["text"] for i in pq.list_queue("t1")] == ["rerun it"]


def test_dialog_safe_paste_is_409_when_the_screen_shows_a_dialog(
    client, screens, monkeypatch
):
    from backend.web import server

    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    screens["t1"] = GLITCH
    r = client.post(
        "/api/instances/t1/send",
        json={"text": "Check on your workers", "submit": False, "dialog_safe": True},
    )
    assert r.status_code == 409
    assert r.json()["in_dialog"] is True
    assert client._sent == []


def test_dialog_safe_send_types_when_the_screen_is_clear(client, screens, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    r = client.post(
        "/api/instances/t1/send", json={"text": "go on", "dialog_safe": True}
    )
    assert r.json() == {"sent": True, "submitted": True}
    assert client._sent == [("agent_t1", "go on", True)]


def test_a_plain_send_is_the_humans_own_and_is_not_guarded(client, screens):
    """``/send`` without ``dialog_safe`` is a person typing at a pane they
    are looking at — the guard is for the automated typers."""
    screens["t1"] = DIALOG
    r = client.post("/api/instances/t1/send", json={"text": "1"})
    assert r.json() == {"sent": True, "submitted": True}


def test_a_button_message_queues_when_the_screen_shows_a_dialog(
    client, screens, monkeypatch
):
    """``_deliver_to_agent(boot=False)``: the Code Map's notices."""
    from backend.web import server

    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    screens["t1"] = DIALOG
    inst = server.ENGINE.instances["t1"]
    told, reason = server._deliver_to_agent(inst, "t1", "zone changed")
    assert (told, reason) == ("queued", None)
    assert client._sent == []


# --------------------------------------------------------------------------- #
# Playbook render (right before the paste) and the limit watcher               #
# --------------------------------------------------------------------------- #
def test_playbook_blocked_live_sees_the_screen(client, screens, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server, "_mcp_unattachable", lambda program: None)
    monkeypatch.setattr(
        server.providers.mcp_attach, "launch_attached", lambda name: True
    )
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    inst = server.ENGINE.instances["t1"]
    assert server._playbook_blocked(inst, "t1", live=True) is None
    screens["t1"] = DIALOG
    assert server._playbook_blocked(inst, "t1", live=True) == server._PB_IN_DIALOG
    # The menu's memoized read takes no capture.
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: "idle")
    n = len(screens["_captured"])
    assert server._playbook_blocked(inst, "t1") is None
    assert len(screens["_captured"]) == n


def test_the_limit_watcher_does_not_type_continue_into_a_dialog(
    qfile, screens, monkeypatch
):
    import types

    from backend.web import server

    sent: list = []
    inst = types.SimpleNamespace(
        Program="claude", Started=lambda: True, Status="running"
    )
    monkeypatch.setattr(server.ENGINE, "instances", {"alpha": inst})
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: ("sess", None))
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: True)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        server, "_send_to_agent", lambda n, text, submit=True: sent.append(text) or True
    )
    server._QUEUE_STATE.pop("alpha", None)
    screens["sess"] = DIALOG
    server._watch_one_limited("alpha")
    assert sent == []
    screens["sess"] = PLAIN
    server._watch_one_limited("alpha")
    assert sent == [server._LIMIT_RESUME_PROMPT]
    server._QUEUE_STATE.pop("alpha", None)


def test_a_failed_capture_is_no_evidence(lane, monkeypatch):
    """The session is gone or tmux is down: the typer's own send decides."""
    server, st = lane
    monkeypatch.setattr(
        server._agent_io, "capture_screen", lambda name: (None, "no such session")
    )
    mb.post("w1", "hello", sender="orch", now=st.clock["now"])
    _settle(server, st, "w1")
    assert len(st.typed) == 1
