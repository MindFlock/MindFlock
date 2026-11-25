"""Favorite sessions: the ones you keep coming back to.

A person who returns to the same kind of work keeps ONE session for it rather
than starting a fresh one each time. Starring a session (its ⋯ menu →
Favorite) puts it in the sidebar's Favorites list, open or closed: an open
one is a click away, a closed one reopens from its Recently closed entry with
the same worktree, branch and conversation. A favorite is the person's on
purpose, so the auto-close sweep never closes it (core.auto_close,
core.remote_origin).

Keyed by title (a reopened session keeps its title; a rename moves the star —
core.rename_stores). Persisted at ``<config dir>/favorites.json`` (or
``$MINDFLOCK_FAVORITES_FILE``): ``{"favorites": {title: {"at": epoch}}}``.
Synced nowhere: it names this machine's sessions.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard
from backend.fsutil import atomic_write_json, read_json
from backend.web.core import title_store

__all__ = [
    "store_path",
    "favorites",
    "is_favorite",
    "set_favorite",
    "rename",
    "forget",
    "listing",
]

_FileName = "favorites.json"
_LOCK = threading.RLock()
#: ``(path, mtime_ns, size)`` of the last read and what it parsed to: every
#: row build asks.
_CACHE: Dict[str, object] = {"sig": None, "data": None}


def store_path() -> str:
    """``$MINDFLOCK_FAVORITES_FILE`` (tests point it at a tmp file), else
    ``<config dir>/favorites.json``. Refused under pytest when it resolves
    into the real home (``backend.config.home_guard``)."""
    env = os.environ.get("MINDFLOCK_FAVORITES_FILE")
    if env:
        return guard(env, "favorites")
    return guard(os.path.join(GetConfigDir(), _FileName), "favorites")


def _signature(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return (p, None, None)
    return (p, st.st_mtime_ns, st.st_size)


def _read(p: str) -> Dict[str, dict]:
    data = read_json(p, {}, expect=dict) or {}
    part = data.get("favorites")
    if not isinstance(part, dict):
        return {}
    return {
        k: dict(v)
        for k, v in part.items()
        if isinstance(k, str) and k and isinstance(v, dict)
    }


def favorites() -> Dict[str, dict]:
    """``{title: {"at": epoch}}`` — a private copy, cached by the file's
    signature. Never raises (unreadable reads as none)."""
    try:
        p = store_path()
    except Exception:  # noqa: BLE001
        return {}
    sig = _signature(p)
    with _LOCK:
        if _CACHE["sig"] == sig and _CACHE["data"] is not None:
            return {k: dict(v) for k, v in _CACHE["data"].items()}  # type: ignore[union-attr]
    data = _read(p)
    with _LOCK:
        _CACHE["sig"], _CACHE["data"] = sig, data
    return {k: dict(v) for k, v in data.items()}


def is_favorite(title: str) -> bool:
    return bool(title) and title in favorites()


def _write(mutate) -> Dict[str, dict]:
    """Read-modify-write under the process lock AND the file lock (another
    server on this config dir writes it too). Returns the new map."""
    p = store_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with _LOCK, title_store.file_lock(p):
        data = _read(p)
        before = repr(data)
        mutate(data)
        if repr(data) != before:
            atomic_write_json(
                p, {"favorites": data}, indent=None, newline=False, ensure_ascii=True
            )
        _CACHE["sig"], _CACHE["data"] = None, None
        return data


def set_favorite(title: str, on: bool, now: Optional[float] = None) -> bool:
    """Star (``on``) or unstar ``title``. Returns whether it is a favorite."""
    title = str(title or "").strip()
    if not title:
        return False
    at = time.time() if now is None else float(now)

    def _m(data):
        if on:
            data.setdefault(title, {"at": at})
        else:
            data.pop(title, None)

    return title in _write(_m)


def rename(old: str, new: str) -> None:
    """The star follows a renamed session (core.rename_stores)."""
    if not old or not new or old == new:
        return

    def _m(data):
        if old in data:
            data[new] = data.pop(old)

    _write(_m)


def forget(title: str) -> None:
    _write(lambda data: data.pop(title, None))


def listing(live: Dict[str, dict], closed: List[dict]) -> List[dict]:
    """The Favorites list, starred first-come first: one row per favorite —
    ``{"title", "at", "state": "open" | "closed" | "gone", "entry_id",
    "branch", "folder", "closed_at", "wiped"}``. ``live`` maps this server's
    session titles to their published rows; ``closed`` is the Recently closed
    store (newest last), whose newest entry for a closed favorite is the one
    its Reopen uses. ``gone``: neither open nor closed (forgotten, or its
    folder removed with it)."""
    favs = favorites()
    newest: Dict[str, dict] = {}
    for e in closed or ():
        if isinstance(e, dict) and e.get("title") in favs:
            newest[str(e["title"])] = e  # later entries win
    out: List[dict] = []
    for title, rec in sorted(favs.items(), key=lambda kv: float(kv[1].get("at") or 0)):
        row = {"title": title, "at": rec.get("at"), "entry_id": "", "closed_at": ""}
        if title in live:
            r = live[title] or {}
            row.update(
                state="open",
                branch=str(r.get("branch") or ""),
                folder=str(r.get("folder") or r.get("path") or ""),
                wiped=False,
            )
        elif title in newest:
            e = newest[title]
            wiped = bool(e.get("wiped"))
            row.update(
                state="gone" if wiped else "closed",
                entry_id=str(e.get("id") or ""),
                branch=str(e.get("branch") or ""),
                folder=str(e.get("folder") or ""),
                closed_at=str(e.get("closed_at") or ""),
                wiped=wiped,
            )
        else:
            row.update(state="gone", branch="", folder="", wiped=False)
        out.append(row)
    return out
