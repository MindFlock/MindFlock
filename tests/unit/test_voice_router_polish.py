"""The voice-command polish round (2026-11-10): a spoken command triggers the
first time, and a miss never passes silently.

Fillers before a navigation or read command, numbers to 99 and a session
number's homophones, "switch the <name>", the looser scroll words, "row N",
"the other one", "what am I looking at", "stop it" (No to a permission,
else an interrupt), a ``pending`` pick that remembers what it was for,
whisper over Moonshine when both heard a command, ``near`` on a prompt that
nearly was a command, and the stop/hush words read from voice_words.KIN.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import aliases, voice_actions
from backend.web.core import voice_router as vr
from backend.web.core import voice_words as vw
from backend.web.routes import voice as voice_routes
from backend.web.server import app

client = TestClient(app)
NOW = 1_800_000_000.0

ROWS = [
    {"title": "docs", "name": "docs", "activity": "idle", "status": "ready"},
    {"title": "login-fix", "name": "login-fix", "activity": "idle", "status": "ready"},
    {
        "title": "login-page",
        "name": "login-page",
        "activity": "idle",
        "status": "ready",
        "last_say": "Page is done.",
    },
    {
        "title": "sitecheck-bot7",
        "name": "sitecheck-bot7",
        "activity": "working",
        "status": "ready",
    },
]
OPTS = [
    {"key": "1", "label": "Yes", "kind": "yes"},
    {"key": "2", "label": "Yes, and don't ask again", "kind": "always"},
    {"key": "3", "label": "No", "kind": "no"},
]
PERM = {"id": "d1", "parsed": True, "question": "Run ls?", "options": OPTS}


def ans(text, session="", title="", pending=None):
    return vr.answer(text, session, ROWS, now=NOW, title=title, pending=pending)


def cmd(text):
    got = vr.command(text)
    return got and got["kind"]


# --- 1. fillers ---------------------------------------------------------------
@pytest.mark.parametrize(
    "text, kind",
    [
        ("um switch to docs", "switch"),
        ("uh, take me to docs", "switch"),
        ("so scroll up", "scroll"),
        ("okay so, scroll down", "scroll"),
        ("alright, next session", "switch_rel"),
        ("now next session", "switch_rel"),
        ("now status", "status"),
        ("um, status", "status"),
        ("so what needs me", "needs"),
        ("um list sessions", "list"),
        ("uh go back", "switch_back"),
        ("so where am i", "where"),
        ("um session three", "switch_index"),
        ("well, what's docs doing", "status"),
        ("so what did docs say", "read"),
        ("can you go to the next session", "switch_rel"),
        ("could you go to the previous one", "switch_rel"),
    ],
)
def test_fillers_before_a_navigation_command(text, kind):
    assert cmd(text) == kind


def test_a_filler_never_widens_an_answer_or_a_new_session():
    # "um yes" is no bare yes: "um" would be a session's name, and there is
    # none, so it is words.
    assert ans("um yes", session="docs") == {"kind": "prompt"}
    assert ans("so yes", session="docs") == {"kind": "prompt"}
    assert cmd("so new session to fix it") is None
    assert cmd("um cancel") != "cancel"


def test_a_filler_makes_a_switch_need_a_match():
    """ "So switch to the new driver" is likely words: no session, a prompt."""
    assert ans("so switch to the new parser")["kind"] == "prompt"
    got = ans("um switch to docs")
    assert (got["kind"], got["title"]) == ("switch", "docs")


# --- 2. numbers -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text, n",
    [
        ("session number three", 3),
        ("session twenty one", 21),
        ("session twenty-one", 21),
        ("session ninety nine", 99),
        ("session forty", 40),
        ("switch to session number 12", 12),
        ("go to session thirty two", 32),
        ("window number 2", 2),
        ("row 2", 2),
        ("go to row two", 2),
        ("the twenty first session", None),  # ordinals stop at tenth (words)
        ("session to", 2),
        ("session too", 2),
        ("session for", 4),
        ("session won", 1),
        ("session free", 3),
        ("session ate", 8),
        ("switch to session for", 4),
    ],
)
def test_session_numbers(text, n):
    got = vr.command(text)
    if n is None:
        assert got is None or got["kind"] != "switch_index"
        return
    assert got == {"kind": "switch_index", "index": n}


@pytest.mark.parametrize(
    "text", ["option to", "option for", "number to", "choice won", "to", "for"]
)
def test_homophones_never_pick_an_option(text):
    """A wrong number is costly in a dialog: "to"/"for" stay words there."""
    assert vr.choice_of(text) is None
    got = vr.command(text)
    assert got is None or got["kind"] not in ("switch_index", "answer", "pick")
    assert vr.parse_answer(text, OPTS) is None


# --- 3. "switch the <name>" -----------------------------------------------------
def test_switch_the_name_only_when_a_session_matches():
    got = ans("switch the docs")
    assert (got["kind"], got["title"]) == ("switch", "docs")
    got = ans("switch the sitecheck bot seven")
    assert (got["kind"], got["title"]) == ("switch", "sitecheck-bot7")
    assert ans("switch the database driver to postgres") == {"kind": "prompt"}
    assert ans("switch the order of these two lines") == {"kind": "prompt"}


# --- 4. scroll and the gate's held phrases ---------------------------------------
@pytest.mark.parametrize(
    "text, dir_, amount, count",
    [
        ("scroll a bit up", "up", "half", 1),
        ("scroll a little down", "down", "half", 1),
        ("scroll up more", "up", "page", 1),
        ("scroll down a bit more", "down", "half", 1),
        ("scroll way up", "up", "page", 3),
        ("go up", "up", "page", 1),
        ("go down a bit", "down", "half", 1),
        ("move down", "down", "page", 1),
        ("up", "up", "page", 1),
        ("down", "down", "page", 1),
        ("next page", "down", "page", 1),
        ("previous page", "up", "page", 1),
        ("bottom", "down", "bottom", 1),
        ("the bottom", "down", "bottom", 1),
        ("to the top", "up", "top", 1),
        ("the very top", "up", "top", 1),
        ("now scroll a bit up", "up", "half", 1),
    ],
)
def test_looser_scroll_words(text, dir_, amount, count):
    assert vr.command(text) == {
        "kind": "scroll",
        "dir": dir_,
        "amount": amount,
        "count": count,
    }


@pytest.mark.parametrize(
    "text",
    [
        "top",  # how "stop" is often written
        "scroll position is wrong",
        "go up the stairs",
        "the top of the file",
        "next page of results is empty",
        "up to you",
    ],
)
def test_scroll_words_that_stay_words(text):
    assert cmd(text) != "scroll"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("the other one", "switch_back"),
        ("go to the other session", "switch_back"),
        ("what am i looking at", "where"),
        ("which session is that", "where"),
        ("what session is it", "where"),
        ("which one is this", "where"),
    ],
)
def test_the_gates_held_phrases(text, kind):
    assert cmd(text) == kind


# --- 5. "stop it" ----------------------------------------------------------------
def fin(text, dialog, session="docs"):
    return vr.finish(ans(text, session=session), dialog)


@pytest.mark.parametrize("text", ["stop it", "stop that", "stop this"])
def test_stop_it_is_no_to_a_permission_else_an_interrupt(text):
    got = fin(text, PERM)
    assert (got["kind"], got["keys"]) == ("answer", ["3"])  # No, unchanged
    assert fin(text, None) == {"kind": "interrupt", "title": "docs", "name": "docs"}
    unparsed = {"id": "d3", "parsed": False, "options": []}
    assert fin(text, unparsed)["kind"] == "interrupt"


def test_only_stop_it_interrupts():
    assert fin("no", None)["kind"] == "prompt"
    assert fin("don't", None)["kind"] == "prompt"


# --- 6. a "Which one" remembers what it was for ----------------------------------
LOGINS = [
    {"title": "login-fix", "name": "login-fix"},
    {"title": "login-page", "name": "login-page"},
]


def test_every_which_one_carries_a_pending_pick():
    for text, then in (
        ("tell login yes", {"kind": "answer", "choice": "yes", "words": "yes"}),
        ("interrupt login", {"kind": "interrupt"}),
        ("what does login want", {"kind": "question"}),
        ("switch to login", None),
    ):
        got = ans(text, session="docs")
        assert got["kind"] == "ambiguous", (text, got)
        assert got["pending"]["kind"] == "pick"
        assert got["pending"]["candidates"] == LOGINS
        assert got["pending"].get("then") == then, text


def test_a_pick_does_what_the_question_was_for():
    pend = ans("interrupt login", session="docs")["pending"]
    assert vr.pick("the second one", pend, ROWS, NOW) == {
        "kind": "interrupt",
        "title": "login-page",
        "name": "login-page",
    }
    pend = ans("tell login no", session="docs")["pending"]
    got = vr.pick("the first one", pend, ROWS, NOW)
    assert got == {
        "kind": "answer",
        "title": "login-fix",
        "name": "login-fix",
        "choice": "no",
        "words": "no",
    }
    pend = {"kind": "pick", "candidates": LOGINS, "then": {"kind": "read"}}
    got = vr.pick("login page", pend, ROWS, NOW)
    assert (got["kind"], got["title"]) == ("read", "login-page")
    assert got["say"] == "login-page said: Page is done."
    # Asked again, narrowed or not, it still remembers.
    again = vr.pick("the fifth one", pend, ROWS, NOW)
    assert again["pending"]["then"] == {"kind": "read"}


def test_a_pick_by_number_counts_what_was_read_out():
    # "Which one: login-fix or login-page?" — then login-fix closes. "The
    # first one" is still login-fix (gone), never login-page, which moved up
    # into first place: a "no" for login-fix must not press it in login-page.
    rows = [r for r in ROWS if r["title"] != "login-fix"]
    then = {"kind": "answer", "choice": "no", "words": "no"}
    pend = {"kind": "pick", "candidates": LOGINS, "then": then}
    got = vr.pick("the first one", pend, rows, NOW)
    assert got.get("title") != "login-page", got
    assert got == {"kind": "no-session", "say": "login-fix is not listed."}
    # The second (and last) one read out is still there.
    for text in ("the second one", "the last one"):
        got = vr.pick(text, pend, rows, NOW)
        assert (got["kind"], got["title"]) == ("answer", "login-page"), text
    # login-page gone: the last one read out is gone, not login-fix.
    rows = [r for r in ROWS if r["title"] != "login-page"]
    assert vr.pick("the last one", pend, rows, NOW)["kind"] == "no-session"


def test_a_pick_without_then_still_switches():
    pend = {"kind": "pick", "candidates": LOGINS}
    got = vr.pick("the first one", pend, ROWS, NOW)
    assert (got["kind"], got["title"]) == ("switch", "login-fix")
    pend = {"kind": "pick", "candidates": LOGINS, "then": {"kind": "bogus"}}
    assert vr.pick("the first one", pend, ROWS, NOW)["kind"] == "switch"


# --- 8. near: a prompt that nearly was a command -----------------------------------
def test_a_sound_alike_name_switches_after_a_switch_verb():
    for text in ("switch to docks", "go to dogs", "take me to the docks session"):
        got = ans(text)
        assert (got["kind"], got["title"]) == ("switch", "docs"), text


def test_a_sound_alike_after_a_soft_verb_is_only_suggested():
    assert ans("open docks") == {
        "kind": "prompt",
        "near": {
            "kind": "switch",
            "why": "no_session",
            "arg": "docks",
            "suggest": "docs",
        },
    }
    got = ans("switch the docks")
    assert got["kind"] == "prompt" and got["near"]["suggest"] == "docs"


def test_near_shapes():
    assert ans("can you switch to narnia") == {
        "kind": "prompt",
        "near": {"kind": "switch", "why": "no_session", "arg": "narnia"},
    }
    assert ans("what's narnia doing") == {
        "kind": "prompt",
        "near": {"kind": "status", "why": "no_session", "arg": "narnia"},
    }
    assert ans("what does narnia want") == {
        "kind": "prompt",
        "near": {"kind": "question", "why": "no_session", "arg": "narnia"},
    }
    assert ans("interrupt the narnia session", session="docs") == {
        "kind": "prompt",
        "near": {"kind": "interrupt", "why": "no_session", "arg": "the narnia session"},
    }
    assert ans("yes", session="docs")["kind"] == "answer"  # finished by the route
    assert fin("yes", None)["near"] == {
        "kind": "answer",
        "why": "no_dialog",
        "arg": "yes",
    }


@pytest.mark.parametrize(
    "text",
    [
        "what is the test doing",
        "what did the tests say",
        "what does the user want",
        "interrupt handler is broken",
        "open the file",
        "show me the logs",
        "could you switch to tabs instead of spaces",
        "can you go to the docs folder and fix the links",
        "open a new session tab in the browser",
    ],
)
def test_dictation_gets_no_near(text):
    assert ans(text, session="docs") == {"kind": "prompt"}


# --- 7. whisper over Moonshine --------------------------------------------------------
KINDS = voice_routes._heard_kinds()


@pytest.mark.parametrize(
    "text, heard, want",
    [
        ("go bag", "go back", ("go back", None)),  # whisper no command: Moonshine
        ("where am i", "go back", ("where am i", None)),  # both: whisper
        ("scroll up", "scroll down", ("scroll up", None)),  # disagree: whisper
        ("fix the go back button", "go back", ("go back", None)),
        ("go back", "fix the go back button", ("go back", None)),  # heard is words
        ("yes", "new session to fix it", ("yes", None)),  # heard carries words
        ("anything", "", ("anything", None)),
        (
            "Which is sitecheck bot seven",
            "switch to site check about seven",
            ("switch to site check about seven", "switch to sitecheck bot seven"),
        ),
    ],
)
def test_heard_text(text, heard, want):
    assert vr.heard_text(text, heard, None, KINDS) == want


# --- 9. the stop / hush words are voice_words' ------------------------------------------
def _said(kind):
    out = []
    for p in vw.DEFAULTS[kind]:
        out.append(p)
        out.extend(vw.KIN.get(kind, {}).get(p, ()))
    return out


@pytest.mark.parametrize(
    "kind, phrase", [(k, p) for k in ("stop", "hush") for p in _said(k)]
)
def test_router_stop_and_hush_read_voice_words_kin(kind, phrase):
    """Every default stop/hush phrase and every KIN wording of it is that
    kind to the router — and to voice_words.control_kind when it reads the
    phrase at all (the client's radio check)."""
    assert cmd(phrase) == kind, phrase
    ck = vw.control_kind(phrase)
    assert ck in (None, kind), (phrase, ck)


def test_ok_stop_is_hush_as_voice_words_says():
    assert vw.control_kind("ok stop") == "hush"
    assert cmd("ok stop") == "hush"
    assert cmd("okay stop") == "hush"
    assert cmd("stop") == "stop"


# --- the route --------------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(aliases, "owner_of", lambda *a, **k: "")
    state = {"dialogs": {}, "pressed": []}

    async def read_dialog(title):
        return state["dialogs"].get(title)

    def press(title):
        state["pressed"].append(title)
        return True

    monkeypatch.setattr(voice_routes, "_session_rows", lambda request: list(ROWS))
    monkeypatch.setattr(voice_routes, "_read_dialog", read_dialog)
    monkeypatch.setattr(voice_routes, "_read_spoken", lambda t: {})
    monkeypatch.setattr(voice_routes, "_press_escape", press)
    voice_actions._reset_for_tests()
    yield state
    voice_actions._reset_for_tests()


def route(text, session="", **more):
    body = dict({"text": text, "session": session}, **more)
    r = client.post("/api/voice/route", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize(
    "text, more",
    [
        ("Session one.", {}),
        ("Session one.", {"heard": "session one"}),
        ("Session one.", {"heard": "session won"}),
        ("session one", {"heard": "go back"}),  # whisper's command wins
        (
            "Session one.",
            {"pending": {"kind": "pick", "candidates": LOGINS}},  # a stale pick
        ),
        ("session six", {}),  # beyond the list: the CLIENT says so
    ],
)
def test_route_session_number_comes_back_as_an_index(env, text, more):
    """The router answers a numbered session with ``index`` for the client
    to map — never a prompt, whatever rides along."""
    got = route(text, "docs", **more)
    assert got["kind"] == "switch_index" and got["index"] in (1, 6), got


def test_route_stop_it_interrupts_with_nothing_asking(env):
    assert route("stop it", "docs") == {
        "kind": "interrupt",
        "title": "docs",
        "ok": True,
    }
    assert env["pressed"] == ["docs"]


def test_route_stop_it_answers_no_to_a_permission(env):
    env["dialogs"]["docs"] = PERM
    got = route(
        "stop it",
        "docs",
        spoke_ms_ago=0,
        dialog_seen={"docs": {"id": "d1", "ms_ago": 10_000}},
    )
    assert (got["kind"], got["keys"]) == ("answer", ["3"])
    assert env["pressed"] == []


def test_route_pick_interrupts_the_one_picked(env):
    got = route("interrupt login", "docs")
    assert got["kind"] == "ambiguous" and env["pressed"] == []
    got = route("the second one", "docs", pending=got["pending"])
    assert got == {"kind": "interrupt", "title": "login-page", "ok": True}
    assert env["pressed"] == ["login-page"]


def test_route_pick_answers_the_one_picked(env):
    env["dialogs"]["login-fix"] = PERM
    got = route("tell login yes", "docs")
    assert got["kind"] == "ambiguous"
    got = route(
        "the first one",
        "docs",
        pending=got["pending"],
        spoke_ms_ago=0,
        dialog_seen={"login-fix": {"id": "d1", "ms_ago": 10_000}},
    )
    assert (got["kind"], got["title"], got["keys"]) == ("answer", "login-fix", ["1"])


def test_route_heard_switch_takes_whispers_name(env):
    got = route(
        "Which is sitecheck bot seven",
        "docs",
        heard="switch to site check about seven",
    )
    assert (got["kind"], got["title"]) == ("switch", "sitecheck-bot7")


def test_route_whisper_wins_when_both_are_commands(env):
    assert route("where am i", "docs", heard="go back") == {"kind": "where"}
    assert route("go bag", "docs", heard="go back") == {"kind": "switch_back"}


def test_route_near_reply(env):
    assert route("can you switch to narnia", "docs") == {
        "kind": "prompt",
        "near": {"kind": "switch", "why": "no_session", "arg": "narnia"},
    }
    assert route("yes", "docs") == {
        "kind": "prompt",
        "near": {"kind": "answer", "why": "no_dialog", "arg": "yes"},
    }
