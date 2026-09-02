"""Unit tests for :mod:`backend.web.core.workspaces`.

The path-safety helpers now live standalone, so lock them directly — especially
``_remove_worktree_path``, the guarded permanent-deletion path that must refuse
anything outside the managed workspace roots.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import time
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


# --------------------------------------------------------------------------- #
# _worktree_gitdir / _worktree_repo_path — the "never a repo" discriminator
# --------------------------------------------------------------------------- #
def test_gitdir_empty_for_a_real_repo(tmp_path):
    """A clone's ``.git`` is a DIRECTORY — the sweep must see nothing here."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert ws._worktree_gitdir(str(repo)) == ""


def test_gitdir_empty_for_a_plain_folder(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    assert ws._worktree_gitdir(str(plain)) == ""


def test_gitdir_reads_a_linked_worktree_pointer(tmp_path):
    leaf = tmp_path / "leaf_abc"
    leaf.mkdir()
    admin = tmp_path / "repo" / ".git" / "worktrees" / "leaf_abc"
    admin.mkdir(parents=True)
    (leaf / ".git").write_text("gitdir: %s\n" % admin)
    # git writes this back-pointer for a linked worktree, and only for one.
    (admin / "gitdir").write_text("%s\n" % (leaf / ".git"))
    assert ws._worktree_gitdir(str(leaf)) == str(admin)


def test_gitdir_ignores_a_git_file_that_is_not_a_pointer(tmp_path):
    leaf = tmp_path / "leaf_abc"
    leaf.mkdir()
    (leaf / ".git").write_text("this is not a worktree\n")
    assert ws._worktree_gitdir(str(leaf)) == ""


def test_gitdir_refuses_a_separate_git_dir_repository(tmp_path):
    """`git init --separate-git-dir` makes a REAL repo whose .git is a file.

    So is a submodule checkout. Both would pass a bare "starts with gitdir:"
    test, and both are working trees somebody would be furious to lose — which
    is why the predicate also demands the pointer run through the repo's
    `worktrees/` dir and carry git's back-pointer.
    """
    repo = tmp_path / "sepdir_repo"
    elsewhere = tmp_path / "elsewhere.git"
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(elsewhere), str(repo)],
        check=True,
    )
    assert (repo / ".git").is_file()  # the shape the naive test would accept
    assert ws._worktree_gitdir(str(repo)) == ""


def test_gitdir_refuses_a_submodule_style_pointer(tmp_path):
    leaf = tmp_path / "sub"
    leaf.mkdir()
    admin = tmp_path / "parent" / ".git" / "modules" / "sub"
    admin.mkdir(parents=True)
    (leaf / ".git").write_text("gitdir: %s\n" % admin)
    (admin / "gitdir").write_text("%s\n" % (leaf / ".git"))
    assert ws._worktree_gitdir(str(leaf)) == ""


@pytest.mark.parametrize(
    "gitdir,expected",
    [
        ("/r/.git/worktrees/leaf", "/r"),
        ("/r/repo.git/worktrees/leaf", "/r/repo.git"),  # bare repo
        ("/r/.git", ""),  # not a worktree pointer at all
        ("", ""),
    ],
)
def test_worktree_repo_path(gitdir, expected):
    assert ws._worktree_repo_path(gitdir) == expected


# --------------------------------------------------------------------------- #
# The merged Recently-closed page + the unused-worktree sweep
# --------------------------------------------------------------------------- #
def _git_q(*args: str, cwd: str) -> None:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout


def _iso_days_ago(days: float) -> str:
    """A closed_at stamp `days` in the past, in the store's own format."""
    when = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)
    return when.isoformat()


def _age(path: str, days: float, only: str = "all") -> None:
    """Backdate a worktree by ``days``.

    ``only`` picks which signal moves — ``"dir"`` the checkout's own mtime,
    ``"git"`` the linked worktree's admin stamps in the base repo, ``"all"``
    both. The knob matters: ``_last_used`` exists BECAUSE those two disagree, so
    a test that always moves them together cannot see the aggregation at all.
    """
    when = time.time() - days * 86400.0
    targets = [] if only == "git" else [path]
    gitdir = ws._worktree_gitdir(path)
    if gitdir and only != "dir":
        targets += [os.path.join(gitdir, n) for n in ("index", "HEAD", "logs/HEAD", "")]
    for t in targets:
        if t and os.path.exists(t):
            os.utime(t, (when, when))


