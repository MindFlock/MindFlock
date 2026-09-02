"""Workspace disk management: roots, discovery, classification, deletion.

The helpers behind the Recently-closed page — which is also the disk manager,
since the two were merged — and the delete/cleanup paths: which directories hold
deletable workspaces (:func:`_workspace_roots`), finding worktree leaves and
sizing them, classifying entries (base clone / refresher / pr / worktree / plain
workspace), the K4 delete guard (:func:`_base_clone_references`), the guarded
permanent removal of a worktree directory (:func:`_remove_worktree_path` — only
ever under a managed root), the merged page's row list (:func:`recent_rows`) and
the unused-worktree sweep behind it (:func:`prune_stale_worktrees`, gated on
:func:`_worktree_gitdir` so it can only ever remove worktrees git generated —
never a repository, a clone, or a folder git did not make).

Split out of ``backend.web.server`` (which re-imports these names — the
workspace routes and tests reference them through the server namespace).
"""

from __future__ import annotations

import concurrent.futures
import datetime as _datetime
import os
import shutil
import subprocess
import threading
import time

from backend import config
from backend.providers.claude import remove_trust_entry as _remove_trust_entry
from backend.session import provisioned as provisioning
from backend.web.core.cursor_windows import _close_cursor_window
from backend.web.core.worktree_reclaim import _git as _wt_git
from backend.workspace_setup import is_refresher_dirname as _is_refresher_dirname


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _strictly_under(child: str, root: str) -> bool:
    """True iff ``root`` is a proper ancestor of ``child`` (realpaths)."""
    try:
        return os.path.commonpath([child, root]) == root and child != root
    except ValueError:  # different drives / relative vs absolute
        return False


def _workspace_roots() -> list:
    """Directories whose immediate children are deletable workspaces.

    The provisioning workspace dir (clone-strategy workspaces, ``_base_*``
    clones, cache refreshers, pr-* dirs) — both the
    configured one and the default local-repo one — and MindFlock's worktrees
    dir (worktree-strategy sessions). Only existing roots are returned, as
    resolved absolute paths.
    """
    roots = []
    s = provisioning.load_provision_settings()
    if s is not None:
        roots.append(os.path.realpath(str(s.workspace_dir)))
    # The default workspace dir used for local-repo provisioned sessions when
    # no config resolves (and harmless to include alongside a configured one).
    try:
        roots.append(os.path.realpath(str(provisioning.default_workspace_dir())))
    except Exception:  # noqa: BLE001
        pass
    try:
        roots.append(os.path.realpath(os.path.join(config.GetConfigDir(), "worktrees")))
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for r in roots:
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)
    return out


