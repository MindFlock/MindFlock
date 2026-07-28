"""Workspace disk management: roots, discovery, classification, deletion.

The helpers behind the Workspaces / disk manager screen and the delete/cleanup
paths: which directories hold deletable workspaces (:func:`_workspace_roots`),
finding worktree leaves and sizing them, classifying entries (base clone /
refresher / pr / worktree / plain workspace), the K4 delete guard
(:func:`_base_clone_references`), and the guarded permanent removal of a
worktree directory (:func:`_remove_worktree_path` — only ever under a managed
root).

Split out of ``backend.web.server`` (which re-imports these names — the
workspace routes and tests reference them through the server namespace).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from backend import config
from backend.providers.claude import remove_trust_entry as _remove_trust_entry
from backend.session import provisioned as provisioning
from backend.web.core.cursor_windows import _close_cursor_window
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
    return True


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
