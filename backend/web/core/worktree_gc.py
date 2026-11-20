"""The garbage collector for what agents leave behind: an inventory of
worktrees and session clones, a classifier per item, and removal of only the
provably safe.

Why: agents spin up worktrees all the time and nothing took them away. On the
owner's box on 2026-11-20 the MindFlock repo had 110 worktrees, 84 of them
fully merged, clean and used by no session, and ``~/.mindflock/worktrees``
held 141 folders from five repositories, 118 GB, most from sessions closed
weeks before. Cleaning that by hand meant a survey of every one (unmerged
commits? uncommitted files? pushed? a session on it?) before anything could
go. This module is that survey, done the same way every time. The server
side — who uses what, the report cache, the loop, the setting — is
``worktree_gc_driver.py``.

**Inventory** (:func:`discover`), three kinds of item:

* ``worktree`` — every linked worktree of every known repository (``git
  worktree list`` of the repos the engine, the recently-closed store and the
  creation records name, plus the repo each folder under the worktrees root
  points back to). A repository's main checkout is never an item.
* ``orphan`` — a folder under the worktrees root git no longer lists (its
  repository or its registration is gone), and a ``*.clone-tmp`` leftover of
  a failed clone. Its files can't be checked, so it is never collected.
* ``clone`` — a session's full clone (clone strategy) or a ``pr-*`` review
  clone in the provisioning workspace dir. Base clones and cache refreshers
  are shared infrastructure and never items.

An item is MindFlock's own (``ours``) when it lies under the worktrees root
or the workspace dir, or a creation record (``backend/session/git/created.py``)
names it; anything else (an agent's ``.claude/worktrees/…``, one made by
hand) is listed with its state and never removed automatically.

**Classifier** (:func:`classify`), cheap git plumbing, never on the event
loop: in use (the ``Usage`` the caller builds: a session — running, paused,
sleeping, queued —, one closed recently enough to reopen, a route or run that
names it), uncommitted work (tracked changes and untracked non-ignored files,
via ``git --no-optional-locks status`` so the person's index is never
refreshed), unmerged work (commits in no base branch AND on no remote),
merged-ness, last use, size on disk.

Merged is more than "ancestor of the base": this repo's ``main`` has been
squashed to a single commit, which turns every merged branch into one with
"commits not in main". A branch also counts as merged when merging it into a
base would change nothing (``git merge-tree --write-tree`` gives back the
base's own tree — a squash or rebase merge, and with
``--allow-unrelated-histories`` the squashed-root case), or when its PR is
merged at this very HEAD (``Usage.pr_merged``, asked only about an item that
nothing else keeps). A merge-tree that conflicts or fails answers "not
merged": when unsure, keep.

**Safe** = ours, a worktree or clone, not locked, not in use, nothing
uncommitted, every commit in a base branch or on a remote (a clone: no stash
either, it lives in the clone), and unused longer than the threshold.
:func:`collect` removes only those, re-checking each right before, a worktree
with plain ``git worktree remove`` (never ``--force``), and deletes the local
branch only when it is merged (not merely pushed) and MindFlock cut it.

**On request** (:func:`remove_one`) an item that holds work can go too, never
silently: uncommitted changes (untracked files included) are saved first as
``changes.patch`` in a new folder under ``<config>/kept-work/``, a clone's
unpushed commits and stash as ``commits.bundle``, a detached HEAD's unmerged
commits on a ``mindflock-kept/…`` branch; an unmerged branch is never
deleted. If saving fails nothing is removed.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

from backend import gitutil
from backend.session.git.created import forget_records, load_records

DAY = 24 * 3600.0

#: Default: a safe item unused this long may be collected.
DEFAULT_AFTER_DAYS = 14
#: Items removed per automatic pass at most (a backlog drains over passes).
MAX_PER_PASS = 25
#: Per-git-call ceiling; a hung git is "couldn't check", which keeps the item.
GIT_TIMEOUT = 30
#: ``du`` ceiling per folder; past it the size is unknown (never blocks).
SIZE_TIMEOUT = 120
#: A measured size is reused this long if the folder shows no newer use.
SIZE_TTL_S = 24 * 3600.0
#: Items classified concurrently (git + du are I/O bound).
WORKERS = 4

KIND_WORKTREE = "worktree"
KIND_ORPHAN = "orphan"
KIND_CLONE = "clone"

#: Item states, most decisive first (the badge the UI shows).
IN_USE = "in_use"
NOT_OURS = "not_ours"
HOLDS_WORK = "holds_work"
RECENT = "recent"
UNKNOWN = "unknown"
SAFE = "safe"
MISSING = "missing"

#: Branches never deleted, whatever a record says.
_PROTECTED_BRANCHES = {"main", "master", "staging", "develop", "development", "HEAD"}

#: The kept-work folder, under the config dir.
KEPT_DIR = "kept-work"


# --------------------------------------------------------------------- paths


def config_dir() -> str:
    from backend import config

    return config.GetConfigDir()


def worktrees_root() -> str:
    from backend.session.git.worktree import get_worktree_directory

    return get_worktree_directory()


def kept_root() -> str:
    return os.path.join(config_dir(), KEPT_DIR)


def norm(path: str) -> str:
    """The comparable form of a path (absolute, symlinks resolved)."""
    if not path:
        return ""
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return os.path.abspath(path)


def item_id(path: str) -> str:
    return hashlib.sha1(norm(path).encode("utf-8", "replace")).hexdigest()[:16]


def strictly_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p, r = norm(path), norm(root)
    try:
        return os.path.commonpath([p, r]) == r and p != r
    except ValueError:
        return False


# ----------------------------------------------------------------- git probes


#: Every probe runs with optional locks off (``--no-optional-locks`` and the
#: env var, which also reaches git's own sub-commands): ``git status`` would
#: otherwise rewrite the index while refreshing it, and a scan must leave a
#: worktree exactly as it found it.
READ_ONLY_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


def _git(cwd: str, *args: str, env=None, timeout: float = GIT_TIMEOUT, text=True):
    full = dict(READ_ONLY_ENV)
    if env:
        full.update(env)
    return gitutil.git(
        ["--no-optional-locks", *args],
        cwd,
        timeout=timeout,
        env=full,
        text=text,
        stdin=subprocess.DEVNULL,
    )


def _out(cwd: str, *args: str) -> Optional[str]:
    """Stripped stdout, or None on any failure."""
    cp = _git(cwd, *args)
    return cp.stdout.strip() if cp.returncode == 0 else None


def gitdir_of(path: str) -> str:
    """The gitdir a linked worktree's ``.git`` FILE points at, "" otherwise
    (no ``.git``, or a ``.git`` directory — a real repository)."""
    dot = os.path.join(path, ".git")
    if not os.path.isfile(dot):
        return ""
    try:
        with open(dot, encoding="utf-8", errors="replace") as f:
            line = f.read(4096).strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    if not line.startswith("gitdir:"):
        return ""
    g = line[len("gitdir:") :].strip()
    return os.path.normpath(g if os.path.isabs(g) else os.path.join(path, g))


def main_repo_of(path: str) -> str:
    """The main checkout of the repository a linked worktree at ``path``
    belongs to, read from its ``.git`` file (``<repo>/.git/worktrees/<n>``)
    without running git, so it works for a folder git has forgotten too.
    "" when it can't be told."""
    gd = gitdir_of(path)
    parent = os.path.dirname(gd) if gd else ""
    if not parent or os.path.basename(parent) != "worktrees":
        return ""
    common = os.path.dirname(parent)
    if os.path.basename(common) == ".git":
        return norm(os.path.dirname(common))
    return norm(common)


