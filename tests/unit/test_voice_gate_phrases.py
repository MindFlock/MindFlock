"""The pinned gate phrases (tests/unit/data/voice_gate_phrases.json) against
the router itself: every ``route`` pair is that command, every ``text``
phrase — run through ``POST /api/voice/route`` as a client sends it, with
the fixture's sessions — ends a prompt (typed), never an action.

The clients' gates (desktop voiceDesktop.ts, phone mobile.js) read the same
file in their own tests; this one keeps the router's half honest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import aliases, voice_actions
from backend.web.core import voice_router as vr
from backend.web.routes import voice as voice_routes
from backend.web.server import app

DATA = json.loads(
    (Path(__file__).parent / "data" / "voice_gate_phrases.json").read_text()
)
client = TestClient(app)


def _rows():
    """The fixture's sessions as ``/api/instances`` rows."""
    return [
        {
            "title": name.replace(" ", "-"),
            "name": name,
            "activity": "idle",
            "status": "ready",
        }
        for name in DATA["sessions"]
    ]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(aliases, "owner_of", lambda *a, **k: "")
    pressed = []

    async def no_create(payload, person):  # pragma: no cover - must not run
        raise AssertionError("a text phrase created a session: %r" % payload)

    async def no_dialog(title):
        return None

    monkeypatch.setattr(voice_routes, "_session_rows", lambda request: _rows())
    monkeypatch.setattr(voice_routes, "_create_session", no_create)
    monkeypatch.setattr(voice_routes, "_read_dialog", no_dialog)
    monkeypatch.setattr(voice_routes, "_read_spoken", lambda t: {})
    monkeypatch.setattr(voice_routes, "_scroll_pane", lambda *a: pressed.append(a))
    monkeypatch.setattr(voice_routes, "_press_escape", lambda t: pressed.append(t))
    voice_actions._reset_for_tests()
    yield pressed
    voice_actions._reset_for_tests()


def test_the_fixture_is_there():
    assert len(DATA["route"]) > 80 and DATA["text"] and DATA["sessions"]
    said = [p for p, _ in DATA["route"]]
    assert len(said) == len(set(said)), "a route phrase is listed twice"
    assert not set(said) & set(DATA["want"]), "a want phrase already routes"
    assert not set(said) & set(DATA["text"])


@pytest.mark.parametrize("phrase, kind", DATA["route"])
def test_every_route_phrase_is_its_command(phrase, kind):
    got = vr.command(phrase)
    assert got is not None and got["kind"] == kind, (phrase, got)


@pytest.mark.parametrize("phrase", DATA["text"])
def test_every_text_phrase_is_no_command_or_falls_to_words(phrase):
    """Pure: no command, or one that resolves to a prompt with the
    fixture's sessions (none of them is called what was said)."""
    got = vr.command(phrase)
    if got is None:
        return
    assert got["kind"] in ("switch", "status", "read", "question", "answer"), got
    assert vr.answer(phrase, "docs", _rows(), now=0)["kind"] == "prompt"


@pytest.mark.parametrize("phrase", DATA["text"])
def test_every_text_phrase_routes_typed(env, phrase):
    r = client.post("/api/voice/route", json={"text": phrase, "session": "docs"})
    assert r.status_code == 200
    got = r.json()
    assert got["kind"] == "prompt", (phrase, got)
    # Typed without a caption: none of these is a near miss of a command.
    assert "near" not in got, (phrase, got)
    assert env == [], "a text phrase acted on a pane"


def test_open_the_settings_file_is_a_strict_switch_that_falls_to_words(env):
    """It parses as a soft switch ("open <name>") and only falls to words
    because no session is called that — pinned so a looser matcher can't
    make it switch."""
    phrase = "open the settings file and check the port"
    assert vr.command(phrase)["kind"] == "switch"
    assert vr.command(phrase)["mode"] == "strict"
    r = client.post("/api/voice/route", json={"text": phrase, "session": "docs"})
    assert r.json() == {"kind": "prompt"}


def test_switch_the_database_driver_stays_words():
    """ "switch the <name>" acts only on a session called that."""
    got = vr.answer("switch the database driver to postgres", "docs", _rows(), now=0)
    assert got == {"kind": "prompt"}
    got = vr.answer("switch the docs", "", _rows(), now=0)
    assert (got["kind"], got["title"]) == ("switch", "docs")


def test_pause_is_left_out_on_purpose():
    """ "Pause" could mean the mic or the agent: a wrong guess stops one of
    them, so it stays words (the person can add it to their stop words)."""
    assert vr.command("pause") is None
    assert "pause" in DATA["want"]
