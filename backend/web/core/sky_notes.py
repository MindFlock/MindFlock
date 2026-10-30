"""Sticky notes on the Sky (docs/flights.md "Round 15" → ``objects``): the
owner's notes, stored server-side in ``<flights dir>/sky_notes.json`` (per
server — never synced, unlike nothing else of the sky's layout, which lives
in the browser). ``GET`` / ``PUT /api/sky/notes`` (``routes/sky.py``) read
and replace the whole list: ``{"notes": [{id, text, x, y, w, h, line,
updated}]}``.

A note is ``x`` / ``y`` plane px (relative to its line's spot when ``line``
names one — it moves with the line), ``w`` / ``h`` its frame, ``text`` at
most :data:`MAX_TEXT` characters, ``updated`` unix seconds. Junk is dropped
quietly on the way in; a note with no id gets one.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, List, Optional

from backend.fsutil import atomic_write_json, read_json

NOTES_FILE = "sky_notes.json"
MAX_NOTES = 500
MAX_TEXT = 2000
MAX_LINE = 300
MIN_SIZE = {"w": 120, "h": 70}
MAX_SIZE = {"w": 800, "h": 800}
DEFAULT_SIZE = {"w": 200, "h": 120}


class NotesError(ValueError):
    """The payload is not a list of notes."""


def notes_path() -> str:
    from backend.web.core import flights as _flights

    return os.path.join(_flights.flights_dir(), NOTES_FILE)


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _text(v: Any, limit: int) -> str:
    return str(v)[:limit] if isinstance(v, (str, int, float)) else ""


def new_id() -> str:
    return "n_" + secrets.token_hex(6)


def normalize_note(raw: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One note as stored, or None for junk."""
    if not isinstance(raw, dict):
        return None
    nid = _text(raw.get("id"), 64).strip() or new_id()
    w = int(
        min(MAX_SIZE["w"], max(MIN_SIZE["w"], _num(raw.get("w"), DEFAULT_SIZE["w"])))
    )
    h = int(
        min(MAX_SIZE["h"], max(MIN_SIZE["h"], _num(raw.get("h"), DEFAULT_SIZE["h"])))
    )
    return {
        "id": nid,
        "text": _text(raw.get("text"), MAX_TEXT),
        "x": int(round(_num(raw.get("x"), 0.0))),
        "y": int(round(_num(raw.get("y"), 0.0))),
        "w": w,
        "h": h,
        "line": _text(raw.get("line"), MAX_LINE),
        "updated": _num(raw.get("updated"), now if now is not None else time.time()),
    }


def normalize_notes(raw: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """A list of notes as stored: junk dropped, ids made unique, capped."""
    if not isinstance(raw, list):
        raise NotesError("notes must be a list")
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        n = normalize_note(item, now)
        if n is None:
            continue
        if n["id"] in seen:
            n["id"] = new_id()
        seen.add(n["id"])
        out.append(n)
        if len(out) >= MAX_NOTES:
            break
    return out


def load_notes() -> List[Dict[str, Any]]:
    data = read_json(notes_path(), {}, expect=dict)
    items = data.get("notes") if isinstance(data, dict) else None
    try:
        return normalize_notes(items if isinstance(items, list) else [])
    except NotesError:
        return []


def save_notes(raw: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Replace the notes with ``raw`` (a list); returns them as stored.
    Raises :class:`NotesError` when ``raw`` is not a list."""
    notes = normalize_notes(raw, now)
    path = notes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, {"notes": notes})
    return notes