def base_refs(repo: str, hint: str = "") -> List[str]:
    """The base branches work in ``repo`` merges into, as refs that exist:
    the recorded base (``hint``), what ``origin/HEAD`` points at, and local
    ``main`` / ``master`` / ``staging`` / ``develop``, each with its
    ``origin/`` counterpart. Content found in any of them is content a remote
    (or the repo's own integration branch) already has."""
    names: List[str] = []
    h = (hint or "").strip()
    for pre in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if h.startswith(pre):
            h = h[len(pre) :]
    if h and not _looks_like_sha(h):
        names.append(h)
    head = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and head.startswith("origin/"):
        names.append(head[len("origin/") :])
    # The usual integration branches too: a repo that ships through
    # `staging` (sitecheck does) squash-merges into it, not into main.
    names += ["main", "master", "staging", "develop", "development"]
    out: List[str] = []
    for n in names:
        for ref in ("refs/heads/" + n, "refs/remotes/origin/" + n):
            if ref not in out and gitutil.ref_exists(repo, ref, timeout=GIT_TIMEOUT):
                out.append(ref)
    return out


def _looks_like_sha(s: str) -> bool:
    return len(s) >= 7 and all(c in "0123456789abcdef" for c in s.lower())


def _count(repo: str, *revs: str) -> Optional[int]:
    cp = _git(repo, "rev-list", "--count", *revs)
    if cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.strip() or "0")
    except ValueError:
        return None


