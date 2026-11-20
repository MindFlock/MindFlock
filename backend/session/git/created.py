"""A small record of every worktree MindFlock adds: ``<config>/worktrees_created.jsonl``.

The worktree garbage collector (``backend/web/core/worktree_gc.py``) derives
almost everything it needs from what already exists — the engine's state, the
recently-closed store, ``git worktree list`` — but two facts are gone once a
session's records age out: that MindFlock made a worktree that does not sit
under its worktrees root, and whether the branch was one MindFlock cut (and may
delete once merged) or one the person brought (never deleted). One JSON line
per ``Setup`` keeps both. Append-only while a worktree lives; the collector
drops a line when it removes the worktree. Never raises.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Iterable

FILE_NAME = "worktrees_created.jsonl"

_lock = threading.Lock()


def _norm(path: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return os.path.abspath(path)


def records_path() -> str:
    from backend import config

    return os.path.join(config.GetConfigDir(), FILE_NAME)


def record_created(
    path: str,
    repo: str,
    branch: str,
    *,
    title: str = "",
    base: str = "",
    existing_branch: bool = False,
) -> None:
    """Append "MindFlock made the worktree at ``path``" (its repo, branch,
    session title, base, and whether the branch pre-existed). A path already
    recorded with the same branch is left as it is: ``Setup`` runs again on
    every resume, and the first line (written when the worktree was cut) is
    the one that knows whether MindFlock made the branch."""
    if not path:
        return
    prior = load_records().get(_norm(path))
    if prior and prior.get("branch") == (branch or ""):
        return
    rec = {
        "path": _norm(path),
        "repo": _norm(repo) if repo else "",
        "branch": branch or "",
        "title": title or "",
        "base": base or "",
        "existing_branch": bool(existing_branch),
        "ts": time.time(),
    }
    try:
        p = records_path()
        with _lock:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — a record must never fail a launch
        pass


def load_records() -> Dict[str, dict]:
    """The records by normalized path (the newest line per path wins)."""
    out: Dict[str, dict] = {}
    try:
        with open(records_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("path"):
                    out[_norm(str(rec["path"]))] = rec
    except Exception:  # noqa: BLE001
        pass
    return out


def forget_records(paths: Iterable[str]) -> None:
    """Drop the lines of removed worktrees (rewritten atomically)."""
    gone = {_norm(p) for p in paths if p}
    if not gone:
        return
    try:
        p = records_path()
        with _lock:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            keep = []
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(rec, dict)
                    and _norm(str(rec.get("path") or "")) not in gone
                ):
                    keep.append(line if line.endswith("\n") else line + "\n")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass
