"""Carry a worktree's uncommitted changes into another worktree of the same repo.

What "Branch session" uses so the new branch starts from exactly what the user
sees in the source session — committed or not — while the source is left
untouched:

* :func:`snapshot_changes` records the source worktree's working tree (tracked
  changes, deletions, and untracked files that ``.gitignore`` does not exclude)
  as one dangling commit whose parent is ``HEAD``. It works on a COPY of the
  index (``GIT_INDEX_FILE``), so the source's index, staged state and files are
  never written. Ignored files stay behind, like every other git operation.
* :func:`apply_snapshot` lays that tree over a fresh worktree of the same repo
  (``git restore --source=<snap> --worktree``): modified files modified,
  deleted files deleted, new files present but untracked. The index stays at
  the new branch's ``HEAD``, so nothing is pre-staged — what was staged in the
  source arrives as an ordinary unstaged change.

The snapshot commit is never referenced by a branch or a reflog; git's normal
garbage collection drops it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from backend import gitutil

__all__ = ["snapshot_changes", "apply_snapshot", "SNAPSHOT_MESSAGE"]

#: The (never shown) message on a snapshot commit.
SNAPSHOT_MESSAGE = "mindflock: uncommitted changes carried into a new branch"

# A big untracked tree is hashed in full: give it room, but never hang forever.
_TIMEOUT = 180.0
_IDENTITY = {
    # commit-tree needs an identity; the snapshot is a dangling object nobody
    # pushes, so a fixed one never leaks into history (and works on a machine
    # with no user.name configured).
    "GIT_AUTHOR_NAME": "MindFlock",
    "GIT_AUTHOR_EMAIL": "mindflock@localhost",
    "GIT_COMMITTER_NAME": "MindFlock",
    "GIT_COMMITTER_EMAIL": "mindflock@localhost",
}


def _out(path: str, *args: str, env=None) -> str:
    """Stripped stdout of ``git -C path args`` — raises ``RuntimeError`` with
    git's own words on a failure."""
    cp = gitutil.git(
        list(args), path, timeout=_TIMEOUT, env=env, stdin=subprocess.DEVNULL
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "git {} failed: {}".format(args[0], (cp.stderr or cp.stdout).strip())
        )
    return cp.stdout.strip()


def snapshot_changes(worktree_path: str) -> str:
    """A commit holding ``worktree_path``'s working tree on top of its HEAD, or
    ``""`` when there is nothing uncommitted to carry.

    Raises ``RuntimeError`` when the folder has no commit to build on or git
    refuses (the caller decides whether to go ahead without the changes)."""
    head = _out(worktree_path, "rev-parse", "--verify", "HEAD^{commit}")
    head_tree = _out(worktree_path, "rev-parse", "HEAD^{tree}")
    real_index = _out(
        worktree_path, "rev-parse", "--path-format=absolute", "--git-path", "index"
    )
    with tempfile.TemporaryDirectory(prefix="mindflock-carry-") as tmp:
        index = os.path.join(tmp, "index")
        env = {"GIT_INDEX_FILE": index}
        # Starting from a copy of the real index keeps its stat cache, so only
        # the files that changed are hashed again; a missing index (nothing
        # ever staged) starts from HEAD.
        try:
            shutil.copyfile(real_index, index)
        except OSError:
            _out(worktree_path, "read-tree", "HEAD", env=env)
        _out(worktree_path, "add", "-A", "--", ":/", env=env)
        tree = _out(worktree_path, "write-tree", env=env)
    if tree == head_tree:
        return ""
    return _out(
        worktree_path,
        "commit-tree",
        "--no-gpg-sign",
        tree,
        "-p",
        head,
        "-m",
        SNAPSHOT_MESSAGE,
        env=_IDENTITY,
    )


def apply_snapshot(worktree_path: str, snapshot: str) -> None:
    """Make ``worktree_path``'s files match ``snapshot`` without touching its
    index or HEAD (see the module docstring). Raises ``RuntimeError``."""
    if not snapshot or snapshot.startswith("-"):
        raise RuntimeError("invalid snapshot: {!r}".format(snapshot))
    _out(
        worktree_path,
        "restore",
        "--source=" + snapshot,
        "--worktree",
        "--",
        ":/",
    )