def _dir_size_bytes(path: str) -> int:
    # GNU du takes -b (bytes); BSD/macOS du doesn't, so fall back to the
    # POSIX -k (KiB) form there. Best-effort either way — 0 on any failure.
    srv = _server()
    cp = srv._run_capped(
        ["du", "-sb", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    scale = 1
    if cp.returncode != 0:
        cp = srv._run_capped(
            ["du", "-sk", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        scale = 1024
        if cp.returncode != 0:
            return 0
    try:
        return int(cp.stdout.split()[0]) * scale
    except (ValueError, IndexError):
        return 0


def _find_worktrees(root: str) -> list:
    """Worktree leaf dirs under ``root``. Worktree-mode branch names contain
    slashes, so a worktree like ``feature/sc-19827/slug_hex`` is nested several
    levels under the worktrees dir. A git worktree dir contains a ``.git`` entry
    (a file pointing at the base repo); collect those and don't descend into
    them."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == root:
            continue
        if ".git" in dirnames or ".git" in filenames:
            found.append(dirpath)
            dirnames[:] = []  # it's a worktree leaf; stop descending
    return sorted(found)


def _classify_workspace(name: str, root: str) -> str:
    # Canonical base clones: per-repo `_base_<slug>`.
    if provisioning.is_base_repo_dirname(name):
        return "base"
    if _is_refresher_dirname(name):
        return "refresher"
    if name.startswith("pr-"):
        return "pr"
    if name.endswith(".clone-tmp"):
        return "tmp"
    if root.endswith("worktrees"):
        return "worktree"
    return "workspace"


def _base_clone_references(base_dir: str):
    """``(session_titles, attached_worktree_paths)`` still referencing the base
    clone at ``base_dir`` (K4 delete guard).

    * sessions: any active instance whose git worktree's *repo path* is the
      base clone, or whose working dir lives under it;
    * worktrees: ``git worktree list`` entries other than the clone itself
      whose directory still exists (a pruned/gone registration doesn't block).
    """
    srv = _server()
    real = os.path.realpath(base_dir)
    titles = []
    for title, inst in list(srv.ENGINE.instances.items()):
        try:
            repo = os.path.realpath(inst.GetGitWorktree().GetRepoPath() or "")
        except Exception:  # noqa: BLE001
            repo = ""
        try:
            wp = os.path.realpath(inst.GetWorktreePath() or "")
        except Exception:  # noqa: BLE001
            wp = ""
        if (repo and repo == real) or (
            wp and (wp == real or wp.startswith(real + os.sep))
        ):
            titles.append(title)
    worktrees = []
    cp = srv._run_capped(
        ["git", "-C", real, "worktree", "list", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if cp.returncode == 0:
        for line in cp.stdout.decode("utf-8", "replace").splitlines():
            if not line.startswith("worktree "):
                continue
            p = os.path.realpath(line[len("worktree ") :].strip())
            if p and p != real and os.path.isdir(p):
                worktrees.append(p)
    return titles, worktrees


def _remove_worktree_path(folder: str, repo_path: str = "") -> bool:
    """Permanently remove a worktree directory (guarded to managed roots).

    rmtrees the dir, closes any editor window on it, and prunes the stale
    worktree registration from ``repo_path`` so ``git worktree`` stays clean.
    """
    srv = _server()
    real = os.path.realpath(folder) if folder else ""
    if not real or not os.path.isdir(real):
        return False
    if not any(srv._strictly_under(real, root) for root in srv._workspace_roots()):
        return False
    shutil.rmtree(real, ignore_errors=True)
    _close_cursor_window(real)
    _remove_trust_entry(real)  # GC ~/.claude.json trust entry (G3)
    if repo_path and os.path.isdir(repo_path):
        srv._run_capped(
            ["git", "-C", repo_path, "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    # `ignore_errors=True` swallows every EACCES, so a tree that survived in
    # part would otherwise be reported as removed — and the caller would drop
    # the closed entry that is the only handle back to it.
    return not os.path.exists(real)


def _worktree_in_use_by_other(path: str, exclude_title: str) -> bool:
    """True if a *different* live instance is using the same worktree directory.

    Lets several sessions share one worktree (a copy and its original) without
    closing the shared Cursor window when only one of them is ended — the window
    stays open as long as any session on that dir is still alive.
    """
    srv = _server()
    if not path:
        return False
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for t, inst in list(srv.ENGINE.instances.items()):
        if t == exclude_title:
            continue
        try:
            wp = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            wp = ""
        if wp and os.path.realpath(wp) == real:
            return True
    return False


# --- One list: closed sessions + whatever else is left on disk ---------------
#
# The Recently-closed page is the only workspace surface there is now: it lists
# closed sessions AND the workspace directories no closed entry accounts for, so
# "what is on this disk, and can I have the space back" is one page rather than
# two. Protected shared infrastructure (base clones, cache refreshers) is
# deliberately not a row at all — it is not the user's to delete, and a row
# whose only ever action is "protected" is noise on a page about reclaiming
# work. It is still COUNTED, so the page can say what it is not showing.

# How long a worktree has to sit unused before the sweep offers to take it:
# "not opened, not in the side menu, for over a week".
STALE_WORKTREE_DAYS = 7.0

#: Serializes the sweep's destructive phase. Two clients — or a co-running
#: MindFlock server — can both ask at once, and every individual delete is
#: already re-verified and idempotent, but running two sweeps together would
#: have each report the other's work as its own failures. The second waits, then
#: finds nothing left to do.
_SWEEP_LOCK = threading.Lock()


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path) if path else ""
    except OSError:
        return ""


def _worktrees_root() -> str:
    """MindFlock's own worktrees dir, resolved (``~/.mindflock/worktrees``)."""
    try:
        return os.path.realpath(os.path.join(config.GetConfigDir(), "worktrees"))
    except Exception:  # noqa: BLE001
        return ""


def _worktree_gitdir(path: str) -> str:
    """The gitdir a LINKED worktree's ``.git`` file points at, else ``""``.

    THE test that tells a worktree git generated apart from a real repository:
    ``git worktree add`` writes ``.git`` as a FILE holding
    ``gitdir: <repo>/.git/worktrees/<name>``, while a clone — or any repo the
    user made themselves — has ``.git`` as a DIRECTORY. Every deletion the
    unused-worktree sweep performs is gated on this answering non-empty, which
    is what makes "it can only ever remove generated worktrees, never a repo and
    never a folder git did not make" a property of the code instead of a promise
    about the paths it happens to be pointed at.
    """
    dot = os.path.join(path, ".git")
    # isfile, not exists: a repository's .git is a DIRECTORY, and that is
    # precisely the case that has to answer "" here.
    if not os.path.isfile(dot):
        return ""
    try:
        with open(dot, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    if not first.startswith("gitdir:"):
        return ""
    gitdir = first[len("gitdir:") :].strip()
    if not gitdir:
        return ""
    # A `gitdir:` file is NOT proof on its own: `git init --separate-git-dir`
    # and every submodule checkout write one too, and both are working trees
    # somebody would be furious to lose. Two more cheap facts are true only of a
    # linked worktree: the pointer runs through the repo's `worktrees/` dir, and
    # git writes a back-pointer there naming this very `.git` file.
    if not _worktree_repo_path(gitdir):
        return ""
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(path, gitdir))
    try:
        with open(os.path.join(gitdir, "gitdir")) as fh:
            back = fh.read().strip()
    except OSError:
        return ""
    if _realpath(back) != _realpath(dot):
        return ""
    return gitdir


def _worktree_repo_path(gitdir: str) -> str:
    """The repository that owns a linked worktree, from its gitdir pointer.

    ``<repo>/.git/worktrees/<name>`` -> ``<repo>`` (and the bare form
    ``<repo>.git/worktrees/<name>`` -> ``<repo>.git``), so removing a worktree
    can prune the stale registration from the one repo that holds it instead of
    running ``git worktree prune`` across every base clone on the machine.
    """
    parts = (gitdir or "").replace("\\", "/").rstrip("/").split("/")
    if "worktrees" not in parts:
        return ""
    i = len(parts) - 1 - parts[::-1].index("worktrees")
    base = "/".join(parts[:i])
    if base.endswith("/.git"):
        base = base[: -len("/.git")]
    return base


def _stat_mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _iso_epoch(value) -> float | None:
    """An ISO timestamp (or a bare epoch) as epoch seconds, else None."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _newest(*stamps) -> float | None:
    """The newest of several epoch stamps, ignoring None/0, else None."""
    known = [s for s in stamps if s]
    return max(known) if known else None


def _last_used(path: str, gitdir: str = "") -> float | None:
    """Best cheap guess at when work last happened in this workspace.

    The MAX over a handful of stats, never one of them alone. Each is blind by
    itself: a directory's own mtime only moves when entries at its ROOT change,
    so a week of editing files two levels down leaves it looking untouched;
    while a linked worktree's index and HEAD live in the BASE repo, so they move
    on every add/commit/checkout but say nothing about edits that were never
    staged. Measured on the author's machine, a worktree's own directory mtime
    was three days behind its index — against a seven-day threshold, that gap
    decides. Taking the max can only ever make a directory look NEWER, which is
    the safe direction for a number that decides what gets deleted.
    """
    stamps = [_stat_mtime(path)]
    if gitdir:
        for name in ("index", "HEAD", os.path.join("logs", "HEAD")):
            stamps.append(_stat_mtime(os.path.join(gitdir, name)))
    return _newest(*stamps)


def _worktree_local_work(path: str) -> bool:
    """True when deleting ``path`` would destroy something unrecoverable.

    Worth being exact about, because it is what the sweep's confirmation says
    out loud: a worktree's COMMITS live in the base repository's object store,
    reachable from the branch ref, and survive the directory being removed. So
    this is only ever about what the base repo has never seen — uncommitted or
    untracked changes, or a detached HEAD whose commit no ref contains. Any
    doubt (a git call that fails or times out) answers True: the cost of a wrong
    False is somebody's work.

    What it deliberately does NOT see is git-IGNORED content, because every
    worktree has some (``node_modules``, ``.venv``, build output) and treating
    that as work would mean the sweep could never take anything. The two ignored
    files that would actually matter are the ones worktree setup COPIES IN from
    the repo (``.env``, ``.env.local`` — see core.worktree_setup), so they come
    back with the next setup. The confirmation says out loud that ignored files
    go with the directory rather than pretending this check covers them.
    """
    # --no-optional-locks: `git status` normally REWRITES the index while it
    # refreshes stat data, and that index is one of the stamps _last_used reads —
    # so without this, previewing the sweep would make every candidate look as
    # though it had just been used, and the second click would find nothing.
    rc, out = _wt_git(path, "--no-optional-locks", "status", "--porcelain=v1")
    if rc != 0 or out.strip():
        return True
    # Deliberately NOT `git stash list`: refs/stash lives in the owning repo's
    # common dir, shared by every worktree of that repo, so it is neither
    # evidence about THIS directory nor something removing it can lose.
    rc, _ = _wt_git(path, "symbolic-ref", "-q", "HEAD")
    if rc == 0:
        return False  # on a branch: the branch keeps every commit made here
    # Detached HEAD — which MindFlock's own verify worktrees run in, so this
    # cannot simply answer "at risk", or the most disposable worktrees on the
    # machine would be the ones the sweep never takes. What matters is whether
    # anything still points at the commit: if a branch, a remote branch or a tag
    # contains it, removing the directory loses nothing.
    rc, out = _wt_git(
        path,
        "for-each-ref",
        "--count=1",
        "--contains",
        "HEAD",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    return rc != 0 or not out.strip()


def _prune_empty_worktree_dirs() -> int:
    """Remove the empty branch-slug directories left under the worktrees root.

    A worktree path keeps the branch's slashes
    (``worktrees/feature/shortcut-21129/<slug>_<hex>``), so removing the leaf
    leaves its intermediate directories behind for ever — 80 of them had piled
    up on the author's machine. Only ever ``rmdir``, which refuses a directory
    that still holds anything, and never the root itself: it cannot take
    anything with it. Bottom-up, so a parent emptied by its children going is
    collected in the same pass.
    """
    root = _worktrees_root()
    if not root or not os.path.isdir(root):
        return 0
    # Collect first, delete after — and never walk INTO a worktree. A checkout
    # is full of empty directories its tooling made (logs/, dist/, a .venv
    # skeleton) that git does not track and nothing would restore, and one of
    # them may belong to a session running right now. Stopping at the `.git`
    # entry is the same rule _find_worktrees uses.
    empties = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if ".git" in dirnames or ".git" in filenames:
            dirnames[:] = []  # a worktree leaf: its insides are not ours
            continue
        if dirpath != root and not dirnames and not filenames:
            empties.append(dirpath)
    removed = 0
    # Deepest first, so a parent emptied by its children going is collected in
    # the same pass. (An age guard was tried here against the narrow race with a
    # concurrent `git worktree add`, whose branch-slug dir is empty for an
    # instant before the leaf lands — but the directories worth collecting are
    # precisely the ones the sweep just emptied, so "recent" describes them too,
    # and the guard removed the feature instead of the race.)
    for dirpath in sorted(empties, key=lambda p: p.count(os.sep), reverse=True):
        try:
            os.rmdir(dirpath)
        except OSError:
            continue
        removed += 1
    return removed


def _fill_sizes(rows: list) -> None:
    """Fill ``size_bytes`` on each row — one ``du`` per row, run concurrently
    because a single huge tree otherwise owns the whole wall clock."""
    srv = _server()
    rows = [r for r in rows if r.get("path")]
    if not rows:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        sizes = pool.map(srv._dir_size_bytes, [r["path"] for r in rows])
        for row, n in zip(rows, sizes):
            row["size_bytes"] = n


def scan_workspaces(active: dict | None = None, sizes: bool = False) -> list:
    """Every workspace directory under the managed roots, classified.

    Flat children of the provisioning workspace dirs (clones, ``_base_*``, cache
    refreshers, ``pr-*``) plus the worktree LEAVES under MindFlock's worktrees
    dir, which nest as deep as their branch name has slashes. One scan behind
    both the raw ``/api/workspaces`` listing and the merged Recently-closed
    page, so the page and the disk manager it grew out of cannot disagree about
    what is on disk.

    ``size_bytes`` stays None unless ``sizes``: a ``du`` stats every file in the
    tree and takes seconds on a cold page cache.
    """
    srv = _server()
    if active is None:
        active = srv._active_worktree_titles()
    roots = srv._workspace_roots()
    wt_dir = _worktrees_root()
    out: list = []

    def _entry(path: str, name: str, root: str) -> dict:
        gitdir = _worktree_gitdir(path)
        return {
            "name": name,
            "path": path,
            "root": root,
            "kind": srv._classify_workspace(os.path.basename(name) or name, root),
            "size_bytes": None,
            "mtime": _stat_mtime(path),
            "active_session": active.get(os.path.realpath(path)),
            # The two fields the unused-worktree sweep is built on: whether git
            # generated this directory as a linked worktree, and when anything
            # last happened in it.
            "worktree": bool(gitdir),
            "gitdir": gitdir,
            "last_used": _last_used(path, gitdir),
        }

    for root in roots:
        if root == wt_dir:
            for p in srv._find_worktrees(root):
                out.append(_entry(p, os.path.relpath(p, root), root))
        else:
            try:
                children = sorted(os.scandir(root), key=lambda e: e.name)
            except OSError:
                continue
            for e in children:
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                out.append(_entry(e.path, e.name, root))
    if sizes:
        _fill_sizes(out)
    return out


def recent_rows(sizes: bool = False, days: float = STALE_WORKTREE_DAYS) -> dict:
    """The merged Recently-closed page: closed sessions plus leftover disk.

    Rows are:

    * every recently-closed entry (the reopen targets), annotated with whether
      its workspace is still on disk, how big it is and when it was last worked
      in;
    * every workspace directory that no closed entry accounts for and no LIVE
      session is using — the ex-"Workspaces on disk" rows, which is where a
      provisioned clone or a ``pr-*`` review directory turns up.

    Deliberately absent: protected shared infrastructure (base clones + cache
    refreshers), and anything a live session is working in — that one is in the
    sidebar, which is the surface that owns it, and killing a running agent is
    not an action a page about closed work should offer. Both are counted in
    ``hidden`` so the page reports what it is not showing rather than quietly
    under-stating the disk.

    ``stale`` marks the rows :func:`prune_stale_worktrees` would take, computed
    from the same predicate, so the button's count and the sweep agree.
    """
    srv = _server()
    active = srv._active_worktree_titles()
    disk = scan_workspaces(active=active, sizes=False)
    by_path = {_realpath(d["path"]): d for d in disk}
    wt_root = _worktrees_root()

    def _in_sweep_scope(path: str) -> bool:
        """Whether the sweep would even look at this directory."""
        return bool(wt_root) and srv._strictly_under(_realpath(path), wt_root)

    rows: list = []
    claimed = set()
    for e in srv._load_recently_closed():
        folder = e.get("folder") or ""
        key = _realpath(folder)
        d = by_path.get(key) or {}
        if key:
            claimed.add(key)
        exists = bool(folder and os.path.isdir(folder))
        gitdir = d.get("gitdir") if d else (_worktree_gitdir(folder) if exists else "")
        rows.append(
            {
                "id": e.get("id"),
                "source": "closed",
                "title": e.get("title"),
                "branch": e.get("branch"),
                # The disk name for a row that is also a directory we manage
                # (the worktree's path under the worktrees root reads as the
                # branch); the session identity otherwise.
                "name": d.get("name") or e.get("branch") or e.get("title") or "",
                "path": folder,
                "folder": folder,
                # The directory's classification, and only that: an in-place
                # session is not a workspace kind, it is a flag (and the row
                # already wears its own badge for it).
                "kind": d.get("kind") or "",
                "worktree": bool(gitdir),
                "in_place": bool(e.get("in_place")),
                "provisioned": bool(e.get("provisioned")),
                "exists": exists,
                "closed_at": e.get("closed_at"),
                "mtime": (
                    d.get("mtime") if d else (_stat_mtime(folder) if exists else None)
                ),
                "size_bytes": None,
                "active_session": d.get("active_session"),
                # When it was last touched OR closed, whichever is later: a
                # directory an editor opened yesterday is not stale because the
                # session that owned it closed a month ago.
                "last_used": _newest(
                    d.get("last_used") if d else _last_used(folder, gitdir or ""),
                    _iso_epoch(e.get("closed_at")),
                ),
            }
        )

    hidden_protected: list = []
    hidden_active: list = []
    # The same two, narrowed to what the unused-worktree sweep itself could ever
    # have taken. Its "here is why nothing went" must not name a base clone in
    # the provisioning dir, which it never looks at.
    hidden_active_wt: list = []
    hidden_protected_in_root = 0
    for d in disk:
        # Claimed first, and only then the two "hidden" rules: a directory a
        # closed row already stands for IS on screen, so counting it as withheld
        # would make the header say "1 in use hidden" about a row the user is
        # looking at.
        if _realpath(d["path"]) in claimed:
            continue
        # Protected shared infrastructure — but only when it really is a repo:
        # `_classify_workspace` reads the directory NAME, so a branch whose slug
        # happens to start with `_base_` would otherwise be filed as a base clone
        # and quietly hidden. A linked worktree is never infrastructure.
        if d["kind"] in ("base", "refresher") and not d["worktree"]:
            hidden_protected.append(d)
            if _in_sweep_scope(d["path"]):
                hidden_protected_in_root += 1
            continue
        if d.get("active_session"):
            hidden_active.append(d["active_session"])
            if d["worktree"] and _in_sweep_scope(d["path"]):
                hidden_active_wt.append(d["active_session"])
            continue
        rows.append(
            {
                "id": "disk:" + d["path"],
                "source": "disk",
                "title": None,
                "branch": None,
                "name": d["name"],
                "path": d["path"],
                "folder": d["path"],
                "kind": d["kind"],
                "worktree": bool(d["worktree"]),
                "in_place": False,
                "provisioned": False,
                "exists": True,
                "closed_at": None,
                "mtime": d["mtime"],
                "size_bytes": None,
                "active_session": None,
                "last_used": d["last_used"],
            }
        )

    # One directory, one age. A session and its copy closed on the SAME worktree
    # are two rows (the store dedupes on folder AND title, deliberately), and
    # the directory's last use is the newest of them. Without this the row for
    # the copy closed an hour ago reads "not stale" while the row for the origin
    # closed a month ago reads "stale" — and the sweep, which keys on the path,
    # would take the directory out from under work closed an hour ago.
    newest: dict = {}
    for r in rows:
        key = _realpath(r.get("path") or "")
        lu = r.get("last_used")
        if key and lu and lu > newest.get(key, 0):
            newest[key] = lu
    for r in rows:
        key = _realpath(r.get("path") or "")
        if key in newest:
            r["last_used"] = newest[key]

    cutoff = time.time() - max(0.0, float(days)) * 86400.0
    for r in rows:
        lu = r.get("last_used")
        # Every term the sweep uses, including its root scope — a badge that
        # says "Remove unused worktrees would take it" about a worktree the
        # sweep cannot reach (one the user made inside their own repo, say) is a
        # lie about a destructive button.
        r["stale"] = bool(
            r.get("worktree")
            and r.get("exists")
            and not r.get("active_session")
            and _in_sweep_scope(r.get("path") or "")
            and lu is not None
            and lu < cutoff
        )
    if sizes:
        _fill_sizes([r for r in rows if r.get("exists")] + hidden_protected)
    # Newest first: the thing you just closed is the thing you are most likely
    # to want back. The UI re-sorts, and its sort is stable, so ties keep this.
    rows.sort(key=lambda r: r.get("last_used") or 0.0, reverse=True)
    return {
        "rows": rows,
        "stale_days": float(days),
        "hidden": {
            # Counted and NAMED though they are not rows: these are exactly the
            # directories that fill a disk, and the page that used to explain
            # them is this one.
            "protected": len(hidden_protected),
            "protected_names": sorted(d["name"] for d in hidden_protected),
            "protected_bytes": sum(d.get("size_bytes") or 0 for d in hidden_protected),
            "active": len(set(hidden_active)),
            "active_titles": sorted(set(hidden_active)),
            # Narrowed to the sweep's own scope (see _in_sweep_scope): what it
            # could have taken and did not.
            "active_worktree_titles": sorted(set(hidden_active_wt)),
            "protected_in_root": hidden_protected_in_root,
        },
        "roots": srv._workspace_roots(),
    }


def stale_worktree_targets(days: float = STALE_WORKTREE_DAYS) -> tuple:
    """``(targets, kept)`` — the unused worktrees a sweep would remove, resolved
    HERE from a fresh scan rather than from anything a client sends.

    A target has to pass every one of these, in order:

    1. it sits strictly under a managed workspace root, so no path outside
       MindFlock's own directories is reachable even by traversal;
    2. its ``.git`` is a gitdir FILE — a linked worktree git generated
       (:func:`_worktree_gitdir`). A repository, a clone, a ``_base_*`` mirror
       and any plain folder are all excluded by this one test, which is what the
       feature's "never a repo, never a workspace git did not make" rests on;
    3. no live session is working in it, and no second session shares it;
    4. nothing has touched it for ``days`` (:func:`_last_used`).

    ``kept`` counts what each rule turned away — a sweep that removes three of
    forty should be able to say why, not just report a number.
    """
    srv = _server()
    data = recent_rows(sizes=False, days=days)
    # ONLY MindFlock's own worktrees dir — deliberately not `_workspace_roots()`,
    # which also covers the provisioning workspace dir, and that one holds real
    # clones (`_base_*` mirrors, `pr-*` review clones, clone-strategy
    # workspaces) and on some machines sits inside the user's own repo. Two
    # independent guards, either of which alone would be enough: this root, and
    # the gitdir-file test below.
    root = _worktrees_root()
    targets: dict = {}
    kept = {
        # Seeded from what recent_rows already withheld: a workspace a live
        # session owns is not a row at all, so iterating rows alone would report
        # "kept 0" for the very thing the sweep most needs to say it skipped.
        "active": list(data["hidden"]["active_worktree_titles"]),
        "recent": 0,
        "not_worktree": 0,
        "outside_root": 0,
        "protected": data["hidden"]["protected_in_root"],
    }
    seen, vetoed = set(), set()
    for r in data["rows"]:
        real = _realpath(r.get("path") or "")
        if not real or not os.path.isdir(real):
            continue
        # Only ever a proper child of the worktrees root (realpath has already
        # collapsed any `..`, so this cannot be walked around).
        if not root or not srv._strictly_under(real, root):
            if real not in seen:
                seen.add(real)
                kept["outside_root"] += 1
            continue
        first = real not in seen
        seen.add(real)
        if not _worktree_gitdir(real) or os.path.ismount(real):
            # A repo, a clone or a plain folder: never ours to delete in bulk.
            # A mount point is refused whatever it holds — realpath does not
            # unwind a bind mount, so a repo mounted into the worktrees tree
            # would otherwise look like a path inside it.
            kept["not_worktree"] += 1 if first else 0
            continue
        if r.get("active_session") or srv._worktree_in_use_by_other(real, ""):
            if first:
                kept["active"].append(r.get("active_session") or "")
            continue
        if not r.get("stale"):
            kept["recent"] += 1 if first else 0
            # A veto, not just a skip: two rows can name one directory, and one
            # of them saying "used recently" has to beat the other saying
            # "stale" (recent_rows already harmonizes their age — this is the
            # structural guarantee that nothing can drift past it).
            vetoed.add(real)
            continue
        t = targets.setdefault(
            real,
            {
                "name": r.get("name") or os.path.basename(real),
                "path": real,
                "branch": r.get("branch") or "",
                "last_used": r.get("last_used"),
                "size_bytes": None,
                "dirty": False,
                # Several closed entries can share one worktree (a copy and its
                # origin), and removing the directory has to forget them all —
                # otherwise the page keeps rows offering a Reopen that 410s.
                "closed_ids": [],
                "titles": [],
                "repo_path": _worktree_repo_path(_worktree_gitdir(real)),
            },
        )
        if r.get("source") == "closed" and r.get("id"):
            t["closed_ids"].append(r["id"])
            if r.get("title"):
                t["titles"].append(r["title"])
    for real in vetoed:
        targets.pop(real, None)
    kept["active"] = sorted({t for t in kept["active"] if t})
    return sorted(targets.values(), key=lambda t: t["name"]), kept


def _delete_targets(doomed: list, kept: dict) -> tuple:
    """Remove the resolved targets, re-verifying every one as it goes.

    Split out so the sweep's lock wraps exactly the deletes, and so the two
    re-checks below are impossible to lose in a refactor of the reporting around
    them.
    """
    srv = _server()
    removed, failed, forgot = [], [], []
    for t in doomed:
        # Re-verified at the moment of deletion, not only at scan time: this is
        # the single guard between a generated worktree and somebody's
        # repository, and it costs one stat.
        if not _worktree_gitdir(t["path"]) or os.path.ismount(t["path"]):
            failed.append(t["name"])
            continue
        # And re-check ownership: the active map was read before this sweep
        # started, and a session (or a co-running MindFlock server's session)
        # can have claimed the directory since.
        if srv._worktree_in_use_by_other(t["path"], ""):
            kept["active"].append("(claimed mid-sweep)")
            continue
        if srv._remove_worktree_path(t["path"], t.get("repo_path") or ""):
            removed.append(t["name"])
            forgot.extend(t.get("closed_ids") or [])
        else:
            failed.append(t["name"])
    return removed, failed, forgot


def prune_stale_worktrees(
    days: float = STALE_WORKTREE_DAYS,
    dry_run: bool = True,
    include_dirty: bool = False,
) -> dict:
    """Remove every unused worktree (or, with ``dry_run``, just describe them).

    What a removal costs is worth being precise about, because the UI repeats it
    in the confirmation: a worktree's branch and commits live in the BASE
    repository and survive the directory going, so the only thing at risk is
    what the base repo has never seen — uncommitted changes, a stash, a detached
    HEAD (:func:`_worktree_local_work`). Those rows are reported as ``dirty`` and
    are held back unless ``include_dirty``, so "remove all unused worktrees" can
    be answered honestly in two clicks instead of silently doing the destructive
    half of it.

    Empty branch-slug directories left behind by the removals are collected too
    (:func:`_prune_empty_worktree_dirs`) — they are what a year of nested
    worktree paths leaves under the root.
    """
    srv = _server()
    targets, kept = stale_worktree_targets(days)
    if targets:
        # A `du` and two git calls each — the slow part, so run it concurrently.
        _fill_sizes(targets)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            flags = pool.map(_worktree_local_work, [t["path"] for t in targets])
            for t, dirty in zip(targets, flags):
                t["dirty"] = bool(dirty)
    doomed = [t for t in targets if include_dirty or not t["dirty"]]
    out = {
        "ok": True,
        "dry_run": bool(dry_run),
        "days": float(days),
        "include_dirty": bool(include_dirty),
        "candidates": targets,
        "candidate_count": len(targets),
        "total_bytes": sum(t.get("size_bytes") or 0 for t in targets),
        "dirty_count": sum(1 for t in targets if t.get("dirty")),
        "kept": kept,
    }
    if dry_run:
        return out

    with _SWEEP_LOCK:
        removed, failed, forgot = _delete_targets(doomed, kept)
    if forgot:
        # A removed worktree leaves its closed entry offering a Reopen that can
        # only 410 — the per-row wipe drops the entry for the same reason.
        gone = set(forgot)
        srv._save_recently_closed(
            [e for e in srv._load_recently_closed() if e.get("id") not in gone]
        )
    taken = set(removed)
    out.update(
        {
            "removed": removed,
            "removed_count": len(removed),
            "failed": failed,
            "forgot": len(forgot),
            "kept_dirty": [t["name"] for t in targets if t not in doomed],
            "freed_bytes": sum(
                t.get("size_bytes") or 0 for t in targets if t["name"] in taken
            ),
            "empty_dirs_removed": _prune_empty_worktree_dirs() if removed else 0,
        }
    )
    return out
