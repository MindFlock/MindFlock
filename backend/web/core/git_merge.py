"""Merging a piece's branch back into the lead's worktree — mechanically.

A split (or a one-for-all group) is one task cut into pieces that run in
parallel on their own branches, each forked from the LEAD's commit. When a
piece is done the server — not an agent — merges its branch into the lead's
worktree with ``git merge --no-ff``, so the clean case costs no model call
and no attention. Only a conflict needs judgement, and that is handed to the
lead agent (:mod:`backend.web.core.team_run_driver`), which resolves it and
reports back; the server then re-verifies by ancestry rather than taking the
agent's word for it.

Everything here is plain ``git`` in a subprocess, blocking, and never raises:
an unexpected failure is a result (``"error"``), because a merge queue that
throws stops every group behind it.

:func:`merge_into` REFUSES — before touching anything — when the target has
uncommitted tracked changes or a merge already in progress: merging into a
tree someone is editing would mix their half-done work into the merge commit
(or, on a conflict, the ``--abort`` would throw it away). Whether the lead's
AGENT is idle is the caller's question (it has the activity probe); this
module only answers for the tree.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional

__all__ = [
    "merge_into",
    "is_ancestor",
    "rev_parse",
    "tracked_dirty",
    "merge_in_progress",
    "commit_subjects",
    "diff_stat",
    "conflicted_files",
    "tracked_files",
    "current_branch",
    "operation_in_progress",
    "recover_interrupted",
]

_GIT_TIMEOUT_S = 120
#: Never let git open an editor or ask for anything: the server has no tty.
_ENV = {
    "GIT_EDITOR": "true",
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(path: str, *args: str, timeout: int = _GIT_TIMEOUT_S):
    """``git -C path args`` → CompletedProcess, or None when git could not run."""
    env = dict(os.environ)
    env.update(_ENV)
    try:
        return subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _out(cp) -> str:
    if cp is None:
        return ""
    return (cp.stdout or b"").decode("utf-8", "replace").strip()


def _err(cp) -> str:
    if cp is None:
        return "git could not run"
    text = ((cp.stderr or b"") + b"\n" + (cp.stdout or b"")).decode("utf-8", "replace")
    return re.sub(r"\s+", " ", text).strip()[:400]


def rev_parse(path: str, ref: str = "HEAD") -> str:
    """The full sha ``ref`` names in the repo at ``path``, or ``""``."""
    if not path or not os.path.isdir(path) or not ref:
        return ""
    cp = _git(path, "rev-parse", "--verify", "--quiet", ref + "^{commit}", timeout=20)
    if cp is None or cp.returncode != 0:
        return ""
    return _out(cp)


def is_ancestor(path: str, sha: str, ref: str = "HEAD") -> Optional[bool]:
    """Whether ``sha`` is reachable from ``ref`` (i.e. merged into it). None
    when it cannot be told (no repo, an unknown sha) — never read as "no"."""
    if not path or not sha or not os.path.isdir(path):
        return None
    if not rev_parse(path, sha):
        return None
    cp = _git(path, "merge-base", "--is-ancestor", sha, ref, timeout=30)
    if cp is None:
        return None
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    return None


def tracked_dirty(path: str) -> Optional[bool]:
    """Uncommitted changes to TRACKED files (staged or not). Untracked files
    do not count — an agent's scratch file never blocks a merge; git itself
    refuses (as an ``error``) when one would be overwritten. None when git
    cannot tell."""
    cp = _git(path, "status", "--porcelain", "--untracked-files=no", timeout=30)
    if cp is None or cp.returncode != 0:
        return None
    return bool(_out(cp))


def merge_in_progress(path: str) -> bool:
    """A merge was started and not finished (``MERGE_HEAD`` exists)."""
    cp = _git(path, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", timeout=20)
    return cp is not None and cp.returncode == 0


def current_branch(path: str) -> str:
    """The branch checked out at ``path`` (``""`` for a detached HEAD or when
    git cannot tell)."""
    cp = _git(path, "symbolic-ref", "--short", "-q", "HEAD", timeout=20)
    if cp is None or cp.returncode != 0:
        return ""
    return _out(cp)


def _git_path(path: str, name: str) -> str:
    """``git rev-parse --git-path name`` as an absolute path (``""``)."""
    cp = _git(path, "rev-parse", "--git-path", name, timeout=20)
    rel = _out(cp) if cp is not None and cp.returncode == 0 else ""
    if not rel:
        return ""
    return rel if os.path.isabs(rel) else os.path.join(path, rel)


#: Sequencer states that make a worktree's HEAD a moving target.
_OPERATIONS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)


def operation_in_progress(path: str) -> str:
    """A rebase / cherry-pick / revert / bisect in progress at ``path`` (its
    name), else ``""`` — HEAD is not a branch tip anyone means to ship."""
    for marker, name in _OPERATIONS:
        p = _git_path(path, marker)
        if p and os.path.exists(p):
            return name
    return ""


def conflicted_files(path: str) -> List[str]:
    """Paths with unresolved conflicts in the worktree at ``path``."""
    cp = _git(path, "diff", "--name-only", "--diff-filter=U", timeout=30)
    return [ln for ln in _out(cp).splitlines() if ln.strip()]


def tracked_files(path: str, limit: int = 50000) -> List[str]:
    """``git ls-files`` at ``path`` (what a split's path globs are checked
    against), at most ``limit`` of them."""
    cp = _git(path, "ls-files", "-z", timeout=60)
    if cp is None or cp.returncode != 0:
        return []
    raw = (cp.stdout or b"").decode("utf-8", "replace")
    return [f for f in raw.split("\0") if f][:limit]


def commit_subjects(path: str, base: str, head: str, limit: int = 50) -> List[str]:
    """The subjects of ``base..head``'s own commits, oldest first, merges
    left out — "what this piece committed", as written."""
    if not base or not head:
        return []
    cp = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        "--format=%s",
        "-n",
        str(int(limit)),
        "%s..%s" % (base, head),
        timeout=30,
    )
    if cp is None or cp.returncode != 0:
        return []
    return [ln for ln in _out(cp).splitlines() if ln.strip()]


def diff_stat(path: str, base: str, head: str = "HEAD") -> Dict[str, int]:
    """``{"files", "add", "del"}`` for ``base...head`` (zeros when unknown)."""
    out = {"files": 0, "add": 0, "del": 0}
    if not base:
        return out
    cp = _git(path, "diff", "--shortstat", "%s...%s" % (base, head), timeout=60)
    text = _out(cp)
    for key, pat in (
        ("files", r"(\d+) files? changed"),
        ("add", r"(\d+) insertions?"),
        ("del", r"(\d+) deletions?"),
    ):
        m = re.search(pat, text)
        if m:
            out[key] = int(m.group(1))
    return out


#: Marker (in the worktree's git dir) that THIS module started a merge: set
#: before ``git merge``, cleared once the merge committed or was aborted. Seen
#: with a MERGE_HEAD and no merge running in this process, it means the server
#: died mid-merge — :func:`recover_interrupted` unwinds it.
_MARKER = "mindflock-merging"
_RUNNING: set = set()


def recover_interrupted(worktree: str) -> bool:
    """Abort a merge this module started and never finished (the server died
    between a conflict and its ``--abort``). A merge someone ELSE started has
    no marker and is never touched. Returns whether one was unwound."""
    if not worktree or not os.path.isdir(worktree):
        return False
    marker = _git_path(worktree, _MARKER)
    if not marker or not os.path.exists(marker):
        return False
    if os.path.realpath(worktree) in _RUNNING:
        return False
    if merge_in_progress(worktree):
        _git(worktree, "merge", "--abort", timeout=60)
    try:
        os.unlink(marker)
    except OSError:
        pass
    return True


def merge_into(
    worktree: str, branch: str, *, message: str = "", expect_branch: str = ""
) -> dict:
    """Merge ``branch`` into the branch checked out at ``worktree``:
    ``git merge --no-ff --no-edit``.

    Returns ``{"result", "head", "files", "error"}``:

    * ``clean`` — merged; ``head`` is the new HEAD (the merge commit);
    * ``up_to_date`` — ``branch`` was already merged (nothing to do);
    * ``conflict`` — git stopped on conflicts in ``files``; the merge has been
      ABORTED, so the worktree is exactly as it was;
    * ``refused`` — the tree had uncommitted tracked changes or a merge in
      progress (``error`` says which); nothing was touched;
    * ``error`` — anything else (an unknown branch, an untracked file in the
      way, git missing), with git's own words in ``error``.

    Merge hooks are skipped (``--no-verify``): every commit being merged went
    through the pre-commit gate when the piece committed it, and the check
    runs on the merged branch afterwards — a hook firing here would only block
    a mechanical merge on a tree nobody can answer from.
    """
    res = {"result": "error", "head": "", "files": [], "error": ""}
    if not worktree or not os.path.isdir(worktree):
        res["error"] = "the lead's worktree is missing"
        return res
    if not branch:
        res["error"] = "no branch to merge"
        return res
    if not rev_parse(worktree, branch):
        res["error"] = "branch %s does not exist" % branch
        return res
    recover_interrupted(worktree)
    if merge_in_progress(worktree):
        res["result"] = "refused"
        res["error"] = "a merge is already in progress in the lead's worktree"
        return res
    op = operation_in_progress(worktree)
    if op:
        res["result"] = "refused"
        res["error"] = "a %s is in progress in the lead's worktree" % op
        return res
    if expect_branch:
        live = current_branch(worktree)
        if live != expect_branch:
            res["result"] = "refused"
            res["error"] = "the lead is on %s, not %s" % (
                live or "a detached HEAD",
                expect_branch,
            )
            return res
    dirty = tracked_dirty(worktree)
    if dirty is None:
        res["error"] = "could not read the lead's worktree status"
        return res
    if dirty:
        res["result"] = "refused"
        res["error"] = "the lead's worktree has uncommitted changes"
        return res
    target = rev_parse(worktree, branch)
    if is_ancestor(worktree, target, "HEAD"):
        res["result"] = "up_to_date"
        res["head"] = rev_parse(worktree, "HEAD")
        return res
    args = ["merge", "--no-ff", "--no-edit", "--no-verify"]
    if message:
        args += ["-m", message]
    key = os.path.realpath(worktree)
    marker = _git_path(worktree, _MARKER)
    _RUNNING.add(key)
    try:
        if marker:
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(branch + "\n")
            except OSError:
                marker = ""
        cp = _git(worktree, *args, branch)
        if cp is not None and cp.returncode == 0:
            res["result"] = "clean"
            res["head"] = rev_parse(worktree, "HEAD")
            return res
        files = conflicted_files(worktree) if merge_in_progress(worktree) else []
        if files:
            _git(worktree, "merge", "--abort", timeout=60)
            res["result"] = "conflict"
            res["files"] = files
            res["error"] = "conflicts in %s" % ", ".join(files[:5])
            return res
        # A failure that left a half-merge behind (rare) is unwound too: the
        # worktree must always come back exactly as it was.
        if merge_in_progress(worktree):
            _git(worktree, "merge", "--abort", timeout=60)
        res["error"] = _err(cp) or "git merge failed"
        return res
    finally:
        _RUNNING.discard(key)
        if marker:
            try:
                os.unlink(marker)
            except OSError:
                pass
