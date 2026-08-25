"""The workspace a previous run of an intake item left behind on this machine.

Intake rows answer "should this be started?" — and for an item that has been
worked once already they answer it with chips: *already ingested*, *a feature
branch for it already exists on the remote*. Both are true, and both used to
lead nowhere: the only button on the row was **Begin work**, which starts a
*fresh* session. But the work is usually still right there — ending a session
keeps its worktree on disk (see :mod:`backend.web.core.recently_closed`), and a
run whose session was lost to a restart leaves its worktree behind too (the
state :mod:`backend.web.core.worktree_reclaim` exists to clean up). Starting
over would either collide with that worktree or silently duplicate it.

So this module answers the other question — *is the work still on this
machine?* — for the three intake panels, in one place, because the three
already share their row rendering and must not disagree about what a row can
do. Resolution order, most informative first:

1. **A recently-closed session** for this item. Best case by far: the stashed
   ``InstanceData`` carries the branch, program, prompt and provisioning flags,
   so reopening restores the session rather than approximating it.
2. **A provisioned clone directory** — the PR-review workspace (``pr-<slug>``)
   or a clone-strategy ticket/issue workspace, both of which are deterministic
   paths, so an ``isdir`` is the whole probe.
3. **A worktree still holding the item's branch**, found by asking the base
   clone (one ``git worktree list`` per repo per request, indexed).

Everything is best-effort and read-only: any failure answers "nothing found",
which is exactly the behaviour the panels had before. The reopen itself lives
in ``server.py`` (it needs the engine).
"""

from __future__ import annotations

import os
import subprocess

#: Per-git-call ceiling. This runs while a panel request is open, so a hung git
#: must degrade to "no workspace found" rather than hold the listing.
_GIT_TIMEOUT = 15


def _is_repo_dir(path: str) -> bool:
    """True when ``path`` is a directory that still holds a git checkout.

    A bare ``isdir`` isn't enough: a half-deleted workspace (or a directory the
    user emptied by hand) would offer a Reopen that lands the agent in a
    non-repo, where every git-backed panel in the app then fails.
    """
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def _repo_dir(path: str, cache: dict) -> bool:
    """:func:`_is_repo_dir`, memoised for the whole annotation pass.

    A panel annotates every row against every closed entry, so the naive form
    restats the same handful of directories once per (row, entry) pair — tens
    of thousands of stats on a listing of a thousand tickets, all of them on
    the request path. The set of directories involved is tiny and cannot change
    while one response is being built, so one stat each is the whole cost.
    """
    key = ("isdir", path)
    if key not in cache:
        cache[key] = _is_repo_dir(path)
    return cache[key]


def _closed_entries(cache: dict) -> list:
    """The recently-closed store, read once per annotation pass."""
    if "closed" not in cache:
        try:
            from backend.web import server

            cache["closed"] = server._load_recently_closed()
        except Exception:  # noqa: BLE001 — unreadable store: nothing to offer
            cache["closed"] = []
    return cache["closed"]


def _closed_index(cache: dict) -> tuple[dict, dict]:
    """``({title: [entry]}, {branch: [entry]})`` over the still-present closed
    sessions, built once per annotation pass.

    :func:`_closed_match` used to scan the whole store per row and stat every
    entry's folder as it went. Both halves of that are per-pass work, not
    per-row work: the store is read once, each folder is checked once, and a
    row's lookup becomes two dict hits. Order within each bucket is the store's
    own (newest first), so the entry a row resolves to is unchanged.
    """
    if "closed_index" in cache:
        return cache["closed_index"]
    by_title: dict = {}
    by_branch: dict = {}
    for pos, entry in enumerate(_closed_entries(cache)):
        if not isinstance(entry, dict):
            continue
        if not _repo_dir(str(entry.get("folder") or ""), cache):
            continue
        title = entry.get("title") or ""
        branch = entry.get("branch") or ""
        # The position rides along so a row matching one entry by title and an
        # earlier one by branch still resolves to the earlier one, exactly as
        # the single ordered scan did.
        if title:
            by_title.setdefault(title, []).append((pos, entry))
        if branch:
            by_branch.setdefault(branch, []).append((pos, entry))
    cache["closed_index"] = (by_title, by_branch)
    return cache["closed_index"]


