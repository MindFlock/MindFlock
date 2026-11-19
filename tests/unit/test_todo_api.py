"""The To-do REST API (/api/todo…), the manifest, and the Assistant's old
/api/assistant/todos routes now sharing the one store.

The store is pointed at a tmp dir (see test_todo_store.py) — never the real
``~/.mindflock-assistant``."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import assistant as A
from backend.web.addons.base import validate_extension_spec
from backend.web.addons.todo import TodoAddon
from backend.web.core import events as E


@pytest.fixture()
def files(tmp_path, monkeypatch):
    d = tmp_path / "assistant"
    d.mkdir()
    monkeypatch.setattr(A, "ASSIST_DIR", d)
    monkeypatch.setattr(A, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setattr(A, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(A, "ASSIST_USER_MD", d / "user_instructions.md")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")

    class F:
        todos = d / "todos.json"

        @staticmethod
        def write(items):
            F.todos.write_text(json.dumps(items), encoding="utf-8")

        @staticmethod
        def read():
            return json.loads(F.todos.read_text(encoding="utf-8"))

    return F


@pytest.fixture()
def client(files):
    return TestClient(server.app)  # no lifespan: no auto-source thread


@pytest.fixture()
def changed():
    seen = []

    def cb(env):
        if env["event"] == "addon.todo.changed":
            seen.append(env["data"])

    unsub = E.BUS.subscribe(cb)
    yield seen
    unsub()


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def test_manifest_validates_and_matches_the_contract():
    spec = TodoAddon().extension()
    assert validate_extension_spec("todo", spec) == []
    d = spec.to_dict()
    assert d["module"] == "/extensions/todo/index.js"
    assert d["bar_label"] == "To-do" and d["stylesheet"] is True
    assert d["api_version"] == 1 and d["badge"] == "/api/todo/badge"
    assert [c["id"] for c in d["commands"]] == ["todo.open", "todo.add", "todo.history"]
    assert {s["id"]: s["kind"] for s in d["surfaces"]} == {
        "list": "pane",
        "add": "dialog",
    }


def test_todo_is_listed_in_the_addon_manifest(client):
    addons = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
    assert addons["todo"]["label"] == "To-do"
    assert addons["todo"]["extension"]["badge"] == "/api/todo/badge"


def test_todo_is_a_reserved_extension_id():
    from backend.web.addons import RESERVED_EXTENSION_IDS

    assert "todo" in RESERVED_EXTENSION_IDS


# --------------------------------------------------------------------------- #
# /api/todo
# --------------------------------------------------------------------------- #
def test_get_empty(client):
    assert client.get("/api/todo").json() == {
        "todos": [],
        "counts": {"open": 0, "needs_you": 0, "high": 0},
    }


def test_add_one_and_many(client, changed):
    r = client.post(
        "/api/todo/items",
        json={"text": "Check login", "kind": "manual_test", "session": "s1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["added"][0]["text"] == "Check login" and body["updated"] == []
    assert body["counts"] == {"open": 1, "needs_you": 1, "high": 0}
    assert changed[-1]["reason"] == "added"

    r = client.post(
        "/api/todo/items",
        json={"items": [{"text": "a", "priority": "high"}, {"text": "b", "key": "k"}]},
    )
    assert [i["text"] for i in r.json()["added"]] == ["a", "b"]
    got = client.get("/api/todo").json()
    assert [t["text"] for t in got["todos"]] == ["a", "Check login", "b"]
    assert got["counts"]["high"] == 1

    # Same open key: updated in place.
    r = client.post("/api/todo/items", json={"text": "b2", "key": "k"})
    assert r.json()["added"] == [] and r.json()["updated"][0]["text"] == "b2"
    assert len(client.get("/api/todo").json()["todos"]) == 3


def test_add_rejects_bad_input(client):
    assert client.post("/api/todo/items", json={}).status_code == 400
    assert client.post("/api/todo/items", json={"items": []}).status_code == 400
    many = {"items": [{"text": "x"}] * 51}
    assert client.post("/api/todo/items", json=many).status_code == 400
    r = client.post("/api/todo/items", json={"text": "x", "priority": "urgent"})
    assert r.status_code == 400 and "priority" in r.json()["error"]
    # All or nothing.
    r = client.post("/api/todo/items", json={"items": [{"text": "ok"}, {"text": ""}]})
    assert r.status_code == 400
    assert client.get("/api/todo").json()["todos"] == []


def test_patch_delete_and_404(client, changed):
    tid = client.post("/api/todo/items", json={"text": "x"}).json()["added"][0]["id"]
    r = client.patch(
        "/api/todo/items/%s" % tid,
        json={"details": "more", "priority": "low", "done": True},
    )
    todo = r.json()["todo"]
    assert todo["details"] == "more" and todo["priority"] == "low"
    assert todo["done"] is True and todo["done_at"] > 0
    assert changed[-1]["reason"] == "done"
    r = client.patch("/api/todo/items/%s" % tid, json={"priority": "", "kind": ""})
    assert "priority" not in r.json()["todo"]
    assert client.patch("/api/todo/items/zz", json={"text": "y"}).status_code == 404
    assert (
        client.patch("/api/todo/items/%s" % tid, json={"text": ""}).status_code == 400
    )
    assert client.delete("/api/todo/items/%s" % tid).json() == {"ok": True}
    assert client.delete("/api/todo/items/%s" % tid).status_code == 404
    assert changed[-1]["reason"] == "removed"


def test_order(client):
    ids = [
        client.post("/api/todo/items", json={"text": t}).json()["added"][0]["id"]
        for t in "abc"
    ]
    r = client.post("/api/todo/order", json={"ids": [ids[2], ids[0]]})
    assert [t["text"] for t in r.json()["todos"]] == ["c", "a", "b"]
    assert client.post("/api/todo/order", json={"ids": "x"}).status_code == 400


def test_history_restore_forget(client):
    tid = client.post("/api/todo/items", json={"text": "Buy milk"}).json()["added"][0][
        "id"
    ]
    client.delete("/api/todo/items/%s" % tid)
    hist = client.get("/api/todo/history", params={"q": "MILK"}).json()["history"]
    assert hist[0]["id"] == tid and hist[0]["outcome"] == "removed"
    assert (
        client.get("/api/todo/history", params={"q": "bread"}).json()["history"] == []
    )
    assert client.get("/api/todo/history", params={"limit": "x"}).status_code == 400
    r = client.post("/api/todo/history/%s/restore" % tid)
    assert r.json()["todo"]["done"] is False
    assert client.post("/api/todo/history/zz/restore").status_code == 404
    client.delete("/api/todo/items/%s" % tid)
    assert client.delete("/api/todo/history/%s" % tid).json() == {"ok": True}
    assert client.get("/api/todo/history").json()["history"] == []
    assert client.delete("/api/todo/history/%s" % tid).status_code == 404


def test_badge_route(client):
    assert client.get("/api/todo/badge").json()["count"] == 0
    client.post("/api/todo/items", json={"text": "a", "kind": "needs_you"})
    assert client.get("/api/todo/badge").json() == {
        "count": 1,
        "title": "1 needs you · 1 open",
    }


def test_a_broken_file_is_a_500_not_an_overwrite(client, files):
    files.todos.write_text("{nope", encoding="utf-8")
    r = client.post("/api/todo/items", json={"text": "x"})
    assert r.status_code == 500 and "todos.json" in r.json()["error"]
    assert files.todos.read_text() == "{nope"


# --------------------------------------------------------------------------- #
# The Assistant's routes on the same store
# --------------------------------------------------------------------------- #
def test_assistant_put_keeps_fields_the_client_did_not_send(client, files, changed):
    files.write(
        [
            {
                "id": "a",
                "text": "Check login",
                "done": False,
                "details": "steps",
                "priority": "high",
                "kind": "manual_test",
                "session": "s1",
                "source": "verify",
                "key": "verify:s1:s1",
                "link": "https://x",
                "created_at": 123.0,
                "colour": "blue",
            },
            {"id": "b", "text": "Other", "done": False},
        ]
    )
    # An old client that only knows {id, text, done} reorders and ticks one off.
    r = client.put(
        "/api/assistant/todos",
        json={"todos": [{"id": "a", "text": "Check login!", "done": True}]},
    )
    assert r.status_code == 200
    row = files.read()[0]
    assert row["text"] == "Check login!" and row["done"] is True
    assert row["done_at"] > 0
    for k, v in {
        "details": "steps",
        "priority": "high",
        "kind": "manual_test",
        "session": "s1",
        "source": "verify",
        "key": "verify:s1:s1",
        "link": "https://x",
        "created_at": 123.0,
        "colour": "blue",
    }.items():
        assert row[k] == v, k
    # The one it left out went to history, not nowhere.
    hist = client.get("/api/todo/history").json()["history"]
    assert {h["id"]: h["outcome"] for h in hist} == {"a": "done", "b": "removed"}
    assert changed and changed[-1]["reason"] == "updated"
    # The old shape: no bookkeeping stamps.
    assert "done_at" not in r.json()["todos"][0]


def test_assistant_reminder_routes_emit_changed(client, files, changed):
    files.write([{"id": "a", "text": "x", "done": False, "due": "2026-11-10T09:00"}])
    client.post("/api/assistant/todos/a/snooze", json={"minutes": 5})
    assert changed[-1] == {"reason": "updated", "id": "a", "open": 1, "needs_you": 0}
    client.post("/api/assistant/todos/a/done")
    assert changed[-1]["reason"] == "done"
    assert files.read()[0]["done_at"] > 0
    # And the new list sees it.
    assert client.get("/api/todo").json()["todos"][0]["done"] is True


def test_the_new_and_old_routes_see_one_list(client):
    client.post("/api/todo/items", json={"text": "from the new API", "details": "d"})
    old = client.get("/api/assistant/todos").json()["todos"]
    assert old[0]["text"] == "from the new API" and old[0]["details"] == "d"
    client.put("/api/assistant/todos", json={"todos": old + [{"text": "from old"}]})
    texts = [t["text"] for t in client.get("/api/todo").json()["todos"]]
    assert texts == ["from the new API", "from old"]


def test_seed_documents_the_new_fields_and_never_deleting():
    seed = A._ASSIST_CLAUDE_MD_SEED
    for word in ('"details"', '"priority"', '"kind"', '"session"', "mark it done"):
        assert word in seed, word


def test_announce_due_does_not_emit_changed(files, changed):
    now = time.time()
    files.write(
        [
            {
                "id": "a",
                "text": "x",
                "done": False,
                "due": time.strftime("%Y-%m-%dT%H:%M", time.localtime(now - 120)),
            }
        ]
    )
    assert [t["id"] for t in A._announce_due(now)] == ["a"]
    assert changed == []


def test_badge_must_be_an_api_path():
    import dataclasses

    spec = TodoAddon().extension()
    assert validate_extension_spec("todo", dataclasses.replace(spec, badge=None)) == []
    for bad in ("https://evil.example/badge", "/extensions/todo/badge", ""):
        problems = validate_extension_spec("todo", dataclasses.replace(spec, badge=bad))
        assert any("badge" in p for p in problems), bad
