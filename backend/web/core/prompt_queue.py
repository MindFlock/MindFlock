"""Per-session prompt queue — continuous, self-driving development.

The problem this solves: an agent session goes idle whenever it finishes a turn
OR when the account's usage runs out (Claude prints a limit notice and returns
to its prompt, or the CLI exits). Either way the "ball stops rolling" and a
human has to notice and paste the next instruction. This module keeps a small
FIFO of prompts per session that a background drain loop (in ``web.server``)
feeds into the agent the moment it is idle again — so a queued run picks straight
back up as soon as usage returns, with no one watching.

Two shapes fall out of one primitive:

* **A task list** — enqueue several prompts; each is sent once, in order, as the
  agent frees up, then dropped.
* **A self-improving loop** — turn ``loop`` on and the sent prompt is re-appended
  instead of dropped, so a single "keep improving the repo" prompt (or a small
  rotation of them) cycles forever, maximizing token usage across an outage.

State lives in its own JSON file (``~/.mindflock/prompt_queues.json``), NOT in
the engine's ``state.json`` — queues are ancillary per-session data and keeping
them separate avoids touching engine serialization (and the multi-server
merge-on-save logic) for a feature that is purely additive. The store is
tolerant: a missing/corrupt file reads as "no queues", and every write is atomic
(temp file + ``os.replace``) so a concurrent reader never sees a partial file.

Thread-safety: sync FastAPI routes run in the worker threadpool and the drain
loop runs on the event loop, so every accessor takes a module lock and
re-reads/writes the file under it (the file is tiny; simplicity beats caching).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from backend.config.config import GetConfigDir

__all__ = [
    "queue_path",
    "get_state",
    "list_queue",
    "enqueue",
    "enqueue_many",
    "update_item",
    "remove_item",
    "clear",
    "move_item",
    "move_item_to",
    "set_flags",
    "peek_next",
    "record_sent",
    "prune",
    "all_titles",
]

_FileName = "prompt_queues.json"
_LOCK = threading.Lock()

# Cap per session so a runaway enqueue can't grow the file without bound.
_MAX_ITEMS = 200


def queue_path() -> str:
    """Path to the queue store.

    Honors ``$MINDFLOCK_PROMPT_QUEUE_FILE`` (tests point it at a tmp file);
    otherwise ``<config dir>/prompt_queues.json``.
    """
    env = os.environ.get("MINDFLOCK_PROMPT_QUEUE_FILE")
    if env:
        return env
    return os.path.join(GetConfigDir(), _FileName)


# On-disk shape::
#   {"<title>": {"items": [{"id", "text", "added"}],
#                "loop": bool, "enabled": bool,
#                "last_sent": epoch|null, "last_text": str}}
# ``enabled`` gates the drain loop for that session; ``loop`` re-appends a sent
# item instead of dropping it. Everything defaults sanely when absent.
def _load() -> dict:
    try:
        with open(queue_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = queue_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".pq.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _blank() -> dict:
    return {
        "items": [],
        "loop": False,
        # Minutes to space out a looping queue's sends (0 = re-send as soon as the
        # agent is idle). With loop on and this > 0, the drain only sends the next
        # prompt every N minutes — a timed self-improving cycle.
        "loop_interval": 0,
        "enabled": True,
        # When True (default) the drain loop holds a session's queue while its
        # agent is usage-limited and auto-resumes the moment the window reopens.
        # When False, hitting a limit stops the queue (auto-run flips off) instead
        # of waiting — the user resumes it by hand.
        "wait_for_limit": True,
        "last_sent": None,
        "last_text": "",
    }


def _normalize(entry) -> dict:
    """Coerce a stored entry (any shape) into the canonical dict."""
    e = _blank()
    if isinstance(entry, dict):
        raw = entry.get("items")
        if isinstance(raw, list):
            items = []
            for it in raw:
                if isinstance(it, dict) and str(it.get("text", "")).strip():
                    items.append(
                        {
                            "id": str(it.get("id") or _new_id(len(items))),
                            "text": str(it["text"]),
                            "added": float(it.get("added") or 0.0),
                        }
                    )
            e["items"] = items[:_MAX_ITEMS]
        e["loop"] = bool(entry.get("loop", False))
        try:
            e["loop_interval"] = max(0, int(entry.get("loop_interval", 0) or 0))
        except (TypeError, ValueError):
            e["loop_interval"] = 0
        e["enabled"] = bool(entry.get("enabled", True))
        e["wait_for_limit"] = bool(entry.get("wait_for_limit", True))
        ls = entry.get("last_sent")
        e["last_sent"] = float(ls) if isinstance(ls, (int, float)) else None
        e["last_text"] = str(entry.get("last_text", "") or "")
    return e


# IDs must be stable but not depend on Date.now()/random (those are fine here —
# this runs in the server process, not a workflow script). Kept monotonic-ish
# via a counter mixed with time so two rapid enqueues never collide.
_ID_COUNTER = 0


def _new_id(salt: int = 0) -> str:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return "q%d_%d" % (int(time.time() * 1000), _ID_COUNTER + salt)


def get_state(title: str) -> dict:
    """The full queue entry for ``title`` (canonicalized), or a blank one."""
    with _LOCK:
        return _normalize(_load().get(title))


def list_queue(title: str) -> List[dict]:
    """Just the pending items for ``title`` (list of ``{id, text, added}``)."""
    return get_state(title)["items"]


def enqueue(title: str, text: str, index: Optional[int] = None) -> dict:
    """Add a prompt to ``title``'s queue. Returns the new entry.

    Appends by default; with ``index`` the prompt is inserted at that 0-based
    position (clamped), which is how "add above/below this item" lands in the
    right slot. Blank/whitespace text is ignored (returns the unchanged
    entry). Enqueuing always leaves the queue ``enabled`` so a freshly added
    prompt actually drains without a separate toggle.
    """
    text = str(text or "").strip()
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        if text:
            if len(entry["items"]) >= _MAX_ITEMS:
                raise ValueError("queue is full (%d items)" % _MAX_ITEMS)
            item = {"id": _new_id(), "text": text, "added": time.time()}
            if index is None:
                entry["items"].append(item)
            else:
                pos = max(0, min(int(index), len(entry["items"])))
                entry["items"].insert(pos, item)
            entry["enabled"] = True
            data[title] = entry
            _save(data)
        return entry


def enqueue_many(title: str, texts) -> tuple:
    """Append several prompts to ``title``'s queue in one write — the bulk
    path behind dropping a CSV/text file on the Queue tab (one prompt per
    row). Blank/whitespace rows are skipped; rows beyond the queue cap are
    dropped rather than raising, so a big file adds what fits.

    Returns ``(entry, added, skipped)`` where ``skipped`` counts non-blank
    rows that didn't fit. Adding anything leaves the queue ``enabled``,
    exactly like a single enqueue."""
    cleaned = [t for t in (str(x or "").strip() for x in (texts or [])) if t]
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        room = max(0, _MAX_ITEMS - len(entry["items"]))
        take = cleaned[:room]
        now = time.time()
        for t in take:
            entry["items"].append({"id": _new_id(), "text": t, "added": now})
        if take:
            entry["enabled"] = True
            data[title] = entry
            _save(data)
        return entry, len(take), len(cleaned) - len(take)


def update_item(title: str, item_id: str, text: str) -> dict:
    """Replace the text of one pending item (id and position are kept, so an
    edit never loses the item's place in line).

    Blank/whitespace text or an unknown id leaves the queue unchanged —
    removing is what the delete control is for.
    """
    text = str(text or "").strip()
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        if text:
            for it in entry["items"]:
                if it["id"] == item_id:
                    it["text"] = text
                    data[title] = entry
                    _save(data)
                    break
        return entry


def remove_item(title: str, item_id: str) -> dict:
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        entry["items"] = [it for it in entry["items"] if it["id"] != item_id]
        data[title] = entry
        _save(data)
        return entry


def clear(title: str) -> dict:
    """Drop all pending items (keeps flags)."""
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        entry["items"] = []
        data[title] = entry
        _save(data)
        return entry


def move_item(title: str, item_id: str, direction: str) -> dict:
    """Reorder one item up/down within the queue."""
    delta = -1 if direction == "up" else 1
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        items = entry["items"]
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), -1)
        if idx >= 0:
            j = idx + delta
            if 0 <= j < len(items):
                items[idx], items[j] = items[j], items[idx]
                data[title] = entry
                _save(data)
        return entry


def move_item_to(title: str, item_id: str, index: int) -> dict:
    """Move one item to an absolute 0-based position (clamped) — the
    drag-and-drop reorder. An unknown id leaves the queue unchanged."""
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        items = entry["items"]
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), -1)
        if idx >= 0:
            it = items.pop(idx)
            items.insert(max(0, min(int(index), len(items))), it)
            data[title] = entry
            _save(data)
        return entry


def set_flags(
    title: str,
    *,
    enabled: Optional[bool] = None,
    loop: Optional[bool] = None,
    loop_interval: Optional[int] = None,
    wait_for_limit: Optional[bool] = None,
) -> dict:
    """Toggle the per-session ``enabled`` (drain on/off), ``loop`` (re-queue sent
    prompts), ``loop_interval`` (minutes between looped sends; 0 = immediate), and
    ``wait_for_limit`` (hold + auto-resume across usage limits vs. stop when
    limited) flags. Any left ``None`` is unchanged."""
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if loop is not None:
            entry["loop"] = bool(loop)
        if loop_interval is not None:
            try:
                entry["loop_interval"] = max(0, int(loop_interval))
            except (TypeError, ValueError):
                entry["loop_interval"] = 0
        if wait_for_limit is not None:
            entry["wait_for_limit"] = bool(wait_for_limit)
        data[title] = entry
        _save(data)
        return entry


def peek_next(title: str) -> Optional[dict]:
    """The item that would be sent next, without mutating the queue.

    Returns ``None`` when the queue is empty or draining is disabled.
    """
    entry = get_state(title)
    if not entry["enabled"] or not entry["items"]:
        return None
    return entry["items"][0]


def record_sent(title: str, item_id: str) -> dict:
    """Mark ``item_id`` as just sent: pop it — wherever it sits, since the
    drain sends the front item but Send-now may fire a mid-queue one — stamp
    ``last_sent``, and (when ``loop`` is on) re-append it so the rotation
    continues.

    Returns the updated entry. If the item no longer exists (it was removed
    between peek and send) only the timestamp is stamped."""
    now = time.time()
    with _LOCK:
        data = _load()
        entry = _normalize(data.get(title))
        items = entry["items"]
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), -1)
        if idx < 0:
            # Raced with a remove — just stamp the time and bail.
            entry["last_sent"] = now
            data[title] = entry
            _save(data)
            return entry
        sent = items.pop(idx)
        entry["last_sent"] = now
        entry["last_text"] = sent["text"]
        if entry["loop"]:
            items.append({"id": _new_id(), "text": sent["text"], "added": now})
        data[title] = entry
        _save(data)
        return entry


def prune(live_titles) -> None:
    """Drop queues for sessions that no longer exist (called from the drain
    loop with the set of current instance titles)."""
    live = set(live_titles or ())
    with _LOCK:
        data = _load()
        pruned = {k: v for k, v in data.items() if k in live}
        if len(pruned) != len(data):
            _save(pruned)


def all_titles() -> List[str]:
    """Titles that currently have a stored queue entry."""
    with _LOCK:
        return list(_load().keys())


def snapshot() -> Dict[str, dict]:
    """Every queue entry, canonicalized, in one read — for the ``/api/instances``
    poll to attach a per-session ``{pending, enabled, loop}`` summary without a
    file read per session."""
    with _LOCK:
        data = _load()
    return {title: _normalize(entry) for title, entry in data.items()}
