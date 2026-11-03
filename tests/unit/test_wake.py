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
        # What whisper writes for it (core.voice_words.HEARD), however far
        # from the letters: "my flock" is three off.
        "My flock",
        "mind flog.",
        "Min flock",
        "mindflocked",
    ],
)
def test_the_word_alone_wakes(heard):
    assert wake.is_wake(heard) is True


def test_the_known_forms_are_the_shared_tables():
    from backend.web.core import voice_words as vw

    for form in vw.HEARD["mindflock"]:
        assert wake.is_wake(form) is True, form
        assert wake.is_wake(form + " please") is False, form
    # A custom word has no table: the letter slack alone.
    assert wake.is_wake("my flock", "jarvis") is False
    assert wake.is_wake("mindful") is False  # a real word, not a form


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


# --- a greeting in front, a repeat, and ours matched strictly ---------------------
@pytest.mark.parametrize(
    "heard, reason",
    [
        ("Mind Flock.", "word"),  # the letters exactly, split
        ("Mindflock", "word"),
        ("Hey, MindFlock!", "word"),
        ("OK MindFlock", "word"),
        ("hello, mine flock", "form"),
        ("MindFlock MindFlock", "word"),  # the hinted decoder loops
        ("MindFlock MindFlock MindFlock", "word"),
        ("Hey, mind flunk.", "form"),
        ("Mind Flap.", "sound"),  # the sound shape, no table entry
        ("Minh Flock", "form"),
        ("Mend clock", "sound"),
        ("Mindflopping", "sound"),
    ],
)
def test_ours_wakes_with_a_lead_a_repeat_or_its_sound(heard, reason):
    assert wake.judge(heard) == (True, reason)


@pytest.mark.parametrize(
    "heard",
    [
        "Windlock",  # two letters off: the old slack woke on it
        "Wind lock.",
        "Mild block",
        "Mindful",
        "Mindful.",
        "Mind the gap",
        "Mind fog.",
        "Line clock.",
        "mindflock now",  # the sound shape is the word whole, not a start
        "Hey",
        "Hey hey",
        "Hey, MindFlock, open the docs",
        "Mind flock mind flock",  # a repeated PAIR is not collapsed
    ],
)
def test_ours_is_strict(heard):
    assert wake.is_wake(heard) is False


def test_the_reasons_for_a_no():
    assert wake.judge("") == (False, "empty")
    assert wake.judge("...") == (False, "empty")
    assert wake.judge("MindFlock is great") == (False, "long")
    assert wake.judge("Windlock") == (False, "other")


def test_a_custom_word_keeps_the_letter_slack():
    # Ours turns "Windlock" down; as the person's own word, "Mindlock" is
    # one letter off it and wakes as before.
    assert wake.judge("Mindlock", "windlock") == (True, "near")
    assert wake.judge("Windlock", "windlock") == (True, "word")
    # A greeting and a repeat are dropped for any word ...
    assert wake.judge("Hey, Jarvis!", "jarvis") == (True, "word")
    assert wake.judge("Jarvis Jarvis", "jarvis") == (True, "word")
    # ... but not when the word starts with it.
    assert wake.judge("Hey Jarvis", "hey jarvis") == (True, "word")
    assert wake.is_wake("Jarvis", "hey jarvis") is False
    # The sound shape and the veto's start are ours only.
    assert wake.is_wake("Mind Flap", "jarvis") is False


# --- the veto: the plain (un-hinted) decode heard another word --------------------
@pytest.mark.parametrize(
    "plain",
    [
        "mind",
        "Mine.",
        "Mindset.",
        "Minefield.",
        "Mind field.",
        "mind again.",
        "Mind the gap.",
        "Mind fog.",
        "Mindful.",
        "Hey, mine.",
        "Mindhole.",
    ],
)
def test_the_veto_turns_down_another_word(plain):
    assert wake.veto(plain).startswith("plain:")


@pytest.mark.parametrize(
    "plain",
    [
        "Mind Flock.",
        "Mindflock",
        "Mind clock",
        "Mine Pluck.",
        "mind-tlook",
        "Hey, mind flunk.",
        "My flock",
        # The plain model missing the word is not hearing another one.
        "",
        "Men flat.",
        "Amen.",
        "My",
        "It's been fun.",
    ],
)
def test_the_veto_keeps_the_word_and_a_plain_miss(plain):
    assert wake.veto(plain) == ""
