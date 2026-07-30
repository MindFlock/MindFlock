"""Port of the Go ``session/git`` package (mindflock/session/git).

Re-exports the public surface of ``util.go``, ``diff.go`` and the
``worktree*.go`` files so the package namespace mirrors the Go package, e.g.::

    from backend.session import git

    if git.is_git_repo(path):
        wt, branch = git.NewGitWorktree(path, "my-session")
        wt.Setup()
        stats = wt.Diff()

``GitWorktree`` is a single class (as in Go) whose methods are composed from the
per-file mixins; free functions live in the module that mirrors their Go file.
"""

from __future__ import annotations

from backend.session.git.diff import (
    DiffStats,
    GitWorktreeDiffMixin,
    parse_numstat,
    parseNumstat,
)
from backend.session.git.remote_url import (
    RemoteRef,
    branch_url,
    compare_url,
    is_local_path,
    parse_remote,
    pr_list_url,
    same_repo,
    to_https,
    to_ssh,
)
from backend.session.git.util import (
    IsGitRepo,
    check_gh_cli,
    checkGHCLI,
    find_git_repo_root,
    findGitRepoRoot,
    gh_available,
    is_git_repo,
    sanitize_branch_name,
    sanitizeBranchName,
)
from backend.session.git.worktree import (
    GitWorktree,
    NewGitWorktree,
    NewGitWorktreeFromBranch,
    NewGitWorktreeFromStorage,
    get_worktree_directory,
    getWorktreeDirectory,
    new_git_worktree,
    new_git_worktree_from_branch,
    new_git_worktree_from_storage,
    resolve_worktree_paths,
    resolveWorktreePaths,
)
from backend.session.git.worktree_branch import GitWorktreeBranchMixin
from backend.session.git.worktree_git import (
    FetchBranches,
    GitWorktreeGitMixin,
    MaxBranchSearchResults,
    SearchBranches,
    fetch_branches,
    search_branches,
)
from backend.session.git.worktree_ops import (
    CleanupWorktrees,
    GitWorktreeOpsMixin,
    cleanup_worktrees,
)

__all__ = [
    # util.go
    "sanitize_branch_name",
    "sanitizeBranchName",
    "check_gh_cli",
    "checkGHCLI",
    "gh_available",
    "IsGitRepo",
    "is_git_repo",
    "find_git_repo_root",
    "findGitRepoRoot",
    # remote_url.py (no Go counterpart — MindFlock-only)
    "RemoteRef",
    "parse_remote",
    "is_local_path",
    "same_repo",
    "branch_url",
    "compare_url",
    "pr_list_url",
    "to_ssh",
    "to_https",
    # diff.go
    "DiffStats",
    "parse_numstat",
    "parseNumstat",
    "GitWorktreeDiffMixin",
    # worktree.go
    "GitWorktree",
    "NewGitWorktree",
    "new_git_worktree",
    "NewGitWorktreeFromBranch",
    "new_git_worktree_from_branch",
    "NewGitWorktreeFromStorage",
    "new_git_worktree_from_storage",
    "get_worktree_directory",
    "getWorktreeDirectory",
    "resolve_worktree_paths",
    "resolveWorktreePaths",
    # worktree_git.go
    "MaxBranchSearchResults",
    "FetchBranches",
    "fetch_branches",
    "SearchBranches",
    "search_branches",
    "GitWorktreeGitMixin",
    # worktree_ops.go
    "CleanupWorktrees",
    "cleanup_worktrees",
    "GitWorktreeOpsMixin",
    # worktree_branch.go
    "GitWorktreeBranchMixin",
]
