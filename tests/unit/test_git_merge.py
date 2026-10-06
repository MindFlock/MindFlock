"""git_merge: the mechanical merge-back of a piece into the lead's worktree.

Real temp repositories throughout — a lead worktree and piece branches cut
from its commit, exactly the shape a split has — because the module's whole
job is git's behaviour: a clean merge, an already-merged branch, a conflict
that must leave the tree EXACTLY as it was, and the refusals that protect a
tree someone is editing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from backend.web.core import git_merge as gm


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=check, capture_output=True, text=True
    )


def _write(path, rel, text):
    full = os.path.join(str(path), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)


def _commit(path, rel, text, msg):
    _write(path, rel, text)
    _git(path, "add", rel)
    _git(path, "commit", "-q", "-m", msg)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with two files, a lead worktree on ``lead`` and two
    piece branches forked from the lead's commit."""
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(root, *args)
    _commit(root, "auth/tokens.py", "TOKENS = 1\n", "base: tokens")
    _commit(root, "auth/session.py", "SESSION = 1\n", "base: session")
    lead = tmp_path / "lead"
    _git(root, "worktree", "add", "-q", "-b", "lead", str(lead), "main")
    for name in ("tokens", "session"):
        wt = tmp_path / name
        _git(root, "worktree", "add", "-q", "-b", "piece/" + name, str(wt), "lead")
    return {"root": str(root), "lead": str(lead), "tmp": tmp_path}


def _piece(repo, name, rel, text, msg):
    return _commit(repo["tmp"] / name, rel, text, msg)


class TestMergeInto:
    def test_clean_merge_is_a_merge_commit_and_ancestry_proves_it(self, repo):
        head = _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "auth: rotate")
        assert gm.is_ancestor(repo["lead"], head) is False
        res = gm.merge_into(repo["lead"], "piece/tokens", message="Merge tokens")
        assert res["result"] == "clean", res
        assert res["head"] == gm.rev_parse(repo["lead"], "HEAD")
        assert gm.is_ancestor(repo["lead"], head) is True
        # --no-ff: a real merge commit, with the message we gave it
        parents = _git(repo["lead"], "rev-list", "--parents", "-n1", "HEAD").stdout
        assert len(parents.split()) == 3
        assert _git(repo["lead"], "log", "-1", "--format=%s").stdout.strip() == (
            "Merge tokens"
        )

    def test_two_disjoint_pieces_merge_one_after_the_other(self, repo):
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "auth: tokens")
        _piece(repo, "session", "auth/session.py", "SESSION = 2\n", "auth: session")
        assert gm.merge_into(repo["lead"], "piece/tokens")["result"] == "clean"
        assert gm.merge_into(repo["lead"], "piece/session")["result"] == "clean"
        with open(os.path.join(repo["lead"], "auth/session.py")) as f:
            assert f.read() == "SESSION = 2\n"

    def test_an_already_merged_branch_is_up_to_date_not_an_empty_merge(self, repo):
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "auth: tokens")
        gm.merge_into(repo["lead"], "piece/tokens")
        before = gm.rev_parse(repo["lead"], "HEAD")
        res = gm.merge_into(repo["lead"], "piece/tokens")
        assert res["result"] == "up_to_date"
        assert gm.rev_parse(repo["lead"], "HEAD") == before

    def test_a_conflict_is_aborted_and_the_tree_is_exactly_as_it_was(self, repo):
        _piece(repo, "tokens", "auth/session.py", "SESSION = 'tokens'\n", "t")
        _piece(repo, "session", "auth/session.py", "SESSION = 'session'\n", "s")
        assert gm.merge_into(repo["lead"], "piece/tokens")["result"] == "clean"
        before = gm.rev_parse(repo["lead"], "HEAD")
        res = gm.merge_into(repo["lead"], "piece/session")
        assert res["result"] == "conflict"
        assert res["files"] == ["auth/session.py"]
        assert "auth/session.py" in res["error"]
        assert gm.rev_parse(repo["lead"], "HEAD") == before
        assert not gm.merge_in_progress(repo["lead"])
        assert gm.tracked_dirty(repo["lead"]) is False
        with open(os.path.join(repo["lead"], "auth/session.py")) as f:
            assert f.read() == "SESSION = 'tokens'\n"

    def test_refuses_a_dirty_lead_without_touching_it(self, repo):
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "t")
        _write(repo["lead"], "auth/session.py", "SESSION = 'wip'\n")
        before = gm.rev_parse(repo["lead"], "HEAD")
        res = gm.merge_into(repo["lead"], "piece/tokens")
        assert res["result"] == "refused"
        assert "uncommitted" in res["error"]
        assert gm.rev_parse(repo["lead"], "HEAD") == before
        with open(os.path.join(repo["lead"], "auth/session.py")) as f:
            assert f.read() == "SESSION = 'wip'\n"  # the work in progress is kept

    def test_an_untracked_scratch_file_does_not_block_a_merge(self, repo):
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "t")
        _write(repo["lead"], "notes.txt", "scratch\n")
        assert gm.tracked_dirty(repo["lead"]) is False
        assert gm.merge_into(repo["lead"], "piece/tokens")["result"] == "clean"

    def test_an_untracked_file_in_the_way_is_an_error_and_nothing_changes(self, repo):
        _piece(repo, "tokens", "auth/new.py", "NEW = 1\n", "t")
        _write(repo["lead"], "auth/new.py", "mine\n")
        before = gm.rev_parse(repo["lead"], "HEAD")
        res = gm.merge_into(repo["lead"], "piece/tokens")
        assert res["result"] == "error"
        assert res["error"]
        assert gm.rev_parse(repo["lead"], "HEAD") == before
        assert not gm.merge_in_progress(repo["lead"])

    def test_refuses_while_a_merge_is_half_done(self, repo):
        _piece(repo, "tokens", "auth/session.py", "a\n", "t")
        _piece(repo, "session", "auth/session.py", "b\n", "s")
        gm.merge_into(repo["lead"], "piece/tokens")
        _git(repo["lead"], "merge", "--no-ff", "piece/session", check=False)
        assert gm.merge_in_progress(repo["lead"])
        res = gm.merge_into(repo["lead"], "piece/session")
        assert res["result"] == "refused" and "in progress" in res["error"]

    def test_unknown_branch_and_missing_worktree(self, repo, tmp_path):
        assert gm.merge_into(repo["lead"], "piece/nope")["result"] == "error"
        assert (
            gm.merge_into(str(tmp_path / "gone"), "piece/tokens")["result"] == "error"
        )
        assert gm.merge_into(repo["lead"], "")["result"] == "error"

    def test_a_failing_merge_hook_never_blocks_the_mechanical_merge(self, repo):
        hooks = os.path.join(repo["root"], ".git", "hooks")
        for name in ("pre-merge-commit", "commit-msg"):
            path = os.path.join(hooks, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(path, 0o755)
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "t")
        assert gm.merge_into(repo["lead"], "piece/tokens")["result"] == "clean"


