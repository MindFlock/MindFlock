"""Dictation is not a question to MindFlock (owner, 2026-11-12: "sometimes it
thinks I am asking it a question when I am just trying to type in a
session").

The owner's prompts to an agent working ON MindFlock are question-shaped
and name its features ("where do the tickets get picked up"), and often
start with its name as the sentence's subject ("MindFlock should restart
the server when …"). While a session is open to type into, neither is
taken from the agent: a question is help only when MindFlock is addressed
first ("MindFlock, …", "help: …"), and the name first is an address only
with a pause after it or a request after it. The unaddressed guess stays
for when there is nothing to type into, and a typed guess carries ``near``
help so the client can caption how to ask MindFlock instead.

The router (``core.voice_router``) is pure and run directly; the phone's
own wake check (mobile.js ``voiceWakeAddressed``) and its captions run
under node through test_mobile_voice_polish's harnesses.
"""

from __future__ import annotations

import pytest

from backend.web.core import voice_router as vr
from tests.unit.test_mobile_voice_polish import (
    _addressed_harness,
    _js,
    _routed_harness,
    _run,
    needs_node,
)
from tests.unit.test_voice_router import ROWS, env, route  # noqa: F401 — env: fixture

NOW = 1_800_000_000.0

#: Real dictated prompts said to an agent: question-shaped, naming
#: MindFlock's features, never MindFlock's to answer while a session is open.
#: The first four are the owner's.
DICTATED = [
    "why doesn't auto ingest PR do the review flight",
    "where do the tickets get picked up",
    "what happens to notifications when the window is closed",
    "can we add a setting for that",
    "why do the sessions on the rail lose their order after a restart",
    "where are the notification sounds configured",
    "how should the phone page show a dialog when the mic is on",
    "what happens to the worktrees when a session is deleted",
    "which settings does the doctor check",
    "can we make the verify checklist shorter",
    "why is usage double counted for workers",
    "when does autopilot ship the PR",
    "how do pull requests get merged when green",
    "what does the bell do when two sessions need me at once",
    "is there a way to show token usage per session on the sky",
    "how does the fence work in MindFlock",
]

#: The product's name as the SUBJECT of a dictated sentence: words for the
#: agent, never the assistant's.
NAME_AS_SUBJECT = [
    "MindFlock should restart the server when the build changes",
    "MindFlock's voice mode is better but it still mishears",
    "MindFlock keeps dropping the mic after a dialog",
    "mindflock needs a setting for the voice speed",
    "Mindflock doesn't show the PR link anymore",
    "mind flock still types the wake word into the session",
    "flock should be the default view",
]


def ans(text, session="docs", rows=ROWS):
    return vr.answer(text, session, rows, now=NOW)


@pytest.mark.parametrize("text", DICTATED + NAME_AS_SUBJECT)
def test_dictated_prompts_are_typed_while_a_session_is_open(text):
    got = ans(text)
    assert got["kind"] == "prompt", got
    # A near miss may only ever be help's: captioned, the words still typed.
    assert set(got) <= {"kind", "near"}
    if "near" in got:
        assert got["near"]["kind"] == "help"
        assert got["near"]["why"] == "unaddressed"


@pytest.mark.parametrize("text", DICTATED)
def test_a_typed_help_guess_says_how_to_ask_mindflock(text):
    """The guess is kept as ``near``: "MindFlock, <the question>" — and is
    answered as help when no session is open to type it into."""
    cmd = vr.command(text)
    if cmd is None or cmd["kind"] != "help":
        assert ans(text) == {"kind": "prompt"}  # never a guess at all
        return
    assert cmd["unaddressed"] is True
    assert ans(text) == {
        "kind": "prompt",
        "near": {
            "kind": "help",
            "why": "unaddressed",
            "suggest": "MindFlock, " + cmd["text"],
        },
    }
    # Nothing to type into: answering is the only useful thing.
    assert ans(text, session="") == {"kind": "help", "text": cmd["text"]}
    # A focused title no longer listed is nothing to type into either.
    assert ans(text, session="closed-one")["kind"] == "help"


@pytest.mark.parametrize("text", NAME_AS_SUBJECT)
def test_the_name_as_a_subject_is_not_said_to_mindflock(text):
    got = vr.command(text)
    assert got is None or got["kind"] not in ("assistant", "help"), got