def merged_into(repo: str, head: str, bases: Sequence[str]) -> str:
    """How ``head`` is already in one of ``bases``: ``"ancestor"`` (every
    commit reachable), ``"content"`` (merging it would change nothing: a
    squash or rebase merge, or a squashed history), or ``""`` (not shown to
    be merged — the answer whenever git can't tell)."""
    if not head or not bases:
        return ""
    n = _count(repo, head, "--not", *bases)
    if n == 0:
        return "ancestor"
    if n is None:
        return ""
    head_tree = _out(repo, "rev-parse", head + "^{tree}")
    objects = _out(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    if not objects or not os.path.isdir(objects):
        return ""
    # merge-tree --write-tree WRITES the merged trees (and any merged blobs)
    # into the object store. A scan must leave the person's repository as it
    # found it, so the writes land in a throwaway object directory and the
    # real one is only read, as an alternate.
    with tempfile.TemporaryDirectory(prefix="mindflock-gc-objects-") as scratch:
        env = {
            "GIT_OBJECT_DIRECTORY": scratch,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
        }
        for b in bases:
            base_tree = _out(repo, "rev-parse", b + "^{tree}")
            if not base_tree:
                continue
            if head_tree and head_tree == base_tree:
                return "content"
            cp = _git(
                repo,
                "merge-tree",
                "--write-tree",
                "--allow-unrelated-histories",
                b,
                head,
                env=env,
            )
            first = cp.stdout.split("\n", 1)[0].strip() if cp.returncode == 0 else ""
            if first and first == base_tree:
                return "content"
    return ""


def status_counts(path: str) -> Optional[Dict[str, int]]:
    """``{"tracked": n, "untracked": m}`` changed files (ignored files don't
    count — they are build output and caches), or None when git can't say."""
    cp = _git(path, "status", "--porcelain=v1", "--untracked-files=normal")
    if cp.returncode != 0:
        return None
    tracked = untracked = 0
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            tracked += 1
    return {"tracked": tracked, "untracked": untracked}


def _reflog_last(path: str) -> float:
    """The time of the newest entry of a reflog file (``<old> <new> <who>
    <epoch> <tz>\t<msg>``), 0 when there is none. Parsed from the entries,
    not the file's mtime: ``git gc`` rewrites the file when it expires old
    entries."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return 0.0
    best = 0.0
    for line in tail.splitlines():
        head = line.split("\t", 1)[0].rsplit(" ", 2)
        if len(head) == 3:
            try:
                best = max(best, float(head[1]))
            except ValueError:
                continue
    return best


def last_activity(path: str) -> float:
    """When work last happened here, from signals a READ cannot move: the
    newest entry of the worktree's own HEAD reflog (a commit, checkout, reset,
    rebase, the ``worktree add`` itself), else its HEAD commit's time.

    Deliberately NOT the folder's mtime or the git index's: ``git status``
    (anyone's — a person's survey, the server's diff-stat probe) rewrites the
    index to refresh it, and editors and tools touch folders, so either would
    keep resetting the very clock that decides what is old. Work that never
    reached a commit does not need this clock: it shows as uncommitted or
    untracked, which keeps the worktree on its own. The caller adds
    MindFlock's own records (a session's close time, its last update, the
    worktree's creation record)."""
    gd = gitdir_of(path) or (
        os.path.join(path, ".git") if os.path.isdir(os.path.join(path, ".git")) else ""
    )
    best = _reflog_last(os.path.join(gd, "logs", "HEAD")) if gd else 0.0
    if not best:
        out = _out(path, "log", "-1", "--format=%ct", "HEAD")
        try:
            best = float(out) if out else 0.0
        except ValueError:
            best = 0.0
    return best


_size_cache: Dict[tuple, tuple] = {}
_size_lock = threading.Lock()


def _du(path: str, exclude: Sequence[str] = ()) -> Optional[int]:
    """Bytes on disk under ``path`` (``du -sk -x``: what removing it frees),
    leaving out the folders in ``exclude`` (other items nested inside it);
    a walk's apparent size where there is no ``du``. None past
    :data:`SIZE_TIMEOUT`."""
    return du_total([path], exclude)


def du_total(paths: Sequence[str], exclude: Sequence[str] = ()) -> Optional[int]:
    """Bytes on disk of ``paths`` together, each file counted ONCE however
    many of them hard-link it (uv and pnpm hard-link their caches into every
    checkout, so a per-folder sum counts the same bytes many times over), and
    without ``exclude``. None when it can't be measured in time."""
    paths = [p for p in paths if p and os.path.isdir(p)]
    if not paths:
        return 0
    if os.name != "nt" and shutil.which("du"):
        argv = ["du", "-sck", "-x"]
        argv += ["--exclude=" + e for e in exclude]
        try:
            cp = subprocess.run(
                argv + ["--"] + list(paths),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=SIZE_TIMEOUT * max(1, min(len(paths), 10)),
                text=True,
            )
            # du exits 1 on an unreadable entry but still prints the total.
            last = (cp.stdout or "").strip().splitlines()[-1:]
            first = last[0].split("\t", 1)[0].strip() if last else ""
            if first.isdigit():
                return int(first) * 1024
        except (OSError, subprocess.SubprocessError):
            pass
    skip = {norm(e) for e in exclude}
    seen: Set[tuple] = set()
    total = 0
    deadline = time.monotonic() + SIZE_TIMEOUT
    for top in paths:
        for root, dirs, files in os.walk(top):
            if time.monotonic() > deadline:
                return None
            dirs[:] = [d for d in dirs if norm(os.path.join(root, d)) not in skip]
            for name in files:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
    return total


def size_of(
    path: str,
    stamp: float,
    *,
    now: Optional[float] = None,
    exclude: Sequence[str] = (),
) -> Optional[int]:
    """:func:`_du` of the item's OWN folder (nested items left out), cached
    until it shows newer use (``stamp``) or :data:`SIZE_TTL_S` passes."""
    now = time.time() if now is None else now
    key = (norm(path), tuple(sorted(exclude)))
    with _size_lock:
        hit = _size_cache.get(key)
    if hit and hit[0] == stamp and now - hit[1] < SIZE_TTL_S:
        return hit[2]
    n = _du(path, exclude)
    if n is not None:
        with _size_lock:
            _size_cache[key] = (stamp, now, n)
    return n


def _freeable(path: str, exclude: Sequence[str] = ()) -> Optional[int]:
    """Bytes removing ``path`` would actually give back: files no other path
    hard-links (link count 1), leaving out ``exclude``. A checkout's ``.venv``
    is mostly hard links into uv's cache — 8.5 of 10.1 GB in one measured
    worktree — and those blocks stay allocated for as long as the cache
    holds them, so ``du`` alone promises space a removal never frees. None
    when it can't be measured in time."""
    if not os.path.isdir(path):
        return 0
    if os.name != "nt" and shutil.which("find"):
        argv = ["find", path, "-xdev"]
        for e in exclude:
            argv += ["-path", e, "-prune", "-o"]
        argv += ["-type", "f", "-links", "1", "-printf", "%b\n"]
        try:
            cp = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=SIZE_TIMEOUT,
                text=True,
            )
            total = 0
            for line in cp.stdout.splitlines():
                if line.isdigit():
                    total += int(line) * 512
            return total
        except (OSError, subprocess.SubprocessError):
            pass
    skip = {norm(e) for e in exclude}
    total = 0
    deadline = time.monotonic() + SIZE_TIMEOUT
    for root, dirs, files in os.walk(path):
        if time.monotonic() > deadline:
            return None
        dirs[:] = [d for d in dirs if norm(os.path.join(root, d)) not in skip]
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if st.st_nlink == 1 and not os.path.islink(os.path.join(root, name)):
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total


_free_cache: Dict[tuple, tuple] = {}


def free_of(
    path: str,
    stamp: float,
    *,
    now: Optional[float] = None,
    exclude: Sequence[str] = (),
) -> Optional[int]:
    """:func:`_freeable`, cached like :func:`size_of`."""
    now = time.time() if now is None else now
    key = (norm(path), tuple(sorted(exclude)))
    with _size_lock:
        hit = _free_cache.get(key)
    if hit and hit[0] == stamp and now - hit[1] < SIZE_TTL_S:
        return hit[2]
    n = _freeable(path, exclude)
    if n is not None:
        with _size_lock:
            _free_cache[key] = (stamp, now, n)
    return n


# ---------------------------------------------------------------- inventory


