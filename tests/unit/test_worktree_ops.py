"""Hermetic tests for :mod:`backend.session.git.worktree_ops`.

These tests exercise the worktree add / remove / branch lifecycle against a
**real throwaway git repository** created inside pytest's ``tmp_path``. Only
local git is used — no network, no tmux, no claude, and nothing touches the
user's real repo or home directory.

Isolation strategy:
  * ``$HOME`` is monkeypatched to a tmp dir so ``config.GetConfigDir()`` (and
    therefore ``get_worktree_directory()`` = ``$HOME/.mindflock/worktrees``)
    resolves inside tmp_path.
  * ``cleanup_worktrees()`` runs ``git -C <repo>`` (defaulting the repo to the
    process CWD), so those tests ``monkeypatch.chdir`` into the throwaway repo
    or pass it explicitly.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from backend.session.git.worktree import (
    GitWorktree,
    get_worktree_directory,
)
from backend.session.git import worktree_ops
from backend.session.git.worktree_ops import (
    GitWorktreeOpsMixin,
    cleanup_worktrees,
    CleanupWorktrees,
)
from backend.session.git import worktree_git
from backend.session.git.worktree_git import (
    MaxBranchSearchResults,
    search_branches,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _run(cmd, cwd):
    """Run a git command in ``cwd`` and assert success (test-harness only)."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert res.returncode == 0, res.stdout.decode("utf-8", "replace")
    return res.stdout.decode("utf-8", "replace")


