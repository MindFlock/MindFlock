"""Voice help: ``core.voice_help`` (the docs index, the answer) and ``POST
/api/voice/help``.

The ranking runs against the real ``docs/`` (it is the product: a question
must find its section there); the mechanics run against a small docs folder
under ``tmp_path`` (``MINDFLOCK_DOCS_DIR``). The model is never asked:
``_ask_model`` refuses under pytest, and every test that wants a worded
answer replaces it.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from backend.web.core import commit_message
from backend.web.core import voice_help as vh
from backend.web.routes import voice as voice_routes
from backend.web.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(vh, "_INDEX", None)
    vh._CACHE.clear()
    yield
    vh._CACHE.clear()


# --- the real docs ---------------------------------------------------------------
@pytest.mark.parametrize(
    "question, want",
    [
        ("how do I schedule a template", ("web-ui.md", "new-session dialog")),
        ("how does the fence work", ("mcp.md", "fences")),
        ("what does autopilot do", ("team-runs.md", "fast-track")),
        ("what are red zones", ("web-ui.md", "zones")),
        ("how do I change the voice words", ("web-ui.md", "voice words")),
        ("what is the code map", ("web-ui.md", "code map")),
        (
            "how do I restore a recently closed session",
            ("web-ui.md", "recently closed"),
        ),
        ("what does the bell do", ("web-ui.md", "notifications")),
        ("how do I work with other people", ("people.md", "people")),
        ("how do I turn on spoken replies on the phone", ("web-ui.md", "voice")),
        ("what is a team run", ("", "team run")),
        ("how do I add my mac", ("", "device")),
    ],
)
def test_search_finds_the_section(question, want):
    doc, heading = want
    hits = vh.search(question, 3)
    assert hits, question
    got = [(p.doc, " ".join(p.headings).lower()) for _, p in hits]
    assert any((not doc or d == doc) and heading in h for d, h in got), got


def test_the_real_docs_are_found():
    assert vh.docs_dir() is not None
    assert (vh.docs_dir() / "web-ui.md").is_file()


# --- the index, on a docs folder of our own ------------------------------------
DOC = """# Schedules

The Schedule page runs a template at a time you pick. Open it from the top bar.
It needs a folder and a task.

```bash
mindflock run TEMPLATE --secret-code-word
```

## Fences

A **fence** keeps a worker inside the paths it may change. Everything else is
read-only.

| Choice | Meaning |
|---|---|
| Keep out | the agent may read but not change it |

### `fence_session`

The tool that sets a fence.
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
    return d


def test_passages_follow_the_headings_and_drop_code(docs):
    idx = vh._index()
    texts = [p.text for p in idx.passages]
    assert not any("secret-code-word" in t for t in texts)
    row = next(p for p in idx.passages if p.text.startswith("Keep out"))
    assert row.text == "Keep out — the agent may read but not change it"
    assert row.headings == ("Schedules", "Fences")
    first = [p for p in idx.passages if p.first]
    assert {p.heading for p in first} >= {"Schedules", "Fences", "`fence_session`"}


def test_search_ranks_heading_and_body(docs):
    assert vh.search("how do I schedule a template")[0][1].heading == "Schedules"
    assert vh.search("what is a fence")[0][1].heading == "Fences"
    assert vh.search("the of and") == []
    assert vh.search("quantum chromodynamics") == []


