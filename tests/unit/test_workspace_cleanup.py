"""Unit tests for startup workspace cleanup."""

import time

from backend.ticket_ingestion.workspace_cleanup import prune_stale_workspaces
from backend.workspace_setup import refresher_dirname

REFRESHER_DIRNAME = refresher_dirname("testmon")

_THREE_DAYS = 3 * 24 * 60 * 60


def test_removes_dirs_and_files_older_than_three_days(tmp_path):
    """Both stale folders and stale loose files are removed."""
    stale_dir = tmp_path / "pr-1"
    stale_dir.mkdir()
    (stale_dir / "checkout.txt").write_text("x")
    stale_file = tmp_path / "old.log"
    stale_file.write_text("y")

    # Simulate booting 5 days after these entries were created.
    now = time.time() + 5 * 24 * 60 * 60
    removed = prune_stale_workspaces(tmp_path, now=now)

    assert removed == 2
    assert not stale_dir.exists()
    assert not stale_file.exists()


def test_keeps_entries_within_three_days(tmp_path):
    """Freshly created entries are left untouched."""
    fresh_dir = tmp_path / "pr-2"
    fresh_dir.mkdir()
    fresh_file = tmp_path / "recent.log"
    fresh_file.write_text("z")

    removed = prune_stale_workspaces(tmp_path)

    assert removed == 0
    assert fresh_dir.exists()
    assert fresh_file.exists()


def test_only_removes_entries_past_the_cutoff(tmp_path):
    """An entry just under three days old survives; just over is removed."""
    import os

    keep = tmp_path / "pr-keep"
    keep.mkdir()
    drop = tmp_path / "pr-drop"
    drop.mkdir()

    now = time.time() + 30 * 24 * 60 * 60  # fixed boot reference in the future
    day = 24 * 60 * 60
    os.utime(keep, (now - 2.9 * day, now - 2.9 * day))
    os.utime(drop, (now - 3.1 * day, now - 3.1 * day))

    removed = prune_stale_workspaces(tmp_path, now=now)

    assert removed == 1
    assert keep.exists()
    assert not drop.exists()


def test_preserves_testmon_refresher_workspace(tmp_path):
    """The long-lived testmon refresher workspace is never pruned, even when old."""
    refresher = tmp_path / REFRESHER_DIRNAME
    refresher.mkdir()
    (refresher / ".testmondata").write_text("seed")
    stale = tmp_path / "pr-9"
    stale.mkdir()

    # Boot 10 days later: both entries are well past the cutoff.
    now = time.time() + 10 * 24 * 60 * 60
    removed = prune_stale_workspaces(tmp_path, now=now)

    assert removed == 1
    assert refresher.exists()
    assert (refresher / ".testmondata").exists()
    assert not stale.exists()


def test_missing_workspace_dir_is_a_noop(tmp_path):
    """A non-existent workspace directory returns 0 without raising."""
    assert prune_stale_workspaces(tmp_path / "does-not-exist") == 0


def test_live_tmux_session_workspace_is_kept(tmp_path, monkeypatch):
    """A workspace with a live tmux pane working inside it survives cleanup
    regardless of age (a detached long-running agent must not be rmtree'd)."""
    from backend.ticket_ingestion import workspace_cleanup

    busy = tmp_path / "pr-busy"
    busy.mkdir()
    (busy / "src").mkdir()
    stale = tmp_path / "pr-stale"
    stale.mkdir()

    monkeypatch.setattr(
        workspace_cleanup,
        "_live_session_paths",
        lambda: {str((busy / "src").resolve())},
    )

    now = time.time() + 10 * 24 * 60 * 60
    removed = prune_stale_workspaces(tmp_path, now=now)

    assert removed == 1
    assert busy.exists()
    assert not stale.exists()


def test_nested_edit_counts_as_recent(tmp_path, monkeypatch):
    """Recency comes from the newest file in the tree, not the top-level dir
    mtime (which does not change for nested edits)."""
    import os

    from backend.ticket_ingestion import workspace_cleanup

    monkeypatch.setattr(workspace_cleanup, "_live_session_paths", lambda: set())

    ws = tmp_path / "pr-active"
    nested = ws / "src" / "pkg"
    nested.mkdir(parents=True)
    fresh_file = nested / "edited.py"
    fresh_file.write_text("recent work")

    now = time.time() + 10 * 24 * 60 * 60
    day = 24 * 60 * 60
    old = now - 9 * day
    # Everything looks 9 days old EXCEPT one deeply nested recent edit.
    for p in (ws, ws / "src", nested):
        os.utime(p, (old, old))
    os.utime(fresh_file, (now - 0.5 * day, now - 0.5 * day))

    removed = prune_stale_workspaces(tmp_path, now=now)

    assert removed == 0
    assert fresh_file.exists()


def test_does_not_follow_symlinked_dirs(tmp_path):
    """A stale symlink is unlinked, not recursively deleted through its target."""
    target = tmp_path / "real"
    target.mkdir()
    (target / "keep.txt").write_text("important")

    links = tmp_path / "links"
    links.mkdir()
    link = links / "pr-link"
    link.symlink_to(target)

    now = time.time() + 5 * 24 * 60 * 60
    removed = prune_stale_workspaces(links, now=now)

    assert removed == 1
    assert not link.exists()
    # The symlink target and its contents are untouched.
    assert (target / "keep.txt").read_text() == "important"
