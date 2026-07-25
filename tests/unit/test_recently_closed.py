"""Unit tests for :mod:`backend.web.core.recently_closed`.

The undo store behind reopen / Ctrl+Z: closing a session stashes its serialized
data so it can be reopened against the same worktree. These tests isolate the
on-disk store via the ``_recently_closed_path`` server seam and drive the
record → load round-trip, dedup, and the newest-N cap with a light fake
instance (no tmux / git / real Instance construction).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import session
from backend.web import server
from backend.web.core import recently_closed as rc


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "recently_closed.json"
    monkeypatch.setattr(server, "_recently_closed_path", lambda: str(path))
    return path


class _FakeData:
    def __init__(self, title):
        self._title = title

    def to_dict(self):
        return {"title": self._title, "status": 999}


def _fake_inst(
    title="win", branch="feat", folder="/tmp/wt", in_place=False, provisioned=False
):
    inst = SimpleNamespace(
        Title=title,
        Branch=branch,
        Path=folder,
        InPlace=in_place,
        Provisioned=provisioned,
    )
    inst.ToInstanceData = lambda: _FakeData(title)
    inst.GetWorktreePath = lambda: folder
    return inst


def test_save_load_round_trip(store):
    assert rc._load_recently_closed() == []  # empty store reads as []
    rc._save_recently_closed([{"title": "a"}, {"title": "b"}])
    items = rc._load_recently_closed()
    assert [i["title"] for i in items] == ["a", "b"]


def test_save_caps_at_the_newest(store):
    many = [{"title": "s%d" % i} for i in range(rc._RECENTLY_CLOSED_CAP + 25)]
    rc._save_recently_closed(many)
    loaded = rc._load_recently_closed()
    assert len(loaded) == rc._RECENTLY_CLOSED_CAP
    # The head (newest-first slice) is retained.
    assert loaded[0]["title"] == "s0"


def test_record_closed_stashes_reopenable_entry(store, tmp_path):
    wt = tmp_path / "wt-a"
    wt.mkdir()
    rc._record_closed(_fake_inst(title="alpha", branch="b1", folder=str(wt)))
    items = rc._load_recently_closed()
    assert len(items) == 1
    entry = items[0]
    assert entry["title"] == "alpha"
    assert entry["branch"] == "b1"
    assert entry["folder"] == str(wt)
    # Reopen should bring it back running, not paused/loading.
    assert entry["data"]["status"] == int(session.Running)


def test_record_closed_dedupes_same_worktree_and_title(store, tmp_path):
    wt = tmp_path / "wt-shared"
    wt.mkdir()
    rc._record_closed(_fake_inst(title="alpha", folder=str(wt)))
    rc._record_closed(_fake_inst(title="alpha", folder=str(wt)))
    titles = [e["title"] for e in rc._load_recently_closed()]
    assert titles.count("alpha") == 1  # newest close wins, no pile-up


def test_record_closed_keeps_distinct_titles_on_shared_worktree(store, tmp_path):
    # A copy and its origin share one worktree; both must stay recoverable so
    # Ctrl+Z can reopen each in turn.
    wt = tmp_path / "wt-copy"
    wt.mkdir()
    rc._record_closed(_fake_inst(title="origin", folder=str(wt)))
    rc._record_closed(_fake_inst(title="copy", folder=str(wt)))
    titles = sorted(e["title"] for e in rc._load_recently_closed())
    assert titles == ["copy", "origin"]
