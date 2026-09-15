"""Unit tests for the Assistant addon (backend.web.addons.assistant).

Covers the file-backed surface that has no other coverage: the CLAUDE.md
seed/marker composition, the atomic writer, the defensive dir seeding, the
user-instructions read/write, the tolerant todos parser, and the todos +
instructions REST endpoints (id de-duplication, normalization, validation and
the error paths); the window's state pill (the ``/state`` route and the
session-shaped stand-in it probes through); and the one part of
``_ensure_assistant_session`` that can be checked without tmux — that the launch
carries the assistant's directory, which is where the CLI's activity hooks get
installed. The rest of the tmux/PTY surface (actually starting a session, the
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


class TestStateEndpoint:
    """The window's pill: the Assistant read through the SAME activity ladder
    every session row is painted from."""

    def test_title_and_tmux_name_agree(self, A):
        from backend.session import tmux

        # The ladder derives the tmux name from the title itself, so a title
        # that doesn't round-trip to ASSIST_TMUX would silently probe a session
        # that doesn't exist (and report a permanent "offline").
        assert tmux.to_mindflock_tmux_name(A.ASSIST_TITLE) == A.ASSIST_TMUX

    def test_stand_in_answers_what_the_ladder_asks_of_a_session(self, A):
        from backend import session

        inst = A._ASSIST_INST
        assert inst.Title == A.ASSIST_TITLE
        assert inst.Started() is True
        assert inst.Status != session.Paused  # never reported offline for this
        assert inst.GetWorktreePath() == str(A.ASSIST_DIR)
        assert inst.Program  # whatever CLI the Assistant is configured to run

    def test_stand_in_is_one_object(self, A):
        # server._probe_cached only serves its memo to the object it memoized
        # against; a fresh stand-in per request would poll tmux every time.
        assert A._ASSIST_INST is A._ASSIST_INST

    def test_state_reports_the_activity_probe(self, client, A, monkeypatch):
        seen = {}

        def fake_probe(inst, title):
            seen["inst"], seen["title"] = inst, title
            return "clarify"

        from backend.web import server

        monkeypatch.setattr(server, "_agent_activity_cached", fake_probe)
        r = client.get("/api/assistant/state")
        assert r.status_code == 200 and r.json() == {"activity": "clarify"}
        assert seen["inst"] is A._ASSIST_INST and seen["title"] == A.ASSIST_TITLE

    def test_state_reports_offline_when_the_probe_blows_up(self, client, monkeypatch):
        from backend.web import server

        def _boom(*a, **k):
            raise RuntimeError("no tmux")

        monkeypatch.setattr(server, "_agent_activity_cached", _boom)
        # A state read is decoration; it must never 500 the window.
        assert client.get("/api/assistant/state").json() == {"activity": "offline"}


class TestLaunchContext:
    """The Assistant's launch carries its own directory — the thing that makes
    the state pill possible at all (it is where the provider installs its
    activity-reporting hooks, and a hook is the only signal that can say "the
    agent asked you something")."""

    def test_launch_passes_the_assistant_dir_as_the_workdir(self, A, monkeypatch):
        seen = {}

        class _Provider:
            def is_natural_exit(self, _marker):
                return True

            def build_launch_command(self, ctx):
                seen["ctx"] = ctx
                return "claude"

        def fake_run(argv, **kw):
            # has-session: not running -> the launch path; everything else OK.
            rc = 1 if "has-session" in argv else 0
            return subprocess.CompletedProcess(argv, rc, stderr=b"")

        monkeypatch.setattr(A.subprocess, "run", fake_run)
        monkeypatch.setattr(A.providers, "resolve", lambda _p: _Provider())
        monkeypatch.setattr(A, "_read_exit_marker", lambda _n: None)
        monkeypatch.setattr(A, "_clear_exit_marker", lambda _n: None)
        monkeypatch.setattr(A, "_wrap_launch_cmd", lambda cmd, _n: cmd)
        monkeypatch.setattr(A, "apply_scroll_speed", lambda: None)

        name, err = A._ensure_assistant_session()
        assert (name, err) == (A.ASSIST_TMUX, None)
        assert seen["ctx"].workdir == str(A.ASSIST_DIR)
        assert seen["ctx"].session_name == A.ASSIST_TMUX


class TestStateMemo:
    """The reason the stand-in is a module-level singleton at all.

    ``server._probe_cached`` keys its ~2.5s memo on (probe, title) and only
    serves the entry back to the SAME object it memoized against. A fresh
    stand-in per request would key identically and miss every time, so the
    window's poll would shell out to tmux on every tick — which is the cost the
    memo exists to avoid for real sessions.
    """

    @pytest.fixture(autouse=True)
    def _clean_memo(self):
        from backend.web import server

        server._PROBE_CACHE.clear()
        yield
        server._PROBE_CACHE.clear()

    def test_two_reads_inside_the_window_probe_once(self, A, monkeypatch):
        from backend.web import server

        calls = []

        def counting(inst, title):
            calls.append(title)
            return "working"

        monkeypatch.setattr(server, "_agent_activity", counting)
        assert A._assistant_activity() == "working"
        assert A._assistant_activity() == "working"
        assert calls == [A.ASSIST_TITLE]

    def test_the_singleton_is_what_makes_the_memo_hit(self, A, monkeypatch):
        """Same probe, same title, a different object: the memo correctly
        refuses to serve it. This is the miss the singleton avoids."""
        from backend.web import server

        calls = []
        monkeypatch.setattr(
            server, "_agent_activity", lambda inst, title: calls.append(title) or "idle"
        )
        assert A._assistant_activity() == "idle"
        # A second, equal-but-not-identical stand-in.
        fresh = A._AssistantInstance()
        assert server._agent_activity_cached(fresh, A.ASSIST_TITLE) == "idle"
        assert len(calls) == 2

    def test_the_route_reads_through_the_same_memo(self, client, A, monkeypatch):
        from backend.web import server

        calls = []
        monkeypatch.setattr(
            server,
            "_agent_activity",
            lambda inst, title: calls.append(title) or "clarify",
        )
        first = client.get("/api/assistant/state").json()
        second = client.get("/api/assistant/state").json()
        assert first == second == {"activity": "clarify"}
        assert len(calls) == 1


class TestStateVocabulary:
    """``offline`` is a real answer, not an error code.

    The window says "offline" before its first chat (there is no tmux session
    yet) and "idle" once one exists and its agent is between turns. Collapsing
    the two would make a never-used Assistant look like one that is standing by.
    """

    @pytest.fixture(autouse=True)
    def _clean_memo(self):
        from backend.web import server

        server._PROBE_CACHE.clear()
        yield
        server._PROBE_CACHE.clear()

    def test_a_session_that_was_never_started_reads_offline(self, A, monkeypatch):
        from backend.web import server

        # No tmux session by this name: `has-session` answers non-zero, which is
        # the layer that returns "offline" before any pane is inspected.
        probed = []

        def no_session(argv, **kw):
            probed.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, b"", b"")

        monkeypatch.setattr(server, "_run_capped", no_session)
        assert A._assistant_activity() == "offline"
        # Asserted, because `_assistant_activity` also answers "offline" when
        # the read BLOWS UP — and a test that couldn't tell the two apart would
        # keep passing after the ladder stopped being consulted at all.
        assert probed == [["tmux", "has-session", "-t=" + A.ASSIST_TMUX]]

    def test_the_stand_in_never_reports_itself_paused(self, A):
        """The two ``offline`` returns above the tmux probe are "not started"
        and "paused" — neither of which the Assistant can be. Its window is
        offline only when its tmux session is genuinely gone, which is the one
        thing the person looking at it can act on."""
        from backend import session

        inst = A._ASSIST_INST
        assert inst.Started() is True and inst.Status == session.Running
