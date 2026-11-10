"""MindFlock's diff probes must never write the person's git index.

Regression for 2026-11-10: the diff-stat probe ran ``git add -N .`` in each
session's checkout every few seconds. In a shared checkout (sessions running
in place) that turned the person's new files from ``??`` into `` A``
(intent-to-add) and staged deletions of tracked files as ``D `` — ``add -N``
with a pathspec records removals too. The probes now intent-to-add into a
scratch copy of the index (``GIT_INDEX_FILE``); these tests run them on a
throwaway repo and check the real index is byte-for-byte untouched while the
numbers still count untracked files.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.session.git import diff as diff_mod
from backend.session.git.worktree_git import GitWorktreeGitMixin
from backend.session.storage import Status
from backend.web import server
from backend.web.core import code_map
from backend.web.core import commit_message
from backend.web.core import snapshot


def _git(cwd, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout


def _repo(path: Path) -> Path:
    """A committed repo, then the person's in-progress work: one new
    (untracked) file, one edited tracked file, one deleted tracked file."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    (path / "gone.txt").write_text("bye\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _dirty(path: Path) -> None:
    (path / "new.txt").write_text("n1\nn2\nn3\n")
    (path / "a.txt").write_text("one\ntwo\n")
    (path / "gone.txt").unlink()


def _index_file(wt: Path) -> Path:
    rel = _git(wt, "rev-parse", "--git-path", "index").strip()
    return wt / rel


def _snapshot(wt: Path):
    idx = _index_file(wt)
    st = idx.stat()
    return (
        idx.read_bytes(),
        st.st_mtime_ns,
        _git(wt, "ls-files", "--stage"),
        # --no-optional-locks: the check itself must not refresh the index.
        _git(wt, "--no-optional-locks", "status", "--porcelain"),
    )


def _assert_untouched(wt: Path, before) -> None:
    after = _snapshot(wt)
    assert after[2] == before[2], "index entries changed"
    assert after[0] == before[0], "index bytes changed"
    assert after[1] == before[1], "index file rewritten"
    status = after[3].splitlines()
    assert "?? new.txt" in status  # still untracked, NOT " A" (intent-to-add)
    assert " D gone.txt" in status  # deletion still unstaged, NOT "D "
    assert not any(line.startswith(" A") for line in status), status
    assert not any(line.startswith("D ") for line in status), status


