"""The voice assistant's brain (``core.voice_assistant``) and its routes.

No model ever runs: the long-lived process is a :class:`FakePopen` that
answers each user line with scripted ``stream-json`` lines, and the turn
tests use a :class:`ScriptBrain` that hands back output lines directly. The
server executors (create, send, settings, help) are the ``Context``'s
callables, faked per test; settings land in the conftest's per-test
``MINDFLOCK_SETTINGS_FILE``.
"""

from __future__ import annotations

import asyncio
import json
import queue
import time

import pytest
from fastapi.testclient import TestClient

from backend import providers
from backend.config import settings as settings_store
from backend.providers.base import CodingProvider
from backend.web.core import voice_assistant as va
from backend.web.routes import voice as voice_routes
from backend.web.server import app

NOW = time.time()
ROWS = [
    {
        "title": "docs-cleanup",
        "repo": "MindFlock",
        "activity": "idle",
        "activity_since": NOW - 60,
    },
    {
        "title": "sitecheck-bot7-login",
        "repo": "sitecheck-bot7",
        "activity": "working",
        "activity_since": NOW - 420,
        "last_turn": "Running the tests now.",
    },
    {
        "title": "parser",
        "repo": "MindFlock",
        "activity": "clarify",
        "asking": {"kind": "dialog", "text": "Allow edit?"},
    },
    {"title": "parser-two", "repo": "MindFlock", "activity": "idle"},
]
REPOS = [
    {"name": "MindFlock", "path": "/r/MindFlock"},
    {"name": "sitecheck-bot7", "path": "/r/sitecheck-bot7"},
]
SNAP = {
    "view": "grid",
    "focused": "docs-cleanup",
    "commands": ["New session…", "Toggle theme", "Delete session"],
    "clickables": ["New", "Discard changes", "Save"],
    "prefs": [
        {"key": "chime", "label": "Chime", "type": "bool", "value": True},
        {
            "key": "density",
            "label": "Density",
            "type": "select",
            "value": "cozy",
            "options": ["cozy", "compact"],
        },
    ],
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings_store.invalidate()
    va._PENDING.clear()
    yield
    va._PENDING.clear()
    settings_store.invalidate()


def run(coro):
    return asyncio.run(coro)


class Fake:
    """The Context's executors, recording calls."""

    def __init__(self, rows=ROWS, repos=REPOS, may=True):
        self.rows, self.repos_list, self.may = rows, repos, may
        self.created, self.sent, self.written, self.asked = [], [], [], []
        self.create_answer = None
        self.send_answer = (200, {"sent": True})

    def ctx(self):
        async def rows():
            return self.rows

        async def repos(_rows):
            return self.repos_list

        async def create(plan, program):
            self.created.append((plan, program))
            return self.create_answer or {"kind": "new", "title": plan["title_base"]}

        async def send(title, text):
            self.sent.append((title, text))
            return self.send_answer

        async def may_configure():
            return self.may

        async def help_(q):
            self.asked.append(q)
            return {"say": "From Zones: a red zone is a keep-out path."}

        async def write(group, field, value):
            self.written.append((group, field, value))

        return va.Context(
            rows=rows,
            repos=repos,
            create=create,
            send=send,
            may_configure=may_configure,
            help=help_,
            write_setting=write,
        )


class ScriptBrain(va.Brain):
    """A brain whose model answers with ``lines`` (no process)."""

    def __init__(self, lines, errors=()):
        super().__init__()
        self.lines, self.errors, self.messages = list(lines), list(errors), []

    def ask(self, build, conversation, out, follow=None):
        self.messages.append(build({}))
        for e in self.errors:
            out.put(("error", e))
        for line in self.lines:
            out.put(("line", line))
        out.put(None)


def turn(text, lines=(), fake=None, snapshot=SNAP, brain=None, **payload):
    fake = fake or Fake()
    brain = brain or ScriptBrain(lines)
    body = dict({"text": text, "conversation": "c1", "snapshot": snapshot}, **payload)

    async def go():
        return [ev async for ev in va.turn(body, fake.ctx(), brain)]

    return run(go()), fake, brain


def kinds(events):
    return [e["type"] for e in events]


# --- the output protocol --------------------------------------------------------
def test_line_parser_completes_lines_across_chunks():
    p = va.LineParser()
    assert p.feed('ACT {"do":"op') == []
    assert p.feed('en","target":"intake"}\nSA') == [
        'ACT {"do":"open","target":"intake"}'
    ]
    assert p.feed("Y hello") == []
    assert p.feed("\n\n") == ["SAY hello"]
    assert p.feed("ASK which one?") == []
    assert p.flush() == ["ASK which one?"]
    assert p.flush() == []


@pytest.mark.parametrize(
    "line, want",
    [
        ('ACT {"do":"close"}', {"kind": "act", "action": {"do": "close"}}),
        ('act: {"do":"close"}', {"kind": "act", "action": {"do": "close"}}),
        (
            '- ACT {"do":"view","mode":"sky"}',
            {"kind": "act", "action": {"do": "view", "mode": "sky"}},
        ),
        ('`ACT {"do":"close"}`', {"kind": "act", "action": {"do": "close"}}),
        (
            "SAY Two sessions need you.",
            {"kind": "say", "text": "Two sessions need you."},
        ),
        ('SAY "quoted"', {"kind": "say", "text": "quoted"}),
        ("ASK Which project?", {"kind": "ask", "text": "Which project?"}),
        (
            'CONFIRM {"do":"session_action","session":"parser","action":"delete"} Delete parser?',
            {
                "kind": "confirm",
                "action": {
                    "do": "session_action",
                    "session": "parser",
                    "action": "delete",
                },
                "text": "Delete parser?",
            },
        ),
        ("ACT {not json", {"kind": "bad", "text": "ACT {not json"}),
        ("ACT open intake", {"kind": "bad", "text": "ACT open intake"}),
        (
            "Sure, I can help with that.",
            {"kind": "prose", "text": "Sure, I can help with that."},
        ),
        ("```", None),
        ("   ", None),
        ("SAY", None),
    ],
)
def test_parse_line(line, want):
    assert va.parse_line(line) == want


# --- validation -----------------------------------------------------------------
@pytest.mark.parametrize(
    "action, want, destructive",
    [
        ({"do": "open", "target": "intake"}, {"do": "open", "target": "intake"}, False),
        (
            {"do": "open", "target": "settings:voice"},
            {"do": "open", "target": "settings:voice"},
            False,
        ),
        (
            {"do": "open", "target": "settings:Your phone"},
            {"do": "open", "target": "settings:mobile"},
            False,
        ),
        ({"do": "close"}, {"do": "close"}, False),
        ({"do": "view", "mode": "Sky"}, {"do": "view", "mode": "sky"}, False),
        (
            {"do": "focus", "session": "sitecheck"},
            {"do": "focus", "session": "sitecheck-bot7-login"},
            False,
        ),
        (
            {"do": "session_action", "action": "commit"},
            {"do": "session_action", "session": "docs-cleanup", "action": "commit"},
            False,
        ),
        (
            {"do": "session_action", "session": "parser", "action": "delete"},
            {"do": "session_action", "session": "parser", "action": "delete"},
            True,
        ),
        (
            {"do": "session_action", "session": "parser", "action": "merge"},
            {"do": "session_action", "session": "parser", "action": "merge"},
            True,
        ),
        (
            {"do": "command", "label": "toggle theme"},
            {"do": "command", "label": "Toggle theme"},
            False,
        ),
        (
            {"do": "command", "label": "Delete session"},
            {"do": "command", "label": "Delete session"},
            True,
        ),
        ({"do": "click", "label": "save"}, {"do": "click", "label": "Save"}, False),
        (
            {"do": "click", "label": "Discard changes"},
            {"do": "click", "label": "Discard changes"},
            True,
        ),
        (
            {"do": "pref", "key": "chime", "value": "off"},
            {"do": "pref", "key": "chime", "value": False},
            False,
        ),
        (
            {"do": "pref", "key": "density", "value": "Compact"},
            {"do": "pref", "key": "density", "value": "compact"},
            False,
        ),
        ({"do": "type", "text": "fix it"}, {"do": "type", "text": "fix it"}, False),
        (
            {"do": "send", "session": "docs-cleanup", "text": " go "},
            {"do": "send", "session": "docs-cleanup", "text": "go"},
            False,
        ),
        ({"do": "status"}, {"do": "status"}, False),
        (
            {"do": "status", "session": "sitecheck"},
            {"do": "status", "session": "sitecheck-bot7-login"},
            False,
        ),
        (
            {"do": "help", "question": "what is a lane"},
            {"do": "help", "question": "what is a lane"},
            False,
        ),
        (
            {"do": "setting", "path": "notifications.ask_popup", "value": "off"},
            {"do": "setting", "path": "notifications.ask_popup", "value": False},
            False,
        ),
        (
            {"do": "setting", "path": "voice.assistant_talk", "value": "Chatty"},
            {"do": "setting", "path": "voice.assistant_talk", "value": "chatty"},
            False,
        ),
        (
            {"do": "new_session", "repo": "MindFlock", "prompt": "add a README"},
            {"do": "new_session", "repo": "MindFlock", "prompt": "add a README"},
            False,
        ),
    ],
)
def test_validate_accepts_and_normalises(action, want, destructive):
    got, problem, destr = va.validate(action, SNAP, ROWS)
    assert problem is None
    assert got == want
    assert destr is destructive


@pytest.mark.parametrize(
    "action, kind",
    [
        ("open intake", "error"),
        ({"do": "rm_rf"}, "error"),
        ({}, "error"),
        ({"do": "open", "target": "the-matrix"}, "error"),
        (
            {"do": "open", "target": "commit"},
            "error",
        ),  # needs a session: a session action
        ({"do": "open", "target": "settings:traffic"}, "error"),
        ({"do": "view", "mode": "list"}, "error"),
        ({"do": "focus", "session": "nonexistent-thing"}, "error"),
        (
            {"do": "focus", "session": "parser"},
            None,
        ),  # exact title beats the prefix tie
        ({"do": "focus", "session": "pars"}, "ask"),  # parser or parser-two
        ({"do": "session_action", "session": "parser", "action": "explode"}, "error"),
        ({"do": "command", "label": "Format disk"}, "error"),
        ({"do": "click", "label": "Invisible button"}, "error"),
        ({"do": "pref", "key": "nope", "value": 1}, "error"),
        ({"do": "pref", "key": "density", "value": "huge"}, "error"),
        ({"do": "type", "text": ""}, "error"),
        ({"do": "send", "session": "parser", "text": ""}, "error"),
        ({"do": "setting", "path": "general.auth_token", "value": "x"}, "error"),
        ({"do": "setting", "path": "github.token", "value": "x"}, "error"),
        (
            {"do": "setting", "path": "general.serve_mode", "value": "tailscale"},
            "error",
        ),
        ({"do": "setting", "path": "engine.skip_permissions", "value": True}, "error"),
        (
            {"do": "setting", "path": "notifications.ask_popup", "value": "maybe"},
            "error",
        ),
        ({"do": "setting", "path": "voice.assistant_talk", "value": "loud"}, "error"),
        (
            {"do": "setting", "path": "general.max_live_agents", "value": "lots"},
            "error",
        ),
        ({"do": "setting", "path": "made.up", "value": 1}, "error"),
        (
            {"do": "new_session", "repo": "x", "prompt": "y", "program": "not-a-cli"},
            "error",
        ),
        ({"do": "help", "question": ""}, "error"),
    ],
)
def test_validate_refuses(action, kind):
    got, problem, _ = va.validate(action, SNAP, ROWS)
    if kind is None:
        assert problem is None and got is not None
        return
    assert got is None
    assert problem["type"] == kind and problem["text"]


def test_validate_send_without_session_needs_a_focus():
    _, problem, _ = va.validate({"do": "send", "text": "go"}, {}, ROWS)
    assert problem == {"type": "ask", "text": "Which session?"}


# --- the instant path -----------------------------------------------------------
@pytest.mark.parametrize(
    "text, want",
    [
        ("open intake", {"do": "open", "target": "intake"}),
        ("Open Intake.", {"do": "open", "target": "intake"}),
        ("please show me the schedule", {"do": "open", "target": "schedule"}),
        ("open templates", {"do": "open", "target": "schedule"}),
        ("settings", {"do": "open", "target": "settings"}),
        ("open settings", {"do": "open", "target": "settings"}),
        ("voice settings", {"do": "open", "target": "settings:voice"}),
        (
            "open notification settings",
            {"do": "open", "target": "settings:notifications"},
        ),
        (
            "go to the appearance screen",
            {"do": "open", "target": "settings:appearance"},
        ),
        ("settings for your phone", {"do": "open", "target": "settings:mobile"}),
        ("open new session", {"do": "open", "target": "new-session"}),
        ("open the command palette", {"do": "open", "target": "palette"}),
        ("go to keyboard shortcuts", {"do": "open", "target": "shortcuts"}),
        ("switch to the sky", {"do": "view", "mode": "sky"}),
        ("grid view", {"do": "view", "mode": "grid"}),
        ("back to the grid", {"do": "view", "mode": "grid"}),
        ("close this", {"do": "close"}),
        ("close", {"do": "close"}),
        ("run toggle theme", {"do": "command", "label": "Toggle theme"}),
        # No model guess: anything not near-exact is the model's.
        ("open the docs session", None),
        ("open sitecheck", None),
        ("run delete session", None),  # destructive: the model confirms
        ("what needs me", None),
        ("turn on dark mode", None),
        ("open settings for the moon", None),
        ("intake", None),
        ("", None),
    ],
)
def test_instant(text, want):
    assert va.instant(text, SNAP) == want


def test_instant_path_is_fast_and_skips_the_model():
    snap = dict(SNAP, commands=["Command %d" % i for i in range(300)])
    t0 = time.perf_counter()
    events, _, brain = turn("open notification settings", snapshot=snap)
    assert (time.perf_counter() - t0) < 0.1
    assert events[0] == {
        "type": "act",
        "action": {"do": "open", "target": "settings:notifications"},
    }
    assert events[-1]["type"] == "done" and events[-1]["model"] is False
    assert brain.messages == []


# --- turns through the model ----------------------------------------------------
def test_model_turn_streams_validated_events_in_order():
    events, _, brain = turn(
        "open voice settings and the sky",
        [
            'ACT {"do":"view","mode":"sky"}',
            'ACT {"do":"open","target":"settings:voice"}',
        ],
    )
    # "open voice settings and the sky" is no instant phrase: the model ran.
    assert kinds(events) == ["act", "act", "done"]
    assert events[0]["action"] == {"do": "view", "mode": "sky"}
    assert events[-1]["model"] is True and isinstance(events[-1]["ms"], int)
    msg = brain.messages[0]
    assert "The person said: open voice settings and the sky" in msg
    assert "sitecheck-bot7-login [Working]" in msg
    assert "parser [NEEDS YOU: asking: Allow edit?]" in msg
    assert "projects: MindFlock, sitecheck-bot7" in msg
    assert "commands: New session… | Toggle theme | Delete session" in msg
    assert "focused session: docs-cleanup" in msg
    assert "chime (Chime) bool = true" in msg


def test_unknown_action_is_dropped_with_an_error_and_the_rest_still_runs():
    events, _, _ = turn(
        "do stuff",
        [
            'ACT {"do":"format_disk"}',
            "ACT {broken",
            'ACT {"do":"open","target":"intake"}',
        ],
    )
    assert kinds(events) == ["error", "error", "act", "done"]
    assert events[2]["action"] == {"do": "open", "target": "intake"}


def test_prose_only_answer_is_said():
    events, _, _ = turn("hello", ["Hi! I can open screens and start sessions."])
    assert events[0] == {
        "type": "say",
        "text": "Hi! I can open screens and start sessions.",
    }


def test_prose_is_ignored_beside_protocol_lines():
    events, _, _ = turn("open it", ["Sure thing.", 'ACT {"do":"close"}'])
    assert kinds(events) == ["act", "done"]


def test_empty_answer_is_an_error():
    events, _, _ = turn("hmm", [])
    assert kinds(events) == ["error", "done"]


def test_model_error_is_passed_on():
    events, _, _ = turn(
        "hmm",
        [],
        brain=ScriptBrain([], errors=["The assistant took too long to answer."]),
    )
    assert events[0] == {
        "type": "error",
        "text": "The assistant took too long to answer.",
    }


def test_empty_text_without_confirm():
    events, _, _ = turn("   ")
    assert kinds(events) == ["error", "done"]


# --- talk policy -----------------------------------------------------------------
def _talk(monkeypatch, level):
    settings_store.update_settings(voice={"assistant_talk": level})
    settings_store.invalidate()
    assert va.talk_level() == (level or "brief")


@pytest.mark.parametrize("level", ["", "brief", "quiet"])
def test_no_spoken_echo_of_navigation(monkeypatch, level):
    _talk(monkeypatch, level)
    events, _, _ = turn(
        "intake please now",
        [
            'ACT {"do":"open","target":"intake"}',
            "SAY Opening intake.",
            "SAY Okay, done.",
        ],
    )
    assert kinds(events) == ["act", "done"]


def test_chatty_keeps_the_line(monkeypatch):
    _talk(monkeypatch, "chatty")
    events, _, _ = turn(
        "intake please now",
        ['ACT {"do":"open","target":"intake"}', "SAY Opening intake."],
    )
    assert kinds(events) == ["act", "say", "done"]


def test_answers_are_said_even_after_an_action(monkeypatch):
    _talk(monkeypatch, "brief")
    events, _, _ = turn(
        "open intake and tell me what needs me",
        ['ACT {"do":"open","target":"intake"}', "SAY Parser is asking about an edit."],
    )
    assert kinds(events) == ["act", "say", "done"]


@pytest.mark.parametrize(
    "level, rule",
    [("quiet", "QUIET"), ("brief", "BRIEF"), ("chatty", "CHATTY"), ("", "BRIEF")],
)
def test_talk_level_shapes_the_system_prompt(monkeypatch, level, rule):
    _talk(monkeypatch, level)
    prompt = va.build_system_prompt(va.talk_level(), [])
    assert "Talk level %s" % rule in prompt
    assert "ACT {json}" in prompt and "CONFIRM" in prompt
    assert "intake (Intake" in prompt
    assert "voice (Voice)" in prompt
    assert "delete (Delete)" in prompt
    assert "traffic" not in prompt


def test_system_prompt_lists_settings_without_secrets():
    from backend.web.core import voice_assistant_catalog as cat

    prompt = va.build_system_prompt("brief", cat.settings())
    assert "voice.assistant_talk (Assistant talk) select [quiet|brief|chatty]" in prompt
    assert "notifications.ask_popup (Question pop-up) bool = true" in prompt
    for secret in (
        "auth_token",
        "github.token",
        "api_key",
        "ntfy_token",
        "skip_permissions",
    ):
        assert secret not in prompt


# --- destructive actions and confirmations ---------------------------------------
def test_destructive_act_becomes_a_confirm_and_yes_runs_it():
    events, fake, brain = turn(
        "delete parser",
        ['ACT {"do":"session_action","session":"parser","action":"delete"}'],
    )
    assert kinds(events) == ["confirm", "done"]
    c = events[0]
    assert c["action"] == {
        "do": "session_action",
        "session": "parser",
        "action": "delete",
    }
    assert c["text"] == "Delete parser?" and c["id"]
    events, _, _ = turn(
        "yes", fake=fake, brain=brain, confirm={"id": c["id"], "yes": True}
    )
    assert events == [
        {"type": "act", "action": c["action"]},
        {"type": "done", "ms": events[-1]["ms"], "model": False},
    ]
    # Used up: a second yes has nothing to confirm.
    events, _, _ = turn(
        "yes", fake=fake, brain=brain, confirm={"id": c["id"], "yes": True}
    )
    assert kinds(events) == ["error", "done"]


def test_confirm_line_keeps_the_models_question_and_no_drops_it():
    events, fake, brain = turn(
        "click discard",
        ['CONFIRM {"do":"click","label":"Discard changes"} Discard your changes?'],
    )
    c = events[0]
    assert c["type"] == "confirm" and c["text"] == "Discard your changes?"
    events, _, _ = turn(
        "no", fake=fake, brain=brain, confirm={"id": c["id"], "yes": False}
    )
    assert kinds(events) == ["done"]
    assert va._PENDING == {}


def test_confirmed_server_action_runs_on_the_server():
    events, fake, brain = turn(
        "set it",
        [
            'CONFIRM {"do":"setting","path":"notifications.ask_popup","value":false} Turn the question pop-up off?'
        ],
    )
    cid = events[0]["id"]
    events, _, _ = turn("yes", fake=fake, brain=brain, confirm={"id": cid, "yes": True})
    assert kinds(events) == ["did", "done"]
    assert fake.written == [("notifications", "ask_popup", False)]


def test_confirm_expires(monkeypatch):
    events, fake, brain = turn(
        "delete parser",
        ['ACT {"do":"session_action","session":"parser","action":"delete"}'],
    )
    cid = events[0]["id"]
    real = time.time
    monkeypatch.setattr(va.time, "time", lambda: real() + va.CONFIRM_TTL_S + 1)
    events, _, _ = turn("yes", fake=fake, brain=brain, confirm={"id": cid, "yes": True})
    assert events[0] == {
        "type": "error",
        "text": "That question has expired; please ask again.",
    }


def test_invalid_confirm_action_is_never_held():
    events, _, _ = turn(
        "x",
        ['CONFIRM {"do":"session_action","session":"parser","action":"nuke"} Sure?'],
    )
    assert kinds(events) == ["error", "done"]
    assert va._PENDING == {}


# --- server actions ---------------------------------------------------------------
def test_setting_writes_and_captions():
    events, fake, _ = turn(
        "turn off the question popup",
        ['ACT {"do":"setting","path":"notifications.ask_popup","value":"off"}'],
    )
    assert fake.written == [("notifications", "ask_popup", False)]
    did = events[0]
    assert did == {
        "type": "did",
        "action": {"do": "setting", "path": "notifications.ask_popup", "value": False},
        "ok": True,
        "text": "Question pop-up: off",
        "detail": {"path": "notifications.ask_popup", "value": False},
    }


def test_setting_default_writer_saves_to_the_store():
    async def go():
        await va._default_write_setting("voice", "assistant_talk", "quiet")

    run(go())
    settings_store.invalidate()
    assert settings_store.load_settings().voice.assistant_talk == "quiet"


def test_guarded_setting_needs_the_owner():
    fake = Fake(may=False)
    events, _, _ = turn(
        "turn off the popup",
        ['ACT {"do":"setting","path":"notifications.ask_popup","value":false}'],
        fake=fake,
    )
    assert kinds(events) == ["did", "error", "done"]
    assert events[0]["ok"] is False
    assert fake.written == []


def test_secret_setting_is_refused_whatever_the_model_says():
    events, fake, _ = turn(
        "set my github token",
        ['ACT {"do":"setting","path":"github.token","value":"ghp_x"}'],
    )
    assert kinds(events) == ["error", "done"]
    assert fake.written == []


def test_send_types_into_the_session():
    events, fake, _ = turn(
        "tell docs to update the changelog",
        [
            'ACT {"do":"send","session":"docs-cleanup","text":"Also update the changelog."}'
        ],
    )
    assert fake.sent == [("docs-cleanup", "Also update the changelog.")]
    assert events[0]["type"] == "did" and events[0]["ok"] is True
    assert events[0]["text"] == "Sent to docs-cleanup"
    assert events[0]["detail"] == {"title": "docs-cleanup", "queued": False}


def test_send_into_a_dialog_is_refused_by_the_gate():
    fake = Fake()
    fake.send_answer = (409, {"in_dialog": True})
    events, _, _ = turn(
        "tell parser go",
        ['ACT {"do":"send","session":"parser","text":"go"}'],
        fake=fake,
    )
    assert kinds(events) == ["did", "error", "done"]
    assert events[0]["ok"] is False
    assert "showing a dialog" in events[0]["text"]


def test_send_queued():
    fake = Fake()
    fake.send_answer = (200, {"sent": False, "queued": True})
    events, _, _ = turn(
        "x", ['ACT {"do":"send","session":"parser","text":"go"}'], fake=fake
    )
    assert events[0]["text"] == "Queued for parser"


def test_new_session_resolves_the_project_and_creates():
    events, fake, _ = turn(
        "start a session on sitecheck bot seven to add a README",
        [
            'ACT {"do":"new_session","repo":"sitecheck bot 7","prompt":"Add a README","program":"claude"}'
        ],
    )
    plan, program = fake.created[0]
    assert plan == {
        "kind": "new",
        "title_base": "add-readme",
        "name": "",
        "repo_name": "sitecheck-bot7",
        "repo_path": "/r/sitecheck-bot7",
        "task": "Add a README",
        "model": "",
    }
    assert program == "claude"
    assert events[0] == {
        "type": "did",
        "action": {
            "do": "new_session",
            "repo": "sitecheck bot 7",
            "prompt": "Add a README",
            "program": "claude",
        },
        "ok": True,
        "text": "Started add-readme in sitecheck-bot7",
        "detail": {"title": "add-readme", "repo": "sitecheck-bot7"},
    }


def test_new_session_unknown_project_asks():
    events, fake, _ = turn(
        "x", ['ACT {"do":"new_session","repo":"","prompt":"write tests"}']
    )
    assert events[0] == {
        "type": "ask",
        "text": "Which project: MindFlock or sitecheck-bot7?",
    }
    assert fake.created == []


def test_new_session_without_a_task_asks():
    events, fake, _ = turn(
        "x", ['ACT {"do":"new_session","repo":"MindFlock","prompt":""}']
    )
    assert events[0] == {"type": "ask", "text": "What should it do in MindFlock?"}
    assert fake.created == []


def test_new_session_single_project_needs_no_name():
    fake = Fake(repos=REPOS[:1])
    turn("x", ['ACT {"do":"new_session","prompt":"write tests"}'], fake=fake)
    assert fake.created[0][0]["repo_path"] == "/r/MindFlock"


def test_new_session_failure_is_said():
    fake = Fake()
    fake.create_answer = {
        "kind": "new-failed",
        "say": "Memory is low, 3.1 GB free, so I didn't start it.",
    }
    events, _, _ = turn(
        "x", ['ACT {"do":"new_session","repo":"MindFlock","prompt":"go"}'], fake=fake
    )
    assert kinds(events) == ["did", "error", "done"]
    assert events[0]["ok"] is False
    assert events[1]["text"].startswith("Memory is low")


def test_status_of_one_session_and_of_everything():
    events, _, _ = turn(
        "what's sitecheck doing", ['ACT {"do":"status","session":"sitecheck"}']
    )
    assert events[0]["type"] == "say"
    assert events[0]["text"].startswith(
        "sitecheck-bot7-login is working for 7 minutes."
    )
    assert "Running the tests now." in events[0]["text"]
    events, _, _ = turn("what needs me", ['ACT {"do":"status"}'])
    assert events[0] == {"type": "say", "text": "parser is asking: Allow edit?"}


def test_help_is_answered_from_the_docs():
    events, fake, _ = turn(
        "how do red zones work",
        ['ACT {"do":"help","question":"how do red zones work"}'],
    )
    assert fake.asked == ["how do red zones work"]
    assert events[0] == {
        "type": "say",
        "text": "From Zones: a red zone is a keep-out path.",
    }


def test_ui_and_server_actions_mix_in_order():
    events, fake, _ = turn(
        "x",
        [
            'ACT {"do":"focus","session":"parser"}',
            'ACT {"do":"send","session":"parser","text":"yes, go ahead"}',
            "SAY Done.",
        ],
    )
    assert kinds(events) == ["act", "did", "done"]


# --- the process ----------------------------------------------------------------
class FakePopen:
    """A stream-json chat process: each user line on stdin is answered
    with ``reply(text)``'s output lines, as text deltas split mid-line, then
    a ``result`` line."""

    spawned: list = []

    def __init__(self, argv, cwd, reply=None):
        self.argv, self.cwd = argv, cwd
        self.reply = reply or (lambda text: ['ACT {"do":"close"}'])
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self.stdout = self
        self.stdin = self
        self.code = None
        self.received: list = []
        self._buf = b""
        FakePopen.spawned.append(self)

    # stdout
    def readline(self):
        line = self._out.get()
        return line

    # stdin
    def write(self, data):
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            msg = json.loads(line)
            text = msg["message"]["content"][0]["text"]
            self.received.append(text)
            self._answer(text)

    def flush(self):
        pass

    def close(self):
        self.code = 0
        self._out.put(b"")

    def _emit(self, obj):
        self._out.put((json.dumps(obj) + "\n").encode())

    def _answer(self, text):
        out = self.reply(text)
        if out is None:
            return  # hang
        body = "\n".join(out)
        self._emit({"type": "system", "subtype": "init"})
        for i in range(0, len(body), 7):
            self._emit(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": body[i : i + 7]},
                    },
                }
            )
        self._emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": body}]},
            }
        )
        self._emit({"type": "result", "subtype": "success", "is_error": False})

    def poll(self):
        return self.code

    def kill(self):
        if self.code is None:
            self.code = -9
            self._out.put(b"")

    def crash(self):
        self.kill()


