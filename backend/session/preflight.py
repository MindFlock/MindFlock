"""Repo-state preflight for session creation — fail loudly, before any mutation.

Session creation forks a git worktree off the target repo's HEAD. Most repo
states are fine for that (a dirty working tree is *by design* not inherited —
the worktree starts from the HEAD commit), but a few states either break the
worktree fork outright or produce a session the user didn't mean to create.
This module classifies them BEFORE anything is written, so the failure is a
clear message with a recovery command instead of a stack trace from deep
inside ``git worktree add`` — and the user's repo is left untouched.

Severities:

* ``block`` — session creation must not proceed; the message says why and the
  one command that fixes it.
* ``warn``  — creation proceeds; the message is surfaced so the user knows
  (e.g. detached HEAD: the session forks the current commit, not a branch).

Every probe is a cheap file-existence check or a single fast git call; the
whole preflight is bounded to well under a second on any real repo.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

__all__ = ["Issue", "repo_issues", "blocking_error"]

_GIT_TIMEOUT_S = 5


@dataclass(frozen=True)
class Issue:
    """One preflight finding. ``fix`` is the exact command that resolves it."""

    code: str
    severity: str  # "block" | "warn"
    message: str
    fix: str = ""

    def render(self) -> str:
        text = self.message
        if self.fix:
            text += " Fix: " + self.fix
        return text


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )


def _git_dir(repo: str) -> Optional[str]:
    """The repo's .git directory (absolute), or None when not a git repo."""
    try:
        cp = _git(repo, "rev-parse", "--git-dir")
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    path = cp.stdout.strip()
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(repo, path)
    return path


def repo_issues(repo_path: str) -> List[Issue]:
    """Classify ``repo_path``'s git state for session creation.

    Returns a list of :class:`Issue` (possibly empty). Never raises: a probe
    failure degrades to a single ``block`` issue saying git itself failed.
    """
    issues: List[Issue] = []
    repo = os.path.abspath(os.path.expanduser(repo_path or "."))

    if not os.path.isdir(repo):
        return [
            Issue(
                "missing-dir",
                "block",
                f"{repo} does not exist or is not a directory.",
                "check the repo path",
            )
        ]

    git_dir = _git_dir(repo)
    if git_dir is None:
        return [
            Issue(
                "not-a-repo",
                "block",
                f"{repo} is not a git repository — sessions fork a git worktree "
                "off the repo's HEAD, so MindFlock needs one.",
                f"git -C {repo} init && git -C {repo} commit --allow-empty -m 'initial'",
            )
        ]

    # --- operation in progress: rebase / merge / cherry-pick / bisect --------
    # Forking a worktree mid-operation gives the session a half-finished HEAD
    # (and cleanup could later delete a branch the operation still needs), so
    # these block with the exact escape hatch.
    in_progress = (
        (
            "rebase",
            ("rebase-merge", "rebase-apply"),
            "git -C %s rebase --continue (or --abort)",
        ),
        ("merge", ("MERGE_HEAD",), "git -C %s merge --continue (or --abort)"),
        (
            "cherry-pick",
            ("CHERRY_PICK_HEAD",),
            "git -C %s cherry-pick --continue (or --abort)",
        ),
        ("bisect", ("BISECT_LOG",), "git -C %s bisect reset"),
    )
    for name, markers, fix_tpl in in_progress:
        if any(os.path.exists(os.path.join(git_dir, m)) for m in markers):
            issues.append(
                Issue(
                    f"mid-{name}",
                    "block",
                    f"a {name} is in progress in {repo} — finish or abort it "
                    "first so the session doesn't fork a half-finished HEAD.",
                    fix_tpl % repo,
                )
            )

    # --- no commits yet -------------------------------------------------------
    try:
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return issues + [
            Issue("git-failed", "block", f"git failed probing {repo} — is git working?")
        ]
    if head.returncode != 0:
        issues.append(
            Issue(
                "no-commits",
                "block",
                f"{repo} has no commits yet — a worktree needs a HEAD commit "
                "to fork from.",
                f"git -C {repo} commit --allow-empty -m 'initial commit'",
            )
        )
        return issues  # everything below needs a HEAD

    # --- detached HEAD (warn: the fork works, but off a commit, not a branch) --
    try:
        sym = _git(repo, "symbolic-ref", "-q", "HEAD")
        if sym.returncode != 0:
            issues.append(
                Issue(
                    "detached-head",
                    "warn",
                    f"{repo} is on a detached HEAD — the session forks the "
                    "current commit, not a branch tip.",
                    f"git -C {repo} switch <branch>  # if you meant a branch",
                )
            )
    except (OSError, subprocess.SubprocessError):
        pass

    # --- shallow clone (warn: worktrees work; history-dependent flows may not) -
    if os.path.exists(os.path.join(git_dir, "shallow")):
        issues.append(
            Issue(
                "shallow-clone",
                "warn",
                f"{repo} is a shallow clone — worktrees work, but diff bases "
                "and merge-base-dependent flows may misbehave.",
                f"git -C {repo} fetch --unshallow",
            )
        )

    return issues


def blocking_error(repo_path: str) -> Optional[str]:
    """One user-facing error string when the repo is in a blocking state, else
    None. Warnings are not included — callers surface those separately."""
    blockers = [i for i in repo_issues(repo_path) if i.severity == "block"]
    if not blockers:
        return None
    return " ".join(i.render() for i in blockers)
