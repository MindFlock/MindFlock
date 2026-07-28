"""Port of the Go ``session/git`` package's ``util.go``.

Provides branch-name sanitization, GitHub CLI availability checks, and helpers
to detect/resolve the root of a git repository.

External commands are invoked with their exact Go argv via :mod:`subprocess`
(Go's ``os/exec``). Error and format strings are byte-for-byte identical to the
Go source so the port is drop-in compatible.
"""

from __future__ import annotations

import re
import shutil
import subprocess

__all__ = [
    "sanitize_branch_name",
    "sanitizeBranchName",
    "check_gh_cli",
    "checkGHCLI",
    "IsGitRepo",
    "is_git_repo",
    "find_git_repo_root",
    "findGitRepoRoot",
]

# Remove any characters not in our safe subset: letters, digits, dash,
# underscore, slash, and dot.
_RE_DISALLOWED = re.compile(r"[^a-z0-9\-_/.]+")
# Collapse runs of dashes into a single dash.
_RE_DASH = re.compile(r"-+")


def sanitize_branch_name(s: str) -> str:
    """Transform an arbitrary string into a Git-branch-name-friendly string.

    Mirrors Go's ``sanitizeBranchName``:
      1. lower-case the whole string,
      2. replace spaces with a dash,
      3. drop any character outside ``[a-z0-9\\-_/.]``,
      4. collapse runs of ``-`` into a single ``-``,
      5. trim leading/trailing ``-`` and ``/``.
    """
    # Convert to lower-case.
    s = s.lower()

    # Replace spaces with a dash.
    s = s.replace(" ", "-")

    # Remove any characters not allowed in our safe subset.
    s = _RE_DISALLOWED.sub("", s)

    # Replace multiple dashes with a single dash (optional cleanup).
    s = _RE_DASH.sub("-", s)

    # Trim leading and trailing dashes or slashes to avoid issues.
    s = s.strip("-/")

    return s


def check_gh_cli() -> None:
    """Check that the GitHub CLI is installed and configured.

    Raises ``RuntimeError`` with Go's exact messages:
      * ``GitHub CLI (gh) is not installed. Please install it first``
      * ``GitHub CLI is not configured. Please run 'gh auth login' first``

    Returns ``None`` on success (Go returns ``nil`` error).
    """
    # Check if gh is installed (exec.LookPath equivalent).
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI (gh) is not installed. Please install it first")

    # Check if gh is authenticated: `gh auth status`.
    try:
        cmd = subprocess.run(
            ["gh", "auth", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(
            "GitHub CLI check timed out after 30s (gh auth status)"
        ) from err
    if cmd.returncode != 0:
        raise RuntimeError(
            "GitHub CLI is not configured. Please run 'gh auth login' first"
        )


def is_git_repo(path: str) -> bool:
    """Return whether ``path`` is within a git repository.

    Runs ``git -C <path> rev-parse --show-toplevel`` and reports whether it
    exited cleanly (Go: ``cmd.Run() == nil``).
    """
    try:
        cmd = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return cmd.returncode == 0


def find_git_repo_root(path: str) -> str:
    """Return the git repository root containing ``path``.

    Runs ``git -C <path> rev-parse --show-toplevel`` (capturing stdout only,
    like Go's ``cmd.Output()``) and returns the trimmed output. Raises
    ``RuntimeError`` with the exact message
    ``failed to find Git repository root from path: <path>`` on failure.
    """
    try:
        cmd = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(
            "failed to find Git repository root from path: {}".format(path)
        ) from err
    if cmd.returncode != 0:
        raise RuntimeError(
            "failed to find Git repository root from path: {}".format(path)
        )
    return cmd.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Shared low-level helpers (used by worktree_ops.py and worktree_git.py)
# ---------------------------------------------------------------------------
def _exit_error(returncode: int) -> str:
    """Render a process exit failure the way Go's ``*exec.ExitError`` does.

    Go's ``%w`` of a non-zero exit prints ``exit status N``; a process killed by
    signal N prints ``signal: <name>``. We approximate the common case.
    """
    if returncode is None:
        return "exit status 0"
    if returncode < 0:
        return "signal: {}".format(-returncode)
    return "exit status {}".format(returncode)


def _trim_prefix(s: str, prefix: str) -> str:
    """Mirror Go's ``strings.TrimPrefix``."""
    if s.startswith(prefix):
        return s[len(prefix) :]
    return s


# Go-name aliases so the package namespace mirrors the Go package.
sanitizeBranchName = sanitize_branch_name
checkGHCLI = check_gh_cli
IsGitRepo = is_git_repo
findGitRepoRoot = find_git_repo_root