@pytest.fixture
def proc_env(monkeypatch):
    FakePopen.spawned = []
    state = {"reply": None}

    def spawn(argv, cwd):
        return FakePopen(argv, cwd, state["reply"])

    monkeypatch.setattr(va, "spawn_process", spawn)
    monkeypatch.setattr(va, "_neutral_cwd", lambda: "/neutral")
    monkeypatch.setattr(va, "assistant_program", lambda: "claude")
    monkeypatch.setattr(va, "_REAP_EVERY_S", 3600.0)
    brain = va.Brain()
    yield brain, state
    brain.stop()


def pturn(brain, text, conversation="c1", fake=None):
    fake = fake or Fake()
    body = {"text": text, "conversation": conversation, "snapshot": SNAP}

    async def go():
        return [ev async for ev in va.turn(body, fake.ctx(), brain)]

    return run(go())


def test_process_streams_a_turn(proc_env):
    brain, state = proc_env
    state["reply"] = lambda t: [
        'ACT {"do":"view","mode":"sky"}',
        "SAY Parser is asking about an edit.",
    ]
    events = pturn(brain, "show the sky and what needs me")
    assert kinds(events) == ["act", "say", "done"]
    p = FakePopen.spawned[0]
    assert p.cwd == "/neutral"
    assert p.argv[:8] == [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    assert p.argv[p.argv.index("--model") + 1] == "claude-haiku-5-5"
    sysp = p.argv[p.argv.index("--system-prompt") + 1]
    assert "MindFlock's voice assistant" in sysp
    assert "The person said: show the sky and what needs me" in p.received[0]


def test_process_is_reused_within_a_conversation_and_fresh_for_a_new_one(proc_env):
    brain, _ = proc_env
    pturn(brain, "one thing", "c1")
    pturn(brain, "another thing", "c1")
    assert len(FakePopen.spawned) == 1
    assert len(FakePopen.spawned[0].received) == 2
    pturn(brain, "a new talk", "c2")
    assert len(FakePopen.spawned) == 2
    assert FakePopen.spawned[0].code is not None  # the old one stopped


def test_process_restarts_after_a_crash(proc_env):
    brain, _ = proc_env
    pturn(brain, "one thing")
    FakePopen.spawned[0].crash()
    events = pturn(brain, "again")
    assert len(FakePopen.spawned) == 2
    assert kinds(events) == ["act", "done"]


def test_process_restarts_when_the_talk_level_changes(proc_env):
    brain, _ = proc_env
    pturn(brain, "one thing")
    settings_store.update_settings(voice={"assistant_talk": "chatty"})
    settings_store.invalidate()
    pturn(brain, "two")
    assert len(FakePopen.spawned) == 2
    assert (
        "Talk level CHATTY"
        in FakePopen.spawned[1].argv[
            FakePopen.spawned[1].argv.index("--system-prompt") + 1
        ]
    )


def test_changed_settings_ride_in_the_turn(proc_env):
    brain, _ = proc_env
    brain.warm()
    settings_store.update_settings(notifications={"ask_popup": False})
    settings_store.invalidate()
    pturn(brain, "what is set")
    assert (
        "settings changed since the catalog: notifications.ask_popup = false"
        in FakePopen.spawned[0].received[0]
    )


def test_idle_freshen_then_stop(proc_env):
    brain, _ = proc_env
    pturn(brain, "one thing")
    first = FakePopen.spawned[0]
    used = brain._proc.last_used
    assert brain.reap(used + 10) is True  # still fresh
    assert len(FakePopen.spawned) == 1
    assert brain.reap(used + va.FRESH_AFTER_S + 1) is True
    assert len(FakePopen.spawned) == 2 and first.code is not None
    assert brain._proc.turns == 0
    # The fresh one waits for the next conversation; it is not refreshed again.
    assert brain.reap(used + va.FRESH_AFTER_S + 30) is True
    assert len(FakePopen.spawned) == 2
    assert brain.reap(used + va.STOP_AFTER_S + 1) is False
    assert brain._proc is None and FakePopen.spawned[1].code is not None


def test_warm_starts_once(proc_env):
    brain, _ = proc_env
    assert brain.warm() == {
        "ok": True,
        "mode": "stream",
        "model": "claude-haiku-5-5",
        "program": "claude",
    }
    assert brain.warm()["ok"] is True
    assert len(FakePopen.spawned) == 1
    pturn(brain, "go")
    assert len(FakePopen.spawned) == 1  # the warm one answered


def test_turn_timeout_replaces_the_process(proc_env, monkeypatch):
    brain, state = proc_env
    monkeypatch.setattr(va, "TURN_TIMEOUT_S", 0.3)
    state["reply"] = lambda t: None  # never answers
    events = pturn(brain, "hello there")
    assert events[0] == {
        "type": "error",
        "text": "The assistant took too long to answer.",
    }
    assert brain._proc is None
    assert FakePopen.spawned[0].code is not None


def test_model_error_result(proc_env, monkeypatch):
    brain, _ = proc_env

    def spawn(argv, cwd):
        p = FakePopen(argv, cwd)
        p._answer = lambda text: p._emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "rate limited",
            }
        )
        return p

    monkeypatch.setattr(va, "spawn_process", spawn)
    events = pturn(brain, "hello there")
    assert events[0]["type"] == "error" and "rate limited" in events[0]["text"]


