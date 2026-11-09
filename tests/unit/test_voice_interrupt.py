"""Voice interrupt: "interrupt", "press escape", "interrupt docs" → the
route presses Escape in that session's agent pane (``/answer``'s key path,
no dialog gate) and answers ``{kind: "interrupt", title, ok}`` — no
``say``. Ordinary dictation that merely mentions interrupts stays words."""

from __future__ import annotations

import pytest

from backend.web.core import voice_router as vr
from tests.unit.test_voice_router import ROWS, env, route  # noqa: F401 — fixture


@pytest.mark.parametrize(
    "text",
    [
        "interrupt",
        "Interrupt.",
        "interrupt it",
        "interrupt the session",
        "interrupt the agent",
        "interrupt this session",
        "please interrupt",
        "ok interrupt now",
        "press escape",
        "hit escape",
        "send escape",
        "press the escape key",
        "escape",
        "Escape.",
        "Mindflock, interrupt.",
    ],
)
def test_interrupt_phrases(text):
    assert vr.command(text) == {"kind": "interrupt"}


@pytest.mark.parametrize(
    "text",
    [
        "interrupt handler is broken",
        "the interrupt handler is broken",
        "press escape twice in the dialog to close it",
        "escape the quotes in the string",
        "handle the escape key in the modal",
        "interrupt the build when the tests fail",
    ],
)
def test_interrupt_lookalikes_are_words(text):
    assert vr.answer(text, "docs", ROWS) == {"kind": "prompt"}


def test_named_interrupt_resolves_the_session():
    got = vr.answer("interrupt invoice parser", "docs", ROWS)
    assert got == {
        "kind": "interrupt",
        "title": "invoice-parser",
        "name": "invoice-parser",
    }
    assert vr.answer("interrupt the API docs session", "", ROWS)["title"] == "docs"


def test_named_interrupt_needs_a_whole_name_without_session():
    # "login" starts two names: asked, never guessed (and no say → no press).
    got = vr.answer("interrupt login", "docs", ROWS)
    assert got["kind"] == "ambiguous" and "pending" not in got


def test_bare_interrupt_is_the_session_talked_to():
    assert vr.answer("interrupt", "docs", ROWS) == {
        "kind": "interrupt",
        "title": "docs",
    }
    assert (
        vr.answer("interrupt", "docs", ROWS, title="login-fix")["title"] == "login-fix"
    )


# --- the route -------------------------------------------------------------------------


@pytest.fixture
def presses(env, monkeypatch):  # noqa: F811 — the route fixture
    done = []

    def fake(title):
        done.append(title)
        return title != "gone"

    monkeypatch.setattr("backend.web.routes.voice._press_escape", fake)
    return done


def test_route_presses_escape_and_says_nothing(presses, env):  # noqa: F811
    assert route("interrupt", "docs") == {
        "kind": "interrupt",
        "title": "docs",
        "ok": True,
    }
    assert presses == ["docs"]
    assert env.row_reads == 0


def test_route_interrupts_a_named_session(presses):
    got = route("interrupt invoice parser", "docs")
    assert got == {"kind": "interrupt", "title": "invoice-parser", "ok": True}
    assert presses == ["invoice-parser"]


def test_route_unknown_name_is_words(presses):
    assert route("interrupt handler is broken", "docs") == {"kind": "prompt"}
    assert presses == []


def test_route_failed_press_is_ok_false(presses):
    assert route("press escape", "gone") == {
        "kind": "interrupt",
        "title": "gone",
        "ok": False,
    }


def test_route_with_no_session_presses_nothing(presses):
    assert route("interrupt", "") == {"kind": "interrupt", "title": "", "ok": False}
    assert presses == []


def test_press_escape_refuses_another_devices_session(env):  # noqa: F811
    from backend.web.routes import voice as voice_routes

    assert voice_routes._press_escape("mac::billing") is False
    assert voice_routes._press_escape("not-a-session") is False


def test_press_escape_uses_the_answer_key_path(env, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from backend.web import server
    from backend.web.core import agent_io
    from backend.web.routes import voice as voice_routes

    monkeypatch.setattr(server.ENGINE, "instances", {"docs": SimpleNamespace()})
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    sent = []
    monkeypatch.setattr(
        agent_io,
        "send_answer",
        lambda name, text, keys: sent.append((name, text, keys)) or True,
    )
    assert voice_routes._press_escape("docs") is True
    from backend.session import tmux

    assert sent == [(tmux.to_mindflock_tmux_name("docs"), "", ["Escape"])]
