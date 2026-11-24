"""Ask how something works: ``core.help_ask`` (the written answer, its
pointers, the fallback, the cache, the rate limit) and ``POST /api/help/ask``.

The ranking runs against the real ``docs/``; the mechanics against a small
docs folder under ``tmp_path``. The model is never asked: ``_ask_model``
refuses under pytest (voice help's guard), and every test that wants a
worded answer replaces it.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from backend.web.core import commit_message
from backend.web.core import guests
from backend.web.core import help_ask as ha
from backend.web.core import voice_help as vh
from backend.web.routes import help as help_routes
from backend.web.server import app

client = TestClient(app)

PLACES = [
    {"id": "settings.appearance.mode", "label": "Settings → Appearance → Mode"},
    {"id": "topbar.schedule", "label": "Schedule (top bar)"},
    {"id": "sidebar.help", "label": "? Help (sidebar foot)"},
    {"id": "topbar.intake", "label": "Intake (top bar)"},
]
IDS = [p["id"] for p in PLACES]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(vh, "_INDEX", None)
    ha._reset_for_tests()
    yield
    ha._reset_for_tests()


# --- the real docs ---------------------------------------------------------------
@pytest.mark.parametrize(
    "question, heading",
    [
        ("where do I change the theme", "themes"),
        ("how do I connect github", "connect github"),
        ("what are the keyboard shortcuts", "keyboard shortcuts"),
        ("what is the sky", "sky"),
        ("how do I send feedback", "feedback"),
    ],
)
def test_search_finds_a_section_a_user_reads(question, heading):
    hits = ha.search(question, 3)
    assert hits, question
    assert any(heading in p.heading.lower() for _, p in hits), [
        p.heading for _, p in hits
    ]
    # never a builder's section first
    assert not ha._builder(hits[0][1]), hits[0][1].headings


@pytest.mark.parametrize(
    "question",
    [
        "how do I bake bread",
        "who won the football game",
        "what is the capital of france",
    ],
)
def test_a_question_the_docs_do_not_cover_finds_nothing(question):
    assert ha.search(question) == []
    got = ha.answer(question, PLACES)
    assert got == {
        "text": ha.NOT_FOUND,
        "found": False,
        "source": None,
        "model": False,
        "pointers": [],
        "limited": False,
    }


def test_a_number_in_the_question_does_not_sink_it():
    assert ha.search("how do I connect github 1795531960", 3)


def test_builder_sections_rank_under_the_users_page():
    hits = ha.search("what is the sky", 3)
    assert all(not ha._builder(p) for _, p in hits)


# --- the mechanics, on a docs folder of our own -----------------------------------
DOC = """# Schedules