@pytest.fixture()
def wt_world(tmp_path, monkeypatch):
    """A real base repo plus a tmp worktrees root, wired as the managed roots.

    Real ``git worktree add`` output, not a fabricated ``.git`` file: the sweep
    reads the gitdir pointer, runs ``git status`` in the leaf and prunes the
    registration afterwards, and a fake would quietly bypass all three.
    """
    repo = tmp_path / "base"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("one\n")
    _git_q("add", "-A", cwd=str(repo))
    _git_q("commit", "-q", "-m", "init", cwd=str(repo))

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    flat = tmp_path / "workspaces"  # the provisioning root: clones live here
    flat.mkdir()
    real_root = os.path.realpath(str(wt_root))
    monkeypatch.setattr(ws, "_worktrees_root", lambda: real_root)
    monkeypatch.setattr(
        server,
        "_workspace_roots",
        lambda: [os.path.realpath(str(flat)), real_root],
    )
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    monkeypatch.setattr(ws, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(ws, "_remove_trust_entry", lambda p: None)
    monkeypatch.setattr(server.ENGINE, "instances", {}, raising=False)
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    monkeypatch.setattr(server, "_save_recently_closed", lambda items: None)

    def add(branch: str, sub: str = "") -> str:
        """A real linked worktree, nested under branch slugs like the engine's."""
        leaf = str(wt_root / (sub or branch))
        _git_q("worktree", "add", "-q", "-b", branch, leaf, cwd=str(repo))
        return leaf

    def add_detached(sub: str) -> str:
        """A detached checkout — the shape MindFlock's verify worktrees have."""
        leaf = str(wt_root / sub)
        _git_q("worktree", "add", "-q", "--detach", leaf, "HEAD", cwd=str(repo))
        return leaf

    return SimpleNamespace(
        repo=repo, root=wt_root, flat=flat, add=add, add_detached=add_detached
    )


def test_stale_linked_worktree_is_swept_and_the_repo_is_pruned(wt_world):
    leaf = wt_world.add("feature/old", "feature/old_a1")
    _age(leaf, 30)
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == ["feature/old_a1"]
    assert not os.path.isdir(leaf)
    # The registration went with it, so `git worktree add` for this branch can
    # succeed again (the failure worktree_reclaim exists to clean up).
    listed = subprocess.run(
        ["git", "-C", str(wt_world.repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert leaf not in listed
    # ...and the empty branch-slug directory it was nested in is collected too.
    assert not os.path.isdir(str(wt_world.root / "feature"))


def test_sweep_never_touches_a_real_repo_under_the_worktrees_root(wt_world):
    """THE hard rule: a clone parked in the worktrees dir is not a worktree.

    ``_find_worktrees`` cannot tell the two apart (it accepts ``.git`` as a file
    OR a directory), so this is the case the gitdir test exists for.
    """
    parked = wt_world.root / "my-own-repo"
    parked.mkdir()
    subprocess.run(["git", "init", "-q", str(parked)], check=True)
    (parked / "precious.txt").write_text("do not delete")
    _age(str(parked), 400)

    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept"]["not_worktree"] == 1
    assert (parked / "precious.txt").read_text() == "do not delete"


def test_sweep_keeps_a_recently_used_worktree(wt_world):
    fresh = wt_world.add("feature/new", "feature/new_b2")
    _age(fresh, 2)
    body = ws.prune_stale_worktrees(days=7, dry_run=True)
    assert body["candidates"] == []
    assert body["kept"]["recent"] == 1
    assert os.path.isdir(fresh)


def test_sweep_keeps_a_worktree_a_live_session_owns(wt_world, monkeypatch):
    live = wt_world.add("feature/live", "feature/live_c3")
    _age(live, 90)
    inst = SimpleNamespace(Title="holder", GetWorktreePath=lambda: live)
    monkeypatch.setattr(server.ENGINE, "instances", {"holder": inst}, raising=False)
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept"]["active"] == ["holder"]
    assert os.path.isdir(live)


def test_sweep_holds_back_uncommitted_work_until_asked(wt_world):
    dirty = wt_world.add("feature/dirty", "feature/dirty_d4")
    (open(os.path.join(dirty, "scratch.txt"), "w")).write("unsaved thinking\n")
    _age(dirty, 60)

    preview = ws.prune_stale_worktrees(days=7, dry_run=True)
    assert preview["dirty_count"] == 1
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept_dirty"] == ["feature/dirty_d4"]
    assert os.path.isdir(dirty)
    # ...and it goes when the second confirmation says so.
    body = ws.prune_stale_worktrees(days=7, dry_run=False, include_dirty=True)
    assert body["removed"] == ["feature/dirty_d4"]
    assert not os.path.isdir(dirty)


def test_dry_run_deletes_nothing(wt_world):
    leaf = wt_world.add("feature/preview", "feature/preview_e5")
    _age(leaf, 99)
    body = ws.prune_stale_worktrees(days=7, dry_run=True)
    assert [c["name"] for c in body["candidates"]] == ["feature/preview_e5"]
    assert body["candidate_count"] == 1
    assert "removed" not in body
    assert os.path.isdir(leaf)


def test_sweep_forgets_the_closed_entry_whose_worktree_it_removed(
    wt_world, monkeypatch
):
    leaf = wt_world.add("feature/closed", "feature/closed_f6")
    _age(leaf, 20)
    entries = [
        {
            "id": "sess-1",
            "title": "sess",
            "branch": "feature/closed",
            "folder": leaf,
            "closed_at": "2026-01-01T00:00:00+00:00",
        },
        {"id": "other", "title": "keep-me", "folder": str(wt_world.root / "nope")},
    ]
    saved: list = []
    monkeypatch.setattr(server, "_load_recently_closed", lambda: list(entries))
    monkeypatch.setattr(
        server, "_save_recently_closed", lambda items: saved.append(items)
    )

    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == ["feature/closed_f6"]
    assert body["forgot"] == 1
    # The entry for the deleted worktree is gone; the unrelated one survives.
    assert [e["id"] for e in saved[-1]] == ["other"]


def test_recent_rows_merges_closed_and_disk_and_hides_protected(wt_world, monkeypatch):
    stale = wt_world.add("feature/stale", "feature/stale_g7")
    _age(stale, 40)
    fresh = wt_world.add("feature/fresh", "feature/fresh_h8")
    _age(fresh, 1)
    # A protected base clone: counted, never a row. A pr-* review clone in the
    # same root IS a row — it is disk the user can reclaim, just never by the
    # worktree sweep.
    (wt_world.flat / "_base_repo").mkdir()
    (wt_world.flat / "pr-9").mkdir()
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "c1",
                "title": "sess",
                "branch": "feature/stale",
                "folder": stale,
                "closed_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    data = ws.recent_rows(days=7)
    rows = {r["name"]: r for r in data["rows"]}
    # The closed session's worktree is ONE row (not one per surface), carrying
    # both identities; the other worktree shows up as an on-disk row.
    assert rows["feature/stale_g7"]["source"] == "closed"
    assert rows["feature/stale_g7"]["title"] == "sess"
    assert rows["feature/stale_g7"]["stale"] is True
    assert rows["feature/fresh_h8"]["source"] == "disk"
    assert rows["feature/fresh_h8"]["stale"] is False
    assert rows["pr-9"]["source"] == "disk"
    assert rows["pr-9"]["worktree"] is False  # a clone: the sweep can't take it
    assert "_base_repo" not in rows
    assert data["hidden"]["protected"] == 1
    assert data["hidden"]["protected_names"] == ["_base_repo"]


def test_recent_rows_hides_what_a_live_session_is_using(wt_world, monkeypatch):
    live = wt_world.add("feature/busy", "feature/busy_i9")
    inst = SimpleNamespace(Title="busy", GetWorktreePath=lambda: live)
    monkeypatch.setattr(server.ENGINE, "instances", {"busy": inst}, raising=False)
    data = ws.recent_rows(days=7)
    assert data["rows"] == []
    assert data["hidden"]["active_titles"] == ["busy"]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_recent_route_shape(wt_world):
    wt_world.add("feature/r", "feature/r_j0")
    body = json.loads(asyncio.run(server.recent_list()).body)
    assert body["stale_days"] == ws.STALE_WORKTREE_DAYS
    assert [r["name"] for r in body["rows"]] == ["feature/r_j0"]
    assert body["rows"][0]["size_bytes"] is None  # the no-du fast path


def test_prune_route_defaults_to_a_dry_run(wt_world):
    leaf = wt_world.add("feature/dry", "feature/dry_k1")
    _age(leaf, 30)
    body = json.loads(asyncio.run(server.prune_worktrees({})).body)
    assert body["dry_run"] is True
    assert body["candidate_count"] == 1
    assert os.path.isdir(leaf)


def test_prune_route_rejects_a_bad_window(wt_world):
    for payload in ({"days": "soon"}, {"days": -1}):
        resp = asyncio.run(server.prune_worktrees(payload))
        assert resp.status_code == 400


def test_delete_refuses_a_path_inside_a_workspace(monkeypatch, tmp_path):
    """The "direct child" rule the endpoint's docstring always claimed: a deep
    path used to pass every guard, and the base-clone protection tests only the
    basename — so `<root>/_base_repo/src` deleted a subtree inside a protected
    clone."""
    managed = tmp_path / "ws"
    inside = managed / "_base_repo" / "src"
    inside.mkdir(parents=True)
    (inside / "keep.txt").write_text("do not delete")
    monkeypatch.setattr(
        server, "_workspace_roots", lambda: [os.path.realpath(str(managed))]
    )
    resp = asyncio.run(server.delete_workspace({"path": str(inside)}))
    assert resp.status_code == 400
    assert (inside / "keep.txt").exists()


def test_sweep_takes_a_clean_detached_worktree(wt_world):
    """A detached HEAD is not by itself work at risk.

    MindFlock's own verify worktrees are detached checkouts, and they are the
    most disposable directories on the machine — if "detached" alone counted as
    unrecoverable, they would be the ones the sweep could never take.
    """
    leaf = wt_world.add_detached("emandel2630/verify-x_a1")
    _age(leaf, 30)
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["dirty_count"] == 0
    assert body["removed"] == ["emandel2630/verify-x_a1"]


def test_sweep_holds_back_a_detached_commit_no_branch_points_at(wt_world):
    """...but a commit made on a detached HEAD, which no ref contains, IS at
    risk: nothing but this directory knows about it."""
    leaf = wt_world.add_detached("emandel2630/verify-y_b2")
    (open(os.path.join(leaf, "orphan.txt"), "w")).write("only here\n")
    _git_q("add", "-A", cwd=leaf)
    _git_q("commit", "-q", "-m", "detached work", cwd=leaf)
    _age(leaf, 30)
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["dirty_count"] == 1
    assert body["removed"] == []
    assert os.path.isdir(leaf)


def test_wipe_refuses_a_worktree_a_live_session_still_shares(wt_world, monkeypatch):
    """A copy and its origin share one worktree, so a closed session's wipe can
    be aimed at a directory a RUNNING session is working in."""
    shared = wt_world.add("feature/shared", "feature/shared_c3")
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "c1", "title": "origin", "folder": shared}],
    )
    inst = SimpleNamespace(Title="copy", GetWorktreePath=lambda: shared)
    monkeypatch.setattr(server.ENGINE, "instances", {"copy": inst}, raising=False)
    resp = asyncio.run(server.forget_recently_closed("c1", {"wipe": True}))
    assert resp.status_code == 409
    assert os.path.isdir(shared)


def test_last_used_takes_the_newest_signal_not_the_directory_mtime(wt_world):
    """A checkout committed in yesterday, whose ROOT has not changed in a month.

    All the edits are two levels down, so the directory's own mtime is a month
    old while the worktree's git index moved yesterday — the exact gap
    _last_used takes a max over. Measured on the author's machine as three days.
    """
    leaf = wt_world.add("feature/deep", "feature/deep_a1")
    _age(leaf, 30, only="dir")
    _age(leaf, 1, only="git")
    body = ws.prune_stale_worktrees(days=7, dry_run=True)
    assert body["candidates"] == []
    assert body["kept"]["recent"] == 1


def test_last_used_is_not_fooled_by_a_touched_root_either(wt_world):
    """The mirror: a root-level write (MindFlock's own scratch files land there)
    must not make a month-idle checkout look used."""
    leaf = wt_world.add("feature/shallow", "feature/shallow_b2")
    _age(leaf, 30)
    assert ws.prune_stale_worktrees(days=7, dry_run=True)["candidate_count"] == 1
    _age(leaf, 0, only="dir")  # a scratch file written at the root, just now
    assert ws.prune_stale_worktrees(days=7, dry_run=True)["candidate_count"] == 0


def test_sweep_refuses_a_mount_point(wt_world, monkeypatch):
    """realpath does not unwind a bind mount, so a repo mounted into the
    worktrees tree would look like a path inside it."""
    leaf = wt_world.add("feature/mounted", "feature/mounted_c3")
    _age(leaf, 30)
    real = os.path.realpath(leaf)
    monkeypatch.setattr(ws.os.path, "ismount", lambda p: os.path.realpath(p) == real)
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept"]["not_worktree"] == 1
    assert os.path.isdir(leaf)


def test_sweep_re_verifies_at_the_moment_of_deletion(wt_world, monkeypatch):
    """The second gate: even handed a resolved target, the deleter re-checks.

    Simulates the case that matters — a directory that is NOT a linked worktree
    reaching _delete_targets (a refactor, a second caller, or a `.git` that
    turned into a directory between the scan and the delete).
    """
    repo = wt_world.root / "someones-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "precious.txt").write_text("do not delete")
    target = {
        "name": "someones-repo",
        "path": str(repo),
        "branch": "",
        "last_used": 0,
        "size_bytes": 0,
        "dirty": False,
        "closed_ids": ["c1"],
        "titles": [],
        "repo_path": "",
    }
    kept = {
        "active": [],
        "recent": 0,
        "not_worktree": 0,
        "outside_root": 0,
        "protected": 0,
    }
    monkeypatch.setattr(
        ws, "stale_worktree_targets", lambda days=7: ([target], dict(kept))
    )
    # include_dirty, so nothing but the re-verification stands between this
    # repository and an rmtree.
    body = ws.prune_stale_worktrees(days=7, dry_run=False, include_dirty=True)
    assert body["removed"] == []
    assert body["failed"] == ["someones-repo"]
    assert (repo / "precious.txt").exists()


def test_sweep_ignores_a_worktree_outside_the_worktrees_root(wt_world, monkeypatch):
    """A worktree the user cut inside their OWN repo is not MindFlock's to take,
    and the page must not badge it as one either."""
    outside = wt_world.flat / "hand-made-wt"
    _git_q(
        "worktree", "add", "-q", "-b", "hand/made", str(outside), cwd=str(wt_world.repo)
    )
    _age(str(outside), 40)
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "c1", "title": "hand", "folder": str(outside)}],
    )
    rows = {r["name"]: r for r in ws.recent_rows(days=7)["rows"]}
    row = rows["hand-made-wt"]
    assert row["worktree"] is True  # it IS a linked worktree...
    assert row["stale"] is False  # ...but not one the sweep can reach
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept"]["outside_root"] == 1
    assert os.path.isdir(outside)


