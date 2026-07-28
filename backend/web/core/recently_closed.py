"""Recently-closed sessions: the undo store behind reopen / Ctrl+Z.

Ending a session (the ✕ button / Delete key) keeps its worktree on disk and
stashes the instance's serialized data here, so it can be reopened later as a
fresh agent session pointed at the same worktree. Persisted next to the engine
state in ``~/.mindflock/recently_closed.json``, capped at the newest
``_RECENTLY_CLOSED_CAP`` entries.

Split out of ``backend.web.server`` (which re-imports these names — the
recently-closed routes and tests reference them through the server namespace).
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import time

from backend import config, log, session


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


_RECENTLY_CLOSED_CAP = 50


def _recently_closed_path() -> str:
    return os.path.join(config.GetConfigDir(), "recently_closed.json")


def _load_recently_closed() -> list:
    try:
        with open(_server()._recently_closed_path()) as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _save_recently_closed(items: list) -> None:
    try:
        with open(_server()._recently_closed_path(), "w") as f:
            json.dump(items[:_RECENTLY_CLOSED_CAP], f)
    except OSError as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save recently-closed: %v", err)


def _record_closed(inst) -> None:
    """Stash a closed session's serialized data (worktree is left on disk)."""
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
