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
