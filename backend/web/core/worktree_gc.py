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
base would change nothing — a squash or rebase merge, and the squashed-root
case (every file the branch has is in the base as is). Two rungs answer
that: :func:`merged_into` against each base's tip, and, only for a worktree
whose unmerged commits are the one thing keeping it, :func:`landed` at the
commit of the base's history where the work landed (a squash merge whose
base has since touched the same lines — on a repo with a CHANGELOG, every
squash-merged branch). Both decide it with reads only (``diff --raw``,
``ls-tree``, ``cat-file`` and ``git merge-file`` on temp copies; see "the
read-only merge test"). Last, a branch whose PR is merged at this very HEAD
(``Usage.pr_merged``, asked only about an item that nothing else keeps).
A test that conflicts, fails or reads oddly answers "not merged": when
unsure, keep.

**Fresh bases** (:func:`refresh_bases`): before a scan classifies, each
involved repository's ``origin/<base>`` refs are fetched once (throttled,
bounded, no prompt; a failure keeps what is there and is said on the items
it may affect). The only write a scan makes in anyone's repository.

**Sizes** (:func:`measure_sizes`) come after the classification, so the
list never waits on them: one walk per item, kept in the state dir until the
folder changes (:class:`SizeCache`), and hard-link honest headline totals
(:func:`measure_totals`) from the same walks.

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

import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zlib
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
#: Size walk ceiling per folder; past it the size is unknown (never blocks).
SIZE_TIMEOUT = 120
#: A measured size is reused this long if the folder shows no change
#: (:func:`size_key`); after it the folder is walked again.
SIZE_TTL_S = 12 * 3600.0
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


def _git(
    cwd: str,
    *args: str,
    env=None,
    timeout: float = GIT_TIMEOUT,
    text=True,
    input: Optional[bytes] = None,
):
    full = dict(READ_ONLY_ENV)
    if env:
        full.update(env)
    return gitutil.git(
        ["--no-optional-locks", *args],
        cwd,
        timeout=timeout,
        env=full,
        text=text,
        input=input,
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


#: The usual integration branches: a repo that ships through `staging`
#: (sitecheck does) squash-merges into it, not into main.
_BASE_NAMES = ("main", "master", "staging", "develop", "development")


def _base_names(repo: str, hint: str = "") -> List[str]:
    """Candidate base branch NAMES for ``repo``: the recorded base
    (``hint``), what ``origin/HEAD`` points at, then :data:`_BASE_NAMES`."""
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
    for n in _BASE_NAMES:
        if n not in names:
            names.append(n)
    return names


def _existing_refs(repo: str, refs: Sequence[str]) -> Optional[Set[str]]:
    """Which of ``refs`` (full names) exist, in ONE ``for-each-ref``; None
    when git can't say."""
    if not refs:
        return set()
    cp = _git(repo, "for-each-ref", "--format=%(refname)", *refs)
    if cp.returncode != 0:
        return None
    return {ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()}


def base_refs(repo: str, hint: str = "") -> List[str]:
    """The base branches work in ``repo`` merges into, as refs that exist:
    the recorded base (``hint``), what ``origin/HEAD`` points at, and local
    ``main`` / ``master`` / ``staging`` / ``develop``, each with its
    ``origin/`` counterpart. Content found in any of them is content a remote
    (or the repo's own integration branch) already has.

    Both sides on purpose: a local ``staging`` that lags ``origin/staging``
    (nobody pulled) must not hide work that already landed on the remote,
    and :func:`refresh_bases` keeps the ``origin/`` side current."""
    names = _base_names(repo, hint)
    want = [
        ref for n in names for ref in ("refs/heads/" + n, "refs/remotes/origin/" + n)
    ]
    have = _existing_refs(repo, want)
    out: List[str] = []
    for ref in want:
        if ref in out:
            continue
        if have is None:
            if not gitutil.ref_exists(repo, ref, timeout=GIT_TIMEOUT):
                continue
        elif ref not in have:
            continue
        out.append(ref)
    return out


# ------------------------------------------------------------ fresh bases
#
# The classifier compares a worktree with its repository's base branches as
# they are on disk. A remote-tracking ref nobody fetched for a day makes
# every PR merged since look unmerged (a base clone's ``origin/staging`` was
# 25 hours old on the owner's machine). So, once per repository per scan,
# :func:`refresh_bases` fetches exactly the base branches the classifier
# compares against — the ``refs/remotes/origin/<base>`` that already exist —
# and nothing else: no tags, no prune, no other branch, no FETCH_HEAD, no
# hooks, no auto-gc, never a local branch, an index or a working tree. A
# fetch that fails (offline, no credentials, no remote) changes nothing and
# the scan goes on with the refs it has, saying how old they are. Fresher
# refs can only show MORE work as already merged when it really is in the
# base; the merge tests themselves are unchanged.

#: A repository is fetched at most this often (the stamp is MindFlock's,
#: in its state dir, shared by the background pass, the HTTP listing and
#: the CLI).
FETCH_TTL_S = 15 * 60.0
#: One repository's base fetch at most this long.
FETCH_TIMEOUT = 20
#: A base not fetched for this long, that couldn't be refreshed, is said.
BASE_STALE_S = 3600.0
#: The throttle stamps, in the state dir.
FETCH_STATE_FILE = "worktree_gc_fetch.json"
#: The measured sizes, in the state dir (see :class:`SizeCache`).
SIZE_CACHE_FILE = "worktree_gc_sizes.json"
#: Test seam: where the stamp and size-cache files live (None = the config
#: dir).
STATE_DIR: Optional[str] = None

_fetch_lock = threading.Lock()


def state_dir() -> str:
    return STATE_DIR or config_dir()


def _fetch_env(repo: str) -> Dict[str, str]:
    """git that can't ask anybody anything: no terminal prompt, no askpass
    program (an empty ``GIT_ASKPASS`` turns it off), no GUI credential
    manager, ssh in batch mode unless the person set their own ssh command.
    Credential helpers (and MindFlock's GitHub sign-in) still answer."""
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
        "GCM_INTERACTIVE": "never",
    }
    if (
        not os.environ.get("GIT_SSH_COMMAND")
        and not os.environ.get("GIT_SSH")
        and not _out(repo, "config", "--get", "core.sshCommand")
    ):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
    try:
        from backend import git_auth_hints

        env = git_auth_hints.github_signin(env)
    except Exception:  # noqa: BLE001 — a sign-in overlay is optional
        pass
    return env