class Usage:
    """What the caller knows about who uses what (the server builds it from
    its engine and stores; the CLI, offline, from the files alone).

    * ``in_use`` — normalized path → why it is in use ("session 'foo'
      (running)", "route 'x' (flying)").
    * ``last_used`` — normalized path → a time of last use the folder itself
      wouldn't show (a session's last update, its close time).
    * ``repos`` — repositories known to have MindFlock worktrees.
    * ``branch_info`` — normalized path → ``{"title", "branch", "base",
      "existing_branch"}`` from the session records.
    * ``clone_roots`` — the provisioning workspace dirs (session clones).
    * ``pr_merged`` — ``(item) -> bool``: is this branch's PR merged at this
      HEAD. Asked only about an item nothing else keeps; None = never asked.
    """

    def __init__(
        self,
        in_use: Optional[Dict[str, str]] = None,
        last_used: Optional[Dict[str, float]] = None,
        repos: Optional[Iterable[str]] = None,
        branch_info: Optional[Dict[str, dict]] = None,
        clone_roots: Optional[Iterable[str]] = None,
        pr_merged: Optional[Callable[[dict], bool]] = None,
    ) -> None:
        self.in_use: Dict[str, str] = {}
        for k, v in (in_use or {}).items():
            self.use(k, v)
        self.last_used: Dict[str, float] = {}
        for k, v in (last_used or {}).items():
            self.touched(k, v)
        self.repos: Set[str] = {norm(r) for r in (repos or ()) if r}
        self.branch_info = {norm(k): v for k, v in (branch_info or {}).items() if k}
        self.clone_roots = [norm(r) for r in (clone_roots or ()) if r]
        self.pr_merged = pr_merged

    def use(self, path: str, why: str) -> None:
        if path and why:
            self.in_use.setdefault(norm(path), why)

    def touched(self, path: str, ts: float) -> None:
        if path and ts:
            k = norm(path)
            self.last_used[k] = max(self.last_used.get(k, 0.0), float(ts))


def _folders_under(root: str, depth: int = 6) -> List[str]:
    """Folders under ``root`` that are worktrees (hold a ``.git``) and leaf
    folders that aren't (orphans), descending at most ``depth`` levels and
    never into a worktree (its insides are not ours). A folder of folders is
    a branch-slug level and is walked; an empty one is skipped."""
    out: List[str] = []

    def walk(d: str, level: int) -> None:
        try:
            entries = sorted(
                (e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)),
                key=lambda e: e.name,
            )
        except OSError:
            return
        for e in entries:
            if os.path.lexists(os.path.join(e.path, ".git")) or _LEAF.search(e.name):
                # A worktree, or what is left of one (``<slug>_<hex>`` is the
                # name MindFlock gives a worktree folder; a branch-slug level
                # never has it): one item, never its insides.
                out.append(e.path)
                continue
            try:
                kids = list(os.scandir(e.path))
            except OSError:
                continue
            if not kids:
                continue
            only_dirs = all(k.is_dir(follow_symlinks=False) for k in kids)
            if only_dirs and level < depth:
                walk(e.path, level + 1)
            else:
                out.append(e.path)

    if root and os.path.isdir(root):
        walk(root, 1)
    return out


#: The ``_<hex unixnano>`` suffix of a MindFlock worktree folder
#: (``resolve_worktree_paths``).
_LEAF = re.compile(r"_[0-9a-f]{12,}$")


def _is_infra_clone(name: str) -> bool:
    """A base clone (``_base_<slug>``) or a cache refresher: shared, never an
    item."""
    try:
        from backend.session.provisioned import is_base_repo_dirname

        if is_base_repo_dirname(name):
            return True
    except Exception:  # noqa: BLE001
        if name.startswith("_base_"):
            return True
    try:
        from backend.workspace_setup import is_refresher_dirname

        return bool(is_refresher_dirname(name))
    except Exception:  # noqa: BLE001
        return False


def _record(path: str, kind: str, repo: str = "", **kw) -> dict:
    rec = {
        "path": norm(path),
        "kind": kind,
        "repo": repo,
        "branch": "",
        "head": "",
        "detached": False,
        "locked": False,
        "prunable": False,
    }
    rec.update(kw)
    return rec


def discover(usage: Usage, root: Optional[str] = None) -> List[dict]:
    """Raw records of every item to classify (see the module docstring)."""
    root = worktrees_root() if root is None else root
    records = load_records()
    folders = _folders_under(root)
    repos: Set[str] = set(usage.repos)
    for rec in records.values():
        if rec.get("repo"):
            repos.add(norm(rec["repo"]))
    for f in folders:
        r = main_repo_of(f)
        if r:
            repos.add(r)
    seen: Set[str] = set()
    out: List[dict] = []
    for repo in sorted(repos):
        if not os.path.isdir(repo):
            continue
        listing = gitutil.worktree_list(repo, timeout=GIT_TIMEOUT)
        if not listing:
            continue
        main = norm(str(listing[0].get("path") or repo))
        seen.add(main)  # a repository's main checkout is never an item
        for wt in listing[1:]:
            p = norm(str(wt.get("path") or ""))
            if not p or p in seen or wt.get("bare"):
                continue
            seen.add(p)
            branch = str(wt.get("branch") or "")
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/") :]
            out.append(
                _record(
                    p,
                    KIND_WORKTREE,
                    main,
                    branch=branch,
                    head=str(wt.get("head") or ""),
                    detached=bool(wt.get("detached")),
                    locked=bool(wt.get("locked")),
                    prunable=bool(wt.get("prunable")) or not os.path.isdir(p),
                )
            )
    for f in folders:
        p = norm(f)
        if p in seen:
            continue
        seen.add(p)
        out.append(_record(p, KIND_ORPHAN, main_repo_of(f)))
    for croot in usage.clone_roots:
        if not os.path.isdir(croot) or norm(croot) == norm(root):
            continue
        try:
            children = sorted(os.scandir(croot), key=lambda e: e.name)
        except OSError:
            continue
        for e in children:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            p = norm(e.path)
            if p in seen or strictly_under(p, root) or _is_infra_clone(e.name):
                continue
            if os.path.isdir(os.path.join(e.path, ".git")):
                seen.add(p)
                out.append(_record(p, KIND_CLONE, p))
            elif e.name.endswith(".clone-tmp"):
                seen.add(p)
                out.append(_record(p, KIND_ORPHAN, ""))
    return out


