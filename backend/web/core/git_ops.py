"""Provider-agnostic git helpers — pure functions over a worktree path.

Lifted verbatim from ``server.py`` (no ENGINE/state coupling). Used by the
session DTO/stage telemetry and the lifecycle/forge routes.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Dict


def git_available() -> bool:
    """Whether the ``git`` binary is on PATH.

    Git is OPTIONAL: without it sessions run in-place in plain folders and the
    worktree/diff/commit/PR features are simply absent. Probed live (a PATH
    stat, no subprocess) so installing git mid-run is picked up on the next
    request without a server restart.
    """
    return shutil.which("git") is not None


def _run_git(args, **kw):
    """``subprocess.run(["git", ...])`` that degrades instead of raising when
    the git binary itself is missing/unlaunchable (returns None then).

    A default 30s timeout keeps a wedged git from hanging a poll; a hung
    command degrades to None exactly like a missing binary."""
    kw.setdefault("timeout", 30)
    try:
        return subprocess.run(["git", *args], **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_count(wt: str, rev_range: str):
    """`git rev-list --count <range>` -> int, or None if the range is invalid."""
    cp = _run_git(
        ["-C", wt, "rev-list", "--count", rev_range],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if cp is None or cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return None


def _commits_beyond_base(wt: str, base: str):
    for ref in ("origin/" + base, base):
        n = _git_count(wt, ref + "..HEAD")
        if n is not None:
            return n
    return 0


def _has_upstream(wt: str) -> bool:
    cp = _run_git(
        ["-C", wt, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cp is not None and cp.returncode == 0


def _is_dirty(wt: str) -> bool:
    cp = _run_git(
        ["-C", wt, "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return cp is not None and cp.returncode == 0 and bool(cp.stdout.strip())


def _git_head_sha(wt: str) -> str:
    cp = _run_git(
        ["-C", wt, "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return (
        cp.stdout.decode("utf-8", "replace").strip()
        if cp is not None and cp.returncode == 0
        else ""
    )


def _current_branch(wt: str) -> str:
    """The branch actually checked out in the worktree (or "" if detached/error).

    Authoritative for "the branch I'm on" — the stored ``inst.Branch`` can drift
    when the worktree is manually switched, so PR/push actions use this instead.
    """
    cp = _run_git(
        ["-C", wt, "symbolic-ref", "--quiet", "--short", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return (
        cp.stdout.decode("utf-8", "replace").strip()
        if cp is not None and cp.returncode == 0
        else ""
    )


_HAS_ORIGIN_CACHE: Dict[str, tuple] = {}  # wt -> (expires_epoch, bool)


def _has_origin(wt: str, force: bool = False) -> bool:
    """Whether the repo at ``wt`` has an ``origin`` remote configured (L2).

    Purely local (``git remote get-url origin`` — no network), cached ~30s so
    the /api/instances poll stays cheap. Used to gate the guided Push step
    (pushing without an origin dead-ends in the shell) and exposed on the
    instance JSON as ``has_origin`` so the UI can swap the next-step hint.
    """
    if not wt:
        return False
    now = time.time()
    cached = _HAS_ORIGIN_CACHE.get(wt)
    if not force and cached and cached[0] > now:
        return cached[1]
    cp = _run_git(
        ["-C", wt, "remote", "get-url", "origin"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    has = cp is not None and cp.returncode == 0
    _HAS_ORIGIN_CACHE[wt] = (now + 30, has)
    return has


_ORIGIN_SHA_CACHE: Dict[tuple, tuple] = {}  # (wt, branch) -> (expires_epoch, sha|None)
_ORIGIN_SHA_PENDING: Dict[tuple, float] = {}  # (wt, branch) -> bypass_cache_until_epoch


def mark_origin_push_pending(wt: str, branch: str, window: float = 45.0) -> None:
    """After a push is issued, mark ``(wt, branch)`` so ``_origin_branch_sha``
    bypasses its cache and re-queries origin on every call for ``window`` seconds.

    A push into the shell is fire-and-forget: the branch may not be on origin
    yet when we return. A one-shot cache pop is worse than useless — the very
    next poll's ``ls-remote`` re-caches the stale/None SHA for ~10s, stalling the
    "Make PR" button. This keeps the remote SHA fresh until the push lands (or
    the window elapses), so the stage flips within one poll of the push landing."""
    if not branch:
        return
    _ORIGIN_SHA_PENDING[(wt, branch)] = time.time() + window


def _origin_branch_sha(wt: str, branch: str, force: bool = False):
    """SHA of ``branch`` on origin (authoritative ``git ls-remote``), or None if
    the branch isn't on origin. Cached ~45s — this is a NETWORK round-trip per
    (worktree, branch) and the snapshot tick asks for it constantly; pushes
    made through MindFlock bypass the cache via the pending window below, so
    the long TTL only delays noticing pushes made outside the app. Bounded +
    guarded by a timeout. Used to confirm a branch is actually pushed before
    offering "Make PR"."""
    if not branch:
        return None
    key = (wt, branch)
    now = time.time()
    # A recent push bypasses the cache so polls see the new SHA the moment it
    # lands, instead of waiting out a stale ~10s entry. Self-expiring window.
    pending_until = _ORIGIN_SHA_PENDING.get(key)
    pending = pending_until is not None and pending_until > now
    if pending_until is not None and not pending:
        _ORIGIN_SHA_PENDING.pop(key, None)
    cached = _ORIGIN_SHA_CACHE.get(key)
    if not force and not pending and cached and cached[0] > now:
        return cached[1]
    sha = None
    try:
        cp = subprocess.run(
            ["git", "-C", wt, "ls-remote", "--heads", "origin", branch],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=12,
        )
        if cp.returncode == 0:
            out = cp.stdout.decode("utf-8", "replace").strip()
            if out:
                sha = out.split()[0]
    except (subprocess.TimeoutExpired, OSError):
        # Network hiccup: keep the previous answer rather than flapping the stage.
        if cached is not None:
            return cached[1]
        sha = None
    _ORIGIN_SHA_CACHE[key] = (now + 45, sha)
    return sha


# --- Plain (non-provisioned) repo selection -----------------------------------
def _is_git_repo(path: str) -> bool:
    """True if ``path`` is inside a git work tree (has a .git or is a worktree)."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def _git_has_commits(path: str) -> bool:
    """True if the repo at ``path`` has at least one commit (HEAD resolves)."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _make_initial_commit(path: str) -> None:
    """Create an empty initial commit so ``git worktree add <HEAD>`` works.

    Injects a throwaway identity only when the user has none configured, so an
    existing global ``user.name`` / ``user.email`` is preserved.
    """
    if not git_available():
        raise ValueError("git is not installed — install git to create a repo here")
    ident: list = []
    try:
        have_email = subprocess.run(
            ["git", "-C", path, "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        have_name = subprocess.run(
            ["git", "-C", path, "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        have_email = have_name = ""
    if not have_email or not have_name:
        ident = ["-c", "user.email=mindflock@localhost", "-c", "user.name=MindFlock"]
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                path,
                *ident,
                "commit",
                "--allow-empty",
                "-m",
                "Initial commit",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as err:
        raise ValueError(
            "failed to create initial commit: timed out after 60s"
        ) from err
    if r.returncode != 0:
        raise ValueError(
            "failed to create initial commit: " + (r.stderr or r.stdout).strip()
        )
