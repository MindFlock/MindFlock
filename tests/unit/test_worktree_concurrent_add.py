"""Workers spawned together from ONE repository must all get their worktree.

Observed on the owner's machine: an orchestrator that called ``spawn_session``
several times in one batch lost some of the workers. Reproduced here with
plain git: two ``git worktree add`` runs in the same repository at the same
moment can fail, because each one reads every OTHER worktree's admin folder
(``.git/worktrees/<name>/``) to check its branch is not checked out there,
and a sibling's folder may be half-written at that instant::

    fatal: failed to read .git/worktrees/orch-w4_…/commondir: Success

So the worktree commands that change a repository's worktree list run one at
a time per repository (:mod:`backend.session.git.repo_lock`).

These run real git against throwaway repos under ``tmp_path`` (``$HOME``
redirected); nothing touches tmux, the live engine or ``~/.mindflock``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from backend.session.git.worktree import GitWorktree


def _run(cmd, cwd):
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert res.returncode == 0, res.stdout.decode("utf-8", "replace")
    return res.stdout.decode("utf-8", "replace")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "t@example.com"], cwd=path)
    _run(["git", "config", "user.name", "T"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    # Enough files that a checkout takes a moment: the window the race needs.
    for i in range(200):
        with open(os.path.join(path, "f%03d.txt" % i), "w") as fh:
            fh.write("x" * 2000)
    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-q", "-m", "files"], cwd=path)
    return path


def _wt(repo_path, tmp_path, name):
    return GitWorktree(
        repoPath=repo_path,
        worktreePath=str(tmp_path / "wts" / name),
        sessionName=name,
        branchName="mf/" + name,
        baseCommitSHA="",
        isExistingBranch=False,
    )


def _setup_all(trees):
    errors = {}

    def go(t):
        try:
            t.Setup()
        except Exception as err:  # noqa: BLE001 — collected, asserted below
            errors[t.sessionName] = str(err)

    threads = [threading.Thread(target=go, args=(t,)) for t in trees]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    return errors


def test_worktree_adds_in_one_repo_never_overlap(repo, tmp_path, monkeypatch):
    """Deterministic: every ``worktree add`` is slowed down so concurrent
    Setups WOULD overlap; at most one may be inside git at a time."""
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    real = GitWorktree.run_git_command

    def slow(self, path, *args, **kw):
        if args[:2] != ("worktree", "add"):
            return real(self, path, *args, **kw)
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            time.sleep(0.05)
            return real(self, path, *args, **kw)
        finally:
            with lock:
                active["now"] -= 1

    monkeypatch.setattr(GitWorktree, "run_git_command", slow)
    trees = [_wt(repo, tmp_path, "orch-w%d" % i) for i in range(6)]
    errors = _setup_all(trees)
    assert errors == {}
    assert active["max"] == 1
    for t in trees:
        assert os.path.isfile(os.path.join(t.worktreePath, "f000.txt"))


def test_a_batch_of_real_worktree_adds_all_succeed(repo, tmp_path):
    """Real git, no slowdown: a burst of Setups from one repository (what a
    batch of ``spawn_session`` calls turns into) all succeed. Before the
    per-repository lock this failed about one Setup in ten on WSL."""
    errors = {}
    for rnd in range(5):
        trees = [_wt(repo, tmp_path, "r%d-w%d" % (rnd, i)) for i in range(10)]
        errors.update(_setup_all(trees))
    assert errors == {}
    listed = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    assert listed.count("branch refs/heads/mf/") == 50


def test_the_lock_is_per_repository(repo, tmp_path):
    """Two different repositories never wait for each other; two paths into
    one repository (its root and one of its worktrees) share one lock."""
    from backend.session.git import repo_lock

    other = str(tmp_path / "other")
    os.makedirs(other)
    _run(["git", "init", "-q", "-b", "main"], cwd=other)
    t = _wt(repo, tmp_path, "solo")
    t.Setup()
    assert repo_lock.lock_key(repo) == repo_lock.lock_key(t.worktreePath)
    assert repo_lock.lock_key(repo) != repo_lock.lock_key(other)
    got = []

    def other_repo():
        with repo_lock.worktree_lock(other):
            got.append(True)

    with repo_lock.worktree_lock(repo):
        th = threading.Thread(target=other_repo)
        th.start()
        th.join(5)
        assert got, "a lock on another repository must not wait"