def _ago(seconds: float) -> str:
    # A minute of slack: a timestamp round-tripped through ISO text comes
    # back a hair short, and "47 hours ago" for two days reads as a bug.
    d = (seconds + 60) / DAY
    if d >= 2:
        return "%d days ago" % int(d)
    h = seconds / 3600
    if h >= 2:
        return "%d hours ago" % int(h)
    return "just now" if seconds < 120 else "%d minutes ago" % int(seconds / 60)


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def is_ours(path: str, root: str, usage: Usage, records: Dict[str, dict]) -> bool:
    p = norm(path)
    return (
        strictly_under(p, root)
        or any(strictly_under(p, c) for c in usage.clone_roots)
        or p in records
    )


def classify(
    rec: dict,
    usage: Usage,
    *,
    after_days: float = DEFAULT_AFTER_DAYS,
    now: Optional[float] = None,
    root: Optional[str] = None,
    records: Optional[Dict[str, dict]] = None,
    measure: bool = True,
    nested: Sequence[str] = (),
) -> dict:
    """One inventory item: the raw record plus what it holds and whether it
    is safe to collect; ``kept`` lists every reason it is not. A probe that
    fails becomes a "couldn't check" reason, which keeps the item.
    ``nested`` = other items inside this one's folder: they are left out of
    its size, and they keep it (removing it would take them along)."""
    now = time.time() if now is None else now
    root = worktrees_root() if root is None else root
    records = load_records() if records is None else records
    path = rec["path"]
    kind = rec.get("kind") or KIND_WORKTREE
    created = records.get(path) or {}
    info = usage.branch_info.get(path) or {}
    ours = is_ours(path, root, usage, records)
    item = dict(rec)
    item.update(
        id=item_id(path),
        name=(
            os.path.relpath(path, root)
            if strictly_under(path, root)
            else os.path.basename(path)
        ),
        ours=ours,
        in_use=usage.in_use.get(path, ""),
        session=info.get("title") or created.get("title") or "",
        tracked_changes=0,
        untracked_files=0,
        unsaved_commits=0,
        stashes=0,
        merged="",
        merged_into="",
        last_used=0.0,
        size_bytes=None,
        free_bytes=None,
        kept=[],
        safe=False,
        state=UNKNOWN,
    )
    kept: List[str] = item["kept"]
    if not ours:
        kept.append("not created by MindFlock")
    if item["in_use"]:
        kept.append(item["in_use"])
    if rec.get("locked"):
        kept.append("locked in git")
    if nested:
        kept.append(
            "contains %s inside it (%s)"
            % (
                _plural(len(nested), "other worktree or checkout"),
                ", ".join(os.path.relpath(n, path) for n in list(nested)[:3])
                + ("…" if len(nested) > 3 else ""),
            )
        )

    if not os.path.isdir(path):
        item["state"] = MISSING
        kept.append("its folder is already gone; only git's record of it is left")
        return item

    last = max(
        last_activity(path),
        usage.last_used.get(path, 0.0),
        float(created.get("ts") or 0.0),
    )
    item["last_used"] = last
    item["nested"] = list(nested)
    if measure:
        item["size_bytes"] = size_of(path, last, now=now, exclude=nested)
        item["free_bytes"] = free_of(path, last, now=now, exclude=nested)

    def _state(holds: bool, recent: bool) -> str:
        if item["in_use"]:
            return IN_USE
        if not ours:
            return NOT_OURS
        if holds or rec.get("locked"):
            return HOLDS_WORK
        if recent:
            return RECENT
        return SAFE if not kept else UNKNOWN

    if kind == KIND_ORPHAN:
        repo = rec.get("repo") or ""
        if repo and os.path.isdir(repo):
            kept.append(
                "git no longer lists it as a worktree of "
                + repo
                + ", so its files can't be checked"
            )
        else:
            kept.append(
                "not a working git checkout (its repository is gone), so its files can't be checked"
            )
        item["state"] = _state(True, False)
        return item

    holds = False
    sc = status_counts(path)
    if sc is None:
        kept.append("couldn't read git status")
        holds = True
    else:
        item["tracked_changes"] = sc["tracked"]
        item["untracked_files"] = sc["untracked"]
        if sc["tracked"]:
            kept.append(_plural(sc["tracked"], "uncommitted change"))
            holds = True
        if sc["untracked"]:
            kept.append(_plural(sc["untracked"], "untracked file"))
            holds = True

    unsaved_at = -1
    if kind == KIND_CLONE:
        branch = _out(path, "symbolic-ref", "--quiet", "--short", "HEAD") or ""
        item["branch"] = branch
        item["head"] = _out(path, "rev-parse", "HEAD") or ""
        remotes = _out(path, "remote")
        if remotes is None:
            kept.append("couldn't list its remotes")
            holds = True
        else:
            n = (
                _count(path, "--branches", "--tags", "--not", "--remotes")
                if remotes
                else _count(path, "--branches")
            )
            if n is None:
                kept.append("couldn't tell whether its commits are pushed")
                holds = True
            elif n:
                item["unsaved_commits"] = n
                kept.append(
                    _plural(n, "commit")
                    + " on no remote"
                    + ("" if remotes else " (it has no remote)")
                )
                holds = True
        stash = _out(path, "stash", "list")
        if stash is None:
            kept.append("couldn't read its stash")
            holds = True
        elif stash:
            item["stashes"] = len(stash.splitlines())
            kept.append(
                _plural(item["stashes"], "stash entry").replace("entrys", "entries")
            )
            holds = True
    else:
        repo = rec.get("repo") or path
        head = rec.get("head") or ""
        bases = base_refs(repo, info.get("base") or created.get("base") or "")
        if bases:
            item["merged_into"] = bases[0].rsplit("/", 1)[-1]
        if not head:
            kept.append("couldn't read its HEAD")
            holds = True
        else:
            how = merged_into(repo, head, bases)
            item["merged"] = how
            if not how:
                n = _count(repo, head, "--not", *bases, "--remotes")
                if n is None:
                    kept.append("couldn't tell whether its commits are saved elsewhere")
                    holds = True
                elif n:
                    item["unsaved_commits"] = n
                    unsaved_at = len(kept)
                    kept.append(
                        _plural(n, "commit") + " in no base branch and on no remote"
                    )
                    holds = True

    recent = bool(last) and now - last < after_days * DAY
    if recent:
        kept.append("used " + _ago(now - last))

    # The PR rung: asked only when unpushed commits are the ONE thing keeping
    # an otherwise collectable worktree (a squash-merged PR whose remote
    # branch was deleted and pruned, with the base moved on since).
    if (
        unsaved_at >= 0
        and len(kept) == 1
        and usage.pr_merged is not None
        and item.get("branch")
    ):
        try:
            if usage.pr_merged(item):
                kept.pop(unsaved_at)
                item["merged"] = "pr"
                item["unsaved_commits"] = 0
                holds = False
        except Exception:  # noqa: BLE001 — unknown is "not merged"
            pass

    item["safe"] = not kept and ours and kind in (KIND_WORKTREE, KIND_CLONE)
    item["state"] = _state(holds, recent)
    return item