def _fetch_bases(repo: str, names: Sequence[str], timeout: float = FETCH_TIMEOUT):
    """THE network call: fetch ``names`` from ``origin`` into their
    ``refs/remotes/origin/`` refs and nothing else. ``(ok, error)``. The
    test suite guards this (tests/conftest.py): only repositories under
    pytest's temp dir are ever fetched."""
    refspecs = ["+refs/heads/%s:refs/remotes/origin/%s" % (n, n) for n in names]
    cp = gitutil.git(
        [
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "fetch.writeCommitGraph=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-prune",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--no-auto-maintenance",
            "origin",
            *refspecs,
        ],
        repo,
        timeout=timeout,
        env=_fetch_env(repo),
        stdin=subprocess.DEVNULL,
        new_session=True,
    )
    if cp.returncode == 0:
        return True, ""
    err = (cp.stderr or cp.stdout or "git fetch failed").strip().splitlines()
    return False, (err[-1] if err else "git fetch failed")[:200]


def _read_stamps() -> Dict[str, dict]:
    try:
        with open(os.path.join(state_dir(), FETCH_STATE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stamps(d: Dict[str, dict]) -> None:
    p = os.path.join(state_dir(), FETCH_STATE_FILE)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)
    except OSError:
        pass


def _common_dir(repo: str) -> str:
    out = _out(repo, "rev-parse", "--git-common-dir")
    if not out:
        return ""
    return os.path.normpath(out if os.path.isabs(out) else os.path.join(repo, out))


def last_fetched(
    repo: str, names: Sequence[str], stamp: Optional[dict] = None
) -> float:
    """When ``repo``'s base refs were last brought from the remote, as best
    the disk tells: the newest of FETCH_HEAD's mtime (any fetch writes it),
    the newest reflog entry of each ``origin/<base>`` and MindFlock's own
    stamp of a fetch that worked. 0 when nothing says."""
    best = float((stamp or {}).get("ok_at") or 0.0)
    common = _common_dir(repo)
    if common:
        try:
            best = max(best, os.path.getmtime(os.path.join(common, "FETCH_HEAD")))
        except OSError:
            pass
        for n in names:
            best = max(
                best,
                _reflog_last(
                    os.path.join(common, "logs", "refs", "remotes", "origin", n)
                ),
            )
    return best


def _fetchable(repo: str, hints: Iterable[str]) -> List[str]:
    """The base names whose ``refs/remotes/origin/<name>`` exists: exactly
    what the classifier compares against on the remote side."""
    names: List[str] = []
    for h in list(hints) or [""]:
        for n in _base_names(repo, h):
            if n not in names:
                names.append(n)
    have = _existing_refs(repo, ["refs/remotes/origin/" + n for n in names]) or set()
    return [n for n in names if "refs/remotes/origin/" + n in have]


def refresh_bases(
    repos: Dict[str, Set[str]],
    *,
    fetch: bool = True,
    now: Optional[float] = None,
    ttl: float = FETCH_TTL_S,
) -> Dict[str, dict]:
    """Bring each repository's base refs up to date, once (``repos`` = repo
    → the recorded base hints of its worktrees). Per repo: ``{"repo",
    "names", "status", "error", "last_fetched", "stale"}`` where status is
    ``fetched`` / ``failed`` / ``recent`` (fetched within ``ttl``, not
    again) / ``skipped`` (``fetch`` off) / ``no_remote`` / ``no_base``.
    ``stale`` = it couldn't be refreshed and is older than
    :data:`BASE_STALE_S`. Never raises."""
    now = time.time() if now is None else now
    out: Dict[str, dict] = {}
    with _fetch_lock:
        stamps = _read_stamps()
    changed = False

    def one(repo: str) -> dict:
        res = {
            "repo": repo,
            "names": [],
            "status": "no_base",
            "error": "",
            "last_fetched": 0.0,
            "stale": False,
        }
        try:
            names = _fetchable(repo, repos.get(repo) or ())
            res["names"] = names
            if not names:
                return res
            if not _out(repo, "config", "--get", "remote.origin.url"):
                res["status"] = "no_remote"
                return res
            st = stamps.get(repo) or {}
            if not fetch:
                res["status"] = "skipped"
            elif now - float(st.get("at") or 0) < ttl:
                res["status"] = "recent"
                res["error"] = st.get("error") or ""
            else:
                ok, err = _fetch_bases(repo, names)
                st = dict(st, at=now, error=err)
                if ok:
                    st["ok_at"] = now
                res["status"] = "fetched" if ok else "failed"
                res["error"] = err
                res["_stamp"] = st
            res["last_fetched"] = last_fetched(repo, names, st)
            fresh = res["status"] == "fetched" or (
                res["status"] == "recent" and not res["error"]
            )
            res["stale"] = not fresh and (
                not res["last_fetched"] or now - res["last_fetched"] > BASE_STALE_S
            )
        except Exception as exc:  # noqa: BLE001 — unknown is "as it was"
            res["status"] = "failed"
            res["error"] = str(exc)[:200]
            res["stale"] = True
        return res

    todo = sorted(r for r in repos if r and os.path.isdir(r))
    if todo:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(WORKERS, len(todo))
        ) as pool:
            for res in pool.map(one, todo):
                st = res.pop("_stamp", None)
                if st is not None:
                    stamps[res["repo"]] = st
                    changed = True
                out[res["repo"]] = res
    if changed:
        with _fetch_lock:
            merged = _read_stamps()
            for r, res in out.items():
                if r in stamps:
                    merged[r] = stamps[r]
            _write_stamps(merged)
    return out


