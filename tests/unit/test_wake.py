"""core.wake: the wake word on its own, as whisper tends to hear it."""

import pytest

from backend.web.core import wake


@pytest.mark.parametrize(
    "heard",
    [
        "MindFlock",
        "mindflock",
        "Mind flock.",
        "Mind Flock!",
        " mindflock ",
        "Mindlock",  # a letter short
        "mind block",  # one letter off
        "Mindflocked",  # a letter long
        "Mine flock",
        "MindFlock?",
    ],
)
def test_the_word_alone_wakes(heard):
    assert wake.is_wake(heard) is True


@pytest.mark.parametrize(
    "heard",
    [
        "",
        "   ",
        "MindFlock is great",
        "open mindflock",
        "hey mindflock please",
        "mind",
        "flock",
        "Thank you.",
        "my block",  # three letters off
        "Mind flock mind flock",  # repeated: three tokens
        "the mind flock",
    ],
)
def test_anything_else_does_not(heard):
    assert wake.is_wake(heard) is False


def test_normalize_keeps_letters_only():
    assert wake.normalize("Mind-Flock, please!") == "mindflockplease"
    assert wake.normalize("") == ""
    assert wake.WAKE_WORD == "mindflock"


# --- a word of the person's own (voice.words "wake") ----------------------------
@pytest.mark.parametrize(
    "heard, wakes",
    [
        ("Jarvis", True),
        ("Jarvis.", True),
        ("Jervis", True),  # one letter off: a 6-letter word allows one
        ("Jarviss", True),
        ("Jervas", False),  # two off is too many for a short word
        ("MindFlock", False),  # ours no longer wakes
        ("Jarvis, open the docs", False),
    ],
)
def test_a_custom_word(heard, wakes):
    assert wake.is_wake(heard, "jarvis") is wakes


def test_a_two_word_wake_word():
    assert wake.is_wake("Hey Jarvis", "hey jarvis") is True
    assert wake.is_wake("hey jar vis", "hey jarvis") is True  # one token more
    assert wake.is_wake("hey there jar vis", "hey jarvis") is False
    assert wake.is_wake("Jarvis", "hey jarvis") is False


def test_a_short_word_allows_no_slack():
    assert wake.is_wake("Max", "max") is True
    assert wake.is_wake("Mix", "max") is False
    assert wake.is_wake("Maxi", "max") is False


def test_the_hint():
    assert wake.hint() == "MindFlock"
    assert wake.hint("mindflock") == "MindFlock"
    assert wake.hint("hey  jarvis") == "Hey Jarvis"
    assert wake.hint("") == "MindFlock"