def test_partial_rmtree_is_not_reported_as_removed(wt_world, monkeypatch):
    """rmtree(ignore_errors=True) swallows an EACCES deep inside the tree.

    Reporting that as removed would drop the closed entry that is the only
    handle back to a half-deleted checkout.
    """
    leaf = wt_world.add("feature/stuck", "feature/stuck_d4")
    _age(leaf, 30)
    saved: list = []
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "c1", "title": "stuck", "folder": leaf}],
    )
    monkeypatch.setattr(
        server, "_save_recently_closed", lambda items: saved.append(items)
    )
    monkeypatch.setattr(ws.shutil, "rmtree", lambda p, **kw: None)  # nothing goes
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["failed"] == ["feature/stuck_d4"]
    assert body["forgot"] == 0
    assert saved == []  # the Reopen handle is still there
    assert os.path.isdir(leaf)


def test_empty_dir_prune_never_reaches_inside_a_worktree(wt_world):
    """Only the branch-slug scaffolding, never a checkout's own empty dirs.

    A running agent's tooling makes them (logs/, dist/, a .venv skeleton), git
    tracks none of them, and nothing would put them back.
    """
    live = wt_world.add("feature/live", "feature/live_e5")
    (pathlib.Path(live) / "logs").mkdir()
    (pathlib.Path(live) / ".venv" / "lib" / "python3.12").mkdir(parents=True)
    scaffold = wt_world.root / "feature" / "left-behind"
    scaffold.mkdir(parents=True)

    removed = ws._prune_empty_worktree_dirs()

    assert (pathlib.Path(live) / "logs").is_dir()
    assert (pathlib.Path(live) / ".venv" / "lib" / "python3.12").is_dir()
    assert not scaffold.exists()
    assert removed == 1
    assert wt_world.root.is_dir()  # never the root itself


