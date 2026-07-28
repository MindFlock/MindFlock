"""Cursor auto-adopt tick decisions (server._cursor_autoadopt_tick).

The loop turns every folder open in Cursor into an in-place session. These pin
the per-tick decision: adopt a fresh git repo once, skip one that already has a
session, and — the regression these guard against — do NOT resurrect a folder
whose session was explicitly deleted (tombstoned) while it stays open in Cursor.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.web import server


@pytest.fixture
def clean_seen(monkeypatch):
    """Isolate the module-level _CURSOR_SEEN memo per test."""
    monkeypatch.setattr(server, "_CURSOR_SEEN", set())
    # Every candidate looks like a ready git repo unless a test says otherwise.
    monkeypatch.setattr(server.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(server, "_is_git_repo", lambda p: True)
    monkeypatch.setattr(server, "_git_has_commits", lambda p: True)
    monkeypatch.setattr(server, "_session_for_path", lambda real: None)
    # realpath is identity here (paths are already absolute-ish in the tests).
    monkeypatch.setattr(server.os.path, "realpath", lambda p: p)
    # No managed workspace roots unless a test declares them.
    monkeypatch.setattr(server, "_workspace_roots", lambda: [])
    return server._CURSOR_SEEN


def test_fresh_repo_is_adopted_once(clean_seen):
    plan = server._cursor_autoadopt_tick(["/ws/feature-x"], tombs={})
    assert [p for p, _ in plan] == ["/ws/feature-x"]
    # Loop marks it seen after creating; a second tick (still open) re-plans it
    # only because we didn't add it here — simulate the loop adding it:
    server._CURSOR_SEEN.add("/ws/feature-x")
    assert server._cursor_autoadopt_tick(["/ws/feature-x"], tombs={}) == []


def test_tombstoned_folder_is_not_resurrected(clean_seen):
    """A folder whose title was deleted within the tombstone window is skipped
    (and memoized) even though it's still open in Cursor — the fix for sessions
    that 'keep opening up' after you delete them."""
    tombs = {
        "feature-shortcut-20093-add-to-apk-domains-appksfinder-com": 1_783_300_080.0
    }
    path = "/ws/feature-shortcut-20093-add-to-apk-domains-appksfinder-com"
    plan = server._cursor_autoadopt_tick([path], tombs=tombs)
    assert plan == []  # not adopted
    assert path in server._CURSOR_SEEN  # memoized so we don't spin


def test_folder_with_existing_session_is_skipped(clean_seen, monkeypatch):
    monkeypatch.setattr(server, "_session_for_path", lambda real: object())
    plan = server._cursor_autoadopt_tick(["/ws/already"], tombs={})
    assert plan == []
    assert "/ws/already" in server._CURSOR_SEEN


def test_provisioned_worktree_is_not_adopted(clean_seen, monkeypatch):
    """A folder under a managed workspace root is a MindFlock-provisioned
    worktree that already owns a session — often created by the ingestion
    pipeline in a SEPARATE process, so ``_session_for_path`` (in-memory only)
    returns None. It must still be skipped (and memoized), else a Shortcut ticket
    that opened its provisioned session gets a second, duplicate window.
    """
    monkeypatch.setattr(server, "_workspace_roots", lambda: ["/ws"])
    path = "/ws/feature-shortcut-20377-do-the-thing"
    plan = server._cursor_autoadopt_tick([path], tombs={})
    assert plan == []  # not adopted despite no in-memory session
    assert path in server._CURSOR_SEEN  # memoized so the tick doesn't spin


def test_repo_outside_managed_roots_still_adopted(clean_seen, monkeypatch):
    """The exclusion is scoped to managed roots only: a user's own repo opened in
    Cursor from anywhere else is still adopted as before."""
    monkeypatch.setattr(server, "_workspace_roots", lambda: ["/ws"])
    plan = server._cursor_autoadopt_tick(["/home/u/my-project"], tombs={})
    assert [p for p, _ in plan] == ["/home/u/my-project"]


def test_not_a_git_repo_is_not_adopted(clean_seen, monkeypatch):
    # A folder that isn't a usable git repo yet is skipped and NOT memoized, so
    # it's re-checked next tick (it might become a repo once cloned).
    monkeypatch.setattr(server, "_git_has_commits", lambda p: False)
    plan = server._cursor_autoadopt_tick(["/ws/empty"], tombs={})
    assert plan == []
    assert "/ws/empty" not in server._CURSOR_SEEN


def test_git_probe_exception_skips_folder(clean_seen, monkeypatch):
    # A raising git probe means "not ready" (best-effort), not a crash.
    def boom(p):
        raise RuntimeError("git broke")

    monkeypatch.setattr(server, "_is_git_repo", boom)
    assert server._cursor_autoadopt_tick(["/ws/x"], tombs={}) == []


def test_realpath_error_skips_folder(clean_seen, monkeypatch):
    def boom(p):
        raise OSError("bad path")

    monkeypatch.setattr(server.os.path, "realpath", boom)
    assert server._cursor_autoadopt_tick(["/ws/x"], tombs={}) == []


def test_workspace_roots_error_defaults_to_no_managed_roots(clean_seen, monkeypatch):
    # A config hiccup resolving managed roots must not break adoption: it
    # degrades to "no managed roots" and a fresh repo is still adopted.
    def boom():
        raise RuntimeError("config broke")

    monkeypatch.setattr(server, "_workspace_roots", boom)
    plan = server._cursor_autoadopt_tick(["/home/u/proj"], tombs={})
    assert [p for p, _ in plan] == ["/home/u/proj"]


def test_closed_folder_is_forgotten(clean_seen):
    server._CURSOR_SEEN.update({"/ws/a", "/ws/b"})
    # Only /ws/a is still open this tick -> /ws/b is pruned from the memo so
    # reopening it later re-adopts.
    server._cursor_autoadopt_tick(["/ws/a"], tombs={})
    assert "/ws/b" not in server._CURSOR_SEEN


async def test_failed_start_clears_memo(monkeypatch):
    """A folder whose background start FAILS must be dropped from _CURSOR_SEEN,
    otherwise the loop (which memoizes the path right after creating) would skip
    it on every subsequent tick and never retry while the window stays open."""
    real = "/ws/broken"

    class FakeInst:
        Title = "broken"

        def SetStatus(self, status):
            pass

        def Start(self, first_time_setup):
            raise RuntimeError("boom")

    class FakeEngine:
        def __init__(self):
            import threading

            self.instances = {}
            # Real Engine guards instance mutations with an RLock.
            self.lock = threading.RLock()

        def default_program(self):
            return "claude"

        def save(self):
            pass

    monkeypatch.setattr(server.session, "NewInstance", lambda opts: FakeInst())
    monkeypatch.setattr(server, "_unique_title", lambda base: "broken")
    monkeypatch.setattr(server, "ENGINE", FakeEngine())
    monkeypatch.setattr(server.log, "ErrorLog", None)
    monkeypatch.setattr(server.os.path, "realpath", lambda p: real)
    # The loop memoizes the path immediately after _create_inplace_session
    # returns; simulate that so we can prove the failure path clears it.
    monkeypatch.setattr(server, "_CURSOR_SEEN", {real})

    server._create_inplace_session(real)
    # Let the background _bg_start task run to completion (Start raises).
    for _ in range(50):
        await asyncio.sleep(0.01)
        if real not in server._CURSOR_SEEN:
            break

    assert "broken" not in server.ENGINE.instances  # popped on failure
    assert real not in server._CURSOR_SEEN  # memo cleared -> retried next tick
