"""Timed todos (the Assistant's todo list): a todo with ``due`` pops up in a
dialog at that time or any time after, can be snoozed, dismissed or checked
off, and is announced ONCE (``todo.due``, which the notify addon pushes to the
phone) — even with two servers reading the same todo file."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import assistant as A


@pytest.fixture()
def todos(tmp_path, monkeypatch):
    d = tmp_path / "assistant"
    d.mkdir()
    monkeypatch.setattr(A, "ASSIST_DIR", d)
    monkeypatch.setattr(A, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(A, "ASSIST_USER_MD", d / "user_instructions.md")
    monkeypatch.setattr(A, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")

    def write(items):
        (d / "todos.json").write_text(json.dumps(items), encoding="utf-8")

    def read():
        return json.loads((d / "todos.json").read_text(encoding="utf-8"))

    write.read = read
    return write


@pytest.fixture()
def client(todos):
    return TestClient(server.app)  # no lifespan: no reminder loop running


def _local(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.localtime(epoch))


# --------------------------------------------------------------------------- #
# Normalising the due time
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, want",
    [
        ("2026-11-10T09:00", "2026-11-10T09:00"),
        ("2026-11-10T09:00:30", "2026-11-10T09:00"),
        ("", ""),
        (None, ""),
        ("next tuesday", ""),
        ("2026-13-40T99:00", ""),
    ],
)
def test_due_is_a_local_minute_or_nothing(raw, want):
    assert A._norm_due(raw) == want


def test_an_iso_stamp_with_an_offset_becomes_local_time():
    epoch = 1_800_000_000
    utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    assert A._norm_due(utc) == _local(epoch)
    assert A._norm_due(epoch) == _local(epoch)


def test_a_list_without_reminders_reads_as_it_always_did(todos):
    todos([{"id": "a", "text": "x", "done": False}])
    assert A._read_todos() == [{"id": "a", "text": "x", "done": False}]


# --------------------------------------------------------------------------- #
# When it pops up
# --------------------------------------------------------------------------- #
def test_due_now_is_at_the_time_or_any_time_after():
    now = time.time()
    past = {"id": "p", "text": "p", "done": False, "due": _local(now - 3600)}
    future = {"id": "f", "text": "f", "done": False, "due": _local(now + 3600)}
    done = dict(past, id="d", done=True)
    plain = {"id": "n", "text": "n", "done": False}
    assert [t["id"] for t in A.due_now([future, past, done, plain], now)] == ["p"]


def test_snooze_and_dismiss_move_or_stop_the_reminder():
    now = time.time()
    t = {"id": "p", "text": "p", "done": False, "due": _local(now - 3600)}
    assert A.due_now([dict(t, snoozed_until=now + 600)], now) == []
    assert A.due_now([dict(t, snoozed_until=now - 1)], now) != []
    assert A.due_now([dict(t, dismissed_at=now)], now) == []


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #
def test_due_route_snooze_dismiss_done(client, todos):
    now = time.time()
    todos(
        [
            {
                "id": "a",
                "text": "Call the bank",
                "done": False,
                "due": _local(now - 120),
            },
            {"id": "b", "text": "Later", "done": False, "due": _local(now + 86400)},
        ]
    )
    due = client.get("/api/assistant/todos/due").json()["due"]
    assert [t["id"] for t in due] == ["a"]

    r = client.post("/api/assistant/todos/a/snooze", json={"minutes": 10})
    assert r.status_code == 200
    assert client.get("/api/assistant/todos/due").json()["due"] == []
    assert todos.read()[0]["snoozed_until"] > now + 500

    assert client.post("/api/assistant/todos/a/snooze", json={}).status_code == 400
    assert (
        client.post("/api/assistant/todos/zz/snooze", json={"minutes": 5}).status_code
        == 404
    )

    until = _local(now + 7200)
    r = client.post("/api/assistant/todos/a/snooze", json={"until": until})
    assert abs(r.json()["todo"]["snoozed_until"] - A.due_epoch({"due": until})) < 1

    client.post("/api/assistant/todos/b/dismiss")
    assert todos.read()[1]["dismissed_at"] >= A.due_epoch(todos.read()[1])

    client.post("/api/assistant/todos/a/done")
    assert todos.read()[0]["done"] is True


def test_saving_the_list_keeps_a_snooze_and_a_new_time_resets_it(client, todos):
    now = time.time()
    due = _local(now - 60)
    todos(
        [
            {
                "id": "a",
                "text": "x",
                "done": False,
                "due": due,
                "snoozed_until": now + 900,
            }
        ]
    )
    # A client that knows nothing of snoozes saves the list (e.g. a reorder).
    client.put(
        "/api/assistant/todos",
        json={"todos": [{"id": "a", "text": "x", "done": False, "due": due}]},
    )
    assert todos.read()[0]["snoozed_until"] == pytest.approx(now + 900)
    # A new time is a new reminder.
    later = _local(now + 3600)
    client.put(
        "/api/assistant/todos",
        json={"todos": [{"id": "a", "text": "x", "done": False, "due": later}]},
    )
    assert "snoozed_until" not in todos.read()[0]
    assert todos.read()[0]["due"] == later


# --------------------------------------------------------------------------- #
# Announced once
# --------------------------------------------------------------------------- #
def test_a_due_todo_is_announced_once_per_due_time(todos, monkeypatch):
    from backend.web.core import events as E

    seen = []
    monkeypatch.setattr(
        E.BUS, "emit", lambda name, **kw: seen.append((name, kw["data"]["id"]))
    )
    now = time.time()
    todos(
        [{"id": "a", "text": "Call the bank", "done": False, "due": _local(now - 60)}]
    )
    assert [t["id"] for t in A._announce_due(now)] == ["a"]
    assert seen == [("todo.due", "a")]
    # A second pass — or a second server reading the same file — stays quiet.
    assert A._announce_due(now + 20) == []
    assert seen == [("todo.due", "a")]
    # Snoozed then run out: announced again, once.
    items = todos.read()
    items[0]["snoozed_until"] = now + 100
    todos(items)
    assert A._announce_due(now + 50) == []
    assert [t["id"] for t in A._announce_due(now + 120)] == ["a"]
    assert len(seen) == 2


def test_the_notify_rule_pushes_todo_due():
    from backend.web.addons import notify as N

    rules = {r["id"]: r for r in N.NOTIFY_RULES}
    assert rules["todo_due"]["event"] == "todo.due"
    assert rules["todo_due"]["default_enabled"] is True
