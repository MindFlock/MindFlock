"""Usage Optimizer — the sample store.

One JSON line per sample, appended as the sampler ticks, in the extension's
own file (``~/.mindflock/usage_optimizer.jsonl``, mode 0600 — never
settings.json). Appending is the write path because a minute-cadence
rewrite of a month of samples would be the most expensive thing the server
does; a compaction (drop lines older than :data:`RETENTION_S`, rewrite
atomically) runs when the file has grown past a threshold since the last one.

Reads parse the whole file behind a short cache, so a dashboard that polls
every 30 s costs one parse per 30 s at most, not one per request.
``$MINDFLOCK_OPTIMIZER_FILE`` redirects the file (tests point it at a tmp
path; the home guard refuses the real one under pytest).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from backend.fsutil import atomic_write_text
from backend.config.config import GetConfigDir
from backend.config.home_guard import guard

__all__ = ["path", "append", "read", "compact", "RETENTION_S"]

_FILE = "usage_optimizer.jsonl"
#: Samples older than this are dropped at compaction: 35 days keeps a full
#: 30-day view plus the windows that straddle its edge.
RETENTION_S = 35 * 86400.0
#: Compact once this many lines have been appended since the last compaction
#: (or since startup) — about a day of three providers at the heartbeat
#: cadence, with change-driven writes on top.
_COMPACT_EVERY = 6000
_READ_TTL = 20.0

_LOCK = threading.Lock()
_appended = 0
_cache: dict = {"at": 0.0, "path": "", "rows": None, "mtime": 0.0}


def path() -> str:
    env = os.environ.get("MINDFLOCK_OPTIMIZER_FILE")
    if env:
        return guard(env, "usage optimizer store")
    return os.path.join(GetConfigDir(), _FILE)


def _open_append(p: str):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def append(rows: List[dict]) -> None:
    """Append ``rows`` (one JSON object per line). Never raises to the caller
    with a half-written line: the lines are joined first, then written once."""
    global _appended
    if not rows:
        return
    payload = "".join(
        json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows
    )
    with _LOCK:
        p = path()
        with _open_append(p) as f:
            f.write(payload)
        _appended += len(rows)
        _cache["rows"] = None  # invalidate the read cache
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
                if isinstance(obj, dict) and "t" in obj and "p" in obj:
                    rows.append(obj)
    except OSError:
        return []
    return rows


def read(provider: Optional[str] = None, since: Optional[float] = None) -> List[dict]:
    """All samples (optionally one provider's, optionally since an epoch),
    oldest first. Parsed at most once per :data:`_READ_TTL` seconds unless the
    file changed underneath (its mtime is checked)."""
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
    if provider is not None:
        rows = [r for r in rows if r.get("p") == provider]
    if since is not None:
        rows = [r for r in rows if float(r.get("t") or 0) >= since]
    return list(rows)


def _compact_locked(p: str) -> None:
    rows = _parse(p)
    cutoff = time.time() - RETENTION_S
    keep = [r for r in rows if float(r.get("t") or 0) >= cutoff]
    keep.sort(key=lambda r: float(r.get("t") or 0))
    payload = "".join(
        json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in keep
    )
    atomic_write_text(p, payload, mode=0o600, prefix=".uo.")
    _cache["rows"] = None


def compact() -> int:
    """Drop samples past retention and rewrite the file atomically. Returns
    the number of samples kept."""
    with _LOCK:
        p = path()
        _compact_locked(p)
        return len(_parse(p))


def last_by_provider() -> Dict[str, dict]:
    """The newest sample per provider (what the sampler diffs against)."""
    out: Dict[str, dict] = {}
    for r in read():
        out[str(r.get("p"))] = r
    return out