def _head_branch(path: str) -> str:
    """The branch ``path`` currently has checked out ("" if it can't be read)."""
    try:
        cp = subprocess.run(
            ["git", "-C", path, "symbolic-ref", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def _still_holds(entry: dict, cache: dict | None = None) -> bool:
    """Whether a closed session's folder is still the work it was closed on.

    A recently-closed entry records the branch as of the moment it was closed,
    and the folder may have moved on since — the case that made this necessary
    is a session run in place on a real checkout, whose entry claims a ticket's
    feature branch long after the user switched that checkout back to main.
    Offering *that* as "this ticket's workspace" would be a lie the button acts
    on, so a definite mismatch disqualifies the entry. An unreadable HEAD does
    not: it is not evidence of anything, and Recent… would still reopen it.
    """
    branch = str(entry.get("branch") or "")
    if not branch:
        return True
    folder = str(entry.get("folder") or "")
    # Memoised per annotation pass: this is a git subprocess, and one closed
    # session can be a candidate for many rows of the same listing.
    if cache is None:
        head = _head_branch(folder)
    else:
        key = ("head", folder)
        if key not in cache:
            cache[key] = _head_branch(folder)
        head = cache[key]
    return not head or head == branch


def _closed_match(title: str, branch: str, cache: dict) -> dict | None:
    """The newest closed session for this item whose folder is still there and
    still holds its work.

    Matched on the session title (an intake session is titled by the item's own
    slug, so the title *is* the item's identity) or on the branch, which covers
    a session that was renamed or started by the pipeline under its own title.
    Both lookups go through the pass-wide index, so a row that matches nothing
    — which is most rows on a big listing — costs two dict misses.
    """
    by_title, by_branch = _closed_index(cache)
    candidates = (by_title.get(title, []) if title else []) + (
        by_branch.get(branch, []) if branch else []
    )
    seen: set = set()
    for pos, entry in sorted(candidates, key=lambda c: c[0]):
        if pos in seen:
            continue
        seen.add(pos)
        if _still_holds(entry, cache):
            return entry
    return None


def _provision_settings(repo_url: str, cache: dict):
    """Provisioning settings for ``repo_url`` (``None`` when unresolvable).

    Cached per repo URL: the three panels annotate every row, and the sources
    behind them nearly always collapse onto one or two repos.
    """
    key = ("settings", repo_url or "")
    if key not in cache:
        try:
            from backend.session.provisioned import load_provision_settings

            cache[key] = load_provision_settings(repo_url_override=repo_url or "")
        except Exception:  # noqa: BLE001
            cache[key] = None
    return cache[key]


def _worktree_index(repo_url: str, cache: dict) -> dict:
    """``{branch: worktree path}`` for the base clone behind ``repo_url``.

    One ``git worktree list`` per repo per request rather than
    :func:`worktree_holding_branch` per row — a panel lists tens of items and
    they nearly all share one base clone. The base clone is only ever *read*
    here (never ``ensure_base_repo``, which would clone on a panel request).
    """
    key = ("worktrees", repo_url or "")
    if key in cache:
        return cache[key]
    index: dict = {}
    settings = _provision_settings(repo_url, cache)
    base = ""
    if settings is not None:
        try:
            from backend.session.provisioned import resolve_base_repo_dir

            base = str(resolve_base_repo_dir(settings))
        except Exception:  # noqa: BLE001
            base = ""
    if base and os.path.isdir(os.path.join(base, ".git")):
        index = _list_worktrees(base)
    cache[key] = index
    return index


def _list_worktrees(base_repo: str) -> dict:
    """Parse ``git worktree list --porcelain`` into ``{branch: path}``.

    The porcelain form emits a ``worktree <path>`` line followed by the
    attributes of that worktree, so the branch is recorded against whichever
    path was named last. The base clone's own checkout is included and harmless:
    an intake item's feature branch is never what the base clone has out.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", base_repo, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if cp.returncode != 0:
        return {}
    out: dict = {}
    current = ""
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :].strip()
        elif line.startswith("branch refs/heads/") and current:
            out[line[len("branch refs/heads/") :].strip()] = current
    return out


def clone_workspace_path(repo_url: str, branch: str, cache: dict) -> str:
    """Where a clone-strategy session for ``branch`` would live ("" if unknown).

    Mirrors ``build_provisioned_worktree``'s clone branch verbatim
    (``<workspace_dir>/<branch with slashes flattened>``) — the same derivation
    the pipeline's own provisioner uses, so this can't point at a directory the
    launch would never have created.
    """
    settings = _provision_settings(repo_url, cache)
    if settings is None or not branch:
        return ""
    try:
        return str(settings.workspace_dir / branch.replace("/", "-"))
    except Exception:  # noqa: BLE001
        return ""


def find_workspace(
    *,
    title: str = "",
    branch: str = "",
    repo_url: str = "",
    strategy: str = "",
    workspace_path: str = "",
    cache: dict | None = None,
) -> dict | None:
    """The reopenable workspace for one intake item, or ``None``.

    ``cache`` is a per-request scratch dict shared across the rows of one panel
    response (see :func:`_worktree_index`); pass the same one for every row.

    The returned dict is what the row carries to the UI and back to the reopen
    endpoint: ``kind`` (``closed`` | ``clone`` | ``worktree``), the absolute
    ``path``, the ``branch`` it holds, and — for a closed session — the store
    ``entry_id`` that restores it in full.
    """
    cache = cache if cache is not None else {}

    entry = _closed_match(title, branch, cache)
    if entry is not None:
        return {
            "kind": "closed",
            "path": entry.get("folder") or "",
            "entry_id": entry.get("id") or "",
            "branch": entry.get("branch") or branch,
            "closed_at": entry.get("closed_at") or "",
        }

    # A workspace whose path the caller already knows (PR review provisions
    # `pr-<slug>` itself and hands the directory to the engine).
    if workspace_path and _repo_dir(workspace_path, cache):
        return {"kind": "clone", "path": workspace_path, "branch": branch}

    if not branch:
        return None

    if strategy == "clone":
        path = clone_workspace_path(repo_url, branch, cache)
        return (
            {"kind": "clone", "path": path, "branch": branch}
            if _repo_dir(path, cache)
            else None
        )

    path = _worktree_index(repo_url, cache).get(branch, "")
    return (
        {"kind": "worktree", "path": path, "branch": branch}
        if _repo_dir(path, cache)
        else None
    )


def annotate(rows, resolve, cache: dict | None = None) -> None:
    """Stamp ``workspace`` onto every row a reopen is possible for.

    ``resolve(row)`` returns the :func:`find_workspace` kwargs for that row.
    Rows that already have a live session are skipped — the work is on screen,
    so offering to reopen it would be noise (and the probe would be wasted).
    Annotating in place, on the per-request copies the endpoints already make,
    keeps this live on a cache hit for the same reason ``has_session`` is
    annotated there.
    """
    cache = cache if cache is not None else {}
    for row in rows:
        if not isinstance(row, dict) or row.get("has_session"):
            continue
        try:
            found = find_workspace(cache=cache, **resolve(row))
        except Exception:  # noqa: BLE001 — a probe must never fail a listing
            found = None
        if found is not None:
            row["workspace"] = found
