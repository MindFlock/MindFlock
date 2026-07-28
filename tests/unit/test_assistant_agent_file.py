"""The assistant 'agent file' editor: GET/PUT its CLAUDE.md instructions + the
UI button/modal that edit them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import assistant as A


@pytest.fixture()
def client(tmp_path, monkeypatch):
    d = tmp_path / "assistant"
    d.mkdir()
    # The module binds its paths at import; point them at a tmp dir for the test.
    monkeypatch.setattr(A, "ASSIST_DIR", d)
    monkeypatch.setattr(A, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(A, "ASSIST_USER_MD", d / "user_instructions.md")
    monkeypatch.setattr(A, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    with TestClient(server.app) as c:
        yield c


def test_get_returns_only_user_block_not_managed(client):
    # A fresh install has no user instructions yet — the editor opens empty,
    # NOT showing the managed seed (todo rules etc.).
    r = client.get("/api/assistant/instructions").json()
    assert r["text"] == ""
    # But the managed block IS on disk (so the assistant still has its rules).
    assert "todos.json" in A.ASSIST_CLAUDE_MD.read_text()


def test_put_saves_user_block_and_keeps_managed_rules(client):
    body = {"text": "# My assistant\n\nAlways answer in haiku."}
    put = client.put("/api/assistant/instructions", json=body).json()
    assert "haiku" in put["text"]
    # GET returns ONLY the user portion (not the seed).
    got = client.get("/api/assistant/instructions").json()["text"]
    assert got.strip() == "# My assistant\n\nAlways answer in haiku.".strip()
    assert "todos.json" not in got  # managed block is hidden from the editor
    # On disk: the managed todo-list rules survive alongside the user block, so a
    # user's custom instructions can never break todo management.
    on_disk = A.ASSIST_CLAUDE_MD.read_text()
    assert "todos.json" in on_disk and "haiku" in on_disk
    assert A._USER_MARKER in on_disk


def test_repeated_saves_do_not_duplicate_managed_block(client):
    client.put("/api/assistant/instructions", json={"text": "USERTEXT_ALPHA"})
    client.put("/api/assistant/instructions", json={"text": "USERTEXT_BETA"})
    on_disk = A.ASSIST_CLAUDE_MD.read_text()
    assert on_disk.count(A._USER_MARKER) == 1  # exactly one split point
    assert (
        "USERTEXT_ALPHA" not in on_disk and "USERTEXT_BETA" in on_disk
    )  # replaced, not appended
    assert "todos.json" in on_disk  # managed rules still present


def test_seed_only_claude_md_opens_empty_box(client):
    # A CLAUDE.md carrying only the managed seed (no user file) must NOT leak
    # the built-in prompt into the editor — the box opens empty.
    A.ASSIST_USER_MD.unlink(missing_ok=True)
    A.ASSIST_CLAUDE_MD.write_text(A._ASSIST_CLAUDE_MD_SEED, encoding="utf-8")
    got = client.get("/api/assistant/instructions").json()["text"]
    assert got == ""  # seed never shown
    assert A.ASSIST_USER_MD.read_text().strip() == ""  # empty user file created
    assert (
        "todos.json" in A.ASSIST_CLAUDE_MD.read_text()
    )  # seed still on disk for claude


def test_seed_change_never_leaks_into_box(client):
    # Even if a stored CLAUDE.md carries an OLD seed wording, the box shows only
    # the user's file — the managed prompt can't appear.
    A.ASSIST_USER_MD.write_text("Answer in haiku.\n", encoding="utf-8")
    A.ASSIST_CLAUDE_MD.write_text(
        "# MindFlock Personal Assistant\n\nOLD SEED WORDING\n", encoding="utf-8"
    )
    got = client.get("/api/assistant/instructions").json()["text"]
    assert got == "Answer in haiku."
    assert "OLD SEED WORDING" not in got


def test_put_rejects_non_string(client):
    r = client.put("/api/assistant/instructions", json={"text": 123})
    assert r.status_code == 400


def test_restart_endpoint_ok(client, monkeypatch):
    called = {}
    monkeypatch.setattr(
        A, "_restart_assistant_session", lambda: called.setdefault("hit", True)
    )
    r = client.post("/api/assistant/restart").json()
    assert r == {"ok": True}
    assert called.get("hit") is True


def test_ui_exposes_agent_button_and_editor(client):
    html = client.get("/").text
    assert '"assistant-agent-btn"' in client.get("/app.js").text
    assert '"assistant-agent-dialog"' in client.get("/app.js").text
    assert '"assistant-agent-text"' in client.get("/app.js").text
    js = client.get("/app.js").text
    assert "/api/assistant/instructions" in js
    assert "/api/assistant/restart" in js