def test_never_spawns_under_pytest():
    with pytest.raises(va.AssistantError):
        va.spawn_process(["claude"], "/tmp")
    with pytest.raises(va.AssistantError):
        va.run_oneshot(["claude", "-p", "x"])


def test_a_turn_without_a_process_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(va, "assistant_program", lambda: "claude")
    brain = va.Brain()
    events = pturn(brain, "hello there")
    assert events[0]["type"] == "error"
    assert events[-1]["type"] == "done"


class _OneShotOnly(CodingProvider):
    name = "oneshot-cli"

    def oneshot_argv(self, prompt):
        return ["oneshot-cli", "--ask", prompt]


class _Mute(CodingProvider):
    name = "mute-cli"


def test_oneshot_fallback_per_turn(monkeypatch):
    seen = []
    monkeypatch.setattr(va, "assistant_program", lambda: "oneshot-cli")
    monkeypatch.setattr(providers, "resolve", lambda name: _OneShotOnly())
    monkeypatch.setattr(
        va, "run_oneshot", lambda argv: seen.append(argv) or 'ACT {"do":"close"}\n'
    )
    brain = va.Brain()
    assert brain.warm()["mode"] == "oneshot"
    assert kinds(pturn(brain, "tidy up the screen", "c9")) == ["act", "done"]
    pturn(brain, "and again", "c9")
    assert len(seen) == 2
    assert "Earlier, the person said: tidy up the screen" in seen[1][2]
    assert "The person said: and again" in seen[1][2]