@pytest.mark.parametrize(
    "text, rest",
    [
        ("MindFlock, what needs me", "what needs me"),
        ("hey mindflock open settings", "open settings"),
        ("MindFlock what needs me", "what needs me"),
        (
            "MindFlock, where do the tickets get picked up",
            "where do the tickets get picked up",
        ),
        ("mindflock why is the bell red", "why is the bell red"),
        ("mindflock is there a way to pin a prompt", "is there a way to pin a prompt"),
        ("MindFlock can we turn on the bell", "can we turn on the bell"),
        ("MindFlock tell docs to run the tests", "tell docs to run the tests"),
        ("mindflock close the dialog", "close the dialog"),
        # The pause the recogniser writes after a name said to it: addressed,
        # whatever follows.
        ("MindFlock, should the server restart", "should the server restart"),
        ("Flock, the bell is too loud", "the bell is too loud"),
    ],
)
def test_addressed_questions_stay_the_assistants(text, rest):
    want = {"kind": "assistant", "text": rest}
    assert vr.command(text) == want
    assert ans(text) == want


@pytest.mark.parametrize(
    "text, question",
    [
        (
            "help: where do the tickets get picked up",
            "where do the tickets get picked up",
        ),
        ("help: how does autopilot ship a PR?", "how does autopilot ship a pr"),
    ],
)
def test_addressed_help_stays_help_while_a_session_is_open(text, question):
    assert ans(text) == {"kind": "help", "text": question}


def test_route_types_the_owners_question_and_answers_it_with_nothing_open(
    env,
):  # noqa: F811
    said = "where do the tickets get picked up?"
    assert route(said, "docs") == {
        "kind": "prompt",
        "near": {
            "kind": "help",
            "why": "unaddressed",
            "suggest": "MindFlock, where do the tickets get picked up",
        },
    }
    assert route(said, "") == {
        "kind": "help",
        "text": "where do the tickets get picked up",
    }
    assert route("MindFlock should restart the server when it changes", "docs") == {
        "kind": "prompt"
    }
    assert route("MindFlock, where do the tickets get picked up", "docs") == {
        "kind": "assistant",
        "text": "where do the tickets get picked up",
    }


# --- the phone page (mobile.js), under node --------------------------------------
@needs_node
def test_phone_takes_the_name_as_a_subject_as_dictation():
    """voiceAddressed: the wake word first is "rest" (the assistant's, never
    typed) only when said TO MindFlock — a pause after it, a request after
    it, or a command of the page's — else dictation (null) as ever."""
    js = _js()
    said = {
        "subject": NAME_AS_SUBJECT[:6],
        "addressed": [
            "MindFlock, what needs me",
            "hey mindflock open settings",
            "MindFlock what needs me",
            "mindflock why is the bell red",
            "mindflock is there a way to pin a prompt",
            "MindFlock tell docs to run the tests",
            "MindFlock, should the server restart",
            "MindFlock next session",
        ],
    }
    out = _run(
        _addressed_harness(js)
        + "var said = %s, o = {};\n" % __import__("json").dumps(said)
        + "for (var k in said) o[k] = said[k].map(function (t) { voiceListen_.addressed = ''; var a = voiceAddressed(t); return a && a.mode; });\n"
        + "console.log(JSON.stringify(o));\n"
    )
    assert out["subject"] == [None] * len(said["subject"]), out
    assert out["addressed"] == ["rest"] * len(said["addressed"]), out


@needs_node
def test_phone_captions_a_typed_help_guess_and_never_says_it():
    """voiceRouted: a prompt with ``near`` help is typed and captioned (the
    strip) — no speech, no assistant turn; a wake-started listen's first
    phrase that is a help guess goes to the assistant (voiceAddressedRouted)."""
    js = _js()
    near = {
        "kind": "prompt",
        "near": {
            "kind": "help",
            "why": "unaddressed",
            "suggest": "MindFlock, where do the tickets get picked up",
        },
    }
    out = _run(
        _routed_harness(js)
        + "var r = voiceRouted(%s, {text: 'where do the tickets get picked up'});\n"
        % __import__("json").dumps(near)
        + "console.log(JSON.stringify({r: r, did: did}));\n"
    )
    assert [
        "voiceStripNote",
        "Typed — say “MindFlock, …” to ask MindFlock instead",
    ] in out["did"]
    assert any(d[0] == "voiceHeardRest" for d in out["did"])  # the words typed
    assert not any(d[0] in ("voiceAssistTurn", "speakLine") for d in out["did"])

    first = _run(
        _addressed_harness(js)
        + "did = []; var r = voiceAddressedRouted(%s, {text: 'where do the tickets get picked up'}, 'first');\n"
        % __import__("json").dumps(near)
        + "console.log(JSON.stringify({r: r, did: did}));\n"
    )
    assert ["assist", "where do the tickets get picked up"] in first["did"]