def test_shared_worktree_is_kept_when_either_session_closed_recently(
    wt_world, monkeypatch
):
    """A session and its copy share one worktree — the case the store's
    folder-AND-title dedupe exists for.

    The origin closed a month ago, the copy an hour ago. Two rows, one
    directory: the directory's age is the NEWEST of them, or the sweep would
    take the checkout out from under work closed an hour ago and forget only one
    of the two entries (leaving the other offering a Reopen that can only 410).
    """
    shared = wt_world.add("feature/shared", "feature/shared_a1")
    _age(shared, 30)
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "origin-1",
                "title": "origin",
                "folder": shared,
                "closed_at": _iso_days_ago(30),
            },
            {
                "id": "copy-1",
                "title": "copy",
                "folder": shared,
                "closed_at": _iso_days_ago(1 / 24.0),
            },
        ],
    )
    rows = ws.recent_rows(days=7)["rows"]
    assert [r["stale"] for r in rows] == [False, False]
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert body["kept"]["recent"] == 1
    assert os.path.isdir(shared)


def test_shared_worktree_that_is_stale_forgets_every_entry_on_it(wt_world, monkeypatch):
    """...and when the directory really is idle, BOTH entries go with it."""
    shared = wt_world.add("feature/shared", "feature/shared_b2")
    _age(shared, 30)
    saved: list = []
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "origin-1",
                "title": "origin",
                "folder": shared,
                "closed_at": _iso_days_ago(30),
            },
            {
                "id": "copy-1",
                "title": "copy",
                "folder": shared,
                "closed_at": _iso_days_ago(28),
            },
        ],
    )
    monkeypatch.setattr(
        server, "_save_recently_closed", lambda items: saved.append(items)
    )
    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == ["feature/shared_b2"]
    assert body["forgot"] == 2
    assert saved[-1] == []  # neither entry is left pointing at a gone directory