def test_no_model_at_all(monkeypatch):
    monkeypatch.setattr(va, "assistant_program", lambda: "mute-cli")
    monkeypatch.setattr(providers, "resolve", lambda name: _Mute())
    brain = va.Brain()
    assert brain.warm() == {
        "ok": False,
        "mode": "none",
        "model": "claude-haiku-5-5",
        "program": "mute-cli",
    }
    events = pturn(brain, "hello there")
    assert events[0] == {
        "type": "error",
        "text": "mute-cli can't answer the assistant.",
    }
    # The instant path still works.
    assert kinds(pturn(brain, "open intake")) == ["act", "done"]


def test_assistant_program_follows_the_settings(monkeypatch):
    settings_store.update_settings(coding_cli={"assistant_provider": "codex"})
    settings_store.invalidate()
    assert va.assistant_program() == "codex"


# --- the provider ---------------------------------------------------------------
def test_claude_stream_chat_argv():
    argv = providers.resolve("claude").stream_chat_argv("SYS", "claude-haiku-5-5")
    assert argv[1:] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "claude-haiku-5-5",
        "--system-prompt",
        "SYS",
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    assert "--model" not in providers.resolve("claude").stream_chat_argv("SYS")
    assert providers.resolve("claude").stream_chat_argv("") is None