def inventory(
    usage: Usage,
    *,
    after_days: float = DEFAULT_AFTER_DAYS,
    now: Optional[float] = None,
    root: Optional[str] = None,
    measure: bool = True,
) -> List[dict]:
    """Every item, classified (concurrently). Blocking: run it off the loop."""
    root = worktrees_root() if root is None else root
    records = load_records()
    recs = discover(usage, root)
    inner = nesting([r["path"] for r in recs])

    def one(rec: dict) -> dict:
        try:
            return classify(
                rec,
                usage,
                after_days=after_days,
                now=now,
                root=root,
                records=records,
                measure=measure,
                nested=inner.get(rec["path"], ()),
            )
        except Exception as exc:  # noqa: BLE001 — one item never stops the scan
            out = dict(rec)
            out.update(
                id=item_id(rec["path"]),
                name=os.path.basename(rec["path"]),
                ours=False,
                in_use="",
                kept=["couldn't check: %s" % exc],
                safe=False,
                state=UNKNOWN,
                size_bytes=None,
                last_used=0.0,
            )
            return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        items = list(pool.map(one, recs))
    order = {
        SAFE: 0,
        RECENT: 1,
        HOLDS_WORK: 2,
        MISSING: 3,
        UNKNOWN: 4,
        IN_USE: 5,
        NOT_OURS: 6,
    }
    items.sort(
        key=lambda i: (order.get(i.get("state"), 9), -(i.get("size_bytes") or 0))
    )
    return items


def nesting(paths: Iterable[str]) -> Dict[str, List[str]]:
    """For each path, the other paths strictly inside it (a worktree made
    inside another checkout, an agent's ``.claude/worktrees/…``)."""
    ps = sorted({norm(p) for p in paths if p})
    out: Dict[str, List[str]] = {}
    for p in ps:
        kids = [q for q in ps if strictly_under(q, p)]
        if kids:
            out[p] = kids
    return out


def _outermost(paths: Iterable[str]) -> List[str]:
    ps = sorted({norm(p) for p in paths if p})
    return [p for p in ps if not any(strictly_under(p, q) for q in ps if q != p)]


def measure_totals(items: Sequence[dict]) -> Dict[str, Optional[int]]:
    """The on-disk headline numbers, measured as ONE ``du`` each so a file is
    counted once however many items hard-link it or contain it: what
    MindFlock made (``bytes``) and what it did not (``other_bytes``). What
    collecting would FREE is the safe items' ``free_bytes`` (see
    :func:`summarize`). None for a number that couldn't be measured in time."""

    def present(pred):
        return [
            i["path"] for i in items if pred(i) and os.path.isdir(i.get("path") or "")
        ]

    ours = present(lambda i: i.get("ours"))
    others = present(lambda i: not i.get("ours"))
    return {
        "bytes": du_total(_outermost(ours)) if ours else 0,
        "other_bytes": du_total(_outermost(others)) if others else 0,
    }


def summarize(items: Sequence[dict], totals: Optional[dict] = None) -> dict:
    """Counts and sizes. ``totals`` (:func:`measure_totals`) gives the true
    byte numbers; without it they are a sum of the items' own sizes, nested
    items included once (each item's size already leaves its nested ones
    out), which over-counts only shared hard links."""

    def total(xs):
        return sum(int(x.get("size_bytes") or 0) for x in xs)

    def freed(xs):
        return sum(
            int(
                x["free_bytes"]
                if x.get("free_bytes") is not None
                else x.get("size_bytes") or 0
            )
            for x in xs
        )

    safe = [i for i in items if i.get("safe")]
    ours = [i for i in items if i.get("ours")]
    others = [i for i in items if not i.get("ours")]
    by_state: Dict[str, int] = {}
    for i in items:
        s = i.get("state") or UNKNOWN
        by_state[s] = by_state.get(s, 0) + 1
    # ``count`` / ``bytes``: what MindFlock made (the headline). Worktrees
    # it did not make are listed but never removed, so they are counted on
    # their own (``other_*``), never in the number a person reads as "mine".
    out = {
        "count": len(ours),
        "bytes": total(ours),
        "other_count": len(others),
        "other_bytes": total(others),
        "safe_count": len(safe),
        # What collecting the safe set gives back: files nothing else
        # hard-links (an item's free_bytes), never shared cache blocks.
        "safe_bytes": freed(safe),
        "by_state": by_state,
    }
    for k in ("bytes", "other_bytes", "safe_bytes"):
        if totals and totals.get(k) is not None:
            out[k] = int(totals[k])
    return out


# ----------------------------------------------------------------- removing


