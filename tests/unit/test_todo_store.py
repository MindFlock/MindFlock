"""The To-do store (addons/todo/store.py): one todos.json shared with the
Assistant's hand edits, tier order, key dedupe, done_at, history, archive.

Every test points the store at a tmp dir through the Assistant module's
``ASSIST_DIR`` / ``ASSIST_TODOS`` (the store resolves its paths there at call
time) — never the real ``~/.mindflock-assistant``."""

from __future__ import annotations

import json
import time

import pytest

from backend.web.addons import assistant as A
from backend.web.addons.todo import store as S
from backend.web.core import events as E


@pytest.fixture()
def files(tmp_path, monkeypatch):
    d = tmp_path / "assistant"
    d.mkdir()
    monkeypatch.setattr(A, "ASSIST_DIR", d)
    monkeypatch.setattr(A, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setattr(A, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(A, "ASSIST_USER_MD", d / "user_instructions.md")

    class F:
        todos = d / "todos.json"
        history = d / "todos-history.json"

        @staticmethod
        def write(items):
            F.todos.write_text(json.dumps(items), encoding="utf-8")

        @staticmethod
        def read():
            return json.loads(F.todos.read_text(encoding="utf-8"))

        @staticmethod
        def read_history():
            return json.loads(F.history.read_text(encoding="utf-8"))

    return F


@pytest.fixture()
def changed():
    seen = []

    def cb(env):
        if env["event"] == "addon.todo.changed":
            seen.append(env["data"])

    unsub = E.BUS.subscribe(cb)
    yield seen
    unsub()


def _add(**fields):
    added, updated = S.add_items([S.clean_new(dict(fields))])
    return (added or updated)[0]


def test_paths_follow_the_assistant_dir(files):
    assert S.todos_path() == files.todos
    assert S.history_path() == files.history


def test_a_hand_edited_plain_row_reads_back_as_written(files):
    files.write([{"id": "a", "text": "x", "done": False}])
    assert S.list_items() == [{"id": "a", "text": "x", "done": False}]


def test_unknown_fields_survive_a_write(files):
    files.write([{"id": "a", "text": "x", "done": False, "colour": "blue"}])
    _add(text="new one")
    row = files.read()[0]
    assert row["colour"] == "blue"
    assert row["created_at"] > 0  # filled on first sight


def test_add_round_trip_with_every_field(files, changed):
    item = _add(
        text="Check the login page",
        details="Open /login\nPaste: hunter2",
        priority="high",
        kind="manual_test",
        session="sess-a",
        source="agent",
        link="https://example.com",
        due="2026-11-20T09:00",
    )
    assert len(item["id"]) == 8
    got = S.list_items()[0]
    for k in (
        "text",
        "details",
        "priority",
        "kind",
        "session",
        "source",
        "link",
        "due",
    ):
        assert got[k] == item[k]
    assert got["created_at"] > 0 and not got["done"]
    assert changed[-1]["reason"] == "added" and changed[-1]["id"] == item["id"]
    assert changed[-1]["open"] == 1 and changed[-1]["needs_you"] == 1


def test_validation_errors():
    for bad in (
        {},
        {"text": "  "},
        {"text": "x", "priority": "urgent"},
        {"text": "x", "kind": "chore"},
        {"text": "x", "source": "robot"},
        {"text": "x", "due": "next tuesday"},
        {"text": "x", "details": "y" * (S.MAX_DETAILS + 1)},
        {"text": 5},
    ):
        with pytest.raises(ValueError):
            S.clean_new(bad)
    assert S.clean_new({"text": "x", "priority": "normal", "kind": "task"}) == {
        "text": "x",
        "priority": "",
        "kind": "",
    }
    with pytest.raises(ValueError):
        S.clean_patch({"done": "yes"})


def test_tiers_order_open_items_and_priority_moves_to_end_of_tier(files):
    a = _add(text="a")
    b = _add(text="b", priority="high")
    c = _add(text="c", priority="low")
    d = _add(text="d")
    assert [i["text"] for i in S.list_items()] == ["b", "a", "d", "c"]
    # Prioritising d puts it at the END of the high tier.
    e = _add(text="e", priority="high")
    S.patch_item(d["id"], S.clean_patch({"priority": "high"}))
    assert [i["text"] for i in S.list_items()] == ["b", "e", "d", "a", "c"]
    # "normal" clears it: the end of the normal tier.
    S.patch_item(b["id"], S.clean_patch({"priority": "normal"}))
    assert [i["text"] for i in S.list_items()] == ["e", "d", "a", "b", "c"]
    assert "priority" not in S.list_items()[3]
    assert a and c and e


def test_a_hand_written_high_item_is_shown_in_its_tier(files):
    files.write(
        [
            {"id": "a", "text": "a", "done": False},
            {"id": "b", "text": "b", "done": False, "priority": "high"},
        ]
    )
    assert [i["id"] for i in S.list_items()] == ["b", "a"]


def test_order_route_semantics(files, changed):
    ids = {t: _add(text=t)["id"] for t in "abcd"}
    h = _add(text="h", priority="high")
    S.set_order([ids["c"], "nope", ids["a"]])
    # Named first, the rest keep their relative order; tiers still win.
    assert [i["text"] for i in S.list_items()] == ["h", "c", "a", "b", "d"]
    assert changed[-1]["reason"] == "order"
    assert h


def test_key_dedupe_updates_the_open_item(files):
    first = _add(text="Check A", key="verify:p:s1", details="old")
    added, updated = S.add_items(
        [S.clean_new({"text": "Check A again", "key": "verify:p:s1", "details": "new"})]
    )
    assert added == [] and [u["id"] for u in updated] == [first["id"]]
    rows = S.list_items()
    assert len(rows) == 1 and rows[0]["details"] == "new"
    # A DONE item with the key does not swallow a new one.
    S.patch_item(first["id"], {"done": True})
    added, _ = S.add_items([S.clean_new({"text": "Check A", "key": "verify:p:s1"})])
    assert len(added) == 1 and added[0]["id"] != first["id"]


def test_done_stamps_and_reopen_clears_done_at(files, changed):
    item = _add(text="x")
    S.patch_item(item["id"], {"done": True})
    row = S.list_items()[0]
    assert row["done"] and row["done_at"] > 0
    assert changed[-1]["reason"] == "done"
    S.patch_item(item["id"], {"done": False})
    assert "done_at" not in S.list_items()[0]
    assert changed[-1]["reason"] == "reopened"


def test_a_hand_ticked_item_gets_done_at_on_the_next_write(files):
    files.write([{"id": "a", "text": "x", "done": True}])
    _add(text="y")
    assert files.read()[0]["done_at"] > 0


def test_patch_due_resets_reminder_fields(files):
    now = time.time()
    files.write(
        [
            {
                "id": "a",
                "text": "x",
                "done": False,
                "due": "2026-11-10T09:00",
                "snoozed_until": now + 100,
            }
        ]
    )
    S.patch_item("a", S.clean_patch({"text": "y"}))
    assert files.read()[0]["snoozed_until"] == pytest.approx(now + 100)
    S.patch_item("a", S.clean_patch({"due": "2026-11-11T09:00"}))
    assert "snoozed_until" not in files.read()[0]
    S.patch_item("a", S.clean_patch({"due": ""}))
    assert "due" not in files.read()[0]


def test_delete_moves_to_history_restore_brings_it_back(files, changed):
    a = _add(text="a", priority="low", details="keep me")
    _add(text="b", priority="low")
    assert S.delete_item(a["id"]) is True
    assert changed[-1]["reason"] == "removed"
    assert [i["text"] for i in S.list_items()] == ["b"]
    hist = files.read_history()
    assert hist[0]["id"] == a["id"] and hist[0]["outcome"] == "removed"
    assert hist[0]["closed_at"] > 0 and hist[0]["details"] == "keep me"
    assert S.delete_item("nope") is False

    row = S.restore(a["id"])
    assert row["id"] == a["id"] and not row["done"]
    assert "outcome" not in row and "closed_at" not in row
    # At the END of its tier.
    assert [i["text"] for i in S.list_items()] == ["b", "a"]
    assert files.read_history() == []
    assert changed[-1]["reason"] == "restored"
    assert S.restore("nope") is None


def test_done_items_show_in_history_and_restore_reopens_them(files):
    a = _add(text="a")
    S.patch_item(a["id"], {"done": True})
    hist = S.history()
    assert hist[0]["id"] == a["id"] and hist[0]["outcome"] == "done"
    assert hist[0]["closed_at"] == pytest.approx(S.list_items()[0]["done_at"])
    S.restore(a["id"])
    assert S.list_items()[0]["done"] is False
    assert S.history() == []


def test_forget_drops_a_history_entry_or_a_done_item(files):
    a = _add(text="a")
    b = _add(text="b")
    S.delete_item(a["id"])
    assert S.forget(a["id"]) is True
    assert S.history() == []
    S.patch_item(b["id"], {"done": True})
    assert S.forget(b["id"]) is True
    assert S.list_items() == [] and S.history() == []
    c = _add(text="c")
    assert S.forget(c["id"]) is False  # an open item is not history


def test_old_done_items_are_archived(files, changed):
    old = time.time() - S.ARCHIVE_AFTER_S - 60
    files.write(
        [
            {"id": "a", "text": "old", "done": True, "done_at": old},
            {"id": "b", "text": "fresh", "done": True, "done_at": time.time()},
            {"id": "c", "text": "open", "done": False},
        ]
    )
    assert S.archive_now() == 1
    assert [i["id"] for i in S.list_items()] == ["b", "c"]
    hist = files.read_history()
    assert hist[0]["id"] == "a" and hist[0]["outcome"] == "done"
    assert hist[0]["closed_at"] == pytest.approx(old)
    assert changed[-1]["reason"] == "archived"
    # Still listed in history (from the file now).
    assert {h["id"] for h in S.history()} == {"a", "b"}
    assert S.archive_now() == 0


def test_history_search_limit_and_cap(files):
    for t in ("Buy milk", "Call bank", "Milk the cow"):
        S.delete_item(_add(text=t)["id"])
        time.sleep(0.001)
    assert [h["text"] for h in S.history(q="milk")] == ["Milk the cow", "Buy milk"]
    assert len(S.history(limit=1)) == 1
    hist = [
        {
            "id": "h%d" % i,
            "text": "x",
            "done": False,
            "closed_at": i,
            "outcome": "removed",
        }
        for i in range(S.HISTORY_CAP + 5)
    ]
    files.history.write_text(json.dumps(hist), encoding="utf-8")
    S.delete_item(_add(text="one more")["id"])
    assert len(files.read_history()) == S.HISTORY_CAP


def test_badge(files):
    assert S.badge() == {"count": 0, "title": "Nothing to do"}
    _add(text="a")
    _add(text="b", kind="needs_you")
    _add(text="c", kind="manual_test")
    assert S.badge() == {"count": 3, "title": "2 need you · 3 open"}
    assert S.snapshot()["counts"] == {"open": 3, "needs_you": 2, "high": 0}


def test_a_malformed_file_reads_empty_but_is_never_overwritten(files):
    files.todos.write_text("[{broken", encoding="utf-8")
    assert S.list_items() == []
    with pytest.raises(S.StoreError):
        S.add_items([S.clean_new({"text": "x"})])
    assert files.todos.read_text() == "[{broken"


def test_close_items_only_touches_open_items_with_those_keys(files, changed):
    a = _add(text="a", key="k1", source="verify")
    _add(text="b", key="k2", source="verify")
    closed = S.close_items(["k1", "zz"])
    assert [c["id"] for c in closed] == [a["id"]]
    rows = {r["key"]: r for r in S.list_items()}
    assert rows["k1"]["done"] and rows["k1"]["done_at"] > 0
    assert not rows["k2"]["done"]
    assert changed[-1]["reason"] == "done"
    n = len(changed)
    assert S.close_items(["k1"]) == []  # already done: no write, no event
    assert len(changed) == n