def test_other_providers_have_no_stream_mode():
    assert _Mute().stream_chat_argv("SYS", "m") is None
    for name in ("codex", "aider", "goose"):
        assert providers.resolve(name).stream_chat_argv("SYS", "m") is None


# --- the routes -----------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, proc_env):
    brain, state = proc_env
    monkeypatch.setattr(va, "BRAIN", brain)
    monkeypatch.setattr(voice_routes, "_session_rows", lambda request: ROWS)
    monkeypatch.setattr(voice_routes, "_known_repos", lambda rows: REPOS)
    voice_routes._assistant_repos_cache.update(at=0.0, repos=[])
    created = []

    async def create(plan, request, program=""):
        created.append(plan)
        return {"kind": "new", "title": "add-readme", "say": "x"}

    monkeypatch.setattr(voice_routes, "_create_planned", create)
    return TestClient(app), state, created


def _ndjson(resp):
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_route_streams_ndjson_end_to_end(client):
    tc, state, created = client
    state["reply"] = lambda t: [
        'ACT {"do":"new_session","repo":"MindFlock","prompt":"add a README"}',
        "SAY Started.",
    ]
    events = _ndjson(
        tc.post(
            "/api/voice/assistant",
            json={
                "text": "start a session on mindflock to add a readme",
                "conversation": "c1",
                "snapshot": SNAP,
            },
        )
    )
    assert kinds(events) == ["did", "done"]  # "Started." is an echo at brief
    assert events[0]["detail"] == {"title": "add-readme", "repo": "MindFlock"}
    assert created[0]["repo_path"] == "/r/MindFlock"
    assert events[-1]["model"] is True