The **Schedule** page runs a template at a time you pick. Open it from the
[top bar](#top). It needs a folder and a task. Press `Alt+S` to open it. A
fourth sentence that the fallback leaves out.

## Themes

Pick light or dark in Settings → Appearance → Mode. The moon in the top bar
switches too.
"""


@pytest.fixture
def docs(monkeypatch, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "web-ui.md").write_text(DOC, encoding="utf-8")
    (d / "other.md").write_text(
        "# Other\n\nNothing about it here at all.\n", encoding="utf-8"
    )
    monkeypatch.setenv("MINDFLOCK_DOCS_DIR", str(d))
    # A two-page corpus scores low and weighs words oddly: keep the floor
    # and the coverage out of the mechanics (the real docs test both).
    monkeypatch.setattr(ha, "MIN_SCORE", 0.01)
    monkeypatch.setattr(ha, "MIN_COVERAGE", 0.0)
    return d


def test_answer_is_worded_by_the_model_with_registry_pointers(docs, monkeypatch):
    seen = []

    def model(prompt):
        seen.append(prompt)
        return (
            "Open `Settings` → Appearance and pick a mode.\n"
            "PLACES: settings.appearance.mode, made.up.id, topbar.schedule, "
            "sidebar.help, topbar.intake"
        )

    monkeypatch.setattr(ha, "_ask_model", model)
    got = ha.answer("how do I change the theme", PLACES)
    assert got["model"] is True and got["found"] is True and got["limited"] is False
    assert got["text"] == "Open `Settings` → Appearance and pick a mode."
    # only ids it was given, at most three, in its order
    assert got["pointers"] == [
        "settings.appearance.mode",
        "topbar.schedule",
        "sidebar.help",
    ]
    assert got["source"]["doc"] == "web-ui.md"
    assert got["source"]["heading"] == "Themes"
    assert got["source"]["path"] == "Schedules › Themes"
    assert "Settings → Appearance → Mode" in got["source"]["excerpt"]
    # the prompt lists the places and the passages, and says text only
    p = seen[0]
    assert "settings.appearance.mode — Settings → Appearance → Mode" in p
    assert (
        "PLACES:" in p
        and "I don't know" in p
        and "Question: how do I change the theme" in p
    )


def test_answer_is_cached_per_question_and_places(docs, monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ask_model", lambda p: calls.append(p) or "Pick a mode.")
    a = ha.answer("how do I change the theme", PLACES)
    b = ha.answer("How do I  change the THEME", PLACES)
    assert a == b and len(calls) == 1
    ha.answer("how do I change the theme", PLACES[:1])
    assert len(calls) == 2  # another place list is another question


def test_model_says_it_does_not_know(docs, monkeypatch):
    monkeypatch.setattr(
        ha, "_ask_model", lambda p: "I don't know.\nPLACES: topbar.schedule"
    )
    got = ha.answer("how do I change the theme", PLACES)
    assert got == {
        "text": ha.DONT_KNOW,
        "found": False,
        "source": None,
        "model": True,
        "pointers": [],
        "limited": False,
    }


@pytest.mark.parametrize("why", ["error", "empty"])
def test_fallback_reads_the_best_section(docs, monkeypatch, why):
    def model(prompt):
        if why == "error":
            raise vh.HelpModelError("no claude")
        return "  \n"

    monkeypatch.setattr(ha, "_ask_model", model)
    got = ha.answer("how does the schedule page run a template", PLACES)
    assert got["model"] is False and got["found"] is True and got["pointers"] == []
    # the first two sentences, bold and link off
    assert got["text"] == (
        "The Schedule page runs a template at a time you pick. Open it from the top bar."
    )
    assert got["source"]["heading"] == "Schedules"
    assert ha._CACHE == {}  # a fallback is not cached: the model may be back next time


def test_fallback_keeps_inline_code(docs, monkeypatch):
    monkeypatch.setattr(ha, "FALLBACK_SENTENCES", 4)
    got = ha.answer("how does the schedule page run a template", PLACES)
    assert "Press `Alt+S` to open it." in got["text"]


def test_fallback_leaves_out_what_is_mostly_code():
    text = (
        "`dialogs/ConnectGitHub.tsx`, `/api/github/*`. One sign-in opens PRs "
        "(`core/github_auth.py`, `github.token`) for you. Press `Save & test` when done."
    )
    assert (
        ha.fallback_text(text)
        == "One sign-in opens PRs for you. Press `Save & test` when done."
    )


def test_a_rounds_notes_are_a_builders_section():
    p = vh.Passage(
        "flights.md", ("Flights", "The Sky: birds"), "Round 7 of the Sky (to fold in)."
    )
    assert ha._builder(p)
    q = vh.Passage(
        "web-ui.md", ("Web UI guide", "The sky"), "The sky shows every session."
    )
    assert not ha._builder(q)


def test_the_model_is_never_asked_under_pytest(docs, monkeypatch):
    def run(*a, **k):
        pytest.fail("a model call ran under pytest")

    monkeypatch.setattr(commit_message, "_run", run)
    got = ha.answer("how do I change the theme", PLACES)
    assert got["model"] is False and got["found"] is True


def test_no_docs(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_DOCS_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(vh, "docs_dir", lambda: None)
    got = ha.answer("how do I change the theme", PLACES)
    assert got["text"] == ha.NO_DOCS and got["found"] is False


def test_rate_limit_answers_from_the_docs_alone(docs, monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "RATE_CALLS", 2)
    monkeypatch.setattr(ha, "_ask_model", lambda p: calls.append(p) or "Worded.")
    a = ha.answer("how do I change the theme", PLACES)
    b = ha.answer("what does the moon in the top bar do", PLACES)
    c = ha.answer("how does the schedule page run a template", PLACES)
    assert len(calls) == 2
    assert a["model"] and b["model"]
    assert c["model"] is False and c["limited"] is True and c["found"] is True
    # an answer already worded still comes from the cache
    assert ha.answer("how do I change the theme", PLACES)["model"] is True


def test_only_two_model_calls_at_once(docs, monkeypatch):
    gate = threading.Event()
    started = []

    def slow(prompt):
        started.append(prompt)
        gate.wait(5)
        return "Worded."

    monkeypatch.setattr(ha, "_ask_model", slow)
    qs = [
        "how do I change the theme",
        "what does the moon do",
        "how does the schedule page run",
    ]
    out = {}
    threads = [
        threading.Thread(target=lambda q=q: out.__setitem__(q, ha.answer(q, PLACES)))
        for q in qs[:2]
    ]
    for t in threads:
        t.start()
    for _ in range(200):
        if len(started) == 2:
            break
        threading.Event().wait(0.01)
    third = ha.answer(qs[2], PLACES)
    gate.set()
    for t in threads:
        t.join(5)
    assert third["limited"] is True and third["model"] is False
    assert all(out[q]["model"] for q in qs[:2])


# --- parsing and places -------------------------------------------------------------
def test_parse_reply_takes_the_last_places_line_and_cleans_the_text():
    text, picked, dk = ha.parse_reply(
        "Answer: **Open** [Schedule](http://x) and press `Alt+S`.\n"
        "PLACES: none\n"
        "## Steps\n"
        "**PLACES:** `topbar.schedule`, TOPBAR.INTAKE",
        IDS,
    )
    assert dk is False
    assert text == "Open Schedule and press `Alt+S`.\nSteps"
    assert picked == ["topbar.schedule", "topbar.intake"]


def test_parse_reply_none_and_unknown():
    assert ha.parse_reply("Do it.\nPLACES: none", IDS) == ("Do it.", [], False)
    assert ha.parse_reply("Do it.\nPLACES: rm -rf, javascript:alert(1)", IDS) == (
        "Do it.",
        [],
        False,
    )
    assert ha.parse_reply("I don't know", IDS) == ("", [], True)


def test_parse_reply_cuts_a_long_answer():
    text, _, _ = ha.parse_reply("A sentence here. " * 100, IDS)
    assert len(text) <= ha.ANSWER_MAX and text.endswith(".")


def test_clean_places_keeps_only_well_formed_ones():
    raw = [
        {"id": "ok.one", "label": "  One \n place "},
        {"id": "ok.one", "label": "duplicate"},
        {"id": "Bad Id", "label": "x"},
        {"id": "x" * 65, "label": "too long"},
        {"id": "no.label", "label": ""},
        {"id": 3, "label": "number"},
        "not a dict",
    ]
    assert ha.clean_places(raw) == [("ok.one", "One place")]
    assert ha.clean_places("nope") == []
    many = [{"id": "p%d" % i, "label": "P"} for i in range(500)]
    assert len(ha.clean_places(many)) == ha.PLACES_MAX


def test_build_prompt_without_places_asks_for_none():
    p = ha.build_prompt("q", [], [])
    assert "PLACES" not in p


# --- the route ------------------------------------------------------------------------
def test_route_answers(monkeypatch):
    seen = {}

    def ask(q, places):
        seen.update(q=q, places=places)
        return {
            "text": "t",
            "found": True,
            "source": None,
            "model": False,
            "pointers": [],
            "limited": False,
        }

    monkeypatch.setattr(help_routes, "_ask", ask)
    r = client.post(
        "/api/help/ask",
        json={"question": "  where is   the bell  " + "x" * 600, "places": PLACES},
    )
    assert r.status_code == 200 and r.json()["text"] == "t"
    assert (
        seen["q"].startswith("where is the bell x")
        and len(seen["q"]) == help_routes.QUESTION_MAX
    )
    assert seen["places"] == PLACES


@pytest.mark.parametrize(
    "body", [{}, {"question": ""}, {"question": "   "}, {"question": 4}, [1], "x"]
)
def test_route_needs_a_question(monkeypatch, body):
    monkeypatch.setattr(help_routes, "_ask", lambda q, p: pytest.fail("asked"))
    assert client.post("/api/help/ask", json=body).status_code == 400


def test_route_refuses_a_guest(monkeypatch):
    monkeypatch.setattr(
        help_routes, "_ask", lambda q, p: pytest.fail("a guest's question was asked")
    )
    monkeypatch.setattr(guests, "scope_guest", lambda request: {"id": "g"})
    assert (
        client.post("/api/help/ask", json={"question": "where is the bell"}).status_code
        == 403
    )


def test_a_guest_link_never_reaches_the_route():
    assert (
        guests.path_allowed("POST", "/api/help/ask", {"id": "g", "sessions": []})
        is False
    )


def test_voice_help_is_unchanged(docs, monkeypatch):
    """The spoken path still answers in its own words (two sentences, said)."""
    got = vh.answer("how does the schedule page run a template")
    assert got["model"] is False
    assert got["say"].startswith("From Schedules: The Schedule page runs a template")


# --- the pop-up's starter questions ---------------------------------------------------
STARTERS = [
    ("How do I schedule a template?", "scheduled"),
    ("Where do I change the theme?", "themes"),
    ("What does Goes as far as do?", "goes as far as"),
    ("How do I connect GitHub?", "connect github"),
]


def test_the_starter_questions_are_answered_by_the_docs():
    """The empty pop-up offers these (frontend components/helpAsk/askApi.ts STARTERS):
    each must find a user's section, so the first click is a real answer."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/helpAsk/askApi.ts"
    ).read_text()
    for question, heading in STARTERS:
        assert '"%s"' % question in src, question
        hits = ha.search(question, 3)
        assert hits, question
        assert any(heading in " ".join(p.headings).lower() for _, p in hits), (
            question,
            [p.headings[-1] for _, p in hits],
        )
