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
    "repo_of",
    "changed_paths",
    "commits_since",
    "commit_paths",
    "heal_index",
    "switch_new_branch",
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


def repo_of(path: str) -> str:
    """The repository that HOLDS the worktree at ``path`` — its objects and
    its branches — or ``""``.

    That is the parent of ``--git-common-dir``: the main checkout of a linked
    worktree, the checkout itself for a plain clone, and the git dir itself
    for a bare repository. A session's ``Path`` is NOT that: a provisioned
    session (every ticket session) is created with ``path="."`` — the
    server's own cwd — and its worktree hangs off MindFlock's ``_base_<repo>``
    clone (or is a clone of its own). A branch forked from such a session's
    commit, and every probe of the branches beside it, has to happen here.
    """
    if not path or not os.path.isdir(path):
        return ""
    cp = _git(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=20
    )
    if cp is None or cp.returncode != 0:
        return ""
    common = os.path.normpath(_out(cp))
    if not common or not os.path.isdir(common):
        return ""
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return common


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


# --------------------------------------------------------------------------- #
# Same folder: several agents share ONE checkout, and MindFlock commits each
# piece's paths itself. Nothing here may touch a file in the worktree: the
# other pieces are editing it while a commit is written.
# --------------------------------------------------------------------------- #
def _literal_env() -> dict:
    """Paths are passed as LITERAL pathspecs (a file named ``a[1].py`` is
    that file, never a glob)."""
    env = dict(os.environ)
    env.update(_ENV)
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def _git_env(path: str, args, env: dict, timeout: int = _GIT_TIMEOUT_S, data=None):
    try:
        return subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            timeout=timeout,
            env=env,
            input=data,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def changed_paths(path: str) -> Optional[List[str]]:
    """Every path with an uncommitted change at ``path`` — tracked (staged or
    not, deleted included) and untracked files one by one — or None when git
    cannot tell. Read without optional locks: it must never collide with an
    agent's own git command in the same folder."""
    cp = _git(
        path,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        timeout=60,
    )
    if cp is None or cp.returncode != 0:
        return None
    out: List[str] = []
    for entry in (cp.stdout or b"").decode("utf-8", "replace").split("\0"):
        if len(entry) > 3 and entry[2] == " ":
            out.append(entry[3:])
    return out


def _files_of(path: str, sha: str) -> List[str]:
    cp = _git(
        path,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        "--no-renames",
        "--root",
        sha,
        timeout=30,
    )
    if cp is None or cp.returncode != 0:
        return []
    return [f for f in (cp.stdout or b"").decode("utf-8", "replace").split("\0") if f]


def commits_since(path: str, base: str, head: str = "HEAD", limit: int = 50):
    """``base..head``'s own commits (merges left out), oldest first, each
    ``{"sha", "subject", "body", "files"}`` — or None when git cannot tell."""
    if not base:
        return []
    cp = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        "-n",
        str(int(limit)),
        "--format=%H%x1f%s%x1f%b%x1e",
        "%s..%s" % (base, head),
        timeout=30,
    )
    if cp is None or cp.returncode != 0:
        return None
    out = []
    for rec in (cp.stdout or b"").decode("utf-8", "replace").split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 3 or not parts[0].strip():
            continue
        sha = parts[0].strip()
        out.append(
            {
                "sha": sha,
                "subject": parts[1],
                "body": parts[2],
                "files": _files_of(path, sha),
            }
        )
    return out


