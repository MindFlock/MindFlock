"""Reclaiming a leftover worktree so a re-run of the same ticket can proceed.

The bug: ending a session deliberately KEEPS its worktree, so a ticket that ran
once leaves a worktree holding its feature branch. Once the session is gone
nothing blocks the panel's Run ticket button, but ``git worktree add`` still
refuses to check the branch out twice — and the failure was recorded with advice
to delete the ledger entry, which clears the record and leaves the blocker.

These run against real git repos: the whole feature is "what does git think about
this worktree", which a mock cannot answer.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.session.provisioned import worktree_holding_branch
from backend.web.core.worktree_reclaim import (
    reclaim_for_branch,
    worktree_is_pristine,
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path):
    """A base clone with an ``origin`` it can be compared against.

    ``origin`` is a real (bare) remote, because "is this worktree ahead of what
    has been pushed" is the question that decides whether work would be lost.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _git(base, "config", "user.email", "t@t.t")
    _git(base, "config", "user.name", "t")
    (base / "seed.txt").write_text("seed\n")
    _git(base, "add", ".")
    _git(base, "commit", "-m", "seed")
    _git(base, "push", "-u", "origin", "main")
    return base


def _add_worktree(base, branch, tmp_path, name="wt"):
    """A worktree on a new branch, pushed so it has an upstream (the shape a
    ticket session's worktree has once its branch exists on the remote)."""
    path = tmp_path / name
    _git(base, "worktree", "add", "-b", branch, str(path), "main")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    _git(path, "push", "-u", "origin", branch)
    return path


NOTHING_OWNS_IT = lambda path: False  # noqa: E731
SOMETHING_OWNS_IT = lambda path: True  # noqa: E731


class TestWorktreeHoldingBranch:
    def test_finds_the_holder(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        assert worktree_holding_branch(str(repo), "feature/x") == str(path)

    def test_empty_when_nothing_holds_it(self, repo):
        assert worktree_holding_branch(str(repo), "feature/nope") == ""


class TestPristine:
    def test_a_fresh_checkout_is_pristine(self, repo, tmp_path):
        assert worktree_is_pristine(str(_add_worktree(repo, "feature/x", tmp_path)))

    def test_uncommitted_changes_are_work(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "seed.txt").write_text("edited\n")
        assert not worktree_is_pristine(str(path))

    def test_untracked_files_are_work(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "brand-new.txt").write_text("mine\n")
        assert not worktree_is_pristine(str(path))

    def test_unpushed_commits_are_work(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "seed.txt").write_text("committed but not pushed\n")
        _git(path, "commit", "-am", "local work")
        assert not worktree_is_pristine(str(path))

    def test_a_stash_is_work(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "seed.txt").write_text("stashed\n")
        _git(path, "stash")
        assert not worktree_is_pristine(str(path))

    def test_a_branch_with_no_upstream_is_refused(self, repo, tmp_path):
        """Never pushed means nothing to compare against, so there is no way to
        know the commits are safe. Refuse rather than guess."""
        path = tmp_path / "unpushed"
        _git(repo, "worktree", "add", "-b", "feature/local-only", str(path), "main")
        _git(repo, "remote", "set-head", "origin", "--delete")
        assert not worktree_is_pristine(str(path))

    def test_a_nonexistent_path_is_refused(self, tmp_path):
        assert not worktree_is_pristine(str(tmp_path / "gone"))


class TestReclaim:
    def test_reclaims_a_pristine_orphan(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        assert reclaim_for_branch(str(repo), "feature/x", NOTHING_OWNS_IT) == str(path)
        # The branch is free, so a fresh worktree add can have it.
        assert worktree_holding_branch(str(repo), "feature/x") == ""
        _git(repo, "worktree", "add", str(tmp_path / "again"), "feature/x")

    def test_refuses_a_worktree_a_live_session_owns(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        assert reclaim_for_branch(str(repo), "feature/x", SOMETHING_OWNS_IT) == ""
        assert worktree_holding_branch(str(repo), "feature/x") == str(path)
        assert path.is_dir()

    def test_refuses_to_destroy_uncommitted_work(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "precious.txt").write_text("hours of work\n")
        assert reclaim_for_branch(str(repo), "feature/x", NOTHING_OWNS_IT) == ""
        assert (path / "precious.txt").read_text() == "hours of work\n"

    def test_refuses_to_destroy_unpushed_commits(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)
        (path / "seed.txt").write_text("real work\n")
        _git(path, "commit", "-am", "real work")
        assert reclaim_for_branch(str(repo), "feature/x", NOTHING_OWNS_IT) == ""
        assert path.is_dir()

    def test_nothing_to_reclaim_is_not_an_error(self, repo):
        assert (
            reclaim_for_branch(str(repo), "feature/never-existed", NOTHING_OWNS_IT)
            == ""
        )

    def test_an_ownership_probe_that_raises_means_hands_off(self, repo, tmp_path):
        path = _add_worktree(repo, "feature/x", tmp_path)

        def boom(_path):
            raise RuntimeError("engine unreachable")

        assert reclaim_for_branch(str(repo), "feature/x", boom) == ""
        assert path.is_dir()

    def test_a_broken_base_repo_is_not_an_error(self, tmp_path):
        assert (
            reclaim_for_branch(str(tmp_path / "nope"), "feature/x", NOTHING_OWNS_IT)
            == ""
        )


class TestLaunchPathsPreflightTheReclaim:
    """Wiring: both provisioned force-start paths reclaim before launching."""

    def test_ticket_and_issue_routes_call_it(self):
        import inspect

        from backend.web import server

        for fn in (server.ticket_force_start, server.github_issue_force_start):
            src = inspect.getsource(fn)
            assert "_worktree_reclaim.reclaim_for_launch" in src, fn.__name__
