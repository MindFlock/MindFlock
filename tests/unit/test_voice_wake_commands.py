"""A phrase said TO MindFlock starts with the wake word as the recogniser
writes it (owner report 2026-11-10: "When I say 'MindFlock, open settings'
or 'intake' it doesn't do it"; the desktop trace heard "My flock, open
settings." and "Mine flock, open settings.").

``core.voice_words.wake_at`` / ``wake_rest`` (the clients' lib/voiceWords
``wakeAt`` / ``wakeRest``, the same rule) and the router's assistant kind
built on them: the wake word's heard forms count — an ordinary-word form
("my flock", "mind block") only with the pause the recogniser writes after a
name said to it — and the open-X requests the assistant answers without a
model ("open settings", "open intake", "open schedule") come through whole.
"""

from __future__ import annotations

import pytest

from backend.web.core import voice_assistant as va
from backend.web.core import voice_router as vr
from backend.web.core import voice_words as vw


@pytest.mark.parametrize(
    "text, rest",
    [
        # The three phrases of the owner's trace.
        ("My flock, open settings.", "open settings."),
        ("Mine flock, open settings.", "open settings."),
        ("mindflock, open schedule.", "open schedule."),
        ("Mind flock, open intake", "open intake"),
        ("mine flock open intake", "open intake"),  # no pause needed: no ordinary words
        ("Hey MindFlock, open settings", "open settings"),
        ("ok mind flock, turn on the bell", "turn on the bell"),
        ("ask MindFlock what needs me", "what needs me"),
        ("Me flock: open settings", "open settings"),
        ("Mind block, open intake.", "open intake."),
        ("MindFlocks, open intake", "open intake"),  # sounds like it, a pause after
        ("MindFlock.", ""),  # the wake word alone: nothing follows
    ],
)
def test_wake_rest(text, rest):
    assert vw.wake_rest(text) == rest


@pytest.mark.parametrize(
    "text",
    [
        "my flock open settings",  # an ordinary-word form, no pause: dictation
        "MindFlock's settings page is slow",  # about it, not to it
        "tell mindflock to open intake",  # mid-sentence
        "fix the mindflock login bug",
        "my tests fail",
        "mind the gap, please",
        "flock mode on",
        "",
    ],
)
def test_not_the_wake_word(text):
    assert vw.wake_at(text) is None
    assert vw.wake_rest(text) is None


def test_a_persons_own_wake_word_and_the_default_still():
    words = {"wake": "jarvis"}
    assert vw.wake_rest("Jarvis, open intake", words) == "open intake"
    assert vw.wake_rest("jarvis open intake", words) == "open intake"
    assert vw.wake_rest("MindFlock, open intake", words) == "open intake"
    assert vw.wake_rest("My flock, open intake.", words) == "open intake."


def test_heard_serves_the_forms_the_clients_match():
    """``words.heard.wake`` (GET /api/voice) carries the ordinary-word forms
    the clients' wakeAt needs: without "my flock" there the desktop would
    type the owner's "My flock, open settings." as words."""
    forms = vw.effective({})["heard"]["wake"]
    for f in ("my flock", "mine flock", "mind flock", "mind block", "me flock"):
        assert f in forms


@pytest.mark.parametrize(
    "text, rest",
    [
        ("My flock, open settings.", "open settings."),
        ("Mine flock, open settings.", "open settings."),
        ("mindflock, open schedule.", "open schedule."),
        ("My flock, open intake.", "open intake."),
        ("Hey MindFlock, open intake please", "open intake"),
    ],
)
def test_the_router_hands_it_to_the_assistant(text, rest):
    assert vr.command(text) == {"kind": "assistant", "text": rest}


@pytest.mark.parametrize(
    "text",
    ["my flock open settings", "MindFlock's settings page is slow", "flock mode on"],
)
def test_the_router_leaves_dictation_alone(text):
    got = vr.command(text)
    assert got is None or got["kind"] != "assistant", got


def test_the_clients_own_words_stay_commands():
    assert vr.command("My flock, stop listening.")["kind"] == "stop"
    assert vr.command("Mine flock, scroll up")["kind"] == "scroll"


@pytest.mark.parametrize(
    "text, target",
    [
        ("My flock, open settings.", "settings"),
        ("Mine flock, open intake.", "intake"),
        ("mindflock, open schedule.", "schedule"),
        ("My flock, open saved prompts.", "prompts"),
        ("My flock, open verify.", "verify"),
        ("My flock, open voice settings.", "settings:voice"),
    ],
)
def test_open_x_after_the_wake_word_opens_without_a_model(text, target):
    """The assistant's instant path (no model) opens what the rest names —
    the `act` the page performs and confirms out loud ("Opening Settings.")."""
    got = vr.command(text)
    assert got["kind"] == "assistant"
    assert va.instant(got["text"]) == {"do": "open", "target": target}
