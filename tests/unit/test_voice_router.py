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


def ans(text, session="", rows=ROWS):
    return vr.answer(text, session, rows, now=NOW)


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
        ("Stop listening", "stop"),
        ("stop", "stop"),
        ("voice off", "off"),
        ("turn off voice", "off"),
        ("Say that again", "repeat"),
        ("repeat that", "repeat"),
        ("summarize that", "condense"),
        ("what did it say", "condense"),
    ],
)
def test_local_kinds_carry_no_say(text, kind):
    assert ans(text) == {"kind": kind}


# --- switch ---------------------------------------------------------------------
def test_switch_the_phones_cases():
    a = ans("Switch to the invoice parser.")
    assert a == {
        "kind": "switch",
        "title": "invoice-parser",
        "name": "invoice-parser",
        "say": "Now on invoice-parser. Parsing the totals now.",
    }
    a = ans("go to API docs")
    assert (a["kind"], a["title"], a["name"]) == ("switch", "docs", "API docs")
    assert a["say"] == "Now on API docs. Should I push?"
    # Word overlap: "api" is in one session's name only.
    assert ans("go to the api")["title"] == "docs"
    assert ans("jump over to docs")["title"] == "docs"
    assert ans("OK change to invoice please")["title"] == "invoice-parser"


def test_switch_reaches_other_devices_but_never_hidden_rows():
    a = ans("switch to billing")
    assert a["title"] == "mac::billing" and a["say"] == "Now on billing."
    assert ans("go to verify")["kind"] == "no-session"


def test_switch_ambiguous_and_no_match():
    assert ans("go to login") == {
        "kind": "ambiguous",
        "say": "Which one: login-fix, login-page?",
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
    p = plan("open a new session called parser in mindflock")
    assert p == {
        "kind": "new",
        "title_base": "parser",
        "name": "parser",
        "repo_name": "MindFlock",
        "repo_path": "/r/MindFlock",
        "task": "",
    }
    # A name the words begin ("sitecheck" → the most recent sitecheck-bot*).
    p = plan("start a new session in sitecheck and add tests for the queue")
    assert (p["repo_path"], p["task"], p["title_base"]) == (
        "/r/sitecheck-bot5",
        "add tests for the queue",
        "add-tests-queue",
    )
    # Spacing and case don't count; "the … repo" is not part of the name.
    assert (
        plan("new session in the Site Check Bot 6 repo")["repo_path"]
        == "/r/sitecheck-bot6"
    )


def test_plan_repo_words_run_into_the_task():
    p = plan("new session in mindflock fix the tests")
    assert (p["repo_path"], p["task"], p["title_base"]) == (
        "/r/MindFlock",
        "fix the tests",
        "fix-tests",
    )


def test_plan_for_or_on_a_non_repo_is_the_task():
    p = plan("new session for the login bug", session="login-fix")
    assert (p["repo_path"], p["task"]) == ("/r/sitecheck-bot5", "for the login bug")
    assert p["title_base"] == "login-bug"


def test_plan_in_an_unknown_repo_fails():
    assert plan("new session in narnia") == {
        "kind": "new-failed",
        "say": "I don't know a repository called narnia. Repositories: MindFlock, "
        "sitecheck-bot5, sitecheck-bot6, parser.",
    }


def test_plan_defaults_to_the_current_sessions_repo():
    assert (
        plan("new session to fix it", session="login-fix")["repo_path"]
        == "/r/sitecheck-bot5"
    )
    # Another device's session has no repository here: the most recent one.
    assert (
        plan("new session to fix it", session="mac::billing")["repo_path"]
        == "/r/MindFlock"
    )
    assert plan("new session", session="")["repo_path"] == "/r/MindFlock"


def test_plan_only_or_no_repository():
    only = [{"name": "solo", "path": "/r/solo", "source": "nearby"}]
    assert plan("new session", rows=[], repos=only)["repo_path"] == "/r/solo"
    two = only + [{"name": "duo", "path": "/r/duo", "source": "nearby"}]
    assert plan("new session", rows=[], repos=two) == {
        "kind": "new-failed",
        "say": "I don't know which repository: say new session in solo, duo.",
    }
    assert plan("new session", rows=[], repos=[]) == {
        "kind": "new-failed",
        "say": "I don't know which repository: say new session in <repo>.",
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


# --- the route -----------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(aliases, "owner_of", lambda *a, **k: "")
    state = SimpleNamespace(
        rows=list(ROWS), created=[], create=(202, None), row_reads=0
    )

    def rows(request):
        state.row_reads += 1
        return state.rows

    async def create(payload, person):
        state.created.append(payload)
        status, body = state.create
        return status, body if body is not None else {"title": payload["title"]}

    monkeypatch.setattr(voice_routes, "_session_rows", rows)
    monkeypatch.setattr(voice_routes, "_create_session", create)
    monkeypatch.setattr(server, "suggest_repos", lambda **kw: list(REPOS))
    monkeypatch.setattr(server, "_recent_repo_paths", lambda: [])
    return state


def route(text, session=""):
    r = client.post("/api/voice/route", json={"text": text, "session": session})
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
        "say": "Now on invoice-parser. Parsing the totals now.",
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
    got = route("open a new session called parser in mindflock")
    assert got["title"] == "parser" and got["say"] == "Started parser in MindFlock."
    assert env.created == [{"title": "parser", "repo_path": "/r/MindFlock"}]


def test_route_new_title_never_collides(env, monkeypatch):
    taken = SimpleNamespace(Branch="b")
    monkeypatch.setattr(
        server.ENGINE, "instances", {"fix-login-bug": taken, "fix-login-bug-2": taken}
    )
    got = route("new session to fix the login bug", "login-fix")
    assert got["title"] == "fix-login-bug-3"
    assert env.created[0]["title"] == "fix-login-bug-3"
    assert env.created[0]["repo_path"] == "/r/sitecheck-bot5"


def test_route_new_failures_are_spoken(env):
    env.create = (409, {"error": "instance parser already exists."})
    assert route("new session called parser") == {
        "kind": "new-failed",
        "say": "Couldn't start it: instance parser already exists.",
    }
    assert not voice.is_on(tmux.to_mindflock_tmux_name("parser"))
    env.rows = []
    assert route("new session in narnia")["kind"] == "new-failed"
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
        "say": "Now on billing.",
    }