def branch_deletable(
    item: dict, usage: Optional[Usage] = None, records: Optional[Dict[str, dict]] = None
) -> bool:
    """Delete the local branch only when it is merged (not merely pushed),
    MindFlock cut it (a branch the person brought is never deleted), and it
    is not a base branch. Unsure (no record either way) answers False."""
    if item.get("kind") != KIND_WORKTREE:
        return False
    branch = item.get("branch") or ""
    if not branch or item.get("detached") or not item.get("merged"):
        return False
    if branch in _PROTECTED_BRANCHES or branch == item.get("merged_into"):
        return False
    usage = usage or Usage()
    records = load_records() if records is None else records
    path = norm(item["path"])
    created = records.get(path) or {}
    info = usage.branch_info.get(path) or {}
    if not created and not info:
        return False
    for src in (created, info):
        if src.get("existing_branch"):
            return False
        if src.get("branch") and src["branch"] != branch:
            return False
    return True


def _delete_branch(repo: str, branch: str) -> bool:
    if gitutil.worktree_for_branch(repo, branch, timeout=GIT_TIMEOUT):
        return False
    # -D, not -d: -d knows only ancestry, and a squash-merged branch (the
    # case this exists for) is never an ancestor. branch_deletable already
    # established merged-ness the stricter way.
    return _git(repo, "branch", "-D", "--", branch).returncode == 0


def _remove_worktree(repo: str, path: str, force: bool = False) -> str:
    """``git worktree remove``; "" on success, else git's reason."""
    args = ["worktree", "remove"] + (["--force"] if force else []) + [path]
    cp = _git(repo, *args, timeout=600)
    if cp.returncode == 0:
        return ""
    return (cp.stderr or cp.stdout or "git worktree remove failed").strip()


def _remove_folder(item: dict, root: str, usage: Usage, *, allow_orphan: bool) -> str:
    """rmtree a clone (or, on request, an orphan) — only strictly under a
    root MindFlock owns, never a mount point, never a folder whose ``.git`` is
    a live linked worktree. "" on success, else why not."""
    path = norm(item["path"])
    roots = [root] + list(usage.clone_roots)
    if not any(strictly_under(path, r) for r in roots):
        return "it is outside MindFlock's own folders"
    if os.path.ismount(path):
        return "it is a mount point"
    if item.get("kind") == KIND_CLONE:
        if not os.path.isdir(os.path.join(path, ".git")):
            return "it is no longer a clone"
    elif not allow_orphan:
        return "its files can't be checked"
    else:
        gd = gitdir_of(path)
        if gd and os.path.isdir(gd):
            return "git still knows it as a worktree"
    shutil.rmtree(path, ignore_errors=True)
    return "" if not os.path.exists(path) else "some files could not be deleted"


def nested_checkout(path: str, limit_s: float = SIZE_TIMEOUT) -> str:
    """The first git checkout INSIDE ``path`` (a ``.git`` below its top), ""
    when there is none, or ``"?"`` when the walk ran out of time. Removing a
    worktree takes everything git ignores along with it, and an agent's own
    worktree under an ignored ``.claude/worktrees/`` is exactly such a thing."""
    deadline = time.monotonic() + limit_s
    for root, dirs, files in os.walk(path):
        if time.monotonic() > deadline:
            return "?"
        if root != path and (".git" in dirs or ".git" in files):
            return root
        dirs[:] = [d for d in dirs if d != ".git"]
    return ""


def _nested_refusal(path: str) -> str:
    inner = nested_checkout(path)
    if inner == "?":
        return "couldn't finish checking it for checkouts inside it"
    if inner:
        return "it has another git checkout inside it (%s)" % inner
    return ""


def _remove(
    item: dict, root: str, usage: Usage, *, force: bool, allow_orphan: bool = False
) -> str:
    kind = item.get("kind")
    if os.path.isdir(item["path"]):
        # Never take another checkout down with this one (git deletes what
        # it ignores along with the worktree).
        refusal = _nested_refusal(item["path"])
        if refusal:
            return "Not removed: " + refusal + "."
    if kind == KIND_WORKTREE:
        return _remove_worktree(item["repo"], item["path"], force=force)
    return _remove_folder(item, root, usage, allow_orphan=allow_orphan)