def test_route_instant_and_confirm(client):
    tc, state, _ = client
    events = _ndjson(
        tc.post(
            "/api/voice/assistant", json={"text": "open intake", "conversation": "c1"}
        )
    )
    assert events[0] == {"type": "act", "action": {"do": "open", "target": "intake"}}
    assert events[-1]["model"] is False
    state["reply"] = lambda t: [
        'ACT {"do":"session_action","session":"parser","action":"close"}'
    ]
    events = _ndjson(
        tc.post(
            "/api/voice/assistant",
            json={
                "text": "close the parser session",
                "conversation": "c1",
                "snapshot": SNAP,
            },
        )
    )
    assert events[0]["type"] == "confirm"
    events = _ndjson(
        tc.post(
            "/api/voice/assistant",
            json={
                "text": "yes",
                "conversation": "c1",
                "confirm": {"id": events[0]["id"], "yes": True},
            },
        )
    )
    assert events[0] == {
        "type": "act",
        "action": {"do": "session_action", "session": "parser", "action": "close"},
    }


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        [],
        {"text": 3, "conversation": "c"},
        {"text": "hi", "conversation": 1},
        {"text": "hi", "conversation": "c", "snapshot": "grid"},
        {"text": "hi", "conversation": "c", "confirm": {"id": "x"}},
        {"text": "  ", "conversation": "c"},
    ],
)
def test_route_rejects_bad_bodies(client, body):
    tc, _, _ = client
    if isinstance(body, str):
        resp = tc.post(
            "/api/voice/assistant",
            content=body,
            headers={"content-type": "application/json"},
        )
    else:
        resp = tc.post("/api/voice/assistant", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"]


def test_route_warm(client):
    tc, _, _ = client
    resp = tc.post("/api/voice/assistant/warm")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "mode": "stream",
        "model": "claude-haiku-5-5",
        "program": "claude",
    }
    assert len(FakePopen.spawned) == 1


