"""Read-only git probes the MCP server runs locally, in the folders the
``/api/instances`` rows report (the server and the agents share a machine).

Every helper returns ``None`` when the folder cannot be inspected (missing,
not a repo, git absent, timeout) so callers can tell "clean/none" from
"don't know" — the delete guard treats "don't know" as "refuse unless forced".
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional

__all__ = [
    "git",
    "head_sha",
    "is_dirty",
    "canonical_repo_root",
    "branch_exists",
    "commits_between",
    "set_runner",
]

_GIT_TIMEOUT_S = 20

#: subprocess.run stand-in (tests swap it to count or fail calls).
_RUN: Callable[..., subprocess.CompletedProcess] = subprocess.run


def set_runner(run: Optional[Callable[..., subprocess.CompletedProcess]]) -> None:
    """Swap the subprocess runner (``None`` restores :func:`subprocess.run`)."""
    global _RUN
    _RUN = run or subprocess.run


def git(folder: str, *args: str) -> Optional[str]:
    """``git -C folder <args>`` stdout (stripped), or None on any failure."""
    if not folder or not os.path.isdir(folder):
        return None
    try:
        cp = _RUN(
            ["git", "-C", folder, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip()


def head_sha(folder: str) -> Optional[str]:
    """Full sha of ``HEAD`` in ``folder`` (None when not inspectable)."""
    out = git(folder, "rev-parse", "--verify", "HEAD")
    return out or None


def is_dirty(folder: str) -> Optional[bool]:
    """True when the worktree has uncommitted changes (staged, unstaged or
    untracked-but-not-ignored); None when not inspectable."""
    out = git(folder, "status", "--porcelain")
    if out is None:
        return None
    return bool(out)


def canonical_repo_root(folder: str) -> Optional[str]:
    """The main checkout a (possibly linked) worktree belongs to: the parent
    of ``--git-common-dir``. For a plain checkout that is the checkout itself.
    None when ``folder`` is not in a git repo or the repo is bare/odd."""
    common = git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    common = os.path.normpath(common)
    if os.path.basename(common) != ".git":
        return None  # bare repo / separate git dir: no checkout to point at
    return os.path.dirname(common)


def branch_exists(folder: str, branch: str) -> Optional[bool]:
    """Whether local branch ``branch`` exists in ``folder``'s repository; None
    when that can't be told (not a repo, git absent, odd name)."""
    if not folder or not os.path.isdir(folder) or not branch or branch.startswith("-"):
        return None
    try:
        cp = _RUN(
            [
                "git",
                "-C",
                folder,
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/" + branch,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    return None


def commits_between(
    folder: str, base: str, tip: str = "HEAD", limit: int = 20
) -> Optional[List[str]]:
    """``git log --oneline base..tip`` in ``folder`` (newest first, at most
    ``limit`` lines); None when it can't be computed (e.g. ``base`` unknown to
    this repository)."""
    out = git(
        folder,
        "log",
        "--oneline",
        "--no-decorate",
        "-n",
        str(int(limit)),
        "%s..%s" % (base, tip),
        "--",
    )
    if out is None:
        return None
    return [line for line in out.splitlines() if line.strip()]
