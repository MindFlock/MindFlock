"""Reclaim a leftover worktree so a re-run of the same ticket/issue can proceed.

The state this fixes: a ticket ran once, its session later went away (ended,
cleaned up from the engine, or lost with a restart), but the *worktree* stayed on
disk — deliberately, because ending a session keeps its worktree so the work can
be reopened. Nothing owns that worktree any more, yet git still has the feature
branch checked out there, so the next ``git worktree add`` for the same branch
fails with "already checked out at …".

From the panel's point of view the ticket looks perfectly runnable (there is no
live session, so **Run ticket** is enabled), and the run then dies on a leftover
nobody can see. Worse, the failure was recorded as a ledger entry whose advice
was to delete the ledger entry — which clears the record and leaves the actual
blocker in place, so the retry fails identically.

So force-start reclaims the leftover first, under two absolute rules:

* **Never take a worktree a live session owns.** Ownership is decided by the
  caller (it is the engine that knows its sessions), passed in as ``is_owned``.
* **Never destroy work.** A worktree with uncommitted changes, a stash, or
  commits its branch hasn't pushed is left exactly where it is, and the original
  clear error stands. Reclaiming is only ever for a pristine checkout.

Anything it declines to touch keeps the existing behaviour, so the worst case is
the error message the user already got.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from backend import log

#: Per-git-call ceiling. These run on a force-start request, so a hung git must
#: not hold the launch open indefinitely — a timeout is treated as "don't touch".
_GIT_TIMEOUT = 30


def _git(repo: str, *args: str) -> tuple[int, str]:
    """``git -C repo <args>`` -> (returncode, stdout). Never raises."""
    try:
        cp = subprocess.run(
            ["git", "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""
    return cp.returncode, cp.stdout.decode("utf-8", "replace")


def worktree_is_pristine(path: str) -> bool:
    """True when ``path`` holds nothing worth preserving.

    Pristine means: no uncommitted or untracked changes, no stash entries, and
    no commits that aren't already reachable from the branch's upstream (or, with
    no upstream, no commits at all beyond what the checkout started from). Any
    doubt — a git call that fails or times out — answers False, because the
    consequence of a wrong True is deleting someone's work.
    """
    rc, out = _git(path, "status", "--porcelain=v1")
    if rc != 0 or out.strip():
        return False

    rc, out = _git(path, "stash", "list")
    if rc != 0 or out.strip():
        return False

    # Commits not yet on the upstream. No upstream configured (never pushed) is
    # not a licence to delete: compare against the remote's default instead, and
    # if that can't be resolved either, refuse.
    rc, upstream = _git(path, "rev-parse", "--abbrev-ref", "@{upstream}")
    ref = upstream.strip() if rc == 0 and upstream.strip() else ""
    if not ref:
        rc, head = _git(path, "symbolic-ref", "--short", "HEAD")
        rc2, remote_head = _git(path, "rev-parse", "--abbrev-ref", "origin/HEAD")
        ref = remote_head.strip() if rc2 == 0 and remote_head.strip() else ""
        if not ref:
            return False
    rc, ahead = _git(path, "rev-list", "--count", f"{ref}..HEAD")
    if rc != 0:
        return False
    try:
        return int(ahead.strip() or "1") == 0
    except ValueError:
        return False


def reclaim_for_branch(
    base_repo: str,
    branch_name: str,
    is_owned: Callable[[str], bool],
) -> str:
    """Free ``branch_name`` for a fresh ``git worktree add``, if that is safe.

    Returns the path reclaimed, or ``""`` when there was nothing to reclaim or
    reclaiming would not have been safe. Never raises: every refusal leaves the
    repository exactly as it was, and the caller's normal provisioning path then
    produces its usual (now accurate) error.

    ``is_owned(path)`` must return True when a live session is using ``path``.
    """
    from backend.session.provisioned import worktree_holding_branch

    try:
        held_at = worktree_holding_branch(base_repo, branch_name)
    except Exception:  # noqa: BLE001 — a probe must not break a launch
        return ""
    if not held_at:
        return ""

    try:
        if is_owned(held_at):
            return ""
    except Exception:  # noqa: BLE001 — unknown ownership means hands off
        return ""

    if not worktree_is_pristine(held_at):
        if log.InfoLog is not None:
            log.InfoLog.Printf(
                "leftover worktree %s holds branch %s but has local work — "
                "left in place",
                held_at,
                branch_name,
            )
        return ""

    rc, _ = _git(base_repo, "worktree", "remove", held_at)
    if rc != 0:
        # --force only escalates past git's own dirty/locked checks, which
        # worktree_is_pristine has already cleared; it is still refused for a
        # worktree that has work, because we never get here in that case.
        rc, _ = _git(base_repo, "worktree", "remove", "--force", held_at)
    if rc != 0:
        return ""
    _git(base_repo, "worktree", "prune")
    if log.InfoLog is not None:
        log.InfoLog.Printf(
            "reclaimed orphaned worktree %s (branch %s, no live session, no local "
            "work) so the run can proceed",
            held_at,
            branch_name,
        )
    return held_at


def reclaim_for_launch(repo_url: str, branch: str) -> str:
    """Pre-flight a provisioned ticket/issue launch: free ``branch`` if a
    leftover worktree is holding it and nothing would be lost.

    Resolves the base clone exactly the way the engine does
    (:func:`load_provision_settings` + :func:`resolve_base_repo_dir`) so this
    can't drift from where the worktrees are actually added. Returns the path
    reclaimed, or ``""`` for the (usual) case of nothing to do.

    Ownership comes from the engine's own live-instance check, so a worktree
    shared by a session and its copy is never taken out from under them.
    """
    if not branch:
        return ""
    try:
        from backend.session.provisioned import (
            load_provision_settings,
            resolve_base_repo_dir,
        )

        settings = load_provision_settings(repo_url_override=repo_url or "")
        base = str(resolve_base_repo_dir(settings))
    except Exception:  # noqa: BLE001 — unresolvable base repo: nothing to do
        return ""
    if not base or not os.path.isdir(os.path.join(base, ".git")):
        return ""

    def _is_owned(path: str) -> bool:
        from backend.web.core.workspaces import _worktree_in_use_by_other

        # exclude_title="" — any live instance on that directory counts.
        return _worktree_in_use_by_other(path, "")

    return reclaim_for_branch(base, branch, _is_owned)