class TestReads:
    def test_is_ancestor_is_none_when_it_cannot_tell(self, repo):
        assert gm.is_ancestor(repo["lead"], "0" * 40) is None
        assert gm.is_ancestor(repo["lead"], "") is None
        assert gm.is_ancestor("/nonexistent", "HEAD") is None

    def test_commit_subjects_are_the_pieces_own_oldest_first(self, repo):
        base = gm.rev_parse(repo["lead"], "HEAD")
        _piece(repo, "tokens", "auth/tokens.py", "1\n", "auth: first")
        head = _piece(repo, "tokens", "auth/tokens.py", "2\n", "auth: second")
        assert gm.commit_subjects(repo["lead"], base, head) == [
            "auth: first",
            "auth: second",
        ]
        assert gm.commit_subjects(repo["lead"], "", head) == []

    def test_diff_stat_and_tracked_files(self, repo):
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\nMORE = 1\n", "t")
        gm.merge_into(repo["lead"], "piece/tokens")
        stat = gm.diff_stat(repo["lead"], "main", "HEAD")
        assert stat == {"files": 1, "add": 2, "del": 1}
        assert gm.diff_stat(repo["lead"], "", "HEAD") == {
            "files": 0,
            "add": 0,
            "del": 0,
        }
        assert set(gm.tracked_files(repo["lead"])) == {
            "auth/tokens.py",
            "auth/session.py",
        }

    def test_tracked_dirty_none_outside_a_repo(self, tmp_path):
        assert gm.tracked_dirty(str(tmp_path)) is None


class TestMindFlockArtifacts:
    def test_an_intent_to_added_check_file_is_taken_back_out(self, repo):
        """The diff probe's `git add -N .` in a worktree with no exclude yet
        marks the check's status file; that alone makes git refuse a merge.
        exclude_artifacts takes it back out — and never unstages a file the
        repo really tracks."""
        from backend import workspace_setup

        lead = repo["lead"]
        _piece(repo, "tokens", "auth/tokens.py", "TOKENS = 2\n", "t")
        _write(lead, ".mindflock_check.json", "{}")
        _write(lead, "notes.md", "scratch\n")
        _git(lead, "add", "-N", ".mindflock_check.json", "notes.md")
        assert gm.tracked_dirty(lead) is True
        assert gm.merge_into(lead, "piece/tokens")["result"] == "refused"
        workspace_setup.exclude_artifacts(lead)
        status = _git(lead, "status", "--porcelain").stdout
        assert ".mindflock_check.json" not in status  # excluded, out of the index
        assert " A notes.md" in status  # someone's real file is left alone
        _git(lead, "rm", "--cached", "-q", "notes.md")
        assert gm.merge_into(lead, "piece/tokens")["result"] == "clean"

    def test_a_tracked_artifact_name_is_never_unstaged(self, repo):
        from backend import workspace_setup

        lead = repo["lead"]
        _commit(lead, ".testmondata", "db", "a repo that tracks it")
        workspace_setup.exclude_artifacts(lead)
        assert ".testmondata" in _git(lead, "ls-files").stdout
        assert gm.tracked_dirty(lead) is False
