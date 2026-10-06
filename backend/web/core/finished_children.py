"""Sub-sessions that have finished: what an orchestrator's Thread still shows
once a worker is closed or deleted.

A child leaves the registry when it is closed (✕, ``kill_session`` close) or
deleted (``kill_session`` delete, a merge that cleans it up). Its parent link
lived only on the child, so without this store the Thread forgot it the moment
it went — the person watching an orchestrator could not tell "my three workers
finished" from "nothing happened".

On removal, :func:`record` keeps one small entry per finished child under its
parent, built from the child's last snapshot row (branch, how far it got, its
PR, its diff size, its final report). :func:`for_parent` reads them back for
the parent's Thread. Entries carry the parent's creation time, so a parent
title that is reused later does not inherit its namesake's history, and a
removed parent takes its list with it (:func:`forget_parent`).

Persisted at ``<config dir>/finished_children.json`` (or
``$MINDFLOCK_FINISHED_CHILDREN_FILE``), bounded per parent and in total.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Mapping, Optional

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard

__all__ = [
    "PER_PARENT",
    "MAX_PARENTS",
    "store_path",
    "record",
    "for_parent",
    "forget_parent",
]

_FileName = "finished_children.json"
#: Finished children kept per parent (oldest dropped first).
PER_PARENT = 50
#: Parents with a list (the one whose newest entry is oldest is dropped first).
MAX_PARENTS = 200
#: Two creation times within this many seconds are the same session.
_SAME_S = 1.0

_LOCK = threading.Lock()


def store_path() -> str:
    """``$MINDFLOCK_FINISHED_CHILDREN_FILE`` (tests point it at a tmp file),
    else ``<config dir>/finished_children.json``. Refused under pytest when it
    resolves into the real home (see ``backend.config.home_guard``)."""
    env = os.environ.get("MINDFLOCK_FINISHED_CHILDREN_FILE")
    if env:
        return guard(env, "finished children")
    return guard(os.path.join(GetConfigDir(), _FileName), "finished children")


def _load() -> Dict[str, List[dict]]:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, list)}


def _save(data: Mapping[str, List[dict]]) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".finished.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same(a: Optional[float], b: Optional[float]) -> bool:
    """Same session by creation time; an unknown time matches anything."""
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) < _SAME_S


def _num(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record(
    row: Mapping,
    *,
    parent_created: Optional[float],
    how: str,
    seed: str = "",
    now: Optional[float] = None,
) -> Optional[dict]:
    """Remember a child that just left the registry, from its last snapshot
    row. ``how`` is ``"closed"`` (reopenable from Recently closed) or
    ``"deleted"``. Returns the stored entry, or None when the row has no
    parent (a root session is nobody's finished child)."""
    title = str(row.get("title") or "")
    parent = str(row.get("parent") or "")
    if not title or not parent or parent == title:
        return None
    report = (
        row.get("last_report") if isinstance(row.get("last_report"), Mapping) else None
    )
    entry = {
        "title": title,
        "branch": str(row.get("branch") or ""),
        "created_at": _num(row.get("created_at")),
        "ended_at": float(now if now is not None else time.time()),
        "how": how if how in ("closed", "deleted") else "deleted",
        "stage": str(row.get("stage") or ""),
        "pr_url": str(row.get("pr_url") or ""),
        "diff_stat": (
            row.get("diff_stat") if isinstance(row.get("diff_stat"), Mapping) else None
        ),
        "last_report": dict(report) if report else None,
        "seed": str(seed or ""),
        "parent_created": _num(parent_created),
    }
    with _LOCK:
        data = _load()
        kept = [
            e
            for e in data.get(parent, [])
            if not (
                e.get("title") == title
                and _same(e.get("created_at"), entry["created_at"])
            )
        ]
        kept.append(entry)
        data[parent] = kept[-PER_PARENT:]
        if len(data) > MAX_PARENTS:
            newest = {
                p: max((e.get("ended_at") or 0) for e in lst) if lst else 0
                for p, lst in data.items()
            }
            for p in sorted(newest, key=newest.get)[: len(data) - MAX_PARENTS]:
                data.pop(p, None)
        _save(data)
    return entry


def for_parent(parent: str, parent_created: Optional[float]) -> List[dict]:
    """``parent``'s finished children, oldest first — only those recorded
    while it was the session it is now (matched on its creation time)."""
    if not parent:
        return []
    with _LOCK:
        entries = _load().get(parent, [])
    return [e for e in entries if _same(e.get("parent_created"), parent_created)]


def forget_parent(parent: str) -> None:
    """Drop ``parent``'s list (the parent itself was removed)."""
    if not parent:
        return
    with _LOCK:
        data = _load()
        if data.pop(parent, None) is not None:
            _save(data)