def test_kept_only_explains_directories_the_sweep_actually_looks_at(
    wt_world, monkeypatch
):
    """ "Kept: 1 in use by a running session" must not name a clone in the
    provisioning dir — the sweep never considers anything outside its own root,
    so counting those would inflate the explanation."""
    (wt_world.flat / "_base_repo").mkdir()  # protected, outside the sweep's root
    live = wt_world.flat / "feature-x"  # a clone-strategy workspace, ditto
    live.mkdir()
    inst = SimpleNamespace(Title="feature-x", GetWorktreePath=lambda: str(live))
    monkeypatch.setattr(server.ENGINE, "instances", {"feature-x": inst}, raising=False)
    _targets, kept = ws.stale_worktree_targets(days=7)
    assert kept["active"] == []
    assert kept["protected"] == 0
    # ...while the PAGE still reports them, because its subject is the disk.
    hidden = ws.recent_rows(days=7)["hidden"]
    assert hidden["protected"] == 1
    assert hidden["active_titles"] == ["feature-x"]


# --------------------------------------------------------------------------- #
# The staleness window itself, and the "0 days" edge the UI can ask for
# --------------------------------------------------------------------------- #
def test_days_zero_means_everything_unused_not_nothing(wt_world):
    """``days=0`` is a legal window ("take anything not in use right now"), so
    it has to behave as a real zero rather than as an unset value that silently
    falls back to the seven-day default — and it must not divide by anything."""
    leaf = wt_world.add("feature/momentary", "feature/momentary_z0")
    _age(leaf, 0.01)  # ~15 minutes ago: not stale at 7 days, stale at 0
    assert ws.prune_stale_worktrees(days=7, dry_run=True)["candidates"] == []
    body = ws.prune_stale_worktrees(days=0, dry_run=True)
    assert [c["name"] for c in body["candidates"]] == ["feature/momentary_z0"]
    assert body["days"] == 0.0
    assert ws.recent_rows(days=0)["stale_days"] == 0.0


