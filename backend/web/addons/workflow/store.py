"""Workflow Optimizer — the timeline store.

One JSON line per observation, appended as the recorder notices a change, in
the extension's own file (``~/.mindflock/workflow_optimizer.jsonl``, mode
0600 — never settings.json). Row kinds (``k``):

* ``ph``    — a session's phase changed: ``{t, k, s, ph, a}`` (``a`` = someone
  was at the keyboard);
* ``meta``  — what a session is: ``{t, k, s, repo, branch, ticket, name, pr,
  created}``, written when first seen and when any of it changes;
* ``merge`` — the session's PR was merged: ``{t, k, s, pr}``;
* ``end``   — the session is gone (closed / deleted): ``{t, k, s}``;
* ``hb``    — the recorder is alive: ``{t, k}`` (once per
  :data:`~.recorder.HEARTBEAT_S`; a gap between them is server downtime,
  counted as untracked rather than as whatever phase was last seen).

Appending is the write path; a compaction (drop rows older than
:data:`RETENTION_S`, rewrite atomically) runs once the file has grown past a
threshold. Reads parse the whole file behind a short mtime-checked cache.
``$MINDFLOCK_WORKFLOW_FILE`` redirects the file (tests point it at a tmp path;
the home guard refuses the real one under pytest).

The extension's settings — ``include_me``, whether the person's own time
counts toward bottlenecks (default off), and ``hidden``, the phase ids the
person chose not to look at (default none), and ``count_after``, the
handoffs after the commit (pushing, the PR, review) the person counts
toward the headline (default none) — live beside the store in
``workflow_optimizer.settings.json`` (mode 0600, written atomically): an
extension's state is its own file, never a group in settings.json, so it is
this device's, like the timeline it reads.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import List, Optional

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard
from backend.fsutil import atomic_write_text

__all__ = [
    "path",
    "append",
    "read",
    "compact",
    "RETENTION_S",
    "settings_path",
    "load_settings",
    "save_settings",
    "DEFAULT_SETTINGS",
]

_FILE = "workflow_optimizer.jsonl"
_SETTINGS_FILE = "workflow_optimizer.settings.json"
#: Every setting at its default; a missing / unreadable file means these.
DEFAULT_SETTINGS = {"include_me": False, "hidden": [], "count_after": []}
#: Rows older than this are dropped at compaction: a 90-day view plus the
#: work items that straddle its edge.
RETENTION_S = 100 * 86400.0
#: Compact once this many rows have been appended since the last compaction
#: (or since startup) — a few days of heartbeats plus phase changes.
_COMPACT_EVERY = 5000
_READ_TTL = 15.0
_KINDS = ("ph", "meta", "merge", "end", "hb")

_LOCK = threading.Lock()
_appended = 0
_cache: dict = {"at": 0.0, "path": "", "rows": None, "mtime": 0.0}


def path() -> str:
    env = os.environ.get("MINDFLOCK_WORKFLOW_FILE")
    if env:
        return guard(env, "workflow optimizer store")
    return guard(os.path.join(GetConfigDir(), _FILE), "workflow optimizer store")


def _open_append(p: str):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def _lines(rows: List[dict]) -> str:
    return "".join(
        json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows
    )


def append(rows: List[dict]) -> None:
    """Append ``rows`` (one JSON object per line), joined first and written
    once, so a crash never leaves half of a batch."""
    global _appended
    if not rows:
        return
    payload = _lines(rows)
    with _LOCK:
        p = path()
        with _open_append(p) as f:
            f.write(payload)
        _appended += len(rows)
        _cache["rows"] = None
        if _appended >= _COMPACT_EVERY:
            _compact_locked(p)
            _appended = 0


def _parse(p: str) -> List[dict]:
    rows: List[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # a torn last line from a crash mid-write
                if isinstance(obj, dict) and "t" in obj and obj.get("k") in _KINDS:
                    rows.append(obj)
    except OSError:
        return []
    return rows


def read(since: Optional[float] = None) -> List[dict]:
    """All rows (optionally since an epoch), oldest first. Parsed at most once
    per :data:`_READ_TTL` seconds unless the file changed underneath."""
    with _LOCK:
        p = path()
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            mtime = 0.0
        now = time.time()
        stale = (
            _cache["rows"] is None
            or _cache["path"] != p
            or _cache["mtime"] != mtime
            or now - _cache["at"] > _READ_TTL
        )
        if stale:
            rows = _parse(p)
            rows.sort(key=lambda r: float(r.get("t") or 0))
            _cache.update({"at": now, "path": p, "rows": rows, "mtime": mtime})
        rows = _cache["rows"] or []
    if since is not None:
        rows = [r for r in rows if float(r.get("t") or 0) >= since]
    return list(rows)


def _compact_locked(p: str) -> None:
    rows = _parse(p)
    cutoff = time.time() - RETENTION_S
    keep = [r for r in rows if float(r.get("t") or 0) >= cutoff]
    keep.sort(key=lambda r: float(r.get("t") or 0))
    atomic_write_text(p, _lines(keep), mode=0o600, prefix=".wfo.")
    _cache["rows"] = None


def compact() -> int:
    """Drop rows past retention and rewrite the file. Returns rows kept."""
    with _LOCK:
        p = path()
        _compact_locked(p)
        return len(_parse(p))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def settings_path() -> str:
    """Beside the timeline (so ``$MINDFLOCK_WORKFLOW_FILE`` moves both)."""
    return guard(
        os.path.join(os.path.dirname(path()) or ".", _SETTINGS_FILE),
        "workflow optimizer settings",
    )


def _ids(v) -> Optional[list]:
    """A list of phase-id strings (deduplicated, in order), or None when
    ``v`` isn't a list. The analysis drops ids it doesn't know."""
    if not isinstance(v, list):
        return None
    out: list = []
    for x in v:
        if isinstance(x, str) and x and len(x) <= 40 and x not in out:
            out.append(x)
    return out[:40]


def _clean(raw) -> dict:
    out = {
        k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()
    }
    if isinstance(raw, dict) and isinstance(raw.get("include_me"), bool):
        out["include_me"] = raw["include_me"]
    for k in ("hidden", "count_after"):
        if isinstance(raw, dict) and _ids(raw.get(k)) is not None:
            out[k] = _ids(raw.get(k))
    return out


def load_settings() -> dict:
    """The settings, every key present (defaults for missing or bad ones)."""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return _clean(json.load(f))
    except (OSError, ValueError):
        return _clean(None)


def save_settings(patch: dict) -> dict:
    """Merge the known keys of ``patch`` into the settings, write them
    atomically (0600) and return the result. Unknown keys and values of the
    wrong type are ignored."""
    with _LOCK:
        cur = load_settings()
        patch = patch if isinstance(patch, dict) else {}
        if isinstance(patch.get("include_me"), bool):
            cur["include_me"] = patch["include_me"]
        for k in ("hidden", "count_after"):
            if _ids(patch.get(k)) is not None:
                cur[k] = _ids(patch.get(k))
        p = settings_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        atomic_write_text(
            p, json.dumps(cur, indent=2) + "\n", mode=0o600, prefix=".wfo-settings."
        )
        return cur