def _init_repo(path):
    """Initialise a real git repo with one commit on branch ``main``."""
    os.makedirs(path, exist_ok=True)
    _run(["git", "init", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    with open(os.path.join(path, "README.md"), "w") as fh:
        fh.write("hello\n")
    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-m", "initial"], cwd=path)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real throwaway git repo + isolated $HOME (so worktrees live in tmp)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_path = tmp_path / "repo"
    _init_repo(str(repo_path))
    return str(repo_path)


def _head_sha(repo_path):
    return _run(["git", "rev-parse", "HEAD"], cwd=repo_path).strip()


def _branch_exists(repo_path, branch):
    res = subprocess.run(
        ["git", "-C", repo_path, "show-ref", "--verify", "refs/heads/" + branch],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


def _make_worktree(repo_path, branch, isExistingBranch=False):
    wt_dir = get_worktree_directory()
    wt_path = os.path.join(wt_dir, branch + "_wt")
    return GitWorktree(
        repoPath=repo_path,
        worktreePath=wt_path,
        sessionName="sess",
        branchName=branch,
        isExistingBranch=isExistingBranch,
    )


# ---------------------------------------------------------------------------
# Setup: new worktree from HEAD
# ---------------------------------------------------------------------------
def test_setup_new_worktree_creates_branch_and_dir(repo):
    wt = _make_worktree(repo, "feature-x")
    wt.Setup()

    # Worktree directory exists and is a real checkout.
    assert os.path.isdir(wt.worktreePath)
    assert os.path.exists(os.path.join(wt.worktreePath, ".git"))
    assert os.path.exists(os.path.join(wt.worktreePath, "README.md"))

    # Branch was created and points at HEAD.
    assert _branch_exists(repo, "feature-x")
    assert wt.baseCommitSHA == _head_sha(repo)

    # It is registered as a worktree with git.
    listing = _run(["git", "-C", repo, "worktree", "list"], cwd=repo)
    assert wt.worktreePath in listing


def test_setup_worktrees_directory_created(repo):
    wt_dir = get_worktree_directory()
    # Fresh isolated HOME: directory should not exist yet.
    assert not os.path.exists(wt_dir)
    wt = _make_worktree(repo, "feature-y")
    wt.Setup()
    assert os.path.isdir(wt_dir)


def test_setup_enables_untracked_cache(repo):
    # Setup turns on git's untracked-file cache (perf knob for the diff-stat
    # probe's status/add -N calls) — best-effort, but on a healthy repo it
    # must actually land.
    wt = _make_worktree(repo, "feature-uc")
    wt.Setup()
    out = _run(
        ["git", "-C", wt.worktreePath, "config", "core.untrackedCache"], cwd=repo
    )
    assert out.strip() == "true"


def test_setup_when_branch_already_exists_uses_existing_branch(repo):
    # Pre-create a local branch pointing at a *different* commit.
    _run(["git", "-C", repo, "branch", "existing-1"], cwd=repo)

    wt = _make_worktree(repo, "existing-1")
    # isExistingBranch is False, but Setup detects the local branch via show-ref.
    wt.Setup()

    assert os.path.isdir(wt.worktreePath)
    assert _branch_exists(repo, "existing-1")
    listing = _run(["git", "-C", repo, "worktree", "list"], cwd=repo)
    assert wt.worktreePath in listing
    # setup_from_existing_branch does not populate baseCommitSHA.
    assert wt.baseCommitSHA == ""


def test_setup_existing_branch_flag_true(repo):
    _run(["git", "-C", repo, "branch", "preexist"], cwd=repo)
    wt = _make_worktree(repo, "preexist", isExistingBranch=True)
    wt.Setup()
    assert os.path.isdir(wt.worktreePath)
    assert _branch_exists(repo, "preexist")


def test_setup_existing_branch_flag_true_but_missing_raises(repo):
    wt = _make_worktree(repo, "nope-not-here", isExistingBranch=True)
    with pytest.raises(RuntimeError) as ei:
        wt.Setup()
    assert "not found locally or on remote" in str(ei.value)


# ---------------------------------------------------------------------------
# setup_from_existing_branch specifics
# ---------------------------------------------------------------------------
def test_setup_from_existing_branch_removes_orphaned_dir(repo):
    """An orphaned (unregistered) dir at worktreePath is cleared before add."""
    _run(["git", "-C", repo, "branch", "orphan-branch"], cwd=repo)
    wt = _make_worktree(repo, "orphan-branch")

    # Create a stale directory where the worktree should go.
    os.makedirs(wt.worktreePath, exist_ok=True)
    with open(os.path.join(wt.worktreePath, "stale.txt"), "w") as fh:
        fh.write("stale\n")

    wt.setup_from_existing_branch()

    # Stale file gone, real checkout present.
    assert not os.path.exists(os.path.join(wt.worktreePath, "stale.txt"))
    assert os.path.exists(os.path.join(wt.worktreePath, "README.md"))


def test_setup_from_existing_branch_missing_branch_raises(repo):
    # Ensure worktrees dir exists (Setup normally does this).
    os.makedirs(get_worktree_directory(), exist_ok=True)
    wt = _make_worktree(repo, "ghost")
    with pytest.raises(RuntimeError) as ei:
        wt.setup_from_existing_branch()
    assert "branch ghost not found locally or on remote" == str(ei.value)


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------
def test_remove_deletes_worktree_but_keeps_branch(repo):
    wt = _make_worktree(repo, "keepbranch")
    wt.Setup()
    assert os.path.isdir(wt.worktreePath)

    wt.Remove()

    assert not os.path.isdir(wt.worktreePath)
    # Branch survives Remove().
    assert _branch_exists(repo, "keepbranch")


def test_remove_nonexistent_worktree_raises(repo):
    wt = _make_worktree(repo, "never-created")
    with pytest.raises(RuntimeError) as ei:
        wt.Remove()
    assert "failed to remove worktree" in str(ei.value)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def test_cleanup_removes_worktree_and_branch(repo):
    wt = _make_worktree(repo, "cleanme")
    wt.Setup()
    assert os.path.isdir(wt.worktreePath)
    assert _branch_exists(repo, "cleanme")

    wt.Cleanup()

    assert not os.path.isdir(wt.worktreePath)
    # Non-existing branch => Cleanup deletes it.
    assert not _branch_exists(repo, "cleanme")


def test_cleanup_keeps_branch_when_existing(repo):
    _run(["git", "-C", repo, "branch", "userbranch"], cwd=repo)
    wt = _make_worktree(repo, "userbranch", isExistingBranch=True)
    wt.Setup()
    assert os.path.isdir(wt.worktreePath)

    wt.Cleanup()

    assert not os.path.isdir(wt.worktreePath)
    # isExistingBranch => branch is preserved.
    assert _branch_exists(repo, "userbranch")


def test_cleanup_when_worktree_path_absent_is_noop_for_missing(repo):
    """Cleanup on a never-created worktree removes the branch, no error."""
    # Create just a branch (no worktree), non-existing.
    _run(["git", "-C", repo, "branch", "lonely"], cwd=repo)
    wt = _make_worktree(repo, "lonely", isExistingBranch=False)
    # worktreePath does not exist on disk.
    assert not os.path.exists(wt.worktreePath)

    wt.Cleanup()

    # Branch removed, no raise.
    assert not _branch_exists(repo, "lonely")


def test_cleanup_missing_branch_not_recorded_as_error(repo):
    """A branch that doesn't exist must not surface as a Cleanup error."""
    wt = _make_worktree(repo, "phantom", isExistingBranch=False)
    # Neither worktree nor branch exists.
    assert not os.path.exists(wt.worktreePath)
    assert not _branch_exists(repo, "phantom")
    # Should complete without raising.
    wt.Cleanup()


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------
def test_prune_removes_stale_worktree_refs(repo):
    wt = _make_worktree(repo, "pruneme")
    wt.Setup()

    # Manually delete the worktree dir behind git's back -> stale admin ref.
    import shutil

    shutil.rmtree(wt.worktreePath)

    # Prune should succeed and clear the stale reference.
    wt.Prune()

    listing = _run(["git", "-C", repo, "worktree", "list"], cwd=repo)
    assert wt.worktreePath not in listing


def test_prune_on_clean_repo_succeeds(repo):
    wt = _make_worktree(repo, "irrelevant")
    # No worktree created; prune is still a valid no-op.
    wt.Prune()


# ---------------------------------------------------------------------------
# combine_errors integration (via Cleanup collecting multiple failures)
# ---------------------------------------------------------------------------
def test_cleanup_combines_multiple_errors(repo, monkeypatch):
    """Force two failures and assert combine_errors joins them."""
    wt = _make_worktree(repo, "multi", isExistingBranch=False)
    # Make the worktree path "exist" so removal is attempted and fails.
    os.makedirs(wt.worktreePath, exist_ok=True)

    real = wt.run_git_command

    def boom(path, *args):
        # Fail 'worktree remove', 'branch -D' and 'worktree prune'.
        if args[:2] == ("worktree", "remove"):
            raise RuntimeError("git command failed: boom-remove (exit status 1)")
        if args[:2] == ("branch", "-D"):
            raise RuntimeError("git command failed: boom-branch (exit status 1)")
        if args[:2] == ("worktree", "prune"):
            raise RuntimeError("git command failed: boom-prune (exit status 1)")
        return real(path, *args)

    monkeypatch.setattr(wt, "run_git_command", boom)

    with pytest.raises(RuntimeError) as ei:
        wt.Cleanup()
    msg = str(ei.value)
    assert "multiple errors occurred:" in msg
    assert "boom-remove" in msg
    assert "boom-prune" in msg
    # branch -D error mentions 'failed to remove branch'
    assert "failed to remove branch multi" in msg


# ---------------------------------------------------------------------------
# cleanup_worktrees (free function) — real repo, real worktrees
# ---------------------------------------------------------------------------
def test_cleanup_worktrees_removes_dirs_and_prunes(repo, monkeypatch):
    # Create two managed worktrees.
    wt1 = _make_worktree(repo, "cw-alpha")
    wt2 = _make_worktree(repo, "cw-beta")
    wt1.Setup()
    wt2.Setup()
    assert os.path.isdir(wt1.worktreePath)
    assert os.path.isdir(wt2.worktreePath)
    assert _branch_exists(repo, "cw-alpha")
    assert _branch_exists(repo, "cw-beta")

    # Explicit repo argument: git commands run with `-C <repo>` regardless of
    # the process CWD.
    cleanup_worktrees(repo)

    # Both worktree DIRECTORIES are removed and the stale worktree
    # registrations are pruned away.
    assert not os.path.isdir(wt1.worktreePath)
    assert not os.path.isdir(wt2.worktreePath)
    listing = _run(["git", "-C", repo, "worktree", "list"], cwd=repo)
    assert wt1.worktreePath not in listing
    assert wt2.worktreePath not in listing

    # NOTE ON ACTUAL BEHAVIOR: cleanup_worktrees() runs `git branch -D`
    # *before* pruning, while the worktree is still registered. Git refuses to
    # delete a branch checked out in a (still-registered) worktree, so the
    # delete fails, is only logged, and the branch SURVIVES. We assert the real
    # behavior here rather than the seemingly-intended one. See "concerns".
    assert _branch_exists(repo, "cw-alpha")
    assert _branch_exists(repo, "cw-beta")


def test_cleanup_worktrees_empty_dir(repo, monkeypatch):
    # Ensure the worktrees dir exists but is empty.
    os.makedirs(get_worktree_directory(), exist_ok=True)
    monkeypatch.chdir(repo)
    # Should be a clean no-op that still prunes without error.
    cleanup_worktrees()


def test_cleanup_worktrees_missing_dir_raises(repo, monkeypatch):
    # Worktrees dir was never created.
    assert not os.path.exists(get_worktree_directory())
    monkeypatch.chdir(repo)
    with pytest.raises(RuntimeError) as ei:
        cleanup_worktrees()
    assert "failed to read worktree directory" in str(ei.value)


def test_cleanup_worktrees_alias_is_same_function():
    assert CleanupWorktrees is cleanup_worktrees


def test_cleanup_worktrees_exact_path_match_no_substring_deletes(repo, monkeypatch):
    """A worktree entry must only delete ITS OWN branch.

    Historically the dir→branch mapping used a substring test
    (``entry.name in path``), so a session whose name was a prefix of another
    session's path could delete the *other* session's branch. The porcelain
    listing below is crafted so the colliding entry comes first: the old code
    would attempt ``branch -D other-branch``; the exact-match code must only
    attempt ``branch -D own-branch``.
    """
    wt_dir = get_worktree_directory()
    os.makedirs(os.path.join(wt_dir, "cw-a_wt"), exist_ok=True)

    porcelain = (
        "worktree {0}/cw-a_wt2_wt\nHEAD 0000\nbranch refs/heads/other-branch\n"
        "\n"
        "worktree {0}/cw-a_wt\nHEAD 0000\nbranch refs/heads/own-branch\n"
    ).format(wt_dir)

    deleted = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # Commands now carry a `git -C <repo>` prefix — match on the suffix.
        if cmd[-3:] == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=porcelain.encode(), stderr=b""
            )
        if "-D" in cmd and "branch" in cmd:
            deleted.append(cmd[cmd.index("-D") + 1])
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if cmd[-2:] == ["worktree", "prune"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_ops.subprocess, "run", fake_run)
    monkeypatch.chdir(repo)
    cleanup_worktrees()

    assert deleted == ["own-branch"]


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------
def test_trim_prefix():
    assert worktree_ops._trim_prefix("refs/heads/foo", "refs/heads/") == "foo"
    assert worktree_ops._trim_prefix("foo", "refs/heads/") == "foo"


def test_exit_error_formats():
    assert worktree_ops._exit_error(0) == "exit status 0"
    assert worktree_ops._exit_error(1) == "exit status 1"
    assert worktree_ops._exit_error(-9) == "signal: 9"
    assert worktree_ops._exit_error(None) == "exit status 0"


def test_ops_mixin_snake_case_aliases():
    assert GitWorktreeOpsMixin.setup is GitWorktreeOpsMixin.Setup
    assert GitWorktreeOpsMixin.cleanup is GitWorktreeOpsMixin.Cleanup
    assert GitWorktreeOpsMixin.remove is GitWorktreeOpsMixin.Remove
    assert GitWorktreeOpsMixin.prune is GitWorktreeOpsMixin.Prune


# ---------------------------------------------------------------------------
# search_branches — pure transform over `git branch -a` porcelain output.
# The subprocess is monkeypatched with canned output so the dedup / origin-
# stripping / HEAD-skip / case-insensitive filter / cap rules are pinned
# deterministically (no network, no real refs).
# ---------------------------------------------------------------------------
class _FakeCompleted:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _canned_run(output: str, returncode: int = 0):
    def _run(cmd, *args, **kwargs):
        return _FakeCompleted(output.encode("utf-8"), returncode)

    return _run


def test_search_branches_strips_origin_prefix_and_dedups(monkeypatch):
    out = "origin/foo\nfoo\nmain\n"
    monkeypatch.setattr(worktree_git.subprocess, "run", _canned_run(out))
    # origin/foo and foo collapse to a single first-seen 'foo'.
    assert search_branches("/repo", "") == ["foo", "main"]


def test_search_branches_skips_head_lines(monkeypatch):
    out = "origin/HEAD -> origin/main\nmain\nfeature/x\n"
    monkeypatch.setattr(worktree_git.subprocess, "run", _canned_run(out))
    assert search_branches("/repo", "") == ["main", "feature/x"]


def test_search_branches_filter_is_case_insensitive(monkeypatch):
    out = "feature/Login\nmain\nFEATURE/logout\n"
    monkeypatch.setattr(worktree_git.subprocess, "run", _canned_run(out))
    assert search_branches("/repo", "FEAT") == ["feature/Login", "FEATURE/logout"]


def test_search_branches_caps_at_max_results(monkeypatch):
    out = "\n".join("b{}".format(i) for i in range(60)) + "\n"
    monkeypatch.setattr(worktree_git.subprocess, "run", _canned_run(out))
    result = search_branches("/repo", "")
    assert MaxBranchSearchResults == 50
    assert result == ["b{}".format(i) for i in range(50)]


def test_search_branches_raises_on_git_failure(monkeypatch):
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _canned_run("fatal: bad repo\n", returncode=128),
    )
    with pytest.raises(RuntimeError) as ei:
        search_branches("/repo", "")
    msg = str(ei.value)
    assert "failed to list branches" in msg
    assert "exit status 128" in msg