def collect(
    items: Sequence[dict],
    recheck: Callable[[dict], dict],
    *,
    usage: Optional[Usage] = None,
    root: Optional[str] = None,
    limit: int = MAX_PER_PASS,
    after_remove: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Remove the safe items (at most ``limit``), each re-classified by
    ``recheck`` right before, so a session that started on one since the
    scan keeps it. Returns ``{"removed", "skipped", "branches_deleted",
    "freed_bytes"}``."""
    usage = usage or Usage()
    root = worktrees_root() if root is None else root
    records = load_records()
    removed: List[dict] = []
    skipped: List[dict] = []
    branches: List[str] = []
    freed = 0
    for it in items:
        if len(removed) >= limit:
            break
        if not it.get("safe"):
            continue
        try:
            fresh = recheck(it)
        except Exception as exc:  # noqa: BLE001
            skipped.append(
                {"path": it["path"], "reason": "couldn't re-check: %s" % exc}
            )
            continue
        if not fresh.get("safe"):
            skipped.append(
                {
                    "path": it["path"],
                    "reason": "; ".join(fresh.get("kept") or []) or "no longer safe",
                }
            )
            continue
        err = _remove(fresh, root, usage, force=False)
        if err:
            skipped.append({"path": it["path"], "reason": err})
            continue
        size = next(
            (
                v
                for v in (
                    fresh.get("free_bytes"),
                    it.get("free_bytes"),
                    fresh.get("size_bytes"),
                    it.get("size_bytes"),
                )
                if v is not None
            ),
            None,
        )
        removed.append(
            {
                "path": fresh["path"],
                "kind": fresh.get("kind"),
                "branch": fresh.get("branch") or "",
                "merged": fresh.get("merged") or "",
                "freed_bytes": size,
            }
        )
        freed += int(size or 0)
        if branch_deletable(fresh, usage, records) and _delete_branch(
            fresh["repo"], fresh["branch"]
        ):
            branches.append(fresh["branch"])
        if after_remove is not None:
            try:
                after_remove(fresh)
            except Exception:  # noqa: BLE001
                pass
    forget_records(r["path"] for r in removed)
    return {
        "removed": removed,
        "skipped": skipped,
        "branches_deleted": branches,
        "freed_bytes": freed,
    }


def save_work(item: dict, now: Optional[float] = None) -> dict:
    """Save what removing ``item`` would lose into a NEW folder under
    :func:`kept_root`: ``changes.patch`` (tracked + untracked, binary-safe;
    ``git apply`` it), ``commits.bundle`` for a clone's unpushed commits and
    stash, a ``mindflock-kept/…`` branch for a detached HEAD's unmerged
    commits, and ``info.json``. Returns ``{"dir", "patch", "bundle",
    "kept_branch"}``. Raises RuntimeError/OSError when something could not be
    saved — the caller then removes nothing."""
    from backend.session.git.diff import scratch_index_env

    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = os.path.basename(item["path"]) or item_id(item["path"])
    dest = os.path.join(kept_root(), "%s_%s" % (stamp, base))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(kept_root(), "%s_%s-%d" % (stamp, base, n))
    os.makedirs(dest)
    path = item["path"]
    saved = {"dir": dest, "patch": "", "bundle": "", "kept_branch": ""}
    if item.get("tracked_changes") or item.get("untracked_files"):
        try:
            with scratch_index_env(path) as env:
                cp = gitutil.git(
                    ["diff", "HEAD", "--binary", "--no-color", "--no-ext-diff"],
                    path,
                    timeout=600,
                    env=env,
                    text=False,
                    stdin=subprocess.DEVNULL,
                )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("couldn't save its uncommitted changes: %s" % exc)
        if cp.returncode != 0 or not cp.stdout:
            raise RuntimeError(
                "couldn't save its uncommitted changes (git diff failed)"
            )
        patch = os.path.join(dest, "changes.patch")
        with open(patch, "wb") as f:
            f.write(cp.stdout)
        saved["patch"] = patch
    if item.get("kind") == KIND_CLONE and (
        item.get("unsaved_commits") or item.get("stashes")
    ):
        bundle = os.path.join(dest, "commits.bundle")
        refs = ["--branches", "--tags"]
        if item.get("stashes"):
            refs.append("refs/stash")
        remotes = _out(path, "remote")
        if remotes:
            refs += ["--not", "--remotes"]
        cp = _git(path, "bundle", "create", bundle, *refs, timeout=600)
        if cp.returncode != 0 or not os.path.isfile(bundle):
            raise RuntimeError("couldn't bundle its unpushed commits")
        saved["bundle"] = bundle
    if (
        item.get("kind") == KIND_WORKTREE
        and item.get("detached")
        and item.get("unsaved_commits")
        and item.get("head")
    ):
        ref = "mindflock-kept/%s-%s" % (base, stamp)
        cp = _git(item["repo"], "branch", "--", ref, item["head"])
        if cp.returncode != 0:
            raise RuntimeError("couldn't keep its detached commits on a branch")
        saved["kept_branch"] = ref
    info = {
        "path": path,
        "kind": item.get("kind"),
        "repo": item.get("repo"),
        "branch": item.get("branch"),
        "head": item.get("head"),
        "kept_branch": saved["kept_branch"],
        "removed_at": now,
        "why_it_was_kept": item.get("kept"),
        "restore": (
            "Check out %s again, then: git apply --index %s"
            % (item.get("branch") or item.get("head"), saved["patch"])
            if saved["patch"]
            else ""
        ),
    }
    with open(os.path.join(dest, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    return saved


def remove_one(
    item: dict,
    *,
    confirmed: bool,
    usage: Optional[Usage] = None,
    root: Optional[str] = None,
    now: Optional[float] = None,
    after_remove: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Remove one item on a person's explicit request. ``item`` must be a
    FRESH classification. A safe one goes as :func:`collect` would; anything
    else needs ``confirmed`` and has its work saved first
    (:func:`save_work`). Never: a worktree a session uses, a locked one.
    Returns ``{"ok", "error"?, "needs_confirm"?, "saved"?, "branch_deleted"?}``."""
    usage = usage or Usage()
    root = worktrees_root() if root is None else root
    if item.get("in_use"):
        return {
            "ok": False,
            "error": "It is in use: %s. Close that first." % item["in_use"],
        }
    if item.get("locked"):
        return {
            "ok": False,
            "error": "It is locked in git; unlock it first (git worktree unlock).",
        }
    if item.get("state") == MISSING:
        if item.get("kind") != KIND_WORKTREE or not item.get("repo"):
            return {"ok": True}
        # Forget THIS record only: `git worktree remove` of a folder that is
        # gone drops its admin entry, where `prune` would sweep every other
        # stale one in the repo too (one on an unmounted drive, say).
        err = _remove_worktree(item["repo"], item["path"])
        if not err:
            forget_records([item["path"]])
        return {"ok": not err, "error": err}
    if not item.get("safe") and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "error": "It holds work or isn't MindFlock's: %s."
            % "; ".join(item.get("kept") or []),
        }
    records = load_records()
    saved: Optional[dict] = None
    if item.get("safe"):
        err = _remove(item, root, usage, force=False)
    elif item.get("kind") == KIND_ORPHAN:
        err = _remove(item, root, usage, force=True, allow_orphan=True)
    else:
        try:
            saved = save_work(item, now=now)
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": "Nothing was removed: %s" % exc}
        err = _remove(item, root, usage, force=True)
    if err:
        out = {"ok": False, "error": err}
        if saved:
            out["saved"] = saved
        return out
    deleted = branch_deletable(item, usage, records) and _delete_branch(
        item["repo"], item["branch"]
    )
    forget_records([item["path"]])
    if after_remove is not None:
        try:
            after_remove(item)
        except Exception:  # noqa: BLE001
            pass
    out = {"ok": True, "branch_deleted": item["branch"] if deleted else ""}
    if saved:
        out["saved"] = saved
    return out
