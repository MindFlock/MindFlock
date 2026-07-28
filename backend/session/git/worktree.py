"""Port of the Go ``session/git`` package's ``worktree.go``.

Defines the central :class:`GitWorktree` class (one struct in Go, with methods
spread across ``worktree*.go``). Here the per-file method groups are composed as
mixins:

  * :class:`GitWorktreeOpsMixin`    (``worktree_ops.go``)
  * :class:`GitWorktreeGitMixin`    (``worktree_git.go``)
  * :class:`GitWorktreeDiffMixin`   (``diff.go``)
  * :class:`GitWorktreeBranchMixin` (``worktree_branch.go``)

This module owns the constructors (``NewGitWorktree`` etc.), the path resolution
helper, the field getters, and the ``get_worktree_directory`` helper. The
worktree path layout — ``<worktreesDir>/<sanitized>_<lowercase hex unixnano>`` —
matches the Go source byte-for-byte.
"""

from __future__ import annotations

import os
import time
from typing import Tuple

from backend import config, log
from backend.session.git.diff import GitWorktreeDiffMixin
from backend.session.git.util import find_git_repo_root, sanitize_branch_name
from backend.session.git.worktree_branch import GitWorktreeBranchMixin
from backend.session.git.worktree_git import GitWorktreeGitMixin
from backend.session.git.worktree_ops import GitWorktreeOpsMixin

__all__ = [
    "get_worktree_directory",
    "getWorktreeDirectory",
    "GitWorktree",
    "NewGitWorktree",
    "new_git_worktree",
    "NewGitWorktreeFromBranch",
    "new_git_worktree_from_branch",
    "NewGitWorktreeFromStorage",
    "new_git_worktree_from_storage",
    "resolve_worktree_paths",
    "resolveWorktreePaths",
]


def get_worktree_directory() -> str:
    """Return ``<configDir>/worktrees`` (Go's ``getWorktreeDirectory``)."""
    config_dir = config.GetConfigDir()
    return os.path.join(config_dir, "worktrees")


class GitWorktree(
    GitWorktreeOpsMixin,
    GitWorktreeGitMixin,
    GitWorktreeDiffMixin,
    GitWorktreeBranchMixin,
):
    """Manages git worktree operations for a session.

    Mirrors the Go ``GitWorktree`` struct. Fields use the Go field names so the
    mixins can refer to them directly:

      * ``repoPath``         - path to the repository
      * ``worktreePath``     - path to the worktree
      * ``sessionName``      - name of the session
      * ``branchName``       - branch name for the worktree
      * ``baseCommitSHA``    - base commit hash for the worktree
      * ``isExistingBranch`` - True if the branch existed before the session was
        created (when True, the branch is not deleted on cleanup)
    """

    def __init__(
        self,
        repoPath: str = "",
        worktreePath: str = "",
        sessionName: str = "",
        branchName: str = "",
        baseCommitSHA: str = "",
        isExistingBranch: bool = False,
    ) -> None:
        self.repoPath = repoPath
        self.worktreePath = worktreePath
        self.sessionName = sessionName
        self.branchName = branchName
        self.baseCommitSHA = baseCommitSHA
        self.isExistingBranch = isExistingBranch

    # --- Getters ----------------------------------------------------------
    def IsExistingBranch(self) -> bool:
        """Return whether this worktree uses a pre-existing branch."""
        return self.isExistingBranch

    def GetWorktreePath(self) -> str:
        """Return the path to the worktree."""
        return self.worktreePath

    def GetBranchName(self) -> str:
        """Return the name of the branch associated with this worktree."""
        return self.branchName

    def GetRepoPath(self) -> str:
        """Return the path to the repository."""
        return self.repoPath

    def GetRepoName(self) -> str:
        """Return the repository name (last path element of repoPath)."""
        return os.path.basename(self.repoPath)

    def GetBaseCommitSHA(self) -> str:
        """Return the base commit SHA for the worktree."""
        return self.baseCommitSHA

    # snake_case aliases
    is_existing_branch = IsExistingBranch
    get_worktree_path = GetWorktreePath
    get_branch_name = GetBranchName
    get_repo_path = GetRepoPath
    get_repo_name = GetRepoName
    get_base_commit_sha = GetBaseCommitSHA


def new_git_worktree_from_storage(
    repoPath: str,
    worktreePath: str,
    sessionName: str,
    branchName: str,
    baseCommitSHA: str,
    isExistingBranch: bool,
) -> GitWorktree:
    """Reconstruct a ``GitWorktree`` from persisted storage fields."""
    return GitWorktree(
        repoPath=repoPath,
        worktreePath=worktreePath,
        sessionName=sessionName,
        branchName=branchName,
        baseCommitSHA=baseCommitSHA,
        isExistingBranch=isExistingBranch,
    )


def resolve_worktree_paths(repo_path: str, branch_name: str) -> Tuple[str, str]:
    """Resolve the repo root and a unique worktree path for ``branch_name``.

    Returns ``(resolved_repo, worktree_path)``. The worktree path is
    ``<worktreesDir>/<sanitizeBranchName(branch)>_<lowercase hex unixnano>``.
    Falls back to ``repo_path`` (logging) if it can't be made absolute, and
    propagates errors from repo-root / worktree-dir resolution.
    """
    try:
        abs_path = os.path.abspath(repo_path)
    except (OSError, ValueError) as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "git worktree path abs error, falling back to repoPath %s: %s",
                repo_path,
                err,
            )
        abs_path = repo_path

    resolved_repo = find_git_repo_root(abs_path)

    worktree_dir = get_worktree_directory()

    worktree_path = os.path.join(worktree_dir, sanitize_branch_name(branch_name))
    # Suffix = "_" + lowercase hex of time.Now().UnixNano().
    worktree_path = worktree_path + "_" + format(time.time_ns(), "x")

    return resolved_repo, worktree_path


def new_git_worktree(repo_path: str, session_name: str) -> Tuple[GitWorktree, str]:
    """Create a new ``GitWorktree`` instance for a fresh branch.

    Builds the branch name as ``<branch_prefix><session_name>`` (sanitized),
    resolves paths, and returns ``(worktree, branch_name)``.
    """
    cfg = config.LoadConfig()
    branch_name = "{}{}".format(cfg.branch_prefix, session_name)
    # Sanitize the final branch name to handle invalid characters from any
    # source (e.g. backslashes from Windows domain usernames like DOMAIN\user).
    branch_name = sanitize_branch_name(branch_name)

    resolved_repo, worktree_path = resolve_worktree_paths(repo_path, branch_name)

    tree = GitWorktree(
        repoPath=resolved_repo,
        sessionName=session_name,
        branchName=branch_name,
        worktreePath=worktree_path,
    )
    return tree, branch_name


def new_git_worktree_from_branch(
    repo_path: str, branch_name: str, session_name: str
) -> GitWorktree:
    """Create a ``GitWorktree`` that uses an existing branch.

    The branch will not be deleted on cleanup (``isExistingBranch=True``).
    """
    resolved_repo, worktree_path = resolve_worktree_paths(repo_path, branch_name)

    return GitWorktree(
        repoPath=resolved_repo,
        sessionName=session_name,
        branchName=branch_name,
        worktreePath=worktree_path,
        isExistingBranch=True,
    )


# Go-name aliases so the package namespace mirrors the Go package.
getWorktreeDirectory = get_worktree_directory
NewGitWorktree = new_git_worktree
NewGitWorktreeFromBranch = new_git_worktree_from_branch
NewGitWorktreeFromStorage = new_git_worktree_from_storage
resolveWorktreePaths = resolve_worktree_paths
