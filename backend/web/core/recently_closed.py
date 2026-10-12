"""Recently-closed sessions: the undo store behind reopen / Ctrl+Z.

Ending a session (the ✕ button / Delete key) keeps its worktree on disk and
stashes the instance's serialized data here, so it can be reopened later as a
fresh agent session pointed at the same worktree. Persisted next to the engine
state in ``~/.mindflock/recently_closed.json``, capped at the newest
``_RECENTLY_CLOSED_CAP`` entries.

Split out of ``backend.web.server`` (which re-imports these names — the
recently-closed routes and tests reference them through the server namespace).

Entries can also carry a ``reason`` — why the session left, when it was not
the user closing it. The engine records one for a session it drops at load
because its folder AND its tmux are gone (:func:`record_missing_workspace`),
with ``branch_exists`` saying whether its branch survives in the repo, so a
session never disappears without a trace.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import sys
import time

from backend import config, gitutil, log, session


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


_RECENTLY_CLOSED_CAP = 50

#: ``reason`` of an entry the engine recorded for a session whose folder and
#: tmux were both gone at load (not reopenable — ``wiped`` is set too).
REASON_WORKSPACE_MISSING = "workspace missing"


def _path_fn():
    """``server._recently_closed_path`` when the server is loaded (tests patch
    it there), else this module's own — the engine records into this store
    while ``backend.web.server`` is still importing, where reaching for the
    half-built module would fail."""
    srv = sys.modules.get("backend.web.server")
    return getattr(srv, "_recently_closed_path", None) or _recently_closed_path


def _recently_closed_path() -> str:
    return os.path.join(config.GetConfigDir(), "recently_closed.json")


def _load_recently_closed() -> list:
    try:
        with open(_path_fn()()) as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _save_recently_closed(items: list) -> None:
    try:
        path = _path_fn()()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(items[:_RECENTLY_CLOSED_CAP], f)
    except OSError as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save recently-closed: %v", err)


def _hidden_by_of(title: str) -> str:
    """The session's web-metadata hidden flag ("" when none), stashed so a
    reopen can restore it; never raises."""
    try:
        from backend.web.core import hidden_sessions as _hs

        return _hs.hidden_by(title) or ""
    except Exception:  # noqa: BLE001
        return ""


def _person_of(title: str) -> str:
    """The person ``title`` was created for (core.person_sessions), or ""."""
    try:
        from backend.web.core import person_sessions as _ps

        return _ps.person(title) or ""
    except Exception:  # noqa: BLE001
        return ""


def _shared_branch_of(title: str) -> dict:
    """The session's shared-branch record ({remote, branch}) or {} — stashed so
    a reopened session keeps working on the branch it shared. Never raises."""
    try:
        from backend.web.core import shared_branch as _sb

        return dict(_sb.shared(title) or {})
    except Exception:  # noqa: BLE001
        return {}


def _record_closed(inst, wiped: bool = False) -> None:
    """Stash a closed session's serialized data. ``wiped`` records that the
    worktree was removed (listed but not reopenable; the orchestrator's
    Thread shows the worker as deleted, not closed)."""
    srv = _server()
    try:
        data = inst.ToInstanceData().to_dict()
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("record-closed %s: %v", inst.Title, err)
        return
    # Reopen should bring it back as a running session, not paused/loading.
    data["status"] = int(session.Running)
    try:
        folder = inst.GetWorktreePath() or inst.Path or ""
    except Exception:  # noqa: BLE001
        folder = getattr(inst, "Path", "") or ""
    entry = {
        "id": "%s-%d" % (inst.Title, int(time.time() * 1000)),
        "title": inst.Title,
        "branch": inst.Branch,
        "folder": folder,
        "in_place": bool(getattr(inst, "InPlace", False)),
        "provisioned": bool(getattr(inst, "Provisioned", False)),
        "closed_at": _datetime.datetime.now().astimezone().isoformat(),
        "wiped": bool(wiped),
        "hidden_by": _hidden_by_of(inst.Title),
        "shared_branch": _shared_branch_of(inst.Title),
        "person": _person_of(inst.Title),
        "data": data,
    }
    # De-dupe: drop any prior entry for the SAME worktree dir AND title, so the
    # newest close of a given session wins without a stale pile-up. Keying on the
    # title too (not folder alone) is what lets a copy and its origin — which
    # share one worktree — both stay recoverable, so Ctrl+Z can reopen each in
    # turn instead of only the last one closed.
    key = os.path.realpath(folder) if folder else None
    kept = []
    for e in srv._load_recently_closed():
        ef = e.get("folder") or ""
        same_folder = (key and ef and os.path.realpath(ef) == key) or (
            not key and not ef
        )
        same = bool(same_folder) and e.get("title") == inst.Title
        if not same:
            kept.append(e)
    kept.insert(0, entry)
    srv._save_recently_closed(kept)


def _branch_exists(repo: str, branch: str) -> bool:
    """Whether ``branch`` is still a local branch of ``repo`` (one cheap
    ``git show-ref``). False when either is unknown or git fails."""
    if not repo or not branch or not os.path.isdir(repo):
        return False
    # No git / timeout = "can't say it does".
    return gitutil.ref_exists(repo, "refs/heads/" + branch, timeout=10)


def record_missing_workspace(data) -> None:
    """Record a session the engine dropped at load because its folder and its
    tmux session are both gone (``data`` is its serialized ``InstanceData``):
    a Recently closed entry with ``reason: "workspace missing"``, ``wiped``
    (nothing left to reopen) and ``branch_exists`` (whether the branch is
    still in the repo, so the work can be recovered by hand), plus a log
    line. Uses the module's own load/save (never the server namespace): this
    runs while the server module is still being imported. Never raises."""
    try:
        wt = getattr(data, "worktree", None)
        folder = (getattr(wt, "worktree_path", "") or "").strip()
        repo = (getattr(wt, "repo_path", "") or "").strip()
        branch = (
            getattr(data, "branch", "") or getattr(wt, "branch_name", "") or ""
        ).strip()
        exists = _branch_exists(repo, branch)
        try:
            payload = data.to_dict()
        except Exception:  # noqa: BLE001
            payload = {}
        entry = {
            "id": "%s-%d" % (data.title, int(time.time() * 1000)),
            "title": data.title,
            "branch": branch,
            "folder": folder,
            "in_place": bool(getattr(data, "in_place", False)),
            "provisioned": bool(getattr(data, "provisioned", False)),
            "closed_at": _datetime.datetime.now().astimezone().isoformat(),
            "wiped": True,
            "hidden_by": "",
            "reason": REASON_WORKSPACE_MISSING,
            "branch_exists": exists,
            "repo": repo,
            "data": payload,
        }
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "dropped session %s at load: workspace %s and its tmux are gone"
                " (branch %s %s); recorded in Recently closed",
                data.title,
                folder or "?",
                branch or "?",
                "still exists" if exists else "is gone",
            )
        # Same de-dupe as _record_closed: one entry per (folder, title).
        key = os.path.realpath(folder) if folder else None
        kept = [
            e
            for e in _load_recently_closed()
            if not (
                e.get("title") == data.title
                and (
                    (key and e.get("folder") and os.path.realpath(e["folder"]) == key)
                    or (not key and not e.get("folder"))
                )
            )
        ]
        kept.insert(0, entry)
        _save_recently_closed(kept)
    except Exception as err:  # noqa: BLE001 — recording must never break load
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "record missing workspace %s: %v", getattr(data, "title", "?"), err
            )