# --------------------------------------------------------------------------- #
# recent_rows: sizes are opt-in, and every row carries a stable identity
# --------------------------------------------------------------------------- #
def test_recent_rows_only_measures_the_disk_when_asked(wt_world, monkeypatch):
    """A ``du`` per row stats every file in the tree, which is why the page
    loads without it. So the default must not shell out at all — and ``?sizes=1``
    must fill BOTH the rows and the protected directories it only counts."""
    leaf = wt_world.add("feature/measured", "feature/measured_s1")
    base = wt_world.root / "_base_measured"
    base.mkdir()
    subprocess.run(["git", "init", "-q", str(base)], check=True)

    measured: list = []

    def _du(path: str) -> int:
        measured.append(path)
        return 4096

    monkeypatch.setattr(server, "_dir_size_bytes", _du)

    quiet = ws.recent_rows(sizes=False, days=7)
    assert measured == []
    assert all(r["size_bytes"] is None for r in quiet["rows"])
    assert quiet["hidden"]["protected_bytes"] == 0

    loud = ws.recent_rows(sizes=True, days=7)
    assert measured  # the du really ran
    assert [r["size_bytes"] for r in loud["rows"] if r["exists"]] == [4096]
    # The protected clone is not a row, but its bytes are still reported: it is
    # exactly the kind of directory the page exists to explain.
    assert loud["hidden"]["protected_names"] == ["_base_measured"]
    assert loud["hidden"]["protected_bytes"] == 4096
    assert leaf  # (the row above)


def test_recent_row_identity_is_the_store_id_or_the_path(wt_world, monkeypatch):
    """Two kinds of row, two id namespaces — and a closed entry always OWNS its
    directory, so a directory that already has a closed row must never turn up a
    second time as a bare disk row."""
    both = wt_world.add("feature/both", "feature/both_i1")
    orphan = wt_world.add("feature/orphan", "feature/orphan_i2")
    gone = str(wt_world.root / "feature" / "vanished_i3")
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "sess-both",
                "title": "both",
                "branch": "feature/both",
                "folder": both,
            },
            {
                "id": "sess-gone",
                "title": "gone",
                "branch": "feature/gone",
                "folder": gone,
            },
        ],
    )
    rows = {r["id"]: r for r in ws.recent_rows(days=7)["rows"]}
    assert rows["sess-both"]["source"] == "closed" and rows["sess-both"]["exists"]
    assert rows["disk:" + orphan]["source"] == "disk"
    assert rows["disk:" + orphan]["title"] is None
    # The closed entry whose directory is gone is still a row (its identity is
    # the session, not the disk) — and it is the only row for that path.
    assert rows["sess-gone"]["exists"] is False
    assert "disk:" + gone not in rows
    # ...and the live directory is claimed, so it has exactly one row.
    assert [p for p in rows if p.startswith("disk:")] == ["disk:" + orphan]


def test_scan_skips_a_root_it_cannot_read_and_a_broken_symlink(
    wt_world, tmp_path, monkeypatch
):
    """A managed root can disappear (an unmounted volume, a deleted provisioning
    dir) — that must cost the caller the one root, never the whole listing. And a
    dangling symlink is not a workspace: ``is_dir`` says so, and classifying it
    would put an undeletable row on the page."""
    missing = tmp_path / "not-there"
    os.symlink(str(tmp_path / "nowhere"), str(wt_world.flat / "dangling"))
    real = wt_world.add("feature/real", "feature/real_r1")
    monkeypatch.setattr(
        server,
        "_workspace_roots",
        lambda: [
            os.path.realpath(str(missing)),
            os.path.realpath(str(wt_world.flat)),
            ws._worktrees_root(),
        ],
    )
    names = {e["path"] for e in ws.scan_workspaces({}, False)}
    assert real in names
    assert str(wt_world.flat / "dangling") not in names


def test_kept_counters_partition_every_directory_the_sweep_saw(wt_world, monkeypatch):
    """The sweep's "removed 1 of 6" has to add up: each directory it looked at
    lands in exactly one bucket, and the buckets plus the candidates account for
    all of them. Without that the explanation can double-count (or quietly lose)
    a directory, which is the one thing a destructive button's preview cannot do.
    """
    stale = wt_world.add("feature/stale", "feature/stale_k1")
    _age(stale, 40)
    fresh = wt_world.add("feature/fresh", "feature/fresh_k2")
    _age(fresh, 1)
    live = wt_world.add("feature/live", "feature/live_k3")
    _age(live, 90)
    parked = wt_world.root / "my-own-repo"
    parked.mkdir()
    subprocess.run(["git", "init", "-q", str(parked)], check=True)
    _age(str(parked), 400)
    protected = wt_world.root / "_base_thing"
    protected.mkdir()
    subprocess.run(["git", "init", "-q", str(protected)], check=True)
    outside = wt_world.flat / "leftover-clone"
    outside.mkdir()

    inst = SimpleNamespace(Title="holder", GetWorktreePath=lambda: live)
    monkeypatch.setattr(server.ENGINE, "instances", {"holder": inst}, raising=False)

    targets, kept = ws.stale_worktree_targets(days=7)
    assert [t["name"] for t in targets] == ["feature/stale_k1"]
    assert kept["recent"] == 1  # fresh
    assert kept["not_worktree"] == 1  # the parked repo
    assert kept["active"] == ["holder"]  # live
    assert kept["protected"] == 1  # _base_thing
    assert kept["outside_root"] == 1  # the provisioning-dir clone

    examined = len(ws.scan_workspaces({}, False))
    assert examined == 6
    assert (
        len(targets)
        + kept["recent"]
        + kept["not_worktree"]
        + len(kept["active"])
        + kept["protected"]
        + kept["outside_root"]
    ) == examined
    assert outside.is_dir() and fresh and stale