def commit_paths(
    path: str, files: List[str], message: str, expect_head: str = ""
) -> Dict[str, object]:
    """Commit EXACTLY ``files`` (as they are in the working tree now) on top
    of HEAD at ``path`` — and nothing else, whatever is staged or changed
    beside them.

    Plumbing, never ``git commit``: a temporary index read from HEAD takes
    the files, ``write-tree`` / ``commit-tree`` make the commit, and
    ``update-ref HEAD <new> <old>`` moves the branch only if HEAD is still
    the commit it was built on. The working tree is never touched and no
    commit hook runs (a hook that stashes "unstaged" changes would pull the
    other pieces' edits out from under them mid-turn). The real index is
    then re-read for those paths only, so they show clean.

    → ``{"result": "committed"|"nothing"|"moved"|"error", "sha", "error"}``."""
    files = [f for f in dict.fromkeys(files or []) if f]
    if not files:
        return {"result": "nothing", "sha": "", "error": ""}
    head = rev_parse(path, "HEAD")
    if not head:
        return {"result": "error", "sha": "", "error": "no HEAD to commit on"}
    if expect_head and head != expect_head:
        return {"result": "moved", "sha": "", "error": "HEAD moved"}
    cp = _git(path, "rev-parse", "--absolute-git-dir", timeout=20)
    gitdir = _out(cp) if cp is not None and cp.returncode == 0 else ""
    if not gitdir:
        return {"result": "error", "sha": "", "error": "not a git checkout"}
    index = os.path.join(gitdir, "mindflock-piece-index-%d" % os.getpid())
    env = _literal_env()
    tmp_env = dict(env, GIT_INDEX_FILE=index)
    try:
        cp = _git_env(path, ["read-tree", head], tmp_env, timeout=60)
        if cp is None or cp.returncode != 0:
            return {"result": "error", "sha": "", "error": _err(cp)}
        data = "\0".join(files).encode("utf-8") + b"\0"
        cp = _git_env(
            path,
            [
                "add",
                "-A",
                "--ignore-errors",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            tmp_env,
            timeout=120,
            data=data,
        )
        if cp is None:
            return {"result": "error", "sha": "", "error": "git could not run"}
        cp = _git_env(path, ["write-tree"], tmp_env, timeout=60)
        tree = _out(cp) if cp is not None and cp.returncode == 0 else ""
        if not tree:
            return {"result": "error", "sha": "", "error": _err(cp)}
        if tree == rev_parse(path, "HEAD^{tree}"):
            return {"result": "nothing", "sha": "", "error": ""}
        cp = _git_env(
            path,
            ["commit-tree", tree, "-p", head, "-F", "-"],
            env,
            timeout=60,
            data=(message or "Commit").encode("utf-8"),
        )
        sha = _out(cp) if cp is not None and cp.returncode == 0 else ""
        if not sha:
            return {"result": "error", "sha": "", "error": _err(cp)}
        cp = _git_env(
            path,
            ["update-ref", "-m", "mindflock: commit a piece", "HEAD", sha, head],
            env,
            timeout=30,
        )
        if cp is None or cp.returncode != 0:
            return {"result": "moved", "sha": "", "error": _err(cp)}
    finally:
        for p in (index, index + ".lock"):
            try:
                os.unlink(p)
            except OSError:
                pass
    # The real index still holds the old blobs for these paths: re-read them
    # from the new HEAD (best-effort — heal_index catches a miss).
    _git_env(
        path,
        ["reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul"],
        env,
        timeout=60,
        data="\0".join(files).encode("utf-8") + b"\0",
    )
    return {"result": "committed", "sha": sha, "error": ""}


def heal_index(path: str) -> List[str]:
    """Re-read from HEAD the index entries that differ from HEAD while the
    file itself does NOT (the leftover of a piece commit whose index refresh
    was interrupted). Those entries carry nothing: resetting them loses no
    one's work. Returns the healed paths."""
    cached = _git(
        path, "--no-optional-locks", "diff", "--cached", "--name-only", "-z", timeout=30
    )
    if cached is None or cached.returncode != 0:
        return []
    staged = [
        f for f in (cached.stdout or b"").decode("utf-8", "replace").split("\0") if f
    ]
    if not staged:
        return []
    live = _git(
        path, "--no-optional-locks", "diff", "HEAD", "--name-only", "-z", timeout=30
    )
    if live is None or live.returncode != 0:
        return []
    differ = {
        f for f in (live.stdout or b"").decode("utf-8", "replace").split("\0") if f
    }
    stale = [f for f in staged if f not in differ]
    if not stale:
        return []
    _git_env(
        path,
        ["reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul"],
        _literal_env(),
        timeout=60,
        data="\0".join(stale).encode("utf-8") + b"\0",
    )
    return stale


def switch_new_branch(path: str, branch: str) -> str:
    """``git switch -c branch`` at ``path`` (uncommitted changes come along).
    Returns ``""`` on success, else git's sentence."""
    cp = _git(path, "switch", "-c", branch, timeout=60)
    if cp is None or cp.returncode != 0:
        return _err(cp) or "git could not switch"
    return ""
