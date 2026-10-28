"""The voice router: ``core.voice_router`` (pure) and ``POST /api/voice/route``.

The pure module runs against a fixed row list; the route's seams
(``_session_rows``, ``_create_session``, the repo suggestions) are replaced,
``server.ENGINE.instances`` is swapped whole (``setattr``, never ``setitem``
— the real state.json is behind ENGINE) and the voice flag dir lives under
``tmp_path``, so no real session is read, created or flagged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session import tmux
from backend.web import server
from backend.web.core import aliases, voice
from backend.web.core import voice_router as vr
from backend.web.routes import voice as voice_routes
from backend.web.server import app

client = TestClient(app)

NOW = 1_800_000_000.0

ROWS = [
    {
        "title": "invoice-parser",
        "name": "invoice-parser",
        "activity": "working",
        "activity_since": NOW - 300,
        "last_say": "Parsing the totals now",
        "last_turn": "I **fixed** the `parser`.",
        "repo": "MindFlock",
        "path": "/r/MindFlock",
        "status": "ready",
    },
    {
        "title": "docs",
        "name": "API docs",
        "activity": "clarify",
        "last_say": "Should I push?",
        "repo": "MindFlock",
        "path": "/r/MindFlock",
        "status": "ready",
    },
    {
        "title": "login-fix",
        "name": "login-fix",
        "activity": "idle",
        "check": {"state": "failed", "stale": False},
        "repo": "sitecheck-bot5",
        "path": "/r/sitecheck-bot5",
        "status": "ready",
    },
    {
        "title": "login-page",
        "name": "login-page",
        "activity": "offline",
        "repo": "MindFlock",
        "path": "/r/MindFlock",
        "status": "ready",
    },
    {
        "title": "mac::billing",
        "name": "billing",
        "device": "mac",
        "activity": "idle",
        "repo": "billing",
        "path": "/Users/me/billing",
        "status": "ready",
    },
    {
        "title": "verify-1",
        "name": "verify-1",
        "hidden_by": "verify",
        "activity": "clarify",
    },
]

REPOS = [
    {"name": "MindFlock", "path": "/r/MindFlock", "source": "recent"},
    {"name": "sitecheck-bot5", "path": "/r/sitecheck-bot5", "source": "recent"},
    {"name": "sitecheck-bot6", "path": "/r/sitecheck-bot6", "source": "nearby"},
    {"name": "parser", "path": "/r/parser", "source": "nearby"},
]


def ans(text, session="", rows=ROWS, title="", words=None):
    return vr.answer(text, session, rows, now=NOW, title=title, words=words)


# --- not a command ------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "fix the login bug",
        "move it over to the new session tab",
        "switch",
        "go to",
        "open a new session tab in the browser",
        "start a session",
        "can you list the sessions in the README",
        "what needs to change here",
        "stop the server and restart it",
        "new sessions are slow",
        "stop the server",
        "yes but run the tests first",
        "I think yes is right",
        "no idea what you mean",
        "update the docs, yes",
        "there is no session for that",
        "the answer is no",
        "yes to the refactor",
        "two",
        "what does the user want",
        "open the file",
        "go back to the old approach",
        "session",
        "the second",
    ],
)
def test_conservative_non_matches_are_prompts(text):
    assert ans(text) == {"kind": "prompt"}


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Never mind.", "cancel"),
        ("scratch that", "cancel"),
        ("please cancel", "cancel"),
        ("forget it", "cancel"),
        ("Stop talking.", "hush"),
        ("stop speaking", "hush"),
        ("stop reading", "hush"),
        ("be quiet", "hush"),
        ("Quiet!", "hush"),
        ("shut up", "hush"),
        ("that's enough", "hush"),
        ("okay, enough", "hush"),
        ("Stop listening", "stop"),
        ("stop", "stop"),
        ("stop recording", "stop"),
        ("Stop the mic.", "stop"),
        ("stop the microphone", "stop"),
        ("mic off", "stop"),
        ("turn off the mic", "stop"),
        ("turn the mic off", "stop"),
        ("pause listening", "stop"),
        ("That's all.", "stop"),
        ("that is all", "stop"),
        ("done talking", "stop"),
        ("okay, I'm done talking", "stop"),
        ("voice off", "off"),
        ("turn off voice", "off"),
        ("Say that again", "repeat"),
        ("repeat that", "repeat"),
        ("summarize that", "condense"),
        ("summary", "condense"),
    ],
)
def test_local_kinds_carry_no_say(text, kind):
    assert ans(text) == {"kind": kind}


# --- the person's own stop / hush words (voice.words) ----------------------------
def _words(**kinds):
    from backend.web.core import voice_words

    return voice_words.effective(kinds)


def test_the_default_words_are_the_old_grammar():
    # None and the effective defaults read the same.
    for text in ("stop the microphone", "turn the mic off", "enough of that"):
        assert vr.command(text) == vr.command(text, _words())


def test_custom_stop_and_hush_words_are_matched_whole():
    w = _words(stop=["halt", "don't listen"], hush=["zip it"])
    assert ans("Halt.", words=w) == {"kind": "stop"}
    assert ans("dont listen", words=w) == {"kind": "stop"}
    assert ans("okay, zip it", words=w) == {"kind": "hush"}
    assert ans("halt the build", words=w) == {"kind": "prompt"}
    # The defaults they replaced are gone.
    assert vr.command("stop talking", w) is None
    assert vr.command("stop recording", w) is None


def test_a_kept_default_keeps_its_variants():
    w = _words(stop=["stop listening", "halt"], hush=["enough", "zip it"])
    assert ans("stop the microphone", words=w) == {"kind": "stop"}
    assert ans("halt", words=w) == {"kind": "stop"}
    assert ans("that's enough", words=w) == {"kind": "hush"}
    # "mic off" was dropped, and so were its variants.
    assert vr.command("turn the mic off", w) is None


def test_a_word_moved_from_stop_to_hush():
    w = _words(stop=["halt"], hush=["stop", "quiet"])
    assert ans("stop", words=w) == {"kind": "hush"}
    # ...and it takes its heard forms along (core.voice_words.HEARD).
    assert ans("stock", words=w) == {"kind": "hush"}
    assert ans("quite", words=w) == {"kind": "hush"}


@pytest.mark.parametrize(
    "text, kind",
    [
        # What whisper writes for a default (HEARD): the whole phrase.
        ("stop listing", "stop"),
        ("Stock.", "stop"),
        ("stopped", "stop"),
        ("mike off", "stop"),
        ("paws listening", "stop"),
        ("be quite", "hush"),
        ("quite", "hush"),
        ("okay, enuf", "hush"),
        ("a nuff please", "hush"),
        ("stop torking", "hush"),
        ("shadap", "hush"),
        # Followed or preceded by other words: a prompt.
        ("stop listing the files", "prompt"),
        ("quite a few tests fail", "prompt"),
        ("the room went quite", "prompt"),
        ("stock prices", "prompt"),
    ],
)
def test_heard_forms_of_the_default_words(text, kind):
    assert ans(text)["kind"] == kind
    assert ans(text, words=_words())["kind"] == kind


def test_heard_forms_go_with_their_phrase():
    w = _words(stop=["halt"], hush=["silence"])
    assert ans("stop listing", words=w) == {"kind": "prompt"}
    assert ans("be quite", words=w) == {"kind": "prompt"}


@pytest.mark.parametrize(
    "text, kind",
    [
        # The person's own words, heard a letter off (voice_words.sounds_like).
        ("pipe town", "hush"),
        ("okay pipe town please", "hush"),
        ("hault listening", "stop"),
        ("halt lissening", "stop"),
        # The margin: a four-letter word is only ever itself; two letters off
        # a short phrase, or another sound, is a prompt.
        ("hault", "prompt"),
        ("silents", "prompt"),
        ("pipe gown", "prompt"),
        ("halt the build", "prompt"),
        ("pipe town now and then", "prompt"),
    ],
)
def test_custom_words_a_letter_off(text, kind):
    w = _words(stop=["halt", "halt listening"], hush=["silence", "pipe down"])
    assert ans(text, words=w)["kind"] == kind


# --- switch ---------------------------------------------------------------------
def test_switch_the_phones_cases():
    a = ans("Switch to the invoice parser.")
    assert a == {
        "kind": "switch",
        "title": "invoice-parser",
        "name": "invoice-parser",
        "say": "Now on invoice-parser, working.",
    }
    a = ans("go to API docs")
    assert (a["kind"], a["title"], a["name"]) == ("switch", "docs", "API docs")
    assert a["say"] == "Now on API docs, waiting on a question."
    # Word overlap: "api" is in one session's name only.
    assert ans("go to the api")["title"] == "docs"
    assert ans("jump over to docs")["title"] == "docs"
    assert ans("OK change to invoice please")["title"] == "invoice-parser"


def test_switch_reaches_other_devices_but_never_hidden_rows():
    a = ans("switch to billing")
    assert a["title"] == "mac::billing" and a["say"] == "Now on billing, idle."
    assert ans("go to verify")["kind"] == "no-session"


def test_switch_ambiguous_and_no_match():
    pair = [
        {"title": "login-fix", "name": "login-fix"},
        {"title": "login-page", "name": "login-page"},
    ]
    assert ans("go to login") == {
        "kind": "ambiguous",
        "say": "Which one: login-fix or login-page?",
        "candidates": pair,
        "pending": {"kind": "pick", "candidates": pair},
    }
    assert ans("go to narnia") == {
        "kind": "no-session",
        "say": "No session called narnia. Sessions: invoice-parser, API docs, "
        "login-fix, login-page, billing.",
    }
    assert ans("go to narnia", rows=[]) == {
        "kind": "no-session",
        "say": "There are no sessions.",
    }


def test_no_session_names_at_most_six():
    rows = [{"title": "s%d" % i, "name": "s%d" % i} for i in range(9)]
    say = ans("go to narnia", rows=rows)["say"]
    assert say == "No session called narnia. Sessions: s0, s1, s2, s3, s4, s5."


# --- list / needs / status --------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "what sessions do I have",
        "Which sessions are open?",
        "what sessions are running",
        "list sessions",
        "list my sessions",
        "list the sessions",
        "what's up",
        "what is running",
    ],
)
def test_list(text):
    assert ans(text) == {
        "kind": "list",
        "say": "Sessions: invoice-parser (working), API docs (waiting on you), "
        "login-fix (waiting on failing checks), login-page (offline), billing (idle).",
    }


def test_needs_is_the_bell_in_its_order():
    assert ans("who needs me") == {
        "kind": "needs",
        "say": "API docs is asking: Should I push? login-page is offline. "
        "login-fix is waiting on failing checks.",
    }
    assert ans("what needs my attention", rows=ROWS[:1])["say"] == "Nothing needs you."


def test_stuck_is_in_the_bell_after_twenty_idle_minutes_on_dirty_work():
    row = {
        "title": "t",
        "activity": "idle",
        "activity_since": NOW - 25 * 60,
        "diff_stat": {"uncommitted": {"additions": 3, "deletions": 0}},
    }
    assert vr.session_state(row, NOW)["in_bell"] is True
    assert vr.session_state(dict(row, activity_since=NOW - 60), NOW)["in_bell"] is False
    assert vr.session_state(dict(row, diff_stat=None), NOW)["in_bell"] is False


def test_status_is_the_current_session():
    assert ans("What's it doing?", session="invoice-parser") == {
        "kind": "status",
        "say": "invoice-parser is working for 5 minutes. I fixed the parser.",
    }
    assert ans("status", session="docs")["say"] == "API docs is waiting on a question."
    assert ans("what's going on", session="")["say"] == "No session is open."
    assert ans("status", session="gone")["say"] == "gone is not listed."


# --- new: parsing -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text,want",
    [
        ("new session", ("", "", "")),
        ("New session.", ("", "", "")),
        ("new session to fix the login bug", ("", "", "fix the login bug")),
        ("new session fix the login bug", ("", "", "fix the login bug")),
        ("New session: fix auth.py, please", ("", "", "fix auth.py")),
        ("open a new session called parser in mindflock", ("parser", "mindflock", "")),
        (
            "start a new session in sitecheck and add tests for the queue",
            ("", "sitecheck", "add tests for the queue"),
        ),
        ("create a new session named API docs", ("api docs", "", "")),
        ("make another new session on MindFlock", ("", "mindflock", "")),
        (
            "Hey, spin up a new session for parser that adds tests",
            ("", "parser", "adds tests"),
        ),
        (
            "please open new session called login fix to fix login",
            ("login fix", "", "fix login"),
        ),
        (
            "okay start a new session and refactor the queue",
            ("", "", "refactor the queue"),
        ),
        ("new session in sitecheck, to add tests", ("", "sitecheck", "add tests")),
    ],
)
def test_new_session_parsing(text, want):
    c = vr.command(text)
    assert c is not None and c["kind"] == "new"
    assert (c["name"], c["repo"], c["task"]) == want


def test_new_session_answer_is_handed_to_the_route():
    assert ans("new session to fix it")["kind"] == "new"


# --- new: planning ------------------------------------------------------------------
def plan(text, session="", rows=ROWS, repos=REPOS):
    return vr.plan_new(text, session, rows, repos)


def test_plan_named_repo_and_title():
    p = plan("open a new session called parser in mindflock to fix it")
    assert p == {
        "kind": "new",
        "title_base": "parser",
        "name": "parser",
        "repo_name": "MindFlock",
        "repo_path": "/r/MindFlock",
        "task": "fix it",
    }
    # A name the words begin ("sitecheck" → the most recent sitecheck-bot*).
    p = plan("start a new session in sitecheck and add tests for the queue")
    assert (p["repo_path"], p["task"], p["title_base"]) == (
        "/r/sitecheck-bot5",
        "add tests for the queue",
        "add-tests-queue",
    )
    # Spacing and case don't count; "the … repo" is not part of the name.
    p = plan("new session in the Site Check Bot 6 repo to add tests")
    assert p["repo_path"] == "/r/sitecheck-bot6"


def test_plan_repo_words_run_into_the_task():
    p = plan("new session in mindflock fix the tests")
    assert (p["repo_path"], p["task"], p["title_base"]) == (
        "/r/MindFlock",
        "fix the tests",
        "fix-tests",
    )


def test_plan_the_project_may_come_after_the_task():
    p = plan("create a session to fix the login bug in mindflock")
    assert (p["kind"], p["repo_path"], p["task"]) == (
        "new",
        "/r/MindFlock",
        "fix the login bug",
    )
    # "in" before words that name no project: they stay the task (and the
    # project is asked for).
    p = plan("new session to fix the bug in the parser module")
    assert p["kind"] == "ask"
    assert p["pending"]["task"] == "fix the bug in the parser module"


def test_plan_for_or_on_a_non_repo_is_the_task():
    p = plan("new session for the login bug in parser")
    assert (p["repo_path"], p["task"]) == ("/r/parser", "for the login bug")
    assert p["title_base"] == "login-bug"


def test_plan_without_a_project_asks_for_one():
    # The current session's project first, then the most recent.
    p = plan("new session to fix it", session="login-fix")
    assert p == {
        "kind": "ask",
        "say": "In which project? sitecheck-bot5, MindFlock, sitecheck-bot6 or parser?",
        "pending": {
            "kind": "new",
            "await": "repo",
            "name": "",
            "task": "fix it",
            "choices": ["sitecheck-bot5", "MindFlock", "sitecheck-bot6", "parser"],
        },
    }
    # Another device's session has no project here.
    p = plan("spin one up", session="mac::billing")
    assert (
        p["say"]
        == "In which project? MindFlock, sitecheck-bot5, sitecheck-bot6 or parser?"
    )


def test_plan_without_a_task_asks_for_one():
    assert plan("new session in parser") == {
        "kind": "ask",
        "say": "What should it do in parser?",
        "pending": {
            "kind": "new",
            "await": "task",
            "name": "",
            "task": "",
            "repo_name": "parser",
            "repo_path": "/r/parser",
        },
    }


def test_plan_in_an_unknown_repo_asks_again():
    p = plan("new session in narnia to fix it")
    assert p["kind"] == "ask"
    assert p["say"] == (
        "I don't know a project called narnia. In which project? MindFlock, "
        "sitecheck-bot5, sitecheck-bot6 or parser?"
    )
    assert p["pending"]["task"] == "fix it"


def test_plan_only_or_no_repository():
    only = [{"name": "solo", "path": "/r/solo", "source": "nearby"}]
    p = plan("new session to fix it", rows=[], repos=only)
    assert (p["kind"], p["repo_path"]) == ("new", "/r/solo")
    assert (
        plan("new session", rows=[], repos=only)["say"] == "What should it do in solo?"
    )
    assert plan("new session", rows=[], repos=[]) == {
        "kind": "new-failed",
        "say": "I don't know any projects yet: open one from the New dialog first.",
    }


def test_title_base():
    assert vr.title_base("API docs", "whatever") == "api-docs"
    assert (
        vr.title_base("", "Fix the login bug in the parser please")
        == "fix-login-bug-parser"
    )
    assert vr.title_base("", "") == "voice-session"
    assert vr.title_base("!!", "") == "voice-session"
    assert len(vr.title_base("a very long name " * 5, "")) <= vr.TITLE_MAX


# --- switch by number / relative ------------------------------------------------
@pytest.mark.parametrize(
    "text,index",
    [
        ("switch to session three", 3),
        ("go to 2", 2),
        ("Go over to window 4.", 4),
        ("focus to number twelve", 12),
        ("open to session one", 1),
        ("show to number 20", 20),
        ("select to the third one", 3),
        ("jump to the 2nd session", 2),
        ("session three", 3),
        ("Session 3.", 3),
        ("number two", 2),
        ("window five", 5),
        ("the third session", 3),
        ("okay, session nineteen please", 19),
    ],
)
def test_switch_index_is_the_clients_number(text, index):
    assert ans(text) == {"kind": "switch_index", "index": index}


@pytest.mark.parametrize(
    "text,delta",
    [
        ("next session", 1),
        ("next one", 1),
        ("go to the next window", 1),
        ("previous session", -1),
        ("last one", -1),
        ("switch to the previous one", -1),
        ("go back", -1),
        ("ok go back please", -1),
    ],
)
def test_switch_rel(text, delta):
    assert ans(text) == {"kind": "switch_rel", "delta": delta}


def test_title_maps_a_number_to_the_full_switch():
    assert ans("session two", title="docs") == {
        "kind": "switch",
        "title": "docs",
        "name": "API docs",
        "say": "Now on API docs, waiting on a question.",
    }
    assert ans("next one", title="login-fix")["title"] == "login-fix"
    assert ans("session two", title="gone") == {
        "kind": "no-session",
        "say": "gone is not listed.",
    }


@pytest.mark.parametrize(
    "text,title",
    [
        ("focus to docs", "docs"),
        ("open to the invoice parser", "invoice-parser"),
        ("show to billing", "mac::billing"),
        ("select to login fix", "login-fix"),
    ],
)
def test_switch_by_name_takes_the_new_verbs(text, title):
    a = ans(text)
    assert (a["kind"], a["title"]) == ("switch", title)


# --- answers: the phone's word tables -----------------------------------------------
OPTS = [
    {"key": "1", "label": "Yes", "kind": "yes"},
    {"key": "2", "label": "Yes, always", "kind": "always"},
    {"key": "3", "label": "No", "kind": "no"},
]
YN = [
    {"key": "1", "label": "Yes", "kind": "yes"},
    {"key": "2", "label": "No", "kind": "no"},
]
OTHER = [
    {"key": "1", "label": "main", "kind": "other"},
    {"key": "2", "label": "develop", "kind": "other"},
    {"key": "3", "label": "release", "kind": "other"},
]
# tests/unit/test_mobile_voice.py test_voice_matchers_under_node, verbatim.
PHONE_CASES = {
    "yes": ["yes", "Yeah, sure!", "ok go ahead", "Allow it."],
    "no": ["no", "No, don't.", "nope", "skip that"],
    "always": [
        "always",
        "Always allow.",
        "yes, always",
        "don't ask again",
        "yes and do not ask again",
    ],
    "none": ["banana", "", "maybe later", "I always forget which"],
    "refusals": ["no, not always", "No! don't always allow that", "no, option 2"],
    "two": ["option two", "two", "the second one", "2", "number two"],
}
PHONE_KEYS = {
    "yes": ["1", "1", "1", "1"],
    "no": ["3", "3", "3", "3"],
    "always": ["2", "2", "2", "2", "2"],
    "none": [None, None, None, None],
    "refusals": ["3", "3", "3"],
    "two": [None, None, None, None, None],
}


def _key(opt):
    return opt["key"] if opt else None


@pytest.mark.parametrize("group", sorted(PHONE_CASES))
def test_parse_answer_is_the_phones(group):
    got = [_key(vr.parse_answer(t, OPTS)) for t in PHONE_CASES[group]]
    assert got == PHONE_KEYS[group]


def test_parse_answer_yes_only_and_other_dialogs():
    # With no always option, "always" is still a yes; "yeah sure" never widens.
    assert _key(vr.parse_answer("always allow", YN)) == "1"
    assert _key(vr.parse_answer("yeah sure", YN)) == "1"
    # Dialogs made only of `other` options take a number, by the option's key.
    got = [
        _key(vr.parse_answer(t, OTHER)) for t in ("option two", "three", "first", "yes")
    ]
    assert got == ["2", "3", "1", None]


def test_choice_of_the_phones_cases():
    def choices(group):
        out = []
        for t in PHONE_CASES[group]:
            c = vr.choice_of(t)
            out.append(c and (c["choice"], c.get("number")))
        return out

    assert choices("yes") == [("yes", None)] * 4
    assert choices("no") == [("no", None)] * 4
    assert choices("always") == [("always", None)] * 5
    # Not made only of answer words: no answer at all (a prompt).
    assert choices("none") == [None] * 4
    assert choices("refusals") == [("no", None)] * 3
    # A number is an answer only as "option two" / "the second one".
    assert choices("two") == [("number", 2), None, ("number", 2), None, None]


def test_dialog_choices_are_the_phones():
    assert vr.dialog_choices({"options": OPTS}) == "Say yes, no, or always."
    assert vr.dialog_choices({"options": YN}) == "Say yes or no."
    assert (
        vr.dialog_choices({"options": OTHER})
        == "one: main. two: develop. three: release. Say one, two, or three."
    )
    assert (
        vr.dialog_choices({"options": OTHER[:2]})
        == "one: main. two: develop. Say one or two."
    )


# --- answers: forms and targets -------------------------------------------------------
@pytest.mark.parametrize(
    "text,choice",
    [
        ("yes", "yes"),
        ("Yes.", "yes"),
        ("no", "no"),
        ("always allow", "always"),
        ("approve", "yes"),
        ("deny", "no"),
        ("allow it", "yes"),
        ("go ahead", "yes"),
        ("okay", "yes"),
        ("answer yes", "yes"),
        ("tell it no", "no"),
        ("tell the agent to go ahead", "yes"),
        ("yes and don't ask me again", "always"),
    ],
)
def test_bare_answers_are_for_the_session_being_talked_to(text, choice):
    a = ans(text, session="docs")
    assert (a["kind"], a["title"], a["name"], a["choice"]) == (
        "answer",
        "docs",
        "API docs",
        choice,
    )
    assert a["bare"] is True  # finish() turns it into a prompt without a dialog
    assert ans(text) == {"kind": "prompt"}  # no session: words, as before
    assert ans(text, session="gone") == {"kind": "prompt"}


def test_bare_number_answer():
    a = ans("option two", session="docs")
    assert (a["choice"], a["number"], a["words"]) == ("number", 2, "option two")


def test_the_second_one_is_an_option_only_while_the_session_asks():
    a = ans("the second one", session="docs")  # docs is asking
    assert (a["kind"], a["choice"], a["number"], a["title"]) == (
        "answer",
        "number",
        2,
        "docs",
    )
    assert ans("the second one", session="invoice-parser") == {
        "kind": "switch_index",
        "index": 2,
    }
    assert ans("the 3rd one") == {"kind": "switch_index", "index": 3}


@pytest.mark.parametrize(
    "text,choice,index",
    [
        ("session three, yes", "yes", 3),
        ("Number 2: no.", "no", 2),
        ("tell number 2 no", "no", 2),
        ("tell session four to go ahead", "yes", 4),
        ("answer yes to session 3", "yes", 3),
        ("say no for window one", "no", 1),
        ("the second session, always", "always", 2),
        ("yes to session five", "yes", 5),
    ],
)
def test_answer_by_number_is_mapped_by_the_client(text, choice, index):
    a = ans(text, session="invoice-parser")
    assert a == {
        "kind": "answer",
        "choice": choice,
        "index": index,
        "words": a["words"],
    }
    # Sent back with the client's title: the full answer for that session.
    b = vr.answer(text, "invoice-parser", ROWS, now=NOW, title="docs")
    assert (b["title"], b["name"], b["choice"]) == ("docs", "API docs", choice)
    assert "bare" not in b and "index" not in b


@pytest.mark.parametrize(
    "text,title,choice",
    [
        ("docs, always", "docs", "always"),
        ("API docs, yes", "docs", "yes"),
        ("the invoice parser: no", "invoice-parser", "no"),
        ("invoice, go ahead", "invoice-parser", "yes"),
        ("yes to the invoice parser", "invoice-parser", "yes"),
        ("tell docs to go ahead", "docs", "yes"),
        ("tell docs no", "docs", "no"),
        ("tell the parser yes", "invoice-parser", "yes"),
        ("answer no on billing", "mac::billing", "no"),
        ("answer always for docs", "docs", "always"),
    ],
)
def test_answer_by_name(text, title, choice):
    a = ans(text, session="login-fix")
    assert (a["kind"], a["title"], a["choice"]) == ("answer", title, choice)
    assert "bare" not in a


def test_answer_by_name_ambiguous_and_unknown():
    which = {"kind": "ambiguous", "say": "Which one: login-fix or login-page?"}
    assert ans("tell login yes") == which
    assert ans("login, yes") == which
    assert ans("tell narnia yes")["kind"] == "no-session"
    assert ans("answer yes to verify")["kind"] == "no-session"  # hidden off the rail
    # Said without a verb, an unknown name was never a target: words.
    assert ans("narnia, yes") == {"kind": "prompt"}
    assert ans("yes to narnia") == {"kind": "prompt"}


# --- read back -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "what is it asking",
        "What's it asking me?",
        "read the question",
        "read me the question again",
        "what does it want",
        "read that again",
        "what's the question",
    ],
)
def test_question_is_for_the_session(text):
    assert ans(text, session="docs") == {
        "kind": "question",
        "title": "docs",
        "name": "API docs",
    }
    assert ans(text) == {"kind": "question", "say": "No session is open."}


def test_question_targets():
    assert ans("what does docs want")["title"] == "docs"
    assert ans("what is the invoice parser asking")["title"] == "invoice-parser"
    assert ans("what is session 2 asking") == {"kind": "question", "index": 2}
    assert ans("what is session 2 asking", title="docs")["title"] == "docs"
    assert ans("what is login asking")["kind"] == "ambiguous"
    assert ans("what does the user want") == {"kind": "prompt"}
    assert ans("what did it say")["kind"] == "read"


# --- finish: the dialog --------------------------------------------------------------
PERM = {
    "id": "d1",
    "parsed": True,
    "question": "Run **ls**?",
    "command": "ls -la",
    "options": OPTS,
}
PICK = {"id": "d2", "parsed": True, "question": "Which branch?", "options": OTHER}
UNPARSED = {"id": "d3", "parsed": False, "question": "Should I push?", "options": []}


def fin(text, dialog, session="docs", spoken=None, remote=False, title=""):
    a = vr.answer(text, session, ROWS, now=NOW, title=title)
    return vr.finish(a, dialog, spoken, remote=remote)


def test_finish_picks_the_key_the_phone_would():
    assert fin("yes", PERM) == {
        "kind": "answer",
        "title": "docs",
        "name": "API docs",
        "choice": "yes",
        "words": "yes",
        "dialog_id": "d1",
        "keys": ["1"],
        "say": "Answered yes on API docs.",
    }
    assert fin("docs, always", PERM, session="")["keys"] == ["2"]
    assert fin("no, not always", PERM)["keys"] == ["3"]
    got = fin("always", dict(PERM, options=YN))
    assert (got["keys"], got["say"]) == (["1"], "Answered yes on API docs.")
    got = fin("option two", PICK)
    assert (got["keys"], got["say"]) == (["2"], "Answered develop on API docs.")
    # The target's words never pick an option: "session two" is no "two".
    got = fin("session two, yes", PICK, title="docs")
    assert (got["keys"], got["say"]) == (
        [],
        "I didn't catch that for API docs: say one, two, or three.",
    )


def test_finish_numbers_never_pick_a_permission():
    got = fin("option two", PERM)
    assert got["dialog_id"] == "d1" and got["keys"] == []
    assert got["say"] == "I didn't catch that for API docs: say yes, no, or always."


def test_finish_without_a_dialog():
    # Bare: the agent may have asked in words — the words go to it, as before.
    assert fin("yes", None) == {"kind": "prompt"}
    assert fin("yes", UNPARSED) == {"kind": "prompt"}
    # Targeted: said so.
    got = fin("docs, yes", None, session="")
    assert got["no_dialog"] is True and "keys" not in got
    assert got["say"] == "API docs is not asking anything."
    got = fin("docs, yes", UNPARSED, session="")
    assert got["say"] == "API docs is waiting on you: open the screen."
    got = fin("billing, yes", None, session="", remote=True)
    assert got["say"] == "billing is on another device: answer it there."


def test_finish_question_reads_it_out():
    assert fin("what is it asking", PERM)["say"] == (
        "API docs: Run ls? It wants to run: ls -la. Say yes, no, or always."
    )
    assert fin("read the question", PICK)["say"] == (
        "API docs: Which branch? one: main. two: develop. three: release. "
        "Say one, two, or three."
    )
    assert fin("what does it want", UNPARSED)["say"] == "API docs: Should I push?"
    spoken = {"say": "Should I push", "has_question": True}
    assert fin("what does it want", None, spoken=spoken)["say"] == (
        "API docs: Should I push."
    )
    spoken = {"say": "Done.", "has_question": False}
    assert fin("read that again", None, spoken=spoken)["say"] == (
        "API docs: Nothing is being asked."
    )


# --- the route -----------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(aliases, "owner_of", lambda *a, **k: "")
    state = SimpleNamespace(
        rows=list(ROWS),
        created=[],
        create=(202, None),
        row_reads=0,
        dialogs={},
        dialog_reads=[],
        spoken={},
    )

    def rows(request):
        state.row_reads += 1
        return state.rows

    async def create(payload, person):
        state.created.append(payload)
        status, body = state.create
        return status, body if body is not None else {"title": payload["title"]}

    async def read_dialog(title):
        state.dialog_reads.append(title)
        return state.dialogs.get(title)

    monkeypatch.setattr(voice_routes, "_session_rows", rows)
    monkeypatch.setattr(voice_routes, "_create_session", create)
    monkeypatch.setattr(voice_routes, "_read_dialog", read_dialog)
    monkeypatch.setattr(voice_routes, "_read_spoken", lambda t: state.spoken.get(t, {}))
    monkeypatch.setattr(server, "suggest_repos", lambda **kw: list(REPOS))
    monkeypatch.setattr(server, "_recent_repo_paths", lambda: [])
    return state


def route(text, session="", title=None, pending=None):
    body = {"text": text, "session": session}
    if title is not None:
        body["title"] = title
    if pending is not None:
        body["pending"] = pending
    r = client.post("/api/voice/route", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_route_prompt_reads_no_rows(env):
    assert route("fix the login bug", "docs") == {"kind": "prompt"}
    assert route("") == {"kind": "prompt"}
    assert route("never mind") == {"kind": "cancel"}
    assert env.row_reads == 0


def test_route_switch_and_list(env):
    assert route("Switch to the invoice parser.", "docs") == {
        "kind": "switch",
        "title": "invoice-parser",
        "name": "invoice-parser",
        "say": "Now on invoice-parser, working.",
    }
    assert route("list my sessions")["say"].startswith(
        "Sessions: invoice-parser (working), "
    )


def test_route_new_creates_like_the_new_dialog(env):
    got = route("start a new session in sitecheck and add tests for the queue", "docs")
    assert got == {
        "kind": "new",
        "title": "add-tests-queue",
        "name": "add-tests-queue",
        "say": "Started add-tests-queue in sitecheck-bot5.",
    }
    assert env.created == [
        {
            "title": "add-tests-queue",
            "repo_path": "/r/sitecheck-bot5",
            "prompt": "add tests for the queue",
        }
    ]
    assert voice.is_on(tmux.to_mindflock_tmux_name("add-tests-queue"))


def test_route_new_without_a_task_sends_no_prompt(env):
    asked = route("open a new session called parser in mindflock")
    assert asked["say"] == "What should it do in MindFlock?"
    assert env.created == []
    got = route("nothing yet", pending=asked["pending"])
    assert got["title"] == "parser" and got["say"] == "Started parser in MindFlock."
    assert env.created == [{"title": "parser", "repo_path": "/r/MindFlock"}]


def test_route_new_title_never_collides(env, monkeypatch):
    taken = SimpleNamespace(Branch="b")
    monkeypatch.setattr(
        server.ENGINE, "instances", {"fix-login-bug": taken, "fix-login-bug-2": taken}
    )
    got = route("new session in sitecheck to fix the login bug", "login-fix")
    assert got["title"] == "fix-login-bug-3"
    assert env.created[0]["title"] == "fix-login-bug-3"
    assert env.created[0]["repo_path"] == "/r/sitecheck-bot5"


def test_route_new_failures_are_spoken(env):
    env.create = (409, {"error": "instance parser already exists."})
    assert route("new session called parser in parser to fix it") == {
        "kind": "new-failed",
        "say": "Couldn't start it: instance parser already exists.",
    }
    assert not voice.is_on(tmux.to_mindflock_tmux_name("parser"))
    env.rows = []
    assert route("new session in narnia")["kind"] == "ask"
    assert len(env.created) == 1  # nothing created for an unknown repository


def test_route_bad_body(env):
    r = client.post("/api/voice/route", json=["nope"])
    assert r.status_code == 400


def test_session_rows_are_the_instances_listing(monkeypatch):
    """The real seam: ``GET /api/instances``'s own rows, other devices' too."""
    monkeypatch.setattr(server.ENGINE, "instances", {})
    remote = [{"title": "mac::billing", "name": "billing", "device": "mac"}]
    monkeypatch.setattr(server._remote, "merged_instances", lambda: remote)
    monkeypatch.setattr(server, "_pending_rows", lambda: [])
    got = client.post("/api/voice/route", json={"text": "go to billing", "session": ""})
    assert got.json() == {
        "kind": "switch",
        "title": "mac::billing",
        "name": "billing",
        "say": "Now on billing, idle.",
    }


def test_route_answer_carries_the_dialogs_keys(env):
    env.dialogs["docs"] = PERM
    assert route("yes", "docs") == {
        "kind": "answer",
        "title": "docs",
        "name": "API docs",
        "choice": "yes",
        "words": "yes",
        "dialog_id": "d1",
        "keys": ["1"],
        "say": "Answered yes on API docs.",
    }
    got = route("tell docs no", "invoice-parser")
    assert (got["dialog_id"], got["keys"]) == ("d1", ["3"])
    assert env.dialog_reads == ["docs", "docs"]


def test_route_answer_without_a_dialog(env):
    assert route("yes", "docs") == {"kind": "prompt"}
    got = route("docs, yes", "invoice-parser")
    assert got["no_dialog"] is True
    assert got["say"] == "API docs is not asking anything."


def test_route_numbered_answer_is_mapped_then_resent(env):
    env.dialogs["docs"] = PERM
    first = route("session three, always", "invoice-parser")
    assert first == {
        "kind": "answer",
        "choice": "always",
        "index": 3,
        "words": "always",
    }
    assert env.dialog_reads == []  # nothing to read until the client maps it
    # The client maps 3 → docs and sends the same phrase back with its title …
    got = route("session three, always", "invoice-parser", title="docs")
    assert (got["title"], got["keys"]) == ("docs", ["2"])
    # … or the bare words to that session.
    got = route(first["words"], "docs")
    assert (got["title"], got["keys"]) == ("docs", ["2"])


def test_route_remote_answer_is_never_read(env):
    got = route("billing, yes")
    assert got["no_dialog"] is True and got["title"] == "mac::billing"
    assert env.dialog_reads == []


def test_route_question(env):
    env.dialogs["docs"] = PERM
    assert route("what is it asking", "docs")["say"] == (
        "API docs: Run ls? It wants to run: ls -la. Say yes, no, or always."
    )
    env.spoken["invoice-parser"] = {"say": "Which file?", "has_question": True}
    assert route("what does the invoice parser want")["say"] == (
        "invoice-parser: Which file?"
    )
    assert route("what is session 4 asking") == {"kind": "question", "index": 4}


def test_route_switch_by_number(env):
    assert route("session three") == {"kind": "switch_index", "index": 3}
    assert route("next one") == {"kind": "switch_rel", "delta": 1}
    assert route("session three", title="docs")["say"] == (
        "Now on API docs, waiting on a question."
    )
    assert route("stop recording") == {"kind": "stop"}


def test_route_uses_the_persons_words(env, monkeypatch):
    from backend.web.core import voice_words

    monkeypatch.setattr(
        voice_routes, "_words", lambda: voice_words.effective({"stop": ["halt"]})
    )
    assert route("halt") == {"kind": "stop"}
    assert route("stop recording") == {"kind": "prompt"}
    assert env.row_reads == 0


def test_route_words_come_from_the_settings(env, isolate_settings_store):
    from backend.config import settings as S

    S.update_settings(voice={"words": {"hush": ["zip it"]}})
    S.invalidate()
    assert voice_routes._words()["hush"] == ["zip it"]
    assert route("zip it") == {"kind": "hush"}


# --------------------------------------------------------------------------- #
# Claude's question tool (/dialog kind "select")                               #
# --------------------------------------------------------------------------- #
def _ask(state: str) -> dict:
    """The /dialog body of a real AskUserQuestion capture."""
    from pathlib import Path

    from backend.providers import dialogs
    from backend.providers.claude import ClaudeProvider

    path = Path(__file__).parent / "data" / "dialogs"
    text = (path / ("claude2_ask_%s_w100.screen.txt" % state)).read_text("utf-8")
    screen = text.partition("\n")[2]
    return {
        "kind": "dialog",
        **dialogs.describe(ClaudeProvider().parse_dialog(screen), screen),
    }


def _keys(text, state):
    got = vr.select_answer(text, _ask(state))
    return None if got is None else got["keys"]


def test_select_speech_reads_the_question_and_its_choices():
    assert vr.dialog_speech("API docs", _ask("tabs")) == (
        "API docs asks: Which color should the button be? "
        "Options: Red, Green, Blue — say a number or the option."
    )
    assert vr.dialog_speech("API docs", _ask("multi_one")) == (
        "API docs asks: Which toppings do you want? Options: Cheese, Olives, "
        "Mushrooms — pick as many as you like, then say next. Ticked: Cheese."
    )
    assert vr.dialog_speech("API docs", _ask("review")) == (
        "API docs asks: Ready to submit your answers? "
        "Which color should the button be? Red. "
        "Which toppings do you want? Cheese, Mushrooms. Say submit or cancel."
    )


@pytest.mark.parametrize(
    "text,keys",
    [
        ("two", ["2"]),
        ("option two", ["2"]),
        ("the second one", ["2"]),
        ("Red", ["1"]),
        ("pick blue please", ["3"]),
        ("type something", ["4"]),
        ("other", ["4"]),
        ("chat about this", ["5"]),
        ("plain", None),  # no such label here (see the frameworks below)
        ("next", ["Right"]),
        ("submit", ["Right", "Right"]),  # on to the review tab, never Enter
        # A bare yes never picks a choice; two picks are no single answer.
        ("yes", None),
        ("one and three", None),
        ("back", None),  # already the first tab
        ("purple", None),
    ],
)
def test_select_answer_single(text, keys):
    assert _keys(text, "tabs") == keys


@pytest.mark.parametrize(
    "text,keys",
    [
        ("one and three", ["1", "3"]),
        ("cheese, olives and mushrooms", ["1", "2", "3"]),
        ("two", ["2"]),
        ("next", ["Right"]),  # the last question: on to the review tab
        ("submit", ["Right"]),
        ("done", ["Right"]),
        ("back", ["Left"]),
        ("go back", ["Left"]),
        ("yes", None),
    ],
)
def test_select_answer_multi(text, keys):
    assert _keys(text, "multi") == keys


def test_a_label_prefix_picks_and_a_number_past_the_end_does_not():
    assert _keys("plain", "single") == ["3"]
    assert _keys("plain html", "single") == ["3"]
    assert _keys("seven", "single") is None
    assert _keys("option three then", "single") == ["3"]


def test_a_ticked_box_is_left_alone_and_untick_clears_it():
    assert _keys("cheese and olives", "multi_one") == ["2"]
    got = vr.select_answer("cheese", _ask("multi_one"))
    assert got == {"keys": [], "said": "already ticked"}
    assert _keys("untick cheese", "multi_one") == ["1"]
    assert _keys("untick olives", "multi_one") == []


def test_select_answer_review():
    assert _keys("submit", "review") == ["1"]
    assert _keys("yes", "review") == ["1"]
    assert _keys("cancel", "review") == ["2"]
    assert _keys("no", "review") == ["2"]
    assert _keys("back", "review") == ["Left"]
    assert _keys("red", "review") is None


def test_the_free_text_row_takes_the_words():
    """With the cursor on "Type something" the phrase is the answer, typed,
    then Enter; leaving it for another choice goes Up first (a digit would
    be typed into the row)."""
    assert vr.select_answer("Vue please, with Pinia", _ask("text")) == {
        "keys": ["Enter"],
        "text": "Vue please, with Pinia",
        "said": "Vue please, with Pinia",
    }
    assert _keys("svelte", "text") == ["Up", "2"]
    assert _keys("yes", "text") is None
    # Away from the row, words are no answer.
    assert _keys("Vue please, with Pinia", "single") is None


def test_finish_answers_a_select_dialog():
    ans = {"kind": "answer", "title": "docs", "name": "API docs", "words": "option two"}
    got = vr.finish(dict(ans, choice="number", number=2), _ask("tabs"))
    assert got["keys"] == ["2"] and got["say"] == "Answered Green on API docs."
    assert got["dialog_id"] == _ask("tabs")["id"]
    nope = vr.finish(dict(ans, choice="yes", words="yes", bare=True), _ask("tabs"))
    assert nope["keys"] == []
    assert nope["say"].startswith("I didn't catch that for API docs. Options: Red")
    sure = vr.finish(dict(ans, choice="yes", words="yes"), _ask("review"))
    assert (
        sure["keys"] == ["1"] and sure["say"] == "Answered Submit answers on API docs."
    )


def test_route_answers_the_question_tool(env):
    env.dialogs["docs"] = _ask("multi")
    got = route("cheese and mushrooms", "docs")
    assert got == {
        "kind": "answer",
        "title": "docs",
        "name": "API docs",
        "words": "cheese and mushrooms",
        "dialog_id": _ask("multi")["id"],
        "keys": ["1", "3"],
        "say": "Answered Cheese and Mushrooms on API docs.",
    }
    # "go back" is a tab here, not the previous session.
    assert route("go back", "docs")["keys"] == ["Left"]
    # Words that answer nothing stay a prompt; "never mind" stays local.
    assert route("fix the login bug", "docs") == {"kind": "prompt"}
    assert route("never mind", "docs") == {"kind": "cancel"}


def test_route_cancel_on_the_review_screen_is_cancel(env):
    env.dialogs["docs"] = _ask("review")
    assert route("cancel", "docs")["keys"] == ["2"]
    assert route("submit", "docs")["keys"] == ["1"]


def test_route_free_text_answer_carries_the_text(env):
    env.dialogs["docs"] = _ask("text")
    got = route("Vue please", "docs")
    assert (got["text"], got["keys"]) == ("Vue please", ["Enter"])


def test_route_permission_dialogs_are_untouched(env):
    """Only the question tool reads a prompt phrase as an answer."""
    env.dialogs["docs"] = PERM
    assert route("red", "docs") == {"kind": "prompt"}
    assert route("go back", "docs")["kind"] == "switch_rel"


# --- switching in plain speech ---------------------------------------------------------
FLEET = [
    {
        "title": "sitecheck-bot7",
        "name": "sitecheck-bot7",
        "activity": "working",
        "activity_since": NOW - 600,
        "task_headline": "Fix settings screen",
        "last_say": "Started on the settings screen",
        "repo": "sitecheck-bot7",
        "path": "/r/sitecheck-bot7",
    },
    {
        "title": "sitecheck-bot7-copy-2",
        "name": "sitecheck-bot7-copy-2",
        "activity": "idle",
        "activity_since": NOW - 30,
        "last_say": "Done. The tests pass.",
    },
    {
        "title": "login-fix",
        "name": "login-fix",
        "activity": "idle",
        "activity_since": NOW - 3600,
        "last_say": "I fixed the redirect an hour ago",
        "task_headline": "Fix login redirect",
        "repo": "MindFlock",
        "path": "/r/MindFlock",
    },
    {"title": "settings-ui", "name": "settings-ui", "activity": "idle"},
    {"title": "mf-7", "name": "Invoice parser", "activity": "clarify"},
]
SITECHECK = [
    {"title": "sitecheck-bot7", "name": "sitecheck-bot7"},
    {"title": "sitecheck-bot7-copy-2", "name": "sitecheck-bot7-copy-2"},
]


def fleet(text, session="login-fix", pending=None):
    return vr.answer(text, session, FLEET, now=NOW, pending=pending)


@pytest.mark.parametrize(
    "text,title",
    [
        ("switch to the login fix session", "login-fix"),
        ("put me on the login fix", "login-fix"),
        ("take me to login fix please", "login-fix"),
        ("Jump over to the settings one.", "settings-ui"),
        ("go to settings", "settings-ui"),
        ("switch to the settings session", "settings-ui"),
        ("focus on settings", "settings-ui"),
        ("pull up the settings session", "settings-ui"),
        ("open settings ui", "settings-ui"),
        ("talk to login fix", "login-fix"),
        ("let's go to login fix", "login-fix"),
        ("can you switch to the login fix", "login-fix"),
        ("switch me over to settings", "settings-ui"),
        # Hyphens, digits and spoken numbers line up.
        ("switch to sitecheck bot seven", "sitecheck-bot7"),
        ("switch to site check bot 7", "sitecheck-bot7"),
        ("go to sitecheck bot seven copy two", "sitecheck-bot7-copy-2"),
        ("go to the copy", "sitecheck-bot7-copy-2"),
        # A plural, a wrong letter.
        ("switch to the login fixes", "login-fix"),
        ("go to the setings session", "settings-ui"),
        # The display name too.
        ("switch to the invoice parser", "mf-7"),
        ("open invoice parser", "mf-7"),
    ],
)
def test_switch_by_name_in_plain_speech(text, title):
    a = fleet(text)
    assert (a["kind"], a.get("title")) == ("switch", title), a


@pytest.mark.parametrize(
    "text",
    [
        "switch to the sitecheck session",
        "go to sitecheck bot",
        "open sitecheck",
        "take me to bot seven",
        "jump to the sitecheck one",
        "go to sitechek",
    ],
)
def test_several_matches_ask_which_one(text):
    assert fleet(text) == {
        "kind": "ambiguous",
        "say": "Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?",
        "candidates": SITECHECK,
        "pending": {"kind": "pick", "candidates": SITECHECK},
    }


@pytest.mark.parametrize(
    "text",
    [
        "open the file",
        "open the readme",
        "show me the logs",
        "show me the diff",
        "go back to the old approach",
        "talk to the API",
        "select all of it",
        "focus on the failing test",
        "can you go to the docs folder and fix the links",
        "could you switch to tabs instead of spaces",
        "let's go to lunch",
        "open a new session tab in the browser",
        "start a session",
        "pull up the stack trace",
        "bring up the error again",
        "back to the plan",
        "what did the tests say",
    ],
)
def test_plain_speech_that_stays_a_prompt(text):
    assert fleet(text) == {"kind": "prompt"}


def test_a_plain_go_to_still_says_there_is_no_such_session():
    a = fleet("switch to narnia")
    assert a["kind"] == "no-session" and a["say"].startswith(
        "No session called narnia."
    )


def test_find_sessions_prefers_the_whole_name():
    hit, cands = vr.find_sessions("sitecheck bot 7", FLEET)
    assert hit["title"] == "sitecheck-bot7" and cands == []
    hit, cands = vr.find_sessions("login fix", FLEET, "strict")
    assert hit["title"] == "login-fix"
    assert vr.find_sessions("fix", FLEET, "strict") == (None, [])
    assert vr.find_sessions("the session", FLEET) == (None, [])


# --- names as people SAY them --------------------------------------------------------
NAMED = [
    {"title": "sitecheck-bot7", "name": "sitecheck-bot7"},
    {"title": "SiteCheck_bot7-copy-2", "name": "SiteCheck_bot7-copy-2"},
    {"title": "login-fix", "name": "login-fix"},
    {"title": "URLParser2", "name": "URLParser2"},
    {"title": "voiceFixes", "name": "voiceFixes"},
    {"title": "voice-say", "name": "voice-say"},
    {"title": "vector-store", "name": "vector-store"},
    {"title": "vecter-store", "name": "vecter-store"},
]


def test_toks_split_hyphens_underscores_camel_case_and_digits():
    assert vr._toks("sitecheck-bot7") == ["sitecheck", "bot", "7"]
    assert vr._toks("SiteCheck_bot7-copy-2") == [
        "site",
        "check",
        "bot",
        "7",
        "copy",
        "2",
    ]
    assert vr._toks("URLParser2") == ["url", "parser", "2"]
    assert vr._toks("getHTMLNode") == ["get", "html", "node"]
    assert vr._toks("bot-seven") == ["bot", "7"]  # a number word is its digit
    assert vr._toks("don't-panic") == ["dont", "panic"]


@pytest.mark.parametrize(
    "said, title",
    [
        # Digits and number words, both ways; hyphens, underscores, camelCase.
        ("sitecheck bot seven", "sitecheck-bot7"),
        ("site check bot 7", "sitecheck-bot7"),
        ("sitecheck bot 7", "sitecheck-bot7"),
        ("sitecheck bot seven copy two", "SiteCheck_bot7-copy-2"),
        ("site check bot seven copy 2", "SiteCheck_bot7-copy-2"),
        ("url parser two", "URLParser2"),
        ("url parser 2", "URLParser2"),
        ("the url parser", "URLParser2"),
        ("voice fixes", "voiceFixes"),
        ("voice fix", "voiceFixes"),
        ("login fixes", "login-fix"),
        # Heard a little off: a sound-alike (voice_words.sounds_like), the
        # whole name said.
        ("sight check bot seven", "sitecheck-bot7"),
        ("cite check bot 7", "sitecheck-bot7"),
        ("sight check bot seven copy two", "SiteCheck_bot7-copy-2"),
        ("logon fix", "login-fix"),
        ("vecter store", "vecter-store"),
        ("vector store", "vector-store"),
    ],
)
def test_names_said_in_speech(said, title):
    hit, cands = vr.find_sessions(said, NAMED)
    assert hit is not None and hit["title"] == title, (said, cands)
    assert cands == []


@pytest.mark.parametrize(
    "said, titles",
    [
        # A sound-alike never picks silently: two names it fits are asked.
        ("vectar store", ["vector-store", "vecter-store"]),
        ("sight check", ["sitecheck-bot7", "SiteCheck_bot7-copy-2"]),
        ("bot seven", ["sitecheck-bot7", "SiteCheck_bot7-copy-2"]),
        ("voice six", ["voiceFixes", "voice-say"]),
    ],
)
def test_names_that_fit_two_sessions_ask(said, titles):
    hit, cands = vr.find_sessions(said, NAMED)
    assert hit is None and [c["title"] for c in cands] == titles


def test_a_sound_alike_switch_says_which_one():
    a = vr.answer("switch to the vectar store", "login-fix", NAMED, now=NOW)
    assert a["kind"] == "ambiguous"
    assert a["say"] == "Which one: vector-store or vecter-store?"
    a = vr.answer("switch to sight check bot seven", "login-fix", NAMED, now=NOW)
    assert (a["kind"], a["title"]) == ("switch", "sitecheck-bot7")


# --- the pick: the next phrase after "Which one?" ------------------------------------------
PICK_PENDING = {"kind": "pick", "candidates": SITECHECK}


@pytest.mark.parametrize(
    "text,title",
    [
        ("the first one", "sitecheck-bot7"),
        ("first", "sitecheck-bot7"),
        ("the second", "sitecheck-bot7-copy-2"),
        ("number two", "sitecheck-bot7-copy-2"),
        ("two", "sitecheck-bot7-copy-2"),
        ("the last one", "sitecheck-bot7-copy-2"),
        ("copy two", "sitecheck-bot7-copy-2"),
        ("the copy", "sitecheck-bot7-copy-2"),
        ("bot seven", "sitecheck-bot7"),
        ("I mean bot 7", "sitecheck-bot7"),
        ("sitecheck bot seven copy two", "sitecheck-bot7-copy-2"),
        ("the one that's working", "sitecheck-bot7"),
        ("the working one", "sitecheck-bot7"),
        ("the idle one", "sitecheck-bot7-copy-2"),
        ("go to the copy", "sitecheck-bot7-copy-2"),
    ],
)
def test_pick_answers_which_one(text, title):
    a = fleet(text, pending=PICK_PENDING)
    assert (a["kind"], a.get("title")) == ("switch", title), a


def test_pick_that_picks_nothing_is_routed_as_usual():
    # Not a pick: a prompt, a command, another session.
    assert fleet("fix the flaky test", pending=PICK_PENDING) == {"kind": "prompt"}
    assert fleet("never mind", pending=PICK_PENDING) == {"kind": "cancel"}
    assert fleet("switch to settings", pending=PICK_PENDING)["title"] == "settings-ui"
    assert fleet("stop listening", pending=PICK_PENDING) == {"kind": "stop"}
    # A candidate that is gone is no pick; none left: as usual.
    gone = {"kind": "pick", "candidates": [{"title": "gone", "name": "gone"}]}
    assert fleet("the first one", pending=gone)["kind"] == "switch_index"


def test_pick_out_of_range_or_no_state_asks_again():
    a = fleet("the third one", pending=PICK_PENDING)
    assert (
        a["say"] == "There are 2. Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?"
    )
    assert a["pending"] == PICK_PENDING
    a = fleet("the one asking", pending=PICK_PENDING)
    assert a["say"].startswith("None of them is asking. Which one:")
    assert a["kind"] == "ambiguous"


def test_pick_narrows_but_still_ties():
    three = {
        "kind": "pick",
        "candidates": SITECHECK + [{"title": "login-fix", "name": "login-fix"}],
    }
    a = fleet("the sitecheck one", pending=three)
    assert a["kind"] == "ambiguous" and a["candidates"] == SITECHECK


# --- the switch announcement: the last reply only when it is news -----------------------------
def test_switch_reads_a_reply_only_while_it_is_news():
    row = dict(FLEET[1])
    assert (
        vr.switch_line(row, NOW)
        == "Now on sitecheck-bot7-copy-2, idle. Done. The tests pass."
    )
    # Two minutes is the edge; past it the reply is old news.
    assert vr.switch_line(row, NOW - 30 + vr.FRESH_REPLY_S).endswith("The tests pass.")
    assert (
        vr.switch_line(row, NOW - 29 + vr.FRESH_REPLY_S)
        == "Now on sitecheck-bot7-copy-2, idle."
    )
    # No turn-end stamp: never news.
    assert (
        vr.switch_line(dict(row, activity_since=0), NOW)
        == "Now on sitecheck-bot7-copy-2, idle."
    )


def test_switch_says_the_state_and_the_task_never_a_stale_reply():
    # Working: its previous turn's line is not read.
    assert (
        fleet("go to sitecheck bot seven")["say"]
        == "Now on sitecheck-bot7, working: Fix settings screen."
    )
    # Done an hour ago: the state and the task, nothing more.
    assert (
        fleet("put me on the login fix")["say"]
        == "Now on login-fix, idle: Fix login redirect."
    )
    assert (
        fleet("switch to the invoice parser")["say"]
        == "Now on Invoice parser, waiting on a question."
    )
    assert fleet(
        "session two",
    ) == {"kind": "switch_index", "index": 2}
    assert vr.answer("session two", "", FLEET, now=NOW, title="login-fix")["say"] == (
        "Now on login-fix, idle: Fix login redirect."
    )


# --- read: the last reply on demand -------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "what did it say",
        "What did it say?",
        "read it",
        "read me the last reply",
        "read that",
        "what was its last answer",
    ],
)
def test_read_is_the_last_reply_whatever_its_age(text):
    assert fleet(text) == {
        "kind": "read",
        "title": "login-fix",
        "name": "login-fix",
        "say": "login-fix said: I fixed the redirect an hour ago.",
    }


def test_read_names_and_states():
    a = fleet("what did sitecheck bot7 say")
    assert (
        a["say"]
        == "sitecheck-bot7 is still working. Before that it said: Started on the settings screen."
    )
    assert (
        fleet("what did settings ui say")["say"]
        == "settings-ui hasn't said anything yet."
    )
    assert fleet("what did it say", session="")["say"] == "No session is open."
    assert fleet("what did narnia say") == {"kind": "prompt"}
    # "read it again" is still the question read out again.
    assert fleet("read it again")["kind"] == "question"


# --- the new-session conversation ----------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "start a new session",
        "I want to start a new session",
        "can you start a new session",
        "let's make a new session",
        "give me a new session",
        "spin one up",
        "spin up a new one",
        "spin another one up",
        "start another session",
        "launch a new agent",
        "create a session to fix the login bug",
        "start a session in parser",
    ],
)
def test_new_session_in_plain_speech(text):
    assert vr.command(text)["kind"] == "new"


def step(text, pending, session="login-fix", repos=REPOS):
    return vr.continue_new(text, pending, session, FLEET, repos)


def test_new_session_conversation_project_then_task():
    a = plan("start a new session", session="login-fix", rows=FLEET)
    assert (
        a["say"]
        == "In which project? MindFlock, sitecheck-bot5, sitecheck-bot6 or parser?"
    )
    b = step("the sitecheck repo", a["pending"])
    assert b == {
        "kind": "ask",
        "say": "What should it do in sitecheck-bot5?",
        "pending": {
            "kind": "new",
            "await": "task",
            "name": "",
            "task": "",
            "repo_name": "sitecheck-bot5",
            "repo_path": "/r/sitecheck-bot5",
        },
    }
    c = step("Fix the login bug in auth.py.", b["pending"])
    assert c == {
        "kind": "new",
        "title_base": "fix-login-bug-auth",
        "name": "",
        "repo_name": "sitecheck-bot5",
        "repo_path": "/r/sitecheck-bot5",
        "task": "Fix the login bug in auth.py.",
    }


@pytest.mark.parametrize(
    "said,path",
    [
        ("parser", "/r/parser"),
        ("in parser", "/r/parser"),
        ("the parser one", "/r/parser"),
        ("um, the mindflock project", "/r/MindFlock"),
        ("this one", "/r/MindFlock"),  # the current session's
        ("the same project", "/r/MindFlock"),
        ("the second one", "/r/sitecheck-bot5"),  # as read out
        ("site check bot six", "/r/sitecheck-bot6"),
    ],
)
def test_new_session_project_answers(said, path):
    a = plan("new session to add tests", session="login-fix", rows=FLEET)
    got = step(said, a["pending"])
    assert (got["kind"], got["repo_path"], got["task"]) == ("new", path, "add tests")


def test_new_session_project_and_task_in_one_answer():
    a = plan("spin one up", session="login-fix", rows=FLEET)
    got = step("parser, and add tests for the queue", a["pending"])
    assert (got["kind"], got["repo_path"], got["task"]) == (
        "new",
        "/r/parser",
        "add tests for the queue",
    )


def test_new_session_task_answers():
    p = {
        "kind": "new",
        "await": "task",
        "name": "",
        "task": "",
        "repo_name": "parser",
        "repo_path": "/r/parser",
    }
    assert step("to fix the flaky test", p)["task"] == "fix the flaky test"
    assert step("it should fix the flaky test", p)["task"] == "fix the flaky test"
    for none in ("nothing", "nothing yet", "just start it", "no task"):
        got = step(none, p)
        assert (got["kind"], got["task"], got["title_base"]) == (
            "new",
            "",
            "voice-session",
        )
    # The commands a task never is: the mic, the voice.
    assert step("stop listening", p) is None
    assert step("stop talking", p) is None
    # A project the server does not know: asked for again.
    forged = dict(p, repo_path="/etc")
    assert step("fix it", forged)["pending"]["await"] == "repo"


def test_new_session_cancel_and_not_an_answer():
    a = plan("start a new session", session="login-fix", rows=FLEET)
    for words in ("never mind", "cancel", "forget it", "don't start it"):
        assert step(words, a["pending"]) == {
            "kind": "new-failed",
            "say": "Okay, no new session.",
            "cancelled": True,
        }
    # A command while it asks for the project: routed as usual.
    assert step("switch to settings", a["pending"]) is None
    assert step("what needs me", a["pending"]) is None
    # Anything else: it did not catch a project, and asks again.
    got = step("narnia", a["pending"])
    assert got["say"].startswith(
        "I don't know a project called narnia. In which project?"
    )
    assert got["pending"]["await"] == "repo"


# --- the route: pending -----------------------------------------------------------------------------
def test_route_pick_round_trip(env):
    env.rows = FLEET
    asked = route("switch to the sitecheck session", "login-fix")
    assert asked["kind"] == "ambiguous" and asked["pending"] == PICK_PENDING
    got = route("copy two", "login-fix", pending=asked["pending"])
    assert (got["kind"], got["title"]) == ("switch", "sitecheck-bot7-copy-2")
    # Not a pick: the phrase as usual (a prompt reads no dialog to answer).
    assert route("fix the flaky test", "login-fix", pending=asked["pending"]) == {
        "kind": "prompt"
    }


def test_route_pick_beats_the_question_tool(env):
    env.rows = FLEET
    env.dialogs["login-fix"] = {
        "id": "s1",
        "parsed": True,
        "kind": "select",
        "question": "Which?",
        "options": [
            {"key": "1", "label": "a", "kind": "other"},
            {"key": "2", "label": "b", "kind": "other"},
        ],
    }
    got = route("two", "login-fix", pending=PICK_PENDING)
    assert (got["kind"], got["title"]) == ("switch", "sitecheck-bot7-copy-2")


def test_route_new_session_conversation(env):
    asked = route("start a new session", "docs")
    assert asked["kind"] == "ask" and asked["pending"]["await"] == "repo"
    assert env.created == []
    task = route("parser", "docs", pending=asked["pending"])
    assert task["say"] == "What should it do in parser?"
    got = route("add tests for the queue", "docs", pending=task["pending"])
    assert got == {
        "kind": "new",
        "title": "add-tests-queue",
        "name": "add-tests-queue",
        "say": "Started add-tests-queue in parser.",
    }
    assert env.created == [
        {
            "title": "add-tests-queue",
            "repo_path": "/r/parser",
            "prompt": "add tests for the queue",
        }
    ]
    assert (
        route("never mind", "docs", pending=asked["pending"])["say"]
        == "Okay, no new session."
    )
    assert len(env.created) == 1


def test_route_ignores_a_pending_that_is_not_an_object(env):
    assert route("fix it", "docs", pending="pick") == {"kind": "prompt"}
    assert route("fix it", "docs", pending={"kind": "mystery"}) == {"kind": "prompt"}


# --- help: questions about MindFlock itself ----------------------------------------
@pytest.mark.parametrize(
    "text, question",
    [
        ("how do I schedule a template?", "how do i schedule a template"),
        ("How does the fence work?", "how does the fence work"),
        ("What does autopilot do?", "what does autopilot do"),
        ("how do I add my Mac?", "how do i add my mac"),
        ("Where do I turn on the phone page?", "where do i turn on the phone page"),
        ("what is a team run", "what is a team run"),
        ("what are red zones", "what are red zones"),
        ("how does fast-track work", "how does fast track work"),
        ("what's the code map", "what's the code map"),
        ("how do I change the voice words", "how do i change the voice words"),
        ("tell me about peer links", "tell me about peer links"),
        ("explain team runs", "explain team runs"),
        (
            "is it possible to schedule a template",
            "is it possible to schedule a template",
        ),
        ("is there a way to pin a prompt", "is there a way to pin a prompt"),
        (
            "can I run two sessions on one branch",
            "can i run two sessions on one branch",
        ),
        ("how do you set up a fence", "how do you set up a fence"),
        ("which shortcut opens intake", "which shortcut opens intake"),
        ("what is a worktree in MindFlock", "what is a worktree in mindflock"),
        ("how do I open a PR in MindFlock", "how do i open a pr in mindflock"),
        ("ok, what's a session", "what's a session"),
        # Addressed to MindFlock: the lead is dropped, and the agent's-work
        # words no longer make it a prompt.
        ("MindFlock, how do I fix the login bug?", "how do i fix the login bug"),
        ("Hey MindFlock, what does the bell do?", "what does the bell do"),
        ("mindflock what is a lane", "what is a lane"),
        ("help: what is a team run", "what is a team run"),
    ],
)
def test_help_questions_about_mindflock(text, question):
    want = {"kind": "help", "text": question}
    assert vr.command(text) == want
    assert ans(text, session="docs") == want


@pytest.mark.parametrize(
    "text",
    [
        # The agent's work.
        "how do I fix the login bug?",
        "how do I run the tests",
        "how does this function work",
        "what does this error mean",
        "why are the tests failing",
        "where is the bug",
        "how does the template loader parse the file",
        "add a schedule button to the settings page",
        "how should we structure the settings component",
        "what does my project do",
        "fix the fence code",
        "help me fix the login bug",
        # Said to the agent ("you"), or about the session's state.
        "what are you doing",
        "how did you fix it",
        "can you open the settings",
        "could you add a template",
        "what is the status of the PR",
        "what's the session working on",
        "how many sessions are running",
        "what does the ticket say",
        "why does the session keep failing",
        "is it done",
        # Questions that name nothing of MindFlock's.
        "what is a closure",
        "how do I center a div",
        "what's the weather",
    ],
)
def test_help_is_never_the_agents_work(text):
    assert vr.command(text) is None or vr.command(text)["kind"] != "help"
    assert ans(text, session="docs").get("kind") != "help"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("what is it doing", "status"),
        ("what's going on", "status"),
        ("what did it say", "read"),
        ("what is it asking", "question"),
        ("what does docs want", "question"),
        ("what needs me", "needs"),
        ("what sessions are open", "list"),
        ("mindflock yes", "prompt"),
        ("go to templates", "no-session"),
    ],
)
def test_help_never_takes_another_commands_phrase(text, kind):
    assert ans(text, session="docs")["kind"] == kind


def test_route_help_carries_the_question(env):
    assert route("how does the fence work?", "docs") == {
        "kind": "help",
        "text": "how does the fence work",
    }
    # Said while Claude's question tool is up, help is still help.
    env.dialogs["docs"] = _ask("multi")
    assert route("mindflock, what is a team run", "docs") == {
        "kind": "help",
        "text": "what is a team run",
    }
