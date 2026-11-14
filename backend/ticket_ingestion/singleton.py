"""One ticket pipeline per MindFlock home.

The pipeline's singleton lock used to sit in its cwd (``<repo root>/
.mindflock-pipeline.lock``). Every server starts its own pipeline child in
ITS repo root, so two servers sharing one ``~/.mindflock`` (2026-11-13: the
:8765 service from ``~`` and a :9000 dev server from the app checkout) took
two different locks and ran two pipelines against one engine state: both
polled Shortcut, both launched the same ticket, and the loser's failure path
cancelled the winner's flight.

The lock now lives in the MindFlock home (:func:`lock_path`:
``GetConfigDir()``, so ``$MINDFLOCK_HOME`` moves it) — the thing two
pipelines actually share. The old cwd lock (:func:`legacy_lock_path`) is
still TAKEN by a new pipeline, so an older-build pipeline running in the
same root (started before an update) keeps excluding a new one, and still
READ by the server, so it can find (and restart) that older pipeline.

Each lock file reads: line 1 the holder's PID, line 2 its JSON metadata
(``__main__._lock_meta``: build, owner, the starting server's pid/port and
the pipeline's root).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from backend import compat

#: The lock in the MindFlock home.
LOCK_NAME = "pipeline.lock"
#: The lock older pipelines took in their cwd (the repo root).
LEGACY_LOCK_NAME = ".mindflock-pipeline.lock"


def lock_path(fallback_dir: Path | str) -> Path:
    """The pipeline lock in the MindFlock home; when that can't be resolved
    (no ``$HOME``; under pytest, the real home), the legacy lock in
    ``fallback_dir`` — one pipeline per repo root, as before."""
    try:
        from backend.config.config import GetConfigDir

        home = Path(GetConfigDir())
    except Exception:  # noqa: BLE001 — no home: the old per-root lock
        return legacy_lock_path(fallback_dir)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return legacy_lock_path(fallback_dir)
    return home / LOCK_NAME


def legacy_lock_path(root: Path | str) -> Path:
    """The per-root lock older pipelines took (and new ones still take)."""
    return Path(root) / LEGACY_LOCK_NAME


def probe(path: Path) -> "tuple[Optional[int], Optional[dict]]":
    """``(pid, meta)`` of whoever holds the lock at ``path`` — ``(None,
    None)`` when nobody does (no file, or a stale one we could take).

    Authoritative via a non-blocking ``flock`` (taken and released at once
    when free). ``pid`` is what line 1 records, ``-1`` when held but
    unreadable; ``meta`` is line 2, ``None`` for a lock written before it
    recorded any."""
    if not path.exists():
        return None, None
    try:
        fh = open(path, "a+")
    except OSError:
        return None, None
    try:
        try:
            compat.flock(fh.fileno(), compat.LOCK_EX | compat.LOCK_NB)
        except OSError:
            try:
                fh.seek(0)
                lines = (fh.read() or "").splitlines()
            except OSError:
                return -1, None
            try:
                pid = int((lines[0] if lines else "").strip() or 0) or -1
            except ValueError:
                pid = -1
            meta = None
            if len(lines) > 1:
                try:
                    meta = json.loads(lines[1])
                except ValueError:
                    meta = None
            return pid, meta if isinstance(meta, dict) else None
        compat.flock(fh.fileno(), compat.LOCK_UN)  # nobody holds it -> stale
        return None, None
    finally:
        fh.close()


def take(path: Path, text: str):
    """Take the lock at ``path`` and write ``text`` into it → the open
    handle (keep it: closing releases the lock), or ``None`` when someone
    else holds it.

    Opened WITHOUT truncating (``a+``): a losing process must not wipe the
    winner's PID before its flock check fails. Only the winner rewrites it."""
    fh = open(path, "a+")
    try:
        compat.flock(fh.fileno(), compat.LOCK_EX | compat.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate(0)
    fh.write(text)
    fh.flush()
    return fh