def test_index_is_cached_and_rebuilt_when_a_doc_changes(docs):
    a = vh._index()
    assert vh._index() is a
    f = docs / "other.md"
    f.write_text("# Other\n\nZebras live here now, many zebras.\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    b = vh._index()
    assert b is not a
    assert vh.search("zebras")[0][1].doc == "other.md"


def test_speakable_heading():
    assert (
        vh.speakable_heading("Voice: talking to a session")
        == "Voice, talking to a session"
    )
    assert vh.speakable_heading("Settings (⚙)") == "Settings"
    assert vh.speakable_heading("Mobile UI (`/m`)") == "Mobile UI"
    assert vh.speakable_heading("Git worktrees (`session/git/`)") == "Git worktrees"
    assert vh.speakable_heading("**Fast-track**") == "Fast-track"


# --- the answer ------------------------------------------------------------------
def test_answer_is_worded_by_the_model(docs, monkeypatch):
    asked = []

    def model(prompt):
        asked.append(prompt)
        return (
            "Open **Schedule** in the top bar and pick a time.\nThen save. Then more."
        )

    monkeypatch.setattr(vh, "_ask_model", model)
    got = vh.answer("how do I schedule a template?")
    assert got == {
        "say": "From Schedules: Open Schedule in the top bar and pick a time. Then save.",
        "source": {"doc": "web-ui.md", "heading": "Schedules"},
        "model": True,
    }
    (prompt,) = asked
    assert "Question: how do I schedule a template?" in prompt
    assert "The Schedule page runs a template" in prompt
    assert "I don't know" in prompt
    # Asked again: the kept answer, no second model call.
    assert vh.answer("How do I schedule a template?") == got
    assert len(asked) == 1


def test_model_says_it_does_not_know(docs, monkeypatch):
    monkeypatch.setattr(vh, "_ask_model", lambda p: "I don't know.")
    got = vh.answer("how do I schedule a template")
    assert got == {"say": vh.DONT_KNOW, "source": None, "model": True}


@pytest.mark.parametrize(
    "why", ["claude is not installed", "claude did not answer within 20s"]
)
def test_fallback_reads_the_best_section(docs, monkeypatch, why):
    def boom(prompt):
        raise vh.HelpModelError(why)

    monkeypatch.setattr(vh, "_ask_model", boom)
    got = vh.answer("how do I schedule a template")
    assert got == {
        "say": "From Schedules: The Schedule page runs a template at a time you pick. "
        "Open it from the top bar.",
        "source": {"doc": "web-ui.md", "heading": "Schedules"},
        "model": False,
    }
    # A failure is never kept: the next ask tries the model again.
    monkeypatch.setattr(vh, "_ask_model", lambda p: "Pick a time.")
    assert vh.answer("how do I schedule a template")["model"] is True


def test_fallback_strips_markdown(docs, monkeypatch):
    monkeypatch.setattr(vh, "_ask_model", lambda p: "")
    got = vh.answer("what is a fence")
    assert got["say"] == (
        "From Fences: A fence keeps a worker inside the paths it may change. "
        "Everything else is read-only."
    )
    assert got["model"] is False


def test_nothing_found_and_no_docs(docs, monkeypatch, tmp_path):
    monkeypatch.setattr(vh, "_ask_model", lambda p: pytest.fail("no passage, no model"))
    assert vh.answer("quantum chromodynamics") == {
        "say": vh.NOT_FOUND,
        "source": None,
        "model": False,
    }
    monkeypatch.setenv("MINDFLOCK_DOCS_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(vh, "docs_dir", lambda: None)
    assert vh.answer("what is a fence") == {
        "say": vh.NO_DOCS,
        "source": None,
        "model": False,
    }


def test_the_model_is_never_asked_under_pytest(docs, monkeypatch):
    def run(*a, **k):
        pytest.fail("a model call ran under pytest")

    monkeypatch.setattr(commit_message, "_run", run)
    with pytest.raises(vh.HelpModelError):
        vh._ask_model("anything")
    # The real answer path falls back to the passage.
    assert vh.answer("what is a fence")["model"] is False


def test_model_argv_is_haiku_without_mcp(monkeypatch):
    """Outside pytest the call is ``claude --model <HELP_MODEL> … -p``, from
    the neutral one-shot dir, with HELP_TIMEOUT_S."""
    seen = {}

    def run(argv, cwd, timeout):
        seen.update(argv=argv, cwd=cwd, timeout=timeout)
        return "ok"

    monkeypatch.setattr(commit_message, "_run", run)
    monkeypatch.setattr(commit_message, "neutral_cwd", lambda: "/neutral")
    monkeypatch.setattr(vh, "_under_pytest", lambda: False)
    assert vh._ask_model("the prompt") == "ok"
    argv = seen["argv"]
    assert argv[1:3] == ["--model", vh.HELP_MODEL]
    assert "--strict-mcp-config" in argv and argv[-2:] == ["-p", "the prompt"]
    assert (seen["cwd"], seen["timeout"]) == ("/neutral", vh.HELP_TIMEOUT_S)


def test_build_prompt_keeps_to_the_budget(docs, monkeypatch):
    monkeypatch.setattr(vh, "CONTEXT_MAX", 50)
    passages = [p for _, p in vh.search("fence schedule template")]
    prompt = vh.build_prompt("what?", passages)
    body = prompt.split("Excerpts:", 1)[1]
    assert body.count("[") >= 1
    assert len(body) < 50 + 200 * len(passages)