def test_route_catalog(client):
    tc, _, _ = client
    body = tc.get("/api/voice/assistant/catalog").json()
    assert set(body) == {"settings", "dialogs", "settings_screens", "session_actions"}
    paths = {r["path"]: r for r in body["settings"]}
    assert paths["voice.assistant_talk"]["options"] == ["quiet", "brief", "chatty"]
    assert "github.token" not in paths and "general.auth_token" not in paths
    assert "intake" in body["dialogs"] and "voice" in body["settings_screens"]
    assert "delete" in body["session_actions"]


def test_route_send_goes_through_the_voice_send_path(client, monkeypatch):
    tc, state, _ = client
    calls = []

    async def fake_send(title, payload, request=None):
        from fastapi.responses import JSONResponse

        calls.append((title, payload))
        return JSONResponse(
            {"error": "session is paused — resume it first", "paused": True},
            status_code=409,
        )

    from backend.web import server

    monkeypatch.setattr(server, "instance_send", fake_send)
    state["reply"] = lambda t: ['ACT {"do":"send","session":"parser","text":"go on"}']
    events = _ndjson(
        tc.post(
            "/api/voice/assistant",
            json={
                "text": "tell parser to go on",
                "conversation": "c1",
                "snapshot": SNAP,
            },
        )
    )
    assert calls == [("parser", {"text": "go on", "voice": True, "say": True})]
    assert events[0]["type"] == "did" and events[0]["ok"] is False
    assert "paused" in events[0]["text"]