@pytest.fixture()
def private_tmp(tmp_path, monkeypatch):
    """Point tempfile at an empty dir so leftovers from the scratch index show."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


@pytest.fixture(params=["checkout", "linked-worktree"])
def work(request, tmp_path, private_tmp):
    """The person's own checkout, or a ``git worktree add`` of it (index under
    ``.git/worktrees/<name>/``) — both layouts the server probes."""
    src = _repo(tmp_path / "repo")
    if request.param == "checkout":
        wt = src
    else:
        wt = tmp_path / "wt"
        _git(src, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    _git(wt, "status", "--porcelain")  # settle the index's stat cache first
    _dirty(wt)
    _git(wt, "status", "--porcelain")
    return wt


class _Worktree(diff_mod.GitWorktreeDiffMixin, GitWorktreeGitMixin):
    """Just enough of GitWorktree for the diff methods: the real executor."""

    def __init__(self, path: str) -> None:
        self.worktreePath = path


@pytest.fixture(autouse=True)
def _clear_caches():
    diff_mod._DIFF_CACHE.clear()
    server._DIFF_STAT_CACHE.clear()
    code_map._CHANGED_CACHE.clear()
    yield
    diff_mod._DIFF_CACHE.clear()
    server._DIFF_STAT_CACHE.clear()
    code_map._CHANGED_CACHE.clear()


def test_engine_diff_counts_untracked_without_writing_index(work, private_tmp):
    before = _snapshot(work)
    stats = _Worktree(str(work)).Diff()
    assert stats.Error is None
    assert "+++ b/new.txt" in stats.Content  # the untracked file is in the diff
    assert "--- a/gone.txt" in stats.Content
    # +1 (a.txt) +3 (new.txt) / -1 (gone.txt)
    assert (stats.Added, stats.Removed) == (4, 1)
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []  # scratch index cleaned up


def test_engine_numstat_counts_untracked_without_writing_index(work, private_tmp):
    before = _snapshot(work)
    stats = _Worktree(str(work)).DiffNumstat()
    assert stats.Error is None
    assert (stats.Added, stats.Removed) == (4, 1)
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_engine_diff_output_matches_the_old_in_place_add_n(work, tmp_path):
    """Same bytes as the old probe produced: ``add -N .`` then ``diff HEAD``,
    reproduced here on a disposable COPY of the repo."""
    content = _Worktree(str(work)).Diff().Content
    clone = tmp_path / "old-way"
    _git(work, "worktree", "add", "-q", "--detach", str(clone), "HEAD")
    _dirty(clone)
    _git(clone, "add", "-N", ".")
    assert content == _git(clone, "--no-pager", "diff", "HEAD")


class _Inst:
    Status = Status.Running

    def __init__(self, wt: str) -> None:
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def GetGitWorktree(self):  # noqa: N802
        return self


def test_session_diff_stat_counts_untracked_without_writing_index(
    work, private_tmp, monkeypatch
):
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: "HEAD")
    before = _snapshot(work)
    stat = server._session_diff_stat(_Inst(str(work)))
    assert stat == {
        "files": 3,
        "additions": 4,
        "deletions": 1,
        "uncommitted": {"files": 3, "additions": 4, "deletions": 1},
    }
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_session_diff_stat_cache_rearm_survives_untracked_file(work, monkeypatch):
    """The fingerprint no longer shifts under the probe's own ``add -N``, so
    an unchanged tree re-arms the cache instead of recomputing."""
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: "HEAD")
    inst = _Inst(str(work))
    first = server._session_diff_stat(inst)
    exp, val, fp = server._DIFF_STAT_CACHE[str(work)]
    assert fp == snapshot._worktree_fingerprint(str(work), "HEAD")
    server._DIFF_STAT_CACHE[str(work)] = (0.0, val, fp)
    assert server._session_diff_stat(inst) == first
    assert server._DIFF_STAT_CACHE[str(work)][0] > 0.0


def test_scratch_index_env_refuses_a_non_repo(tmp_path, private_tmp):
    """No index to copy must fail loudly, never fall back to an empty index
    (that would report every tracked file as deleted) or to the real one."""
    with pytest.raises(RuntimeError, match="git command failed"):
        with diff_mod.scratch_index_env(str(tmp_path)):
            pass
    assert list(private_tmp.iterdir()) == []


def test_scratch_index_env_ignores_an_inherited_index_file(work, monkeypatch):
    """A stray GIT_INDEX_FILE in the server's env (e.g. leaked from a hook)
    must not redirect the probe — the copy is of THIS worktree's index."""
    monkeypatch.setenv("GIT_INDEX_FILE", os.devnull)
    with diff_mod.scratch_index_env(str(work)) as env:
        assert env["GIT_INDEX_FILE"] != os.devnull
        ls = subprocess.run(
            ["git", "-C", str(work), "ls-files", "--stage"],
            capture_output=True,
            text=True,
            env=env,
        ).stdout
    assert "a.txt" in ls and "new.txt" in ls  # tracked + intent-to-added


# --------------------------------------------------------------------------- #
# The other read paths: Diff tab routes, Code Map change set, commit drafting.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def routed(work, monkeypatch):
    """``work`` served as session "t", its fork point pinned to HEAD."""
    inst = _Inst(str(work))
    monkeypatch.setattr(server, "_inst_or_404", lambda title: (inst, None))
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: "HEAD")
    return inst


def _body(resp) -> dict:
    assert resp.status_code == 200, resp.body
    return json.loads(resp.body)


def test_diff_tab_fork_mode_without_writing_index(work, routed, private_tmp):
    before = _snapshot(work)
    d = _body(asyncio.run(server.instance_diff("t", base="fork")))
    assert d["error"] is None
    assert "+++ b/new.txt" in d["content"]
    assert (d["added"], d["removed"]) == (4, 1)
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_file_diff_of_an_untracked_file_without_writing_index(
    work, routed, private_tmp
):
    before = _snapshot(work)
    d = _body(asyncio.run(server.instance_file_diff("t", path="new.txt")))
    assert d["error"] is None
    assert "+n1\n+n2\n+n3" in d["content"]
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_code_map_changed_files_without_writing_index(work, routed, private_tmp):
    before = _snapshot(work)
    rows = code_map.changed_files(routed, str(work))
    assert rows == [
        {"path": "a.txt", "status": "M", "added": 1, "removed": 0},
        {"path": "gone.txt", "status": "D", "added": 0, "removed": 1},
        {"path": "new.txt", "status": "A", "added": 3, "removed": 0},
    ]
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_commit_message_draft_without_writing_index(work, private_tmp):
    """Drafting a message is a read: the person may cancel and never commit."""
    before = _snapshot(work)
    stat, patch = commit_message.collect_diff(str(work))
    assert "new.txt" in stat and "gone.txt" in stat
    assert "+++ b/new.txt" in patch
    _assert_untouched(work, before)
    assert list(private_tmp.iterdir()) == []


def test_lenient_variant_falls_back_to_plain_diffs(tmp_path, private_tmp):
    """Not a repo: no scratch index, ``None`` — and nothing left behind."""
    with diff_mod.scratch_index_env_or_none(str(tmp_path)) as env:
        assert env is None
    assert list(private_tmp.iterdir()) == []