def stale_note(base: Optional[dict], now: Optional[float] = None) -> str:
    """The words for a base that may be behind its remote, "" when it is
    current (or can't be behind: no remote)."""
    if not base or not base.get("stale"):
        return ""
    now = time.time() if now is None else now
    names = base.get("names") or []
    what = ("origin/" + names[0]) if len(names) == 1 else "its base branches"
    if base.get("status") == "skipped":
        why = "not refreshed (--no-fetch)"
    else:
        why = "couldn't be refreshed" + (
            ": " + base["error"] if base.get("error") else ""
        )
    if base.get("last_fetched"):
        return "%s was last fetched %s and %s, so it may be merged since" % (
            what,
            _ago(now - base["last_fetched"]),
            why,
        )
    return (
        "%s %s and when it was last fetched is unknown, so it may be merged since"
        % (
            what,
            "was " + why if why.startswith("not") else why,
        )
    )


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
    commit reachable), ``"content"`` (merging it into the base's tip would
    change nothing: a squash or rebase merge the base hasn't touched since,
    or a squashed history), or ``""`` (not shown to be merged — the answer
    whenever git can't tell). :func:`landed` goes further back in the base's
    history; it costs more, so the classifier asks it only when the verdict
    hangs on it."""
    if not head or not bases:
        return ""
    n = _count(repo, head, "--not", *bases)
    if n == 0:
        return "ancestor"
    if n is None:
        return ""
    seen: Set[str] = set()
    forks: Dict[str, Optional[list]] = {}
    with _MergeScratch() as scratch:
        for b in bases:
            tip = _out(repo, "rev-parse", "--verify", "--quiet", b + "^{commit}")
            # main and origin/main are often the same commit: one probe each.
            if not tip or tip in seen:
                continue
            seen.add(tip)
            fork = _out(repo, "merge-base", tip, head)
            if fork is None or (fork and not _looks_like_sha(fork)):
                # No common ancestor — the owner squashes main to a single
                # "initial commit" now and then. Merging the branch into the
                # base would change nothing only if every file the branch has
                # is in the base as is (the base may hold more).
                if _tree_within(repo, head, tip):
                    return "content"
                continue
            if fork not in forks:
                forks[fork] = _raw_changes(repo, fork, head)
            changes = forks[fork]
            if changes is None:
                continue
            if not changes or _contains_changes(repo, changes, tip, scratch):
                return "content"
    return ""


# ----------------------------------------------- the read-only merge test
#
# "Merging the branch into commit C changes nothing" is decided per file, with
# reads only: ``git merge-tree --write-tree`` would answer it in one call, but
# it writes the merged trees into the object store — and even pointed at a
# scratch object dir (the real store as an alternate) it still FRESHENS the
# real store's objects and packs it re-derives (touches their mtimes), so a
# scan would modify every repository it looks at. Instead: the branch's
# changes since it forked (``diff --raw fork head``), C's version of each
# changed path (``ls-tree C -- paths``), and for a path C has in a third
# version a 3-way ``git merge-file`` of the three blobs in a temp dir outside
# any repository. C holds the branch's work when every path the branch
# changed comes out as C's own. A deletion, a mode change, a symlink or a
# submodule must match exactly; a binary file or a conflict fails. Works on
# any git (no ``--write-tree``, which needs 2.38).

#: A path's object as ``(mode, type, sha)``.
_Entry = tuple
#: Paths merged 3-way per probe at most; more is "can't tell".
MAX_FILE_MERGES = 40
#: Paths named to git on a command line at most; past it the whole tree is
#: listed instead (a pathspec this long only slows git down).
MAX_PATHSPEC = 500


def _literal_env() -> Dict[str, str]:
    # A path with * ? [ in it must name itself, not a glob.
    return {"GIT_LITERAL_PATHSPECS": "1"}


def _raw_changes(repo: str, a: str, b: str) -> Optional[list]:
    """``git diff --raw a b`` as ``[(old_mode, new_mode, old_sha, new_sha,
    status, path)]`` (renames off: a rename is a delete and an add), None on
    failure or odd output."""
    cp = _git(
        repo,
        "diff",
        "--raw",
        "-z",
        "--no-renames",
        "--no-abbrev",
        "--no-textconv",
        "--no-ext-diff",
        a,
        b,
        timeout=LANDED_TIMEOUT,
    )
    if cp.returncode != 0:
        return None
    parts = (cp.stdout or "").split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) % 2:
        return None
    out = []
    for meta, path in zip(parts[0::2], parts[1::2]):
        f = meta.split(" ")
        if len(f) != 5 or not f[0].startswith(":") or not path:
            return None
        om, nm, osha, nsha, st = f[0][1:], f[1], f[2], f[3], f[4][:1]
        if st not in "AMDT" or not (_is_oid(osha) and _is_oid(nsha)):
            return None
        out.append((om, nm, osha, nsha, st, path))
    return out


def _is_oid(s: str) -> bool:
    return len(s) in (40, 64) and _looks_like_sha(s)


def _tree_entries(
    repo: str, commit: str, paths: Sequence[str]
) -> Optional[Dict[str, _Entry]]:
    """``{path: (mode, type, sha)}`` for ``paths`` in ``commit`` (a path it
    lacks is absent), None on failure or odd output."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if len(paths) <= MAX_PATHSPEC:
        args += ["--", *paths]
    cp = _git(repo, *args, env=_literal_env(), timeout=LANDED_TIMEOUT)
    if cp.returncode != 0:
        return None
    want = set(paths)
    out: Dict[str, _Entry] = {}
    for rec in (cp.stdout or "").split("\0"):
        if not rec:
            continue
        meta, tab, path = rec.partition("\t")
        f = meta.split(" ")
        if not tab or len(f) != 3 or not _is_oid(f[2]):
            return None
        if path in want:
            out[path] = (f[0], f[1], f[2])
    return out


def _tree_within(repo: str, head: str, base: str) -> bool:
    """Every path ``head`` has is in ``base`` with the same mode and object
    (``base`` may hold more): what merging unrelated histories would leave
    unchanged."""
    changes = _raw_changes(repo, base, head)
    return changes is not None and all(c[4] == "D" for c in changes)


class _MergeScratch:
    """One probe's scratch: a temp dir, made on first use, for
    :func:`_merge_is_noop`'s blobs; the blobs read so far (the branch's side
    is the same for every candidate); and the probe's deadline."""

    def __init__(self, seconds: Optional[float] = None) -> None:
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self.blobs: Dict[str, bytes] = {}
        self.deadline = time.monotonic() + seconds if seconds else None

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def path(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="mindflock-gc-merge-")
        return self._tmp.name

    def __enter__(self) -> "_MergeScratch":
        return self

    def __exit__(self, *exc) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def _blobs(repo: str, shas: Iterable[str]) -> Optional[Dict[str, bytes]]:
    """The contents of ``shas`` (one ``cat-file --batch``), None on failure."""
    want = sorted(set(shas))
    if not want:
        return {}
    cp = _git(
        repo,
        "cat-file",
        "--batch",
        text=False,
        input=("\n".join(want) + "\n").encode(),
        timeout=LANDED_TIMEOUT,
    )
    if cp.returncode != 0:
        return None
    data: bytes = cp.stdout or b""
    out: Dict[str, bytes] = {}
    pos = 0
    for sha in want:
        nl = data.find(b"\n", pos)
        if nl < 0:
            return None
        head = data[pos:nl].split(b" ")
        if (
            len(head) != 3
            or head[0].decode("ascii", "replace") != sha
            or head[1] != b"blob"
        ):
            return None
        try:
            size = int(head[2])
        except ValueError:
            return None
        body = data[nl + 1 : nl + 1 + size]
        if len(body) != size:
            return None
        out[sha] = body
        pos = nl + 1 + size + 1
    return out


def _merge_is_noop(
    base: bytes, branch: bytes, current: bytes, scratch: _MergeScratch
) -> bool:
    """A 3-way merge of ``base`` → ``branch`` into ``current`` is clean and
    gives back ``current`` itself: every change the branch made to this file
    is already in it. Runs ``git merge-file`` on temp copies, outside any
    repository; a conflict, a binary file or a failure is False."""
    d = scratch.path()
    names = []
    for i, body in enumerate((current, base, branch)):
        p = os.path.join(d, "f%d" % i)
        with open(p, "wb") as fh:
            fh.write(body)
        names.append(p)
    cp = gitutil.git(
        ["merge-file", "-p", *names],
        d,
        timeout=LANDED_TIMEOUT,
        env=dict(READ_ONLY_ENV, GIT_CEILING_DIRECTORIES=os.path.dirname(d)),
        text=False,
        stdin=subprocess.DEVNULL,
    )
    return cp.returncode == 0 and cp.stdout == current


def _contains_changes(
    repo: str, changes: list, commit: str, scratch: _MergeScratch
) -> bool:
    """Does ``commit`` already hold every change in ``changes`` (a branch's
    ``diff --raw`` since it forked)? Reads only; unsure is False."""
    entries = _tree_entries(repo, commit, [c[5] for c in changes])
    if entries is None:
        return False
    merges = []
    for om, nm, osha, nsha, st, path in changes:
        cur = entries.get(path)
        if st == "D":
            if cur is not None:
                return False  # the branch deleted it; the commit still has it
            continue
        if cur is None:
            return False  # the branch has it; the commit doesn't
        cmode, ctype, csha = cur
        if csha == nsha and cmode == nm:
            continue  # the commit has the branch's very version
        if st != "M" or ctype != "blob" or csha == osha:
            # Added with other content, a type change, or the commit still
            # has the version the branch started from: the change isn't in it.
            return False
        if om != nm and cmode != nm:
            return False  # the branch changed the mode; the commit didn't
        if {"120000", "160000"} & {om, nm, cmode}:
            return False  # a symlink or submodule must match exactly
        merges.append((osha, nsha, csha))
    if not merges:
        return True
    if len(merges) > MAX_FILE_MERGES:
        return False
    blobs = scratch.blobs
    got = _blobs(repo, [s for m in merges for s in m if s not in blobs])
    if got is None:
        return False
    blobs.update(got)
    # Smallest first: a file that fails fails fast.
    merges.sort(key=lambda m: len(blobs[m[2]]))
    for osha, nsha, csha in merges:
        if scratch.expired():
            return False
        if not _merge_is_noop(blobs[osha], blobs[nsha], blobs[csha], scratch):
            return False
    return True


# ------------------------------------------------------- landed (squash/rebase)

#: The landed-work probe compares at most this many of a branch's own
#: commits; a longer branch is "can't tell", which keeps it.
LANDED_MAX_COMMITS = 200
#: Base commits tried as the one the branch landed in, per item, at most.
LANDED_MAX_TRIES = 8
#: Base history read when looking for that commit, at most.
LANDED_MAX_HISTORY = 2000
#: Ceiling per git call of the probe (it runs on few items, but never long).
LANDED_TIMEOUT = 15
#: Wall-clock budget of one item's probe; past it, "can't tell" (keeps it).
LANDED_BUDGET_S = 10.0


def _lines(
    repo: str, *args: str, env=None, timeout: float = LANDED_TIMEOUT
) -> Optional[List[str]]:
    cp = _git(repo, *args, env=env, timeout=timeout)
    if cp.returncode != 0:
        return None
    return [ln for ln in (cp.stdout or "").splitlines() if ln.strip()]


def _sha_list(lines: Optional[List[str]]) -> Optional[List[str]]:
    """``lines`` when every one is a full object name, else None (odd
    output is "can't tell")."""
    if lines is None:
        return None
    for ln in lines:
        if not _is_oid(ln):
            return None
    return lines


def landed(repo: str, head: str, bases: Sequence[str]) -> Optional[dict]:
    """Is the work of ``head`` already in one of ``bases`` although its
    commits are not ancestors and the base has moved on since (a squash or
    rebase merge followed by more commits — the case :func:`merged_into`
    misses as soon as a later commit touches the same lines, which on a repo
    with a CHANGELOG is every branch)?

    The rule: there is a commit ``C`` on a base branch's first-parent history
    since the branch forked such that merging the branch into ``C`` would
    change nothing — every change the branch makes since it forked is
    already in ``C``, byte for byte (see the read-only merge test above).
    Then that work is in the base branch's history and removing the branch
    loses nothing: the very test :func:`merged_into` makes against the tip,
    made at the commit the work landed in.

    Which ``C`` to try (at most :data:`LANDED_MAX_TRIES`): the base commits
    whose patch equals one of the branch's commits (``git rev-list
    --cherry-mark``: where a rebase merge, or a one-commit squash, put them),
    newest first, then the commits that touch the most of the files the
    branch changes (a squash commit touches all of them). Patch equality
    alone is never the verdict: it ignores whitespace; the merge test
    doesn't.

    ``{"how": "squash" | "rebase", "base": ref, "at": sha}``, or None — also
    whenever git fails, answers oddly, or the branch is longer than
    :data:`LANDED_MAX_COMMITS` or the probe outlasts
    :data:`LANDED_BUDGET_S` (unsure keeps). Reads only."""
    if not head or not bases:
        return None
    mine = _sha_list(
        _lines(
            repo,
            "rev-list",
            "--max-count=%d" % (LANDED_MAX_COMMITS + 1),
            head,
            "--not",
            *bases,
        )
    )
    if not mine or len(mine) > LANDED_MAX_COMMITS:
        return None
    tries = [LANDED_MAX_TRIES]
    seen: Set[str] = set()
    with _MergeScratch(LANDED_BUDGET_S) as scratch:
        for b in bases:
            sha = _out(repo, "rev-parse", "--verify", "--quiet", b + "^{commit}")
            if not sha or sha in seen:
                continue
            seen.add(sha)
            found = _landed_in(repo, sha, head, scratch, len(mine), tries)
            if found:
                found["base"] = b
                return found
            if tries[0] <= 0 or scratch.expired():
                break
    return None


def _landed_in(
    repo: str,
    base: str,
    head: str,
    scratch: _MergeScratch,
    n_mine: int,
    tries: List[int],
) -> Optional[dict]:
    fork = _out(repo, "merge-base", base, head)
    if not fork or not _is_oid(fork):
        # Unrelated histories (a squashed-to-one-commit main): merged_into's
        # whole-tree test is the only one that applies.
        return None
    changes = _raw_changes(repo, fork, head)
    if not changes:
        return None  # failed, or it changes nothing (merged_into said so)
    # Base commits patch-equal to the branch's own (a rebase merge).
    marked = _lines(
        repo,
        "rev-list",
        "--cherry-mark",
        "--left-only",
        "--max-count=%d" % LANDED_MAX_HISTORY,
        base + "..." + head,
    )
    equal = set(
        _sha_list([ln[1:] for ln in (marked or []) if ln.startswith("=")]) or []
    )
    # The base's first-parent commits since the fork that touch the branch's
    # files, newest first, with how many of those files each touches.
    files = [c[5] for c in changes]
    args = [
        "-c",
        "core.quotePath=false",
        "log",
        "--first-parent",
        "--no-renames",
        "--max-count=%d" % LANDED_MAX_HISTORY,
        "--format=@@%H",
        "--name-only",
        fork + ".." + base,
    ]
    if len(files) <= MAX_PATHSPEC:
        args += ["--", *files]
    log = _lines(repo, *args, env=_literal_env())
    if log is None:
        return None
    want = set(files)
    score: Dict[str, int] = {}
    order: List[str] = []
    cur = ""
    for ln in log:
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            if not _is_oid(cur):
                return None
            score[cur] = 0
            order.append(cur)
        elif cur and ln in want:
            score[cur] += 1
    order = [c for c in order if score[c]]
    rank = {c: i for i, c in enumerate(order)}
    # Patch-equal commits first (newest first), then the rest by how many of
    # the branch's files they touch. Only the base's first-parent commits
    # are candidates: the states the base branch itself has been in.
    picks = [c for c in order if c in equal]
    picks += sorted(
        (c for c in order if c not in equal), key=lambda c: (-score[c], rank[c])
    )
    for c in picks:
        if tries[0] <= 0 or scratch.expired():
            return None
        tries[0] -= 1
        if _contains_changes(repo, changes, c, scratch):
            how = "rebase" if c in equal and n_mine > 1 else "squash"
            return {"how": how, "at": c}
    return None


def stash_made_on(repo: str, branch: str, head: str) -> Optional[int]:
    """How many stash entries were made on this branch (their message says
    "On <branch>:" / "WIP on <branch>:") or right on ``head``; None when the
    stash can't be read. The stash is the repository's, not the worktree's,
    so removing the worktree would not drop it — but an entry made on a
    branch is work that branch's owner meant to come back to, and the landed
    rung counts it as work."""
    cp = _git(repo, "stash", "list", "--format=%P%x09%gs", timeout=LANDED_TIMEOUT)
    if cp.returncode != 0:
        return None
    n = 0
    marks = ("On %s:" % branch, "WIP on %s:" % branch) if branch else ()
    for ln in (cp.stdout or "").splitlines():
        if not ln.strip():
            continue
        parents, _, subject = ln.partition("\t")
        first = parents.split(" ", 1)[0]
        if (head and first == head) or (marks and subject.startswith(marks)):
            n += 1
    return n


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


# ------------------------------------------------------------------- sizes
#
# What a listing spends its time on is sizes: ~11 million files under ~200
# worktrees on the owner's machine. So each item is walked ONCE (one
# ``find``, never ``du`` + ``find`` + a second ``du`` for the headline),
# and what the walk learns is kept in the state dir (:class:`SizeCache`)
# until the folder shows a change, so a repeat listing walks only what
# changed.
#
# The numbers stay hard-link honest. A checkout's ``.venv`` is mostly hard
# links into uv's cache (8 of 11 million files on that machine, but only
# 52 thousand distinct ones), so:
#
# * ``size_bytes`` — what the folder takes on disk on its own (``du -x``):
#   a file linked twice inside it counts once.
# * ``free_bytes`` — what removing it alone gives back: regular files
#   nothing else links (link count 1).
# * the headline (:func:`measure_totals`) — every file under the items
#   once, however many of them link it: each item's unshared bytes plus the
#   UNION of the shared files they link. The walk records which shared
#   files (device, inode) an item links as a bitmap over one table of the
#   shared files seen, so the union stays exact when most items come from
#   the cache. (Summing per-item sizes instead once reported 2,039 GB for a
#   210 GB tree.)

#: Sizes walked at once (I/O bound; more threads only thrash the disk).
SIZE_WORKERS = 4
#: Shared files remembered at most; past it the table is rebuilt.
MAX_SHARED = 2_000_000

_size_lock = threading.Lock()
_find_ok: Optional[bool] = None


def _glob_escape(path: str) -> str:
    """``path`` as a literal ``find -path`` pattern."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", path)


def _find_has_printf() -> bool:
    """GNU find (``-printf`` / ``-fprintf``); BSD find (macOS) has neither."""
    global _find_ok
    if _find_ok is None:
        ok = False
        if os.name != "nt" and shutil.which("find"):
            try:
                cp = subprocess.run(
                    ["find", os.sep, "-maxdepth", "0", "-printf", "%b\n"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                )
                ok = cp.returncode == 0 and cp.stdout.strip().isdigit()
            except (OSError, subprocess.SubprocessError):
                ok = False
        _find_ok = ok
    return bool(_find_ok)


def _walk_find(path: str, exclude: Sequence[str]) -> Optional[dict]:
    """One ``find -xdev`` over ``path`` (``exclude`` pruned), its output
    split three ways so Python only sums numbers: blocks of unshared regular
    files (stdout), of directories and other unshared entries (a temp file),
    and ``inode blocks`` of every shared file (another)."""
    with tempfile.TemporaryDirectory(prefix="mindflock-gc-size-") as td:
        other = os.path.join(td, "own")
        shared = os.path.join(td, "shared")
        argv = ["find", path, "-xdev"]
        if exclude:
            argv.append("(")
            for k, e in enumerate(exclude):
                if k:
                    argv.append("-o")
                argv += ["-path", _glob_escape(e)]
            argv += [")", "-prune", "-o"]
        argv += [
            "(", "-type", "f", "-links", "1", "-printf", "%b\n", ")",
            "-o", "(", "(", "-type", "d", "-o", "-links", "1", ")",
            "-fprintf", other, "%b\n", ")",
            "-o", "-fprintf", shared, "%i %b\n",
        ]  # fmt: skip
        try:
            cp = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=SIZE_TIMEOUT,
            )
            # 1 = some entry couldn't be read; the rest still counts (du too).
            if cp.returncode not in (0, 1):
                return None
            free = sum(map(int, cp.stdout.split()))
            with open(other, "rb") as f:
                own = free + sum(map(int, f.read().split()))
            with open(shared, "rb") as f:
                toks = f.read().split()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    links = dict(zip(map(int, toks[0::2]), map(int, toks[1::2])))
    return {"free": free * 512, "own": own * 512, "shared": links}


def _walk_py(path: str, exclude: Sequence[str], dev: int) -> Optional[dict]:
    """:func:`_walk_find` where there is no GNU find (slower, same numbers;
    the apparent size where the OS reports no blocks)."""
    skip = {norm(e) for e in exclude}
    deadline = time.monotonic() + SIZE_TIMEOUT
    free = own = 0
    links: Dict[int, int] = {}

    def blocks(st) -> int:
        b = getattr(st, "st_blocks", None)
        return int(b) if b is not None else (st.st_size + 511) // 512

    try:
        own += blocks(os.lstat(path))
    except OSError:
        return None
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_dev != dev:
                    continue
                b = blocks(st)
                if stat.S_ISDIR(st.st_mode):
                    if skip and norm(e.path) in skip:
                        continue
                    own += b
                    stack.append(e.path)
                elif st.st_nlink > 1:
                    links[st.st_ino] = b
                else:
                    own += b
                    if stat.S_ISREG(st.st_mode):
                        free += b
    return {"free": free * 512, "own": own * 512, "shared": links}


def walk_sizes(path: str, exclude: Sequence[str] = ()) -> Optional[dict]:
    """``{"size", "free", "own", "dev", "shared"}`` of ``path`` without the
    folders in ``exclude`` (items nested inside it): bytes on disk, bytes
    removing it alone frees, bytes of its unshared entries, and its shared
    files as ``{inode: 512-byte blocks}`` on device ``dev``. One walk. None
    when it can't be measured within :data:`SIZE_TIMEOUT`."""
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return None
    if not os.path.isdir(path):
        return None
    m = _walk_find(path, exclude) if _find_has_printf() else None
    if m is None and not _find_has_printf():
        m = _walk_py(path, exclude, dev)
    if m is None:
        return None
    m["dev"] = dev
    m["size"] = m["own"] + sum(m["shared"].values()) * 512
    return m


def size_key(path: str, stamp: float, exclude: Sequence[str] = ()) -> str:
    """The cheap freshness signal a cached size is checked against: the
    item's last use (HEAD reflog, session records), the mtime of its folder
    and of each folder directly in it (a new ``node_modules`` or ``.venv``,
    a build output dir), and the nested items left out of it."""
    try:
        m = os.stat(path).st_mtime_ns
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        m = max(m, e.stat(follow_symlinks=False).st_mtime_ns)
                except OSError:
                    continue
    except OSError:
        m = 0
    nested = hashlib.sha1("\0".join(sorted(exclude)).encode("utf-8", "replace"))
    return "%d:%d:%s" % (int(stamp or 0), m, nested.hexdigest()[:10])


class SizeCache:
    """Measured sizes by folder, kept in ``<state>/worktree_gc_sizes.json``
    across scans, processes and restarts. An entry is used while its
    :func:`size_key` matches and it is younger than :data:`SIZE_TTL_S`.

    Besides an item's three numbers it keeps WHICH shared (hard-linked)
    files the item links, as a bitmap over one table of every shared file
    seen (``shared``: ``[device, inode, blocks]``), so the headline's union
    is exact without walking again (:meth:`shared_bytes`)."""

    VERSION = 1

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.entries: Dict[str, dict] = {}
        self.table: List[List[int]] = []
        self.index: Dict[int, Dict[int, int]] = {}
        self.loaded = False
        self.dirty = False

    # -- persistence

    def _load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("v") != self.VERSION:
                return
            table = [list(map(int, t)) for t in d.get("shared") or []]
            entries = {}
            for p, e in (d.get("items") or {}).items():
                raw = zlib.decompress(base64.b64decode(e.get("bits") or ""))
                entries[p] = {
                    "key": str(e["key"]),
                    "at": float(e["at"]),
                    "size": int(e["size"]),
                    "free": int(e["free"]),
                    "own": int(e["own"]),
                    "bits": int.from_bytes(raw, "little"),
                }
        except (OSError, ValueError, KeyError, TypeError, zlib.error):
            return
        self.table = table
        self.index = {}
        for i, (dev, ino, _b) in enumerate(table):
            self.index.setdefault(dev, {})[ino] = i
        self.entries = entries

    def save(self, keep: Optional[Iterable[str]] = None) -> None:
        """Write it out; with ``keep``, entries for other folders (removed,
        or no longer items) are dropped first, and the table with them."""
        with self.lock:
            self._load()
            if keep is not None:
                keep = set(keep)
                for p in [p for p in self.entries if p not in keep]:
                    del self.entries[p]
                    self.dirty = True
            if not self.dirty:
                return
            self._compact()
            items = {}
            for p, e in self.entries.items():
                b = e["bits"]
                raw = b.to_bytes((b.bit_length() + 7) // 8, "little")
                items[p] = {
                    "key": e["key"],
                    "at": e["at"],
                    "size": e["size"],
                    "free": e["free"],
                    "own": e["own"],
                    "bits": base64.b64encode(zlib.compress(raw, 6)).decode("ascii"),
                }
            doc = {"v": self.VERSION, "shared": self.table, "items": items}
            self.dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(), threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _compact(self) -> None:
        """Drop shared files no entry links any more, once they are most of
        the table (a removed worktree's ``.venv``)."""
        live = 0
        for e in self.entries.values():
            live |= e["bits"]
        n_live = bin(live).count("1")
        if len(self.table) <= MAX_SHARED and (
            len(self.table) < 50_000 or n_live * 2 > len(self.table)
        ):
            return
        remap: Dict[int, int] = {}
        table: List[List[int]] = []
        for i, c in enumerate(bin(live)[:1:-1]):
            if c == "1":
                remap[i] = len(table)
                table.append(self.table[i])
        for e in self.entries.values():
            ba = bytearray((len(table) + 7) // 8)
            for i, c in enumerate(bin(e["bits"])[:1:-1]):
                if c == "1":
                    j = remap[i]
                    ba[j >> 3] |= 1 << (j & 7)
            e["bits"] = int.from_bytes(ba, "little")
        self.table = table
        self.index = {}
        for i, (dev, ino, _b) in enumerate(table):
            self.index.setdefault(dev, {})[ino] = i

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.table = []
            self.index = {}
            self.loaded = True
            self.dirty = True

    # -- use

    def get(self, path: str, key: str, now: float) -> Optional[dict]:
        with self.lock:
            self._load()
            e = self.entries.get(path)
        if e is None or e["key"] != key or not (0 <= now - e["at"] < SIZE_TTL_S):
            return None
        return e

    def put(self, path: str, key: str, now: float, m: dict) -> None:
        dev = int(m["dev"])
        shared = m["shared"]
        with self.lock:
            self._load()
            if len(self.table) + len(shared) > MAX_SHARED:
                self.clear()
            idx = self.index.setdefault(dev, {})
            ba = bytearray((len(self.table) + len(shared) + 7) // 8)
            for ino, blk in shared.items():
                i = idx.get(ino)
                if i is None:
                    i = len(self.table)
                    self.table.append([dev, ino, blk])
                    idx[ino] = i
                else:
                    self.table[i][2] = blk
                ba[i >> 3] |= 1 << (i & 7)
            self.entries[path] = {
                "key": key,
                "at": now,
                "size": int(m["size"]),
                "free": int(m["free"]),
                "own": int(m["own"]),
                "bits": int.from_bytes(ba, "little"),
            }
            self.dirty = True

    def has(self, path: str) -> bool:
        with self.lock:
            self._load()
            return path in self.entries

    def union_bytes(self, paths: Iterable[str]) -> Optional[int]:
        """Bytes on disk of these folders together, every file once: their
        unshared bytes plus each shared file they link, once. None when one
        of them has no entry."""
        with self.lock:
            self._load()
            own = 0
            bits = 0
            for p in paths:
                e = self.entries.get(p)
                if e is None:
                    return None
                own += e["own"]
                bits |= e["bits"]
            blocks = 0
            for i, c in enumerate(bin(bits)[:1:-1]):
                if c == "1":
                    blocks += self.table[i][2]
            return own + blocks * 512


_caches: Dict[str, SizeCache] = {}


def size_cache() -> SizeCache:
    """The size cache of the current state dir (one per process)."""
    p = os.path.join(state_dir(), SIZE_CACHE_FILE)
    with _size_lock:
        c = _caches.get(p)
        if c is None:
            c = _caches[p] = SizeCache(p)
        return c


def _sizable(item: dict) -> bool:
    return item.get("state") != MISSING and os.path.isdir(item.get("path") or "")


def measure_sizes(
    items: Sequence[dict],
    *,
    now: Optional[float] = None,
    walk: bool = True,
    only: Optional[Callable[[dict], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    keep_only_these: bool = False,
    cache: Optional[SizeCache] = None,
) -> int:
    """Fill each item's ``size_bytes`` / ``free_bytes``: from the cache when
    it is current, else by one walk (:data:`SIZE_WORKERS` at a time), unless
    ``walk`` is off or ``only`` says to skip the item (its size stays
    unknown). ``progress(done, total)`` counts the walks. Returns how many
    items are left unmeasured. ``keep_only_these``: ``items`` is the whole
    inventory, so cached entries of other folders are dropped."""
    now = time.time() if now is None else now
    cache = cache or size_cache()
    todo = []
    for it in items:
        if not _sizable(it):
            continue
        key = size_key(it["path"], it.get("last_used") or 0, it.get("nested") or ())
        hit = cache.get(it["path"], key, now)
        if hit is not None:
            it["size_bytes"], it["free_bytes"] = hit["size"], hit["free"]
            continue
        it["size_bytes"] = it["free_bytes"] = None
        if walk and (only is None or only(it)):
            todo.append((it, key))
    done = 0
    lock = threading.Lock()
    if progress is not None and todo:
        progress(0, len(todo))

    def one(pair) -> None:
        nonlocal done
        it, key = pair
        m = walk_sizes(it["path"], it.get("nested") or ())
        if m is not None:
            cache.put(it["path"], key, now, m)
            it["size_bytes"], it["free_bytes"] = m["size"], m["free"]
        with lock:
            done += 1
            n = done
        if progress is not None:
            try:
                progress(n, len(todo))
            except Exception:  # noqa: BLE001 — a progress line never stops a scan
                pass

    if todo:
        # Biggest first is unknowable; the oldest-measured last is not worth
        # it: plain order, a few at a time.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(SIZE_WORKERS, len(todo))
        ) as pool:
            list(pool.map(one, todo))
    cache.save(keep=[i["path"] for i in items] if keep_only_these else None)
    return sum(1 for it in items if _sizable(it) and it.get("size_bytes") is None)


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
    base: Optional[dict] = None,
) -> dict:
    """One inventory item: the raw record plus what it holds and whether it
    is safe to collect; ``kept`` lists every reason it is not. A probe that
    fails becomes a "couldn't check" reason, which keeps the item.
    ``nested`` = other items inside this one's folder: they are left out of
    its size, and they keep it (removing it would take them along).
    ``base`` = :func:`refresh_bases`' word on its repository: when the base
    may be behind its remote, the unmerged-commits reason says so.
    ``measure``: its size too (cached, else one walk)."""
    item = _classify(
        rec,
        usage,
        after_days=after_days,
        now=now,
        root=root,
        records=records,
        nested=nested,
        base=base,
    )
    if measure:
        measure_sizes([item], now=now)
    return item


def _classify(
    rec: dict,
    usage: Usage,
    *,
    after_days: float,
    now: Optional[float],
    root: Optional[str],
    records: Optional[Dict[str, dict]],
    nested: Sequence[str],
    base: Optional[dict],
) -> dict:
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
        merged_how="",
        merged_at="",
        base_stale=False,
        safe_reason="",
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
                    why = _plural(n, "commit") + " in no base branch and on no remote"
                    note = stale_note(base, now)
                    item["base_stale"] = bool(note)
                    kept.append(why + (" (" + note + ")" if note else ""))
                    holds = True

    recent = bool(last) and now - last < after_days * DAY
    if recent:
        kept.append("used " + _ago(now - last))

    # The landed rung: the branch's work is already in a base branch as other
    # commits (a squash or rebase merge, the base moved on since). Asked only
    # when unmerged commits are the ONE thing keeping an otherwise
    # collectable worktree — in use, recent, not ours, locked, nested,
    # uncommitted or untracked all keep it before this runs — so its cost
    # (a few git calls, at most LANDED_MAX_TRIES merge tests) is paid only
    # where the answer can change the verdict.
    if unsaved_at >= 0 and len(kept) == 1:
        try:
            found = landed(repo, head, bases)
        except Exception:  # noqa: BLE001 — unknown is "not merged"
            found = None
        if found:
            stashed = stash_made_on(repo, item.get("branch") or "", head)
            if stashed is None:
                kept.append("couldn't read the stash")
            elif stashed:
                kept.append(
                    _plural(stashed, "stash entry").replace("entrys", "entries")
                    + " made on it"
                )
            else:
                kept.pop(unsaved_at)
                item["merged"] = "content"
                # The branch name, as for the other rungs ("origin/main" ->
                # "main"): branch_deletable compares it with the branch.
                item["merged_into"] = found["base"].rsplit("/", 1)[-1]
                item["merged_how"] = found["how"]
                item["merged_at"] = found.get("at") or ""
                item["unsaved_commits"] = 0
                holds = False

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
    item["safe_reason"] = safe_reason(item) if item["safe"] else ""
    return item


def safe_reason(item: dict) -> str:
    """Why a safe item may go, in plain words (the listing's WHY column)."""
    base = item.get("merged_into") or "the base branch"
    how = item.get("merged")
    if item.get("kind") == KIND_CLONE:
        return "every commit is pushed; nothing uncommitted"
    if how == "ancestor":
        return "every commit is in %s" % base
    if how == "content":
        mh = item.get("merged_how")
        if mh in ("squash", "rebase"):
            return "its changes are already in %s (%s-merged)" % (base, mh)
        return "its changes are already in %s (merging it would change nothing)" % base
    if how == "pr":
        return "its PR was merged at this very commit"
    return "every commit is on a remote; its branch is kept"


def _base_hint(rec: dict, usage: Usage, records: Dict[str, dict]) -> str:
    info = usage.branch_info.get(rec["path"]) or {}
    created = records.get(rec["path"]) or {}
    return info.get("base") or created.get("base") or ""


def classify_all(
    usage: Usage,
    *,
    after_days: float = DEFAULT_AFTER_DAYS,
    now: Optional[float] = None,
    root: Optional[str] = None,
    fetch: Optional[bool] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> tuple:
    """Every item classified (concurrently), sizes not yet known:
    ``(items, bases)``. ``fetch``: True brings the involved repositories'
    base refs up to date first (:func:`refresh_bases`, throttled), False
    only checks how old they are (and says so), None does neither.
    ``progress(phase, done, total)``: ``"fetch"`` then ``"check"``.
    Blocking: run it off the loop."""
    root = worktrees_root() if root is None else root
    records = load_records()
    recs = discover(usage, root)
    inner = nesting([r["path"] for r in recs])

    def tell(phase: str, done: int, total: int) -> None:
        if progress is not None:
            try:
                progress(phase, done, total)
            except Exception:  # noqa: BLE001 — a progress line never stops a scan
                pass

    bases: Dict[str, dict] = {}
    if fetch is not None:
        hints: Dict[str, Set[str]] = {}
        for r in recs:
            if r.get("kind") == KIND_WORKTREE and r.get("repo"):
                hints.setdefault(r["repo"], set()).add(_base_hint(r, usage, records))
        if hints:
            tell("fetch", 0, len(hints))
            bases = refresh_bases(hints, fetch=bool(fetch), now=now)
            tell("fetch", len(hints), len(hints))

    done = 0
    lock = threading.Lock()

    def one(rec: dict) -> dict:
        nonlocal done
        try:
            return _classify(
                rec,
                usage,
                after_days=after_days,
                now=now,
                root=root,
                records=records,
                nested=inner.get(rec["path"], ()),
                base=bases.get(rec.get("repo") or ""),
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
                free_bytes=None,
                last_used=0.0,
            )
            return out
        finally:
            with lock:
                done += 1
                n = done
            tell("check", n, len(recs))

    tell("check", 0, len(recs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        items = list(pool.map(one, recs))
    sort_items(items)
    return items, bases


_ORDER = {
    SAFE: 0,
    RECENT: 1,
    HOLDS_WORK: 2,
    MISSING: 3,
    UNKNOWN: 4,
    IN_USE: 5,
    NOT_OURS: 6,
}


def sort_items(items: List[dict]) -> None:
    """The listing's order: the safe first, then what is kept, biggest first
    within a state."""
    items.sort(
        key=lambda i: (_ORDER.get(i.get("state"), 9), -(i.get("size_bytes") or 0))
    )


def inventory(
    usage: Usage,
    *,
    after_days: float = DEFAULT_AFTER_DAYS,
    now: Optional[float] = None,
    root: Optional[str] = None,
    measure: bool = True,
    fetch: Optional[bool] = None,
) -> List[dict]:
    """Every item, classified (:func:`classify_all`) and sized
    (:func:`measure_sizes`; ``measure`` off = cached sizes only). Blocking:
    run it off the loop."""
    items, _bases = classify_all(
        usage, after_days=after_days, now=now, root=root, fetch=fetch
    )
    measure_sizes(items, now=now, walk=measure, keep_only_these=True)
    sort_items(items)
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


def measure_totals(
    items: Sequence[dict], cache: Optional[SizeCache] = None
) -> Dict[str, Optional[int]]:
    """The on-disk headline numbers, every file counted once however many
    items hard-link it (nested items are items of their own, left out of
    the item around them): what MindFlock made (``bytes``) and what it did
    not (``other_bytes``). Computed from what :func:`measure_sizes` recorded
    — no walk. None for a number one of whose items has no measured size.
    What collecting would FREE is the safe items' ``free_bytes`` (see
    :func:`summarize`)."""
    cache = cache or size_cache()

    def total(pred) -> Optional[int]:
        xs = [i for i in items if pred(i) and _sizable(i)]
        if not xs:
            return 0
        if any(i.get("size_bytes") is None for i in xs):
            return None
        return cache.union_bytes(i["path"] for i in xs)

    return {
        "bytes": total(lambda i: i.get("ours")),
        "other_bytes": total(lambda i: not i.get("ours")),
    }


def summarize(items: Sequence[dict], totals: Optional[dict] = None) -> dict:
    """Counts and sizes. ``bytes`` / ``other_bytes`` come from ``totals``
    (:func:`measure_totals`, computed here when not given) — never a sum of
    per-item sizes, which counts every hard-linked file once per item — and
    are None while not every item is measured. ``safe_bytes`` (what
    collecting the safe set frees) is a plain sum: files nothing else links
    belong to exactly one item; None while a safe item is unmeasured."""

    def freed(xs) -> Optional[int]:
        n = 0
        for x in xs:
            v = x.get("free_bytes")
            if v is None:
                v = x.get("size_bytes")
            if v is None:
                if _sizable(x):
                    return None
                v = 0
            n += int(v)
        return n

    if totals is None:
        totals = measure_totals(items)
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
        "bytes": totals.get("bytes"),
        "other_count": len(others),
        "other_bytes": totals.get("other_bytes"),
        "safe_count": len(safe),
        # What collecting the safe set gives back: files nothing else
        # hard-links (an item's free_bytes), never shared cache blocks.
        "safe_bytes": freed(safe),
        "by_state": by_state,
    }
    for k in ("bytes", "other_bytes", "safe_bytes"):
        if out[k] is not None:
            out[k] = int(out[k])
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