# --- help: answered in the warm process ------------------------------------------
EXCERPTS = [
    {
        "doc": "web-ui.md",
        "heading": "Zones, keep out red",
        "path": "Zones: keep out (red)",
        "text": "A red zone is a path the agent can read but not change.",
    }
]
HELP_ACT = 'ACT {"do":"help","question":"how do red zones work"}'


@pytest.fixture
def excerpts(monkeypatch):
    asked = []

    def passages(q):
        asked.append(q)
        return list(EXCERPTS)

    monkeypatch.setattr(va._help, "passages", passages)
    return asked


@pytest.mark.parametrize(
    "text, want",
    [
        ("how do red zones work", True),
        ("what is a flyway", True),
        ("how do I add a ticket source", True),
        ("what needs me", False),
        ("open intake", False),
        ("what is my sitecheck session doing", False),
        ("turn on the bell", False),
        ("what can you do", False),
    ],
)
def test_docs_question(text, want):
    assert va.docs_question(text) is want


def test_turn_message_carries_docs_before_the_phrase():
    msg = va.build_turn_message(
        "how do red zones work", SNAP, ROWS, REPOS, {}, docs=EXCERPTS
    )
    assert msg.index("</state>") < msg.index("<docs>") < msg.index("The person said")
    assert "[1] web-ui.md — Zones: keep out (red)\nA red zone is a path" in msg
    assert "<docs>" not in va.build_turn_message("x", SNAP, ROWS, REPOS, {})


def test_help_is_a_follow_up_turn_in_the_warm_process(proc_env, excerpts, monkeypatch):
    brain, state = proc_env
    monkeypatch.setattr(va, "docs_question", lambda t: False)
    state["reply"] = lambda t: (
        ["SAY A red zone is read-only for the agent."] if "<docs>" in t else [HELP_ACT]
    )
    fake = Fake()
    events = pturn(brain, "how do red zones work", fake=fake)
    assert events[:-1] == [
        {"type": "say", "text": "A red zone is read-only for the agent."}
    ]
    assert fake.asked == []  # never the cold one-shot
    assert excerpts == ["how do red zones work"]
    p = FakePopen.spawned
    assert len(p) == 1 and len(p[0].received) == 2
    assert "<docs>" not in p[0].received[0]
    assert "A red zone is a path the agent can read" in p[0].received[1]
    assert "(how do red zones work)" in p[0].received[1]
    assert brain._proc.turns == 2
    # The next turn still finds the process idle and answering.
    state["reply"] = lambda t: ['ACT {"do":"close"}']
    assert kinds(pturn(brain, "close it")) == ["act", "done"]


def test_a_docs_question_carries_the_excerpts_up_front(proc_env, excerpts):
    brain, state = proc_env
    state["reply"] = lambda t: ["SAY Agents can read a red zone but not change it."]
    fake = Fake()
    events = pturn(brain, "how do red zones work", fake=fake)
    assert kinds(events) == ["say", "done"]
    received = FakePopen.spawned[0].received
    assert len(received) == 1 and "<docs>" in received[0]
    assert fake.asked == []


def test_follow_up_prose_is_said_and_silence_reads_the_excerpt(
    proc_env, excerpts, monkeypatch
):
    brain, state = proc_env
    monkeypatch.setattr(va, "docs_question", lambda t: False)
    state["reply"] = lambda t: (
        ["Red zones are keep-out paths."] if "<docs>" in t else [HELP_ACT]
    )
    events = pturn(brain, "how do red zones work")
    assert events[0] == {"type": "say", "text": "Red zones are keep-out paths."}
    # The follow-up asks for help again: not followed twice; the excerpt is read.
    state["reply"] = lambda t: [HELP_ACT]
    events = pturn(brain, "how do red zones work", "c2")
    assert events[0] == {
        "type": "say",
        "text": "From Zones, keep out red: A red zone is a path the agent can "
        "read but not change.",
    }
    assert len(FakePopen.spawned[-1].received) == 2


def test_help_with_no_excerpts_falls_back_to_voice_help(proc_env, monkeypatch):
    brain, state = proc_env
    monkeypatch.setattr(va._help, "passages", lambda q: [])
    state["reply"] = lambda t: [HELP_ACT]
    fake = Fake()
    events = pturn(brain, "how do red zones work", fake=fake)
    assert fake.asked == ["how do red zones work"]
    assert events[0]["text"] == "From Zones: a red zone is a keep-out path."
    assert len(FakePopen.spawned[0].received) == 1


def test_oneshot_help_falls_back_to_voice_help(monkeypatch, excerpts):
    monkeypatch.setattr(va, "assistant_program", lambda: "oneshot-cli")
    monkeypatch.setattr(providers, "resolve", lambda name: _OneShotOnly())
    monkeypatch.setattr(va, "docs_question", lambda t: False)
    monkeypatch.setattr(va, "run_oneshot", lambda argv: HELP_ACT + "\n")
    fake = Fake()
    events = pturn(va.Brain(), "how do red zones work", fake=fake)
    assert fake.asked == ["how do red zones work"]
    assert events[:-1] == [
        {"type": "say", "text": "From Zones: a red zone is a keep-out path."}
    ]
