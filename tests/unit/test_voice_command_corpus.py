"""The voice grammar against what the recognisers really wrote.

``tests/unit/data/voice_command_corpus.json`` holds the distinct mis-hearings
``tests/bench/voice_command_ears.py`` recorded: each command phrase rendered
by piper (four voices, two speeds, alone and at the end of a sentence) and
read by Moonshine tiny-streaming with keyterms (the control words) and by
whisper ``small`` with hotwords (the text). Most of them are beyond saving
("tetris" for "status") and stay dictation; these tests pin the two things
that matter:

1. **No mis-hearing becomes a DIFFERENT command** (a missed command still
   reaches the agent as words; a wrong one acts) — except the few listed in
   :data:`KNOWN_WRONG`, each harmless.
2. **The ones the grammar takes on purpose keep working** (:data:`MUST_HEAR`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.web.core import voice_router as R
from backend.web.core import voice_words as W

CORPUS = json.loads(
    (Path(__file__).parent / "data" / "voice_command_corpus.json").read_text()
)
#: The kinds the clients read off the end of a phrase (``voice_words``).
RADIO = frozenset(("send", "end", "clear", "undo", "stop", "hush", "wake"))

#: Mis-hearings read as another command, each judged harmless: "page up"
#: heard as "hey top" stops the mic (a heard form of "stop" after a filler);
#: "which to voice ears" reads as a question about MindFlock's voice feature
#: (a spoken docs answer, nothing done).
KNOWN_WRONG = {
    ("hey top", "stop"),
    ("which to voice ears", "help"),
}

#: Mis-hearings the grammar takes on purpose (whole phrases only).
MUST_HEAR = [
    ("never mine", "cancel"),
    ("with sessions do i have", "list"),
    ("what need me", "needs"),
    ("summerize that", "condense"),
    ("save that again", "repeat"),
    ("to go back", "switch_back"),
    ("to scroll down", "scroll"),
    ("roll up", "scroll"),
    ("growl up", "scroll"),
    ("crawl to the bottom", "scroll"),
    ("growl to the bottom", "scroll"),
    ("roll to the bottom", "scroll"),
    ("pagedown", "scroll"),
    ("interrupts", "interrupt"),
    ("pressescape", "interrupt"),
    ("voice command", "commands"),
    ("twitch to voice ears", "switch"),
    ("twitch to hackathon 2", "switch"),
]


def _reading(heard: str):
    """What the phrase does: a radio word the clients act on, else the
    router's command kind, else None (dictation)."""
    kind = W.control_kind(heard)
    if kind:
        return kind
    cmd = R.command(heard)
    return cmd.get("kind") if cmd else None


def test_the_corpus_is_there():
    assert len(CORPUS) > 100
    assert {"said", "heard", "engine", "kind"} <= set(CORPUS[0])


@pytest.mark.parametrize(
    "row", CORPUS, ids=lambda r: "%s|%s|%s" % (r["engine"], r["said"], r["heard"])
)
def test_no_mishearing_is_another_command(row):
    got = _reading(row["heard"])
    if got is None or got == row["kind"]:
        return
    # A wake word heard alone is the wake route's, not a command here.
    if row["kind"] == "wake":
        return
    assert (row["heard"], got) in KNOWN_WRONG, "%r (said %r) reads as %r, not %r" % (
        row["heard"],
        row["said"],
        got,
        row["kind"],
    )


@pytest.mark.parametrize("heard, kind", MUST_HEAR)
def test_the_mishearings_we_take(heard, kind):
    assert _reading(heard) == kind


@pytest.mark.parametrize(
    "text",
    [
        "roll back the migration",
        "go back and fix the test",
        "to do list",
        "to be honest",
        "the command",
        "press escape twice in the dialog to close it",
        "scroll down to the footer and fix the margin",
        "crawl the docs for broken links",
        "which to choose",
    ],
)
def test_dictation_stays_dictation(text):
    cmd = R.command(text)
    assert cmd is None or cmd.get("kind") not in (
        "scroll",
        "switch_back",
        "commands",
        "cancel",
        "list",
    ), cmd