# --------------------------------------------------------------------------- #
# _worktree_gitdir: the pointer forms it must resolve, and the ones it refuses
# --------------------------------------------------------------------------- #
def test_gitdir_resolves_a_relative_pointer(wt_world):
    """``git worktree add --relative-paths`` (and git's own
    ``worktree.useRelativePaths``) writes the pointer relative to the leaf. It
    still names a real linked worktree, so it must resolve — the sweep's whole
    safety model hangs off this answering non-empty for exactly these."""
    leaf = wt_world.add("feature/rel", "feature/rel_p1")
    absolute = ws._worktree_gitdir(leaf)
    assert absolute
    rel = os.path.relpath(absolute, leaf)
    pathlib.Path(leaf, ".git").write_text("gitdir: %s\n" % rel)
    assert ws._realpath(ws._worktree_gitdir(leaf)) == ws._realpath(absolute)


def test_gitdir_tolerates_crlf_and_trailing_whitespace(wt_world):
    """The pointer file is written by git, but it is a plain text file a user (or
    a Windows checkout) can round-trip — and a stray ``\\r`` must not make a real
    worktree read as "not a worktree" and stop the sweep from ever taking it."""
    leaf = wt_world.add("feature/crlf", "feature/crlf_p2")
    gitdir = ws._worktree_gitdir(leaf)
    assert gitdir
    pathlib.Path(leaf, ".git").write_bytes(
        ("gitdir: %s   \r\n" % gitdir).encode("utf-8")
    )
    assert ws._worktree_gitdir(leaf) == gitdir


def test_gitdir_refuses_a_pointer_at_a_path_that_does_not_exist(wt_world, tmp_path):
    """No back-pointer to read means no proof, and "no proof" has exactly one
    safe answer. (This is also the shape a pruned-but-not-deleted worktree has.)"""
    leaf = wt_world.root / "feature" / "ghost_p3"
    leaf.mkdir(parents=True)
    (leaf / ".git").write_text(
        "gitdir: %s\n" % (tmp_path / "nowhere" / ".git" / "worktrees" / "ghost")
    )
    assert ws._worktree_gitdir(str(leaf)) == ""
    _age(str(leaf), 90)
    assert ws.prune_stale_worktrees(days=7, dry_run=True)["candidates"] == []
    assert leaf.is_dir()


def test_sweep_refuses_a_worktree_whose_realpath_leaves_the_root(wt_world, tmp_path):
    """A symlink inside the worktrees root pointing at a real worktree somewhere
    else: the leaf is a genuine worktree by every gitdir test, so the ONLY thing
    standing between the sweep and a directory outside its root is that the
    containment check runs on realpaths."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    leaf = str(outside / "escaped_p4")
    _git_q("worktree", "add", "-q", "-b", "escaped", leaf, cwd=str(wt_world.repo))
    _age(leaf, 90)
    os.symlink(leaf, str(wt_world.root / "looks-local"))

    body = ws.prune_stale_worktrees(days=7, dry_run=False)
    assert body["removed"] == []
    assert [c["name"] for c in body["candidates"]] == []
    assert os.path.isdir(leaf)
    # Two independent refusals, and the test pins both. The scan never descends
    # through a symlink at all (``os.walk`` does not follow them), so the leaf is
    # not even a row...
    assert "looks-local" not in {r["name"] for r in ws.recent_rows(days=7)["rows"]}
    # ...and were it ever enumerated, containment is decided on the REALPATH, so
    # the sweep's root check would turn it away rather than resolve it in-root.
    assert not ws._strictly_under(
        ws._realpath(str(wt_world.root / "looks-local")), ws._worktrees_root()
    )


# --------------------------------------------------------------------------- #
# The routes on top: /api/workspaces (delete + list) and the forget endpoint
# --------------------------------------------------------------------------- #
def test_delete_accepts_a_nested_worktree_leaf_and_prunes_its_own_repo(wt_world):
    """The listed-workspace guard must not cost the sweep's main subject: a
    worktree whose branch has slashes in it nests several levels under the root,
    so "a direct child of a managed root" is false for every one of them —
    ``scan_workspaces`` is what makes them listed, and it is what the guard has
    to agree with.

    And the registration is pruned through the worktree's OWN gitdir pointer:
    the old test ("is my parent directory called worktrees") is false for a
    nested leaf, so those registrations were never pruned and later made
    ``git worktree add`` for the same branch fail.
    """
    leaf = wt_world.add("feature/deep/thing", "feature/deep/thing_d1")
    assert os.path.basename(os.path.dirname(leaf)) == "deep"  # NOT "worktrees"

    resp = asyncio.run(server.delete_workspace({"path": leaf}))
    assert resp.status_code == 200
    assert not os.path.isdir(leaf)
    listed = subprocess.run(
        ["git", "-C", str(wt_world.repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert leaf not in listed


def test_list_workspaces_keeps_its_shape_and_its_opt_in_sizes(wt_world, monkeypatch):
    """``/api/workspaces`` still answers the raw disk listing after the scan moved
    into core.workspaces — same envelope, same per-row keys, and ``size_bytes``
    still only measured under ``?sizes=1``."""
    leaf = wt_world.add("feature/listed", "feature/listed_l1")
    measured: list = []
    monkeypatch.setattr(
        server, "_dir_size_bytes", lambda p: (measured.append(p), 512)[1]
    )

    body = json.loads(asyncio.run(server.list_workspaces(0)).body)
    assert set(body) == {"workspaces", "roots"}
    row = next(w for w in body["workspaces"] if w["path"] == leaf)
    assert set(row) >= {
        "name",
        "path",
        "root",
        "kind",
        "size_bytes",
        "mtime",
        "active_session",
        "worktree",
        "gitdir",
        "last_used",
    }
    assert row["kind"] == "worktree" and row["worktree"] is True
    assert row["size_bytes"] is None and measured == []

    sized = json.loads(asyncio.run(server.list_workspaces(1)).body)
    assert (
        next(w for w in sized["workspaces"] if w["path"] == leaf)["size_bytes"] == 512
    )


@pytest.fixture()
def closed_store(tmp_path, monkeypatch):
    """A real recently-closed store file behind the ``_recently_closed_path``
    seam, so the forget endpoint's load/save round trip is the real one."""
    path = tmp_path / "recently_closed.json"
    path.write_text("[]")
    monkeypatch.setattr(server, "_recently_closed_path", lambda: str(path))
    return path


