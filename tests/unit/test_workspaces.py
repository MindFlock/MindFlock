"""Unit tests for :mod:`backend.web.core.workspaces`.

The path-safety helpers now live standalone, so lock them directly — especially
``_remove_worktree_path``, the guarded permanent-deletion path that must refuse
anything outside the managed workspace roots.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from backend.web import server
from backend.web.core import workspaces as ws
from backend.workspace_setup import refresher_dirname


# --------------------------------------------------------------------------- #
# _strictly_under
# --------------------------------------------------------------------------- #
def test_strictly_under_true_for_child(tmp_path):
    root = tmp_path / "root"
    child = root / "a" / "b"
    child.mkdir(parents=True)
    assert ws._strictly_under(str(child), str(root)) is True


def test_strictly_under_rejects_equal_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert ws._strictly_under(str(root), str(root)) is False


def test_strictly_under_rejects_parent_escape(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    # A sibling is not under root even though it shares the tmp prefix.
    assert ws._strictly_under(str(sibling), str(root)) is False


# --------------------------------------------------------------------------- #
# _classify_workspace
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,root,expected",
    [
        ("_base_foo", "/x", "base"),
        (refresher_dirname("testmon"), "/x", "refresher"),
        ("pr-1234", "/x", "pr"),
        ("something.clone-tmp", "/x", "tmp"),
        ("leaf_abc", "/home/u/.mindflock/worktrees", "worktree"),
        ("random-folder", "/x", "workspace"),
    ],
)
def test_classify_workspace(name, root, expected):
    assert ws._classify_workspace(name, root) == expected


# --------------------------------------------------------------------------- #
# _worktree_in_use_by_other
# --------------------------------------------------------------------------- #
def test_worktree_in_use_by_other(monkeypatch, tmp_path):
    wt = tmp_path / "shared"
    wt.mkdir()
    other = SimpleNamespace()
    other.GetWorktreePath = lambda: str(wt)
    monkeypatch.setattr(server.ENGINE, "instances", {"copy": other}, raising=False)
    assert ws._worktree_in_use_by_other(str(wt), exclude_title="origin") is True
    # Excluding the only holder → not in use by anyone *else*.
    assert ws._worktree_in_use_by_other(str(wt), exclude_title="copy") is False
    # A different dir isn't in use.
    assert (
        ws._worktree_in_use_by_other(str(tmp_path / "other"), exclude_title="x")
        is False
    )


def test_worktree_in_use_by_other_blank_path():
    assert ws._worktree_in_use_by_other("", exclude_title="x") is False


# --------------------------------------------------------------------------- #
# _remove_worktree_path — the destructive-operation guard
# --------------------------------------------------------------------------- #
def test_remove_refuses_path_outside_managed_roots(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    monkeypatch.setattr(server, "_workspace_roots", lambda: [str(managed)])

    assert ws._remove_worktree_path(str(outside)) is False
    assert outside.exists()  # untouched — refused because it's not under a root


def test_remove_returns_false_for_missing_dir(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(server, "_workspace_roots", lambda: [str(managed)])
    assert ws._remove_worktree_path(str(managed / "gone")) is False


def test_remove_deletes_dir_under_managed_root(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    leaf = managed / "leaf_abc123"
    leaf.mkdir(parents=True)
    (leaf / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_workspace_roots", lambda: [str(managed)])
    # Neutralise the editor/trust side effects — this test only asserts deletion.
    monkeypatch.setattr(ws, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(ws, "_remove_trust_entry", lambda p: None)

    assert ws._remove_worktree_path(str(leaf)) is True
    assert not leaf.exists()


# --------------------------------------------------------------------------- #
# /api/workspaces/clear — bulk sweep of unprotected, idle workspaces
# --------------------------------------------------------------------------- #
def test_clear_sweeps_unprotected_idle_only(monkeypatch, tmp_path):
    managed = tmp_path / "ws"
    (managed / "plain_ws").mkdir(parents=True)
    (managed / "pr-9").mkdir()
    (managed / "_base_repo").mkdir()  # protected: base clone
    (managed / refresher_dirname("testmon")).mkdir()  # protected: refresher
    active = managed / "live_ws"
    active.mkdir()

    monkeypatch.setattr(server, "_workspace_roots", lambda: [str(managed)])
    # Neutralise editor/trust side effects — the test only asserts deletion.
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    inst = SimpleNamespace(Title="live", GetWorktreePath=lambda: str(active))
    monkeypatch.setattr(server.ENGINE, "instances", {"live": inst}, raising=False)

    body = json.loads(asyncio.run(server.clear_workspaces({})).body)
    assert body["ok"] is True
    assert set(body["removed"]) == {"plain_ws", "pr-9"}
    assert body["removed_count"] == 2
    assert body["kept_active"] == ["live"]
    # Unprotected idle dirs are gone; protected + active dirs stay on disk.
    assert not (managed / "plain_ws").exists()
    assert not (managed / "pr-9").exists()
    assert (managed / "_base_repo").exists()
    assert (managed / refresher_dirname("testmon")).exists()
    assert active.exists()
