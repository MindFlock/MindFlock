"""Unit tests for the Assistant addon (backend.web.addons.assistant).

Covers the file-backed surface that has no other coverage: the CLAUDE.md
seed/marker composition, the atomic writer, the defensive dir seeding, the
user-instructions read/write, the tolerant todos parser, and the todos +
instructions REST endpoints (id de-duplication, normalization, validation and
the error paths). The tmux/PTY surface (``_ensure_assistant_session``, the
``/terminal`` websocket) is intentionally left uncovered — it needs a live tmux
server and a real PTY, neither available on CI.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def A(tmp_path, monkeypatch):
    """The assistant module with all four on-disk paths repointed under
    ``tmp_path`` (they are module constants resolved at import time)."""
    from backend.web.addons import assistant as mod

    d = tmp_path / "assist"
    monkeypatch.setattr(mod, "ASSIST_DIR", d)
    monkeypatch.setattr(mod, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setattr(mod, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(mod, "ASSIST_USER_MD", d / "user_instructions.md")
    return mod


@pytest.fixture
def client(A):
    app = FastAPI()
    app.include_router(A.AssistantAddon().router)
    return TestClient(app)


class TestComposeAgentFile:
    def test_empty_user_text_is_seed_plus_marker_only(self, A):
        out = A._compose_agent_file("")
        assert A._USER_MARKER in out
        # The seed's heading survives; no user body is appended after the marker.
        assert "# MindFlock Personal Assistant" in out
        assert out.rstrip().endswith(A._USER_MARKER)

    def test_user_text_appended_after_marker(self, A):
        out = A._compose_agent_file("  be terse  ")
        marker_idx = out.index(A._USER_MARKER)
        # The (stripped) user body appears after the marker, never before it.
        assert out.index("be terse") > marker_idx
        assert "  be terse  " not in out  # leading/trailing whitespace stripped


class TestAtomicWrite:
    def test_writes_content_and_leaves_no_tmp(self, A, tmp_path):
        target = tmp_path / "f.txt"
        A._atomic_write(target, "hello")
        assert target.read_text() == "hello"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_overwrites_existing(self, A, tmp_path):
        target = tmp_path / "f.txt"
        A._atomic_write(target, "old")
        A._atomic_write(target, "new")
        assert target.read_text() == "new"


class TestSeedAssistantDir:
    def test_creates_dir_and_all_files(self, A):
        A._seed_assistant_dir()
        assert A.ASSIST_DIR.is_dir()
        assert A.ASSIST_USER_MD.read_text() == ""
        assert A.ASSIST_TODOS.read_text() == "[]\n"
        claude = A.ASSIST_CLAUDE_MD.read_text()
        assert A._USER_MARKER in claude
        assert "# MindFlock Personal Assistant" in claude

    def test_regenerates_claude_md_from_user_text(self, A):
        A._seed_assistant_dir()
        A.ASSIST_USER_MD.write_text("custom rule", encoding="utf-8")
        A._seed_assistant_dir()  # should fold the new user text into CLAUDE.md
        assert "custom rule" in A.ASSIST_CLAUDE_MD.read_text()

    def test_does_not_rewrite_when_unchanged(self, A):
        A._seed_assistant_dir()
        before = A.ASSIST_CLAUDE_MD.stat().st_mtime_ns
        A._seed_assistant_dir()  # identical -> avoid the needless write
        assert A.ASSIST_CLAUDE_MD.stat().st_mtime_ns == before

    def test_does_not_reseed_existing_todos(self, A):
        A._seed_assistant_dir()
        A.ASSIST_TODOS.write_text('[{"id":"x","text":"keep","done":false}]')
        A._seed_assistant_dir()  # todos.json is seeded once, never clobbered
        assert "keep" in A.ASSIST_TODOS.read_text()

    def test_swallows_errors(self, A, monkeypatch):
        # A failure anywhere in seeding is logged, never raised (import-time /
        # startup safety). Force the atomic writer to blow up mid-seed.
        def _boom(*a, **k):
            raise OSError("nope")

        monkeypatch.setattr(A, "_atomic_write", _boom)
        A._seed_assistant_dir()  # must not raise


class TestReadWriteInstructions:
    def test_read_seeds_then_returns_empty(self, A):
        assert A._read_instructions() == ""
        assert A.ASSIST_USER_MD.exists()  # seeded as a side effect

    def test_write_then_read_round_trip(self, A):
        A._seed_assistant_dir()  # startup seeds the dir before any PUT arrives
        A._write_instructions("do the thing")
        assert A.ASSIST_USER_MD.read_text() == "do the thing\n"
        assert A._read_instructions() == "do the thing"
        # CLAUDE.md was regenerated to include the user text.
        assert "do the thing" in A.ASSIST_CLAUDE_MD.read_text()

    def test_write_empty_clears_file(self, A):
        A._seed_assistant_dir()
        A._write_instructions("something")
        A._write_instructions("   ")  # whitespace-only == cleared
        assert A.ASSIST_USER_MD.read_text() == ""
        assert A._read_instructions() == ""

    def test_unreadable_user_file_is_tolerated(self, A):
        # A directory where the user-instructions file should be makes read_text
        # raise OSError; both seeding and reading must degrade to "" not crash.
        A.ASSIST_DIR.mkdir(parents=True)
        A.ASSIST_USER_MD.mkdir()  # exists() is True, but read_text() raises
        A._seed_assistant_dir()  # must not raise (user text treated as "")
        assert A._read_instructions() == ""


class TestReadTodos:
    def test_missing_file_is_empty_list(self, A):
        assert A._read_todos() == []

    def test_malformed_json_is_empty_list(self, A):
        A.ASSIST_DIR.mkdir(parents=True)
        A.ASSIST_TODOS.write_text("{not json")
        assert A._read_todos() == []

    def test_non_list_root_is_empty_list(self, A):
        A.ASSIST_DIR.mkdir(parents=True)
        A.ASSIST_TODOS.write_text('{"a": 1}')
        assert A._read_todos() == []

    def test_normalizes_items_and_skips_non_dicts(self, A):
        A.ASSIST_DIR.mkdir(parents=True)
        A.ASSIST_TODOS.write_text(
            '[{"id": "a", "text": "t1", "done": true},'
            ' "not-a-dict",'
            ' {"text": "no-id"}]'
        )
        got = A._read_todos()
        assert got == [
            {"id": "a", "text": "t1", "done": True},
            {"id": "t2", "text": "no-id", "done": False},  # index-derived id
        ]


class TestTodosEndpoints:
    def test_get_todos_empty(self, client):
        r = client.get("/api/assistant/todos")
        assert r.status_code == 200
        assert r.json() == {"todos": []}

    def test_put_todos_normalizes_and_persists(self, client, A):
        r = client.put(
            "/api/assistant/todos",
            json={"todos": [{"id": "x", "text": "buy milk", "done": True}]},
        )
        assert r.status_code == 200
        assert r.json()["todos"] == [{"id": "x", "text": "buy milk", "done": True}]
        # Round-trips through the file the agent also edits.
        assert A._read_todos() == [{"id": "x", "text": "buy milk", "done": True}]

    def test_put_todos_dedupes_ids_and_fills_missing(self, client):
        r = client.put(
            "/api/assistant/todos",
            json={
                "todos": [
                    {"id": "a", "text": "one"},
                    {"id": "a", "text": "two"},  # dup id -> suffixed
                    {"text": "three"},  # no id -> index-derived
                    "junk",  # non-dict -> dropped
                ]
            },
        )
        todos = r.json()["todos"]
        assert [t["id"] for t in todos] == ["a", "a_", "t2"]
        assert [t["text"] for t in todos] == ["one", "two", "three"]

    def test_put_todos_rejects_non_list(self, client):
        r = client.put("/api/assistant/todos", json={"todos": "nope"})
        assert r.status_code == 400
        assert "list" in r.json()["error"]

    def test_put_todos_write_failure_is_500(self, client, A, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(A, "_atomic_write", _boom)
        r = client.put("/api/assistant/todos", json={"todos": []})
        assert r.status_code == 500
        assert "disk full" in r.json()["error"]


class TestInstructionsEndpoints:
    def test_get_then_put_round_trip(self, client):
        assert client.get("/api/assistant/instructions").json() == {"text": ""}
        r = client.put("/api/assistant/instructions", json={"text": "stay concise"})
        assert r.status_code == 200
        assert r.json() == {"text": "stay concise"}
        assert client.get("/api/assistant/instructions").json() == {
            "text": "stay concise"
        }

    def test_put_rejects_non_string_text(self, client):
        r = client.put("/api/assistant/instructions", json={"text": 123})
        assert r.status_code == 400
        assert "string" in r.json()["error"]

    def test_put_write_failure_is_500(self, client, A, monkeypatch):
        def _boom(_text):
            raise RuntimeError("boom")

        monkeypatch.setattr(A, "_write_instructions", _boom)
        r = client.put("/api/assistant/instructions", json={"text": "x"})
        assert r.status_code == 500
        assert "boom" in r.json()["error"]


class TestRestartEndpoint:
    def test_restart_kills_session_and_returns_ok(self, client, A, monkeypatch):
        calls = {}

        def fake_run(argv, **kw):
            calls["argv"] = argv
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(A.subprocess, "run", fake_run)
        r = client.post("/api/assistant/restart")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert calls["argv"][:2] == ["tmux", "kill-session"]

    def test_restart_survives_tmux_timeout(self, client, A, monkeypatch):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)

        monkeypatch.setattr(A.subprocess, "run", fake_run)
        # Best-effort: a wedged tmux must not fail the request.
        assert client.post("/api/assistant/restart").json() == {"ok": True}