def test_forget_refuses_to_wipe_a_folder_a_live_session_shares(
    tmp_path, monkeypatch, closed_store
):
    """A copy and its origin keep one directory, so "wipe the thing I closed" can
    be aimed at the working directory of a session that is still running. 409,
    and nothing is deleted — not the folder, not even the entry."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "work.txt").write_text("live\n")
    closed_store.write_text(
        json.dumps([{"id": "c1", "title": "copy", "folder": str(shared)}])
    )
    monkeypatch.setattr(server, "_worktree_in_use_by_other", lambda p, t: True)
    removed: list = []
    monkeypatch.setattr(
        server, "_remove_worktree_path", lambda f, r="": removed.append(f)
    )

    resp = asyncio.run(server.forget_recently_closed("c1", {"wipe": True}))
    assert resp.status_code == 409
    assert removed == []
    assert (shared / "work.txt").exists()
    assert [e["id"] for e in json.loads(closed_store.read_text())] == ["c1"]


def test_forget_never_wipes_an_in_place_folder(tmp_path, monkeypatch, closed_store):
    """An in-place session's folder is the user's own checkout — the entry goes,
    the directory never does, whatever the wipe flag says."""
    own = tmp_path / "my-repo"
    own.mkdir()
    closed_store.write_text(
        json.dumps(
            [{"id": "c1", "title": "here", "folder": str(own), "in_place": True}]
        )
    )
    removed: list = []
    monkeypatch.setattr(
        server, "_remove_worktree_path", lambda f, r="": removed.append(f)
    )

    resp = asyncio.run(server.forget_recently_closed("c1", {"wipe": True}))
    assert resp.status_code == 200
    assert removed == []
    assert own.is_dir()
    assert json.loads(closed_store.read_text()) == []


def test_a_forget_in_flight_does_not_resurrect_a_sibling(
    tmp_path, monkeypatch, closed_store
):
    """Deleting several rows at once fires these concurrently, and a wipe holds
    the request open for as long as the ``rmtree`` takes. The snapshot read at
    the top of the handler is stale by then, so writing it back re-added every
    sibling that had gone in the meantime — each one offering a Reopen that could
    only 410."""
    a_dir, b_dir = tmp_path / "wt-a", tmp_path / "wt-b"
    a_dir.mkdir()
    b_dir.mkdir()
    closed_store.write_text(
        json.dumps(
            [
                {"id": "a", "title": "a", "folder": str(a_dir)},
                {"id": "b", "title": "b", "folder": str(b_dir)},
            ]
        )
    )
    monkeypatch.setattr(server, "_worktree_in_use_by_other", lambda p, t: False)
    interleaved: list = []

    def _remove(folder, repo_path=""):
        # b's forget lands while a's wipe is still running — which is exactly
        # when a's own snapshot goes stale.
        if not interleaved:
            interleaved.append(asyncio.run(server.forget_recently_closed("b")))
        return True

    monkeypatch.setattr(server, "_remove_worktree_path", _remove)

    resp = asyncio.run(server.forget_recently_closed("a", {"wipe": True}))
    assert resp.status_code == 200
    assert interleaved and interleaved[0].status_code == 200
    assert json.loads(closed_store.read_text()) == []
