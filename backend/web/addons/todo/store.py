"""The to-do list's ONE store: ``todos.json`` + ``todos-history.json``.

Both files live in the Assistant's directory (``~/.mindflock-assistant/``,
moved by ``MINDFLOCK_ASSISTANT_DIR``). ``todos.json`` predates this package:
the Assistant's own ``claude`` edits it by hand, so every reader here
tolerates a hand-written row (``{"id", "text", "done"}`` and nothing else),
and every writer keeps the fields it was not told about — a field this module
does not know is carried through verbatim, never wiped.

Every read-modify-write runs under one process-wide lock (:data:`_LOCK`) and
lands with an atomic replace, so the REST routes, the MCP tools (over REST),
the Assistant's compat routes, the reminder loop and the auto sources can all
write at once without losing an update. Every change that a person could see
emits ``addon.todo.changed`` on the shared bus (after the lock is released).

Order: the array order is the display order, kept in three tiers — ``high``,
normal, ``low`` — by a stable sort of the OPEN items into the open items' own
slots (a done item keeps its place). "Prioritize" moves an item to the end of
its new tier; "reorder" is the manual order within a tier.

History: a deleted item moves to ``todos-history.json`` (newest first, capped
at :data:`HISTORY_CAP`) with ``outcome: "removed"``. A done item stays in the
list (the Assistant reads it there) and is also SHOWN in history; once it has
been done for :data:`ARCHIVE_AFTER_S` it moves into the history file for good.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from backend.fsutil import atomic_write_text

#: Tier words. Absent (or ``"normal"`` on the wire) is the middle tier.
PRIORITIES = ("high", "low")
#: What an item asks of the person. Absent (``"task"`` on the wire) = a plain task.
KINDS = ("manual_test", "needs_you")
#: Who added it. Absent = ``"you"``.
SOURCES = ("you", "agent", "assistant", "verify", "flight", "run")
#: The sources the server adds and resolves on its own (``auto.py``).
AUTO_SOURCES = ("verify", "flight", "run")
#: Reminder fields the server owns (see :func:`remind_at`).
REMINDER_FIELDS = ("snoozed_until", "dismissed_at", "notified_at")
#: Fields a writer may set from outside (POST / PATCH / the MCP tools).
TEXT_FIELDS = ("details", "session", "key", "link")

MAX_TEXT = 1000
MAX_DETAILS = 20000
MAX_SHORT = 500  # session / key / link
MAX_BATCH = 50
HISTORY_CAP = 2000
#: A done item older than this leaves todos.json for the history file.
ARCHIVE_AFTER_S = 7 * 86400.0
OUTCOMES = ("done", "removed")

#: A reminder's local time, as the UI and the assistant write it.
DUE_FMT = "%Y-%m-%dT%H:%M"

_KNOWN = frozenset(
    ("id", "text", "done", "priority", "kind", "source", "created_at", "done_at", "due")
    + TEXT_FIELDS
    + REMINDER_FIELDS
)
#: Only on history entries; stripped when one is restored to the list.
_HISTORY_ONLY = ("closed_at", "outcome")

#: The one lock around every read-modify-write of either file.
_LOCK = threading.RLock()


class StoreError(RuntimeError):
    """A file exists but cannot be read as a list. Mutations refuse rather
    than write a fresh list over what is there (the Assistant may have left
    it half-edited); reads treat it as empty."""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def todos_path() -> Path:
    """``todos.json``. Resolved through the Assistant module at CALL time:
    it owns the directory (``ASSIST_DIR`` / ``ASSIST_TODOS``), and the tests
    of both addons point it at a tmp dir by patching it there — a copy taken
    at import would quietly keep writing to the real home directory."""
    from backend.web.addons import assistant as _assistant

    return Path(_assistant.ASSIST_TODOS)


def history_path() -> Path:
    """``todos-history.json``, next to ``todos.json``."""
    return todos_path().with_name("todos-history.json")


#: The atomic writer (a module attribute so a test can make it fail).
_write_text = atomic_write_text


# --------------------------------------------------------------------------- #
# Reminders (moved here from the Assistant; it re-exports these names)
# --------------------------------------------------------------------------- #
def norm_due(value) -> str:
    """A todo's ``due`` → ``"YYYY-MM-DDTHH:MM"`` in LOCAL time, or ``""``.

    Accepts that exact form, a full ISO 8601 stamp (seconds, ``Z`` or an
    offset — converted to local time), or epoch seconds; anything else is no
    reminder at all rather than a guess."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _dt.datetime.fromtimestamp(float(value)).strftime(DUE_FMT)
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        stamp = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp.strftime(DUE_FMT)


def due_epoch(item: dict) -> float:
    """The epoch seconds of ``item["due"]`` (local time), or 0."""
    due = str(item.get("due") or "")
    if not due:
        return 0.0
    try:
        return _dt.datetime.strptime(due, DUE_FMT).timestamp()
    except ValueError:
        return 0.0


def num(value) -> float:
    """A positive float, or 0."""
    try:
        out = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return out if out > 0 else 0.0


def remind_at(item: dict) -> float:
    """When ``item`` should next pop up: its due time, or a later snooze; 0
    when it has no reminder, is done, or was dismissed for this due time."""
    if item.get("done"):
        return 0.0
    due = due_epoch(item)
    if not due:
        return 0.0
    if num(item.get("dismissed_at")) >= due:
        return 0.0
    return max(due, num(item.get("snoozed_until")))


def due_now(items: list, now: Optional[float] = None) -> list:
    """The todos whose reminder time has come (at it or any time after),
    oldest first — what the reminder dialog shows."""
    t = time.time() if now is None else now
    hits = [(remind_at(i), i) for i in items]
    return [i for at, i in sorted(hits, key=lambda x: x[0]) if at and at <= t]


# --------------------------------------------------------------------------- #
# Normalising
# --------------------------------------------------------------------------- #
def _opt_str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def norm_item(raw: dict, index: int) -> dict:
    """One stored row → its canonical shape.

    ``{id, text, done}`` always; every optional field only when set (so a
    plain hand-written row reads back exactly as it was written); the
    reminder fields only alongside a ``due``; ``done_at`` only on a done
    item; and every field this module does not know, verbatim."""
    out: dict = {
        "id": str(raw.get("id") or "").strip() or "t%d" % index,
        "text": str(raw.get("text", "")),
        "done": bool(raw.get("done", False)),
    }
    details = raw.get("details")
    if isinstance(details, str) and details.strip():
        out["details"] = details
    priority = _opt_str(raw.get("priority")).lower()
    if priority in PRIORITIES:
        out["priority"] = priority
    kind = _opt_str(raw.get("kind")).lower()
    if kind in KINDS:
        out["kind"] = kind
    for key in ("session", "key", "link"):
        val = _opt_str(raw.get(key))
        if val:
            out[key] = val
    source = _opt_str(raw.get("source")).lower()
    if source in SOURCES:
        out["source"] = source
    created = num(raw.get("created_at"))
    if created:
        out["created_at"] = created
    done_at = num(raw.get("done_at"))
    if out["done"] and done_at:
        out["done_at"] = done_at
    due = norm_due(raw.get("due"))
    if due:
        out["due"] = due
        for key in REMINDER_FIELDS:
            val = num(raw.get(key))
            if val:
                out[key] = val
    for key, val in raw.items():
        if key not in _KNOWN and key not in out:
            out[key] = val
    return out


def _norm_list(data) -> List[dict]:
    """A parsed array → canonical rows, non-dicts skipped, ids made unique
    (a hand edit can duplicate one; every by-id operation needs one row)."""
    out: List[dict] = []
    seen: set = set()
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        row = norm_item(raw, i)
        while row["id"] in seen:
            row["id"] += "_"
        seen.add(row["id"])
        out.append(row)
    return out


def norm_history(raw: dict, index: int) -> dict:
    """One history entry: the item, plus ``closed_at`` and ``outcome``."""
    row = norm_item(raw, index)
    row["closed_at"] = num(raw.get("closed_at"))
    outcome = _opt_str(raw.get("outcome")).lower()
    row["outcome"] = outcome if outcome in OUTCOMES else "removed"
    return row


def compat(item: dict) -> dict:
    """The row as the Assistant's ``/api/assistant/todos`` routes have always
    returned it: no server bookkeeping stamps (``created_at`` / ``done_at``)."""
    return {k: v for k, v in item.items() if k not in ("created_at", "done_at")}


def _tier(item: dict) -> int:
    p = item.get("priority")
    return 0 if p == "high" else 2 if p == "low" else 1


def tier_sorted(items: List[dict]) -> List[dict]:
    """A stable sort of the OPEN items by tier, into the open items' own
    slots; a done item keeps its place."""
    slots = [i for i, it in enumerate(items) if not it.get("done")]
    ordered = sorted((items[i] for i in slots), key=_tier)
    out = list(items)
    for slot, item in zip(slots, ordered):
        out[slot] = item
    return out


def counts(items: List[dict]) -> Dict[str, int]:
    """``{"open", "needs_you", "high"}`` over the OPEN items."""
    open_items = [i for i in items if not i.get("done")]
    return {
        "open": len(open_items),
        "needs_you": sum(1 for i in open_items if i.get("kind") in KINDS),
        "high": sum(1 for i in open_items if i.get("priority") == "high"),
    }


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def _read_array(path: Path, strict: bool, what: str) -> list:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as err:
        if strict:
            raise StoreError("%s can't be read: %s" % (what, err))
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        if strict:
            raise StoreError(
                "%s is not valid JSON — fix it (or empty it) and try again" % what
            )
        return []
    if not isinstance(data, list):
        if strict:
            raise StoreError("%s is not a JSON array" % what)
        return []
    return data


def _read_items(strict: bool = False) -> List[dict]:
    return _norm_list(_read_array(todos_path(), strict, "todos.json"))


def _read_history(strict: bool = False) -> List[dict]:
    data = _read_array(history_path(), strict, "todos-history.json")
    return [norm_history(d, i) for i, d in enumerate(data) if isinstance(d, dict)]


def _dump(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, json.dumps(rows, indent=2) + "\n")


def _hist_entry(item: dict, outcome: str, closed_at: float) -> dict:
    out = dict(item)
    out["closed_at"] = closed_at
    out["outcome"] = outcome
    return out


def _new_id(taken: set) -> str:
    while True:
        tid = uuid.uuid4().hex[:8]
        if tid not in taken:
            return tid


# --------------------------------------------------------------------------- #
# The transaction
# --------------------------------------------------------------------------- #
class _Tx:
    """One read-modify-write: the list (read strictly), the history (read on
    first touch), and what changed. ``reason`` set = a change worth an
    ``addon.todo.changed``; ``dirty`` alone = write without announcing."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.items: List[dict] = _read_items(strict=True)
        self._history: Optional[List[dict]] = None
        self.history_dirty = False
        self.dirty = False
        self.reason = ""
        self.id = ""

    @property
    def history(self) -> List[dict]:
        if self._history is None:
            self._history = _read_history(strict=True)
        return self._history

    def changed(self, reason: str, tid: str = "") -> None:
        self.dirty = True
        if not self.reason:
            self.reason, self.id = reason, tid

    def to_history(self, item: dict, outcome: str, closed_at: float) -> None:
        self.history.insert(0, _hist_entry(item, outcome, closed_at))
        self.history_dirty = True

    def find(self, tid: str) -> Optional[dict]:
        return next((i for i in self.items if i["id"] == tid), None)

    def taken_ids(self) -> set:
        ids = {i["id"] for i in self.items}
        if self._history is not None:
            ids |= {h["id"] for h in self._history}
        return ids

    def move_to_end(self, item: dict) -> None:
        """To the end of the array, so the tier sort puts it last in its tier."""
        self.items = [i for i in self.items if i is not item] + [item]


def _finish(tx: _Tx) -> None:
    """Stamps, archive, tier order — what every write leaves behind."""
    now = tx.now
    for item in tx.items:
        if not num(item.get("created_at")):
            item["created_at"] = now  # first sight of a hand-written row
        if item.get("done"):
            if not num(item.get("done_at")):
                item["done_at"] = now  # the Assistant ticked it by hand
        else:
            item.pop("done_at", None)
    old = [
        i
        for i in tx.items
        if i.get("done") and now - num(i.get("done_at")) > ARCHIVE_AFTER_S
    ]
    if old:
        gone = {id(i) for i in old}
        tx.items = [i for i in tx.items if id(i) not in gone]
        for item in old:
            tx.to_history(item, "done", num(item.get("done_at")))
        if not tx.reason:
            tx.reason, tx.id = "archived", old[0]["id"]
        tx.dirty = True
    tx.items = tier_sorted(tx.items)
    if tx.history_dirty:
        hist = sorted(tx.history, key=lambda h: -num(h.get("closed_at")))
        tx._history = hist[:HISTORY_CAP]


def _emit_changed(reason: str, tid: str, c: Dict[str, int]) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(
            "addon.todo.changed",
            data={
                "reason": reason,
                "id": tid,
                "open": c["open"],
                "needs_you": c["needs_you"],
            },
        )
    except Exception:  # noqa: BLE001 — clients also poll; never fail a write
        pass


def _transact(fn: Callable[[_Tx], object], announce: bool = True):
    """Run ``fn(tx)`` under the lock; write what it changed; emit after."""
    with _LOCK:
        tx = _Tx(time.time())
        result = fn(tx)
        if not (tx.dirty or tx.history_dirty):
            return result
        _finish(tx)
        if tx.history_dirty:
            _dump(history_path(), tx.history)
        _dump(todos_path(), tx.items)
        reason, tid, c = tx.reason, tx.id, counts(tx.items)
    if reason and announce:
        _emit_changed(reason, tid, c)
    return result


# --------------------------------------------------------------------------- #
# Validation (the REST + MCP input)
# --------------------------------------------------------------------------- #
def _check_str(body: dict, key: str, limit: int) -> Optional[str]:
    """The stripped string at ``body[key]``, ``None`` when absent."""
    if key not in body or body[key] is None:
        return None
    val = body[key]
    if not isinstance(val, str):
        raise ValueError("%s must be a string" % key)
    val = val.strip()
    if len(val) > limit:
        raise ValueError("%s is too long (max %d characters)" % (key, limit))
    return val


def _clean_fields(body: dict, allowed: Tuple[str, ...]) -> dict:
    """The fields of ``body`` in ``allowed``, validated → their stored form
    (``""`` = clear the field). Raises ``ValueError`` with the reason."""
    out: dict = {}
    if "text" in allowed:
        text = _check_str(body, "text", MAX_TEXT)
        if text is not None:
            if not text:
                raise ValueError("text can't be empty")
            out["text"] = text
    if "details" in allowed:
        val = body.get("details")
        if val is not None:
            if not isinstance(val, str):
                raise ValueError("details must be a string")
            if len(val) > MAX_DETAILS:
                raise ValueError(
                    "details is too long (max %d characters)" % MAX_DETAILS
                )
            out["details"] = val.strip("\n")
    for key in ("session", "key", "link"):
        if key in allowed:
            val = _check_str(body, key, MAX_SHORT)
            if val is not None:
                out[key] = val
    if "priority" in allowed:
        val = _check_str(body, "priority", 20)
        if val is not None:
            val = val.lower()
            if val not in PRIORITIES + ("", "normal"):
                raise ValueError("priority must be high, normal or low")
            out["priority"] = "" if val == "normal" else val
    if "kind" in allowed:
        val = _check_str(body, "kind", 20)
        if val is not None:
            val = val.lower()
            if val not in KINDS + ("", "task"):
                raise ValueError("kind must be task, manual_test or needs_you")
            out["kind"] = "" if val == "task" else val
    if "source" in allowed:
        val = _check_str(body, "source", 20)
        if val is not None:
            val = val.lower()
            if val not in SOURCES + ("",):
                raise ValueError("source must be one of %s" % ", ".join(SOURCES))
            out["source"] = val
    if "due" in allowed and "due" in body and body["due"] is not None:
        raw = body["due"]
        due = norm_due(raw)
        if raw not in ("",) and not due:
            raise ValueError("due must be a local time like 2026-11-10T09:00")
        out["due"] = due
    if "done" in allowed and "done" in body:
        if not isinstance(body["done"], bool):
            raise ValueError("done must be true or false")
        out["done"] = body["done"]
    return out


NEW_FIELDS = (
    "text",
    "details",
    "priority",
    "kind",
    "session",
    "source",
    "key",
    "link",
    "due",
)
PATCH_FIELDS = ("text", "details", "priority", "done", "due", "kind", "session", "link")


def clean_new(body) -> dict:
    """One item to add, validated (``text`` required)."""
    if not isinstance(body, dict):
        raise ValueError("each item must be an object")
    out = _clean_fields(body, NEW_FIELDS)
    if not out.get("text"):
        raise ValueError("text is required")
    return out


def clean_patch(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("the body must be an object")
    return _clean_fields(body, PATCH_FIELDS)


def _apply(tx: _Tx, item: dict, changes: dict) -> str:
    """Apply validated ``changes`` to ``item`` in place → the change's reason
    (``done`` / ``reopened`` / ``updated``), or ``""`` when nothing changed."""
    reason = ""
    for key, val in changes.items():
        if key == "done":
            if bool(val) != bool(item.get("done")):
                item["done"] = bool(val)
                if val:
                    item["done_at"] = tx.now
                    reason = "done"
                else:
                    item.pop("done_at", None)
                    reason = "reopened"
                    tx.move_to_end(item)
            continue
        if key == "due":
            if val != item.get("due", ""):
                # A new time is a new reminder: no snooze, not dismissed,
                # not yet announced.
                for r in REMINDER_FIELDS:
                    item.pop(r, None)
                reason = reason or "updated"
        elif key == "priority" and val != item.get("priority", ""):
            tx.move_to_end(item)  # a new tier: the END of it
        if val in ("", None):
            if key in item:
                item.pop(key, None)
                reason = reason or "updated"
        elif item.get(key) != val:
            item[key] = val
            reason = reason or "updated"
    return reason


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_items() -> List[dict]:
    """Every item in todos.json, in display order (a missing or malformed
    file reads as empty)."""
    with _LOCK:
        return tier_sorted(_read_items())


def snapshot() -> dict:
    items = list_items()
    return {"todos": items, "counts": counts(items)}


def history(limit: int = 200, q: str = "") -> List[dict]:
    """History, newest first: the done items still in the list plus the
    history file, optionally filtered by ``q`` (text, details, session)."""
    with _LOCK:
        items = _read_items()
        hist = _read_history()
    rows = [
        _hist_entry(i, "done", num(i.get("done_at")) or num(i.get("created_at")))
        for i in items
        if i.get("done")
    ] + hist
    needle = (q or "").strip().casefold()
    if needle:
        rows = [
            r
            for r in rows
            if needle
            in " ".join(
                str(r.get(k) or "") for k in ("text", "details", "session")
            ).casefold()
        ]
    rows.sort(key=lambda r: -num(r.get("closed_at")))
    return rows[: max(1, min(int(limit), HISTORY_CAP))]


def badge() -> dict:
    """What the sidebar bar shows: ``{"count", "title"}`` (0 = no badge)."""
    c = counts(list_items())
    if not c["open"]:
        return {"count": 0, "title": "Nothing to do"}
    parts = []
    if c["needs_you"]:
        parts.append(
            "%d %s you" % (c["needs_you"], "needs" if c["needs_you"] == 1 else "need")
        )
    parts.append("%d open" % c["open"])
    return {"count": c["open"], "title": " · ".join(parts)}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def add_items(entries: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Add validated items (:func:`clean_new`) → ``(added, updated)``. An
    OPEN item with the same non-empty ``key`` is updated in place instead."""

    def fn(tx: _Tx):
        added: List[dict] = []
        updated: List[dict] = []
        for entry in entries:
            key = entry.get("key") or ""
            hit = None
            if key:
                hit = next(
                    (i for i in tx.items if not i.get("done") and i.get("key") == key),
                    None,
                )
            if hit is not None:
                changes = {k: v for k, v in entry.items() if k != "key"}
                if _apply(tx, hit, changes):
                    tx.changed("updated", hit["id"])
                if hit not in updated:
                    updated.append(hit)
                continue
            item = {"id": _new_id(tx.taken_ids()), "text": entry["text"], "done": False}
            for k, v in entry.items():
                if k != "text" and v not in ("", None):
                    item[k] = v
            item["created_at"] = tx.now
            tx.items.append(item)
            added.append(item)
        if added:
            tx.reason, tx.id = "added", added[0]["id"]
            tx.dirty = True
        return added, updated

    return _transact(fn)


def patch_item(tid: str, changes: dict) -> Optional[dict]:
    """Apply validated changes (:func:`clean_patch`) → the item, or None."""

    def fn(tx: _Tx):
        item = tx.find(tid)
        if item is None:
            return None
        reason = _apply(tx, item, changes)
        if reason:
            tx.changed(reason, tid)
        return item

    return _transact(fn)


def update_item(tid: str, mutate: Callable[[dict], None]) -> Optional[dict]:
    """Read-modify-write one item with an arbitrary ``mutate(item)`` (the
    reminder routes) → the re-normalised item, or None."""

    def fn(tx: _Tx):
        item = tx.find(tid)
        if item is None:
            return None
        was_done = bool(item.get("done"))
        mutate(item)
        clean = norm_item(item, 0)
        item.clear()
        item.update(clean)
        if bool(item.get("done")) != was_done:
            if item.get("done"):
                item["done_at"] = tx.now
                tx.changed("done", tid)
            else:
                item.pop("done_at", None)
                tx.changed("reopened", tid)
        else:
            tx.changed("updated", tid)
        return item

    return _transact(fn)


def delete_item(tid: str) -> bool:
    """Move an item to history (outcome ``removed``)."""

    def fn(tx: _Tx):
        item = tx.find(tid)
        if item is None:
            return False
        tx.items = [i for i in tx.items if i is not item]
        tx.to_history(item, "removed", tx.now)
        tx.changed("removed", tid)
        return True

    return _transact(fn)


def set_order(ids: List[str]) -> List[dict]:
    """The manual order of the OPEN items: the named ones first, in that
    order (unknown ids ignored), then the others in their relative order.
    Tiers still win (the tier sort runs after)."""

    def fn(tx: _Tx):
        open_items = [i for i in tx.items if not i.get("done")]
        by_id = {i["id"]: i for i in open_items}
        named: List[dict] = []
        for x in ids:
            if isinstance(x, str) and x in by_id and by_id[x] not in named:
                named.append(by_id[x])
        rest = [i for i in open_items if i not in named]
        slots = [k for k, i in enumerate(tx.items) if not i.get("done")]
        out = list(tx.items)
        for slot, item in zip(slots, named + rest):
            out[slot] = item
        if [i["id"] for i in out] != [i["id"] for i in tx.items]:
            tx.items = out
            tx.changed("order", "")
        return None

    _transact(fn)
    return list_items()


def restore(tid: str) -> Optional[dict]:
    """Back into the list, open, at the end of its tier → the item, or None.
    Works on a history entry and on a done item still in the list."""

    def fn(tx: _Tx):
        item = tx.find(tid)
        if item is not None:
            if item.get("done"):
                item["done"] = False
                item.pop("done_at", None)
                tx.move_to_end(item)
                tx.changed("restored", tid)
            return item
        hit = next((h for h in tx.history if h["id"] == tid), None)
        if hit is None:
            return None
        tx._history = [h for h in tx.history if h is not hit]
        tx.history_dirty = True
        row = {k: v for k, v in hit.items() if k not in _HISTORY_ONLY}
        row["done"] = False
        row.pop("done_at", None)
        if row["id"] in {i["id"] for i in tx.items}:
            row["id"] = _new_id(tx.taken_ids())
        tx.items.append(row)
        tx.changed("restored", row["id"])
        return row

    return _transact(fn)


def forget(tid: str) -> bool:
    """Drop one history entry for good — the history file's, or a done item
    still in the list (removed without being re-added to history)."""

    def fn(tx: _Tx):
        hist = tx.history
        if any(h["id"] == tid for h in hist):
            tx._history = [h for h in hist if h["id"] != tid]
            tx.history_dirty = True
            tx.changed("removed", tid)
            return True
        item = tx.find(tid)
        if item is not None and item.get("done"):
            tx.items = [i for i in tx.items if i is not item]
            tx.changed("removed", tid)
            return True
        return False

    return _transact(fn)


def replace_all(rows: List[Tuple[str, dict]]) -> List[dict]:
    """The Assistant's ``PUT /api/assistant/todos``: the client's whole list,
    as ``[(id, sent_fields)]``. For an id the store knows, every field the
    client did NOT send is kept (details, priority, kind, session, source,
    key, link, created_at, the reminder fields…); a changed ``due`` is a new
    reminder. An item the client left out moves to history (``removed``)."""

    def fn(tx: _Tx):
        before = {i["id"]: i for i in tx.items}
        clean: List[dict] = []
        for index, (tid, sent) in enumerate(rows):
            old = before.get(tid) or {}
            merged = dict(old)
            merged.update(sent)
            merged["id"] = tid
            row = norm_item(merged, index)
            if row.get("due") != old.get("due"):
                for r in REMINDER_FIELDS:
                    row.pop(r, None)
            if old and row["done"] != bool(old.get("done")):
                if row["done"]:
                    row["done_at"] = tx.now
                else:
                    row.pop("done_at", None)
            clean.append(row)
        kept = {r["id"] for r in clean}
        for old in tx.items:
            if old["id"] not in kept:
                tx.to_history(old, "removed", tx.now)
        tx.items = clean
        tx.changed("updated", "")
        return None

    _transact(fn)
    return list_items()


def close_items(keys: List[str]) -> List[dict]:
    """Mark the OPEN items carrying one of ``keys`` done → the closed items.
    The auto sources' resolution: one change, one event."""
    wanted = set(k for k in keys if k)

    def fn(tx: _Tx):
        closed = []
        for item in tx.items:
            if not item.get("done") and item.get("key") in wanted:
                item["done"] = True
                item["done_at"] = tx.now
                closed.append(item)
        if closed:
            tx.changed("done", closed[0]["id"])
        return closed

    if not wanted:
        return []
    return _transact(fn)


def archive_now() -> int:
    """Move done items older than :data:`ARCHIVE_AFTER_S` to history (startup
    and the slow pass) → how many moved."""

    def fn(tx: _Tx):
        old = [
            i
            for i in tx.items
            if i.get("done")
            and num(i.get("done_at"))
            and tx.now - num(i.get("done_at")) > ARCHIVE_AFTER_S
        ]
        if old:
            tx.dirty = True  # _finish does the move and names the reason
        return len(old)

    return _transact(fn)


def announce_due(now: Optional[float] = None) -> list:
    """Mark and announce the todos that came due since they were last
    announced → the announced rows. ``notified_at`` lives ON the todo, in the
    shared file, so two servers reading one todo list (the installed one and a
    dev one) push a reminder once, not twice. Bookkeeping only: no
    ``addon.todo.changed`` (nothing a person sees moved)."""
    t = time.time() if now is None else now

    def fn(tx: _Tx):
        fresh = []
        for item in tx.items:
            at = remind_at(item)
            if at and at <= t and num(item.get("notified_at")) < at:
                item["notified_at"] = t
                fresh.append(dict(item))
        if fresh:
            tx.dirty = True
        return fresh

    try:
        fresh = _transact(fn, announce=False)
    except StoreError:
        return []
    if not fresh:
        return []
    try:
        from backend.web.core import events as _events

        for item in fresh:
            _events.BUS.emit(
                "todo.due",
                session="",
                data={
                    "id": item["id"],
                    "text": item["text"],
                    "due": item.get("due", ""),
                    "detail": item["text"],
                },
            )
    except Exception as err:  # noqa: BLE001 — the dialog still finds it by polling
        from backend import log

        if log.ErrorLog is not None:
            log.ErrorLog.Printf("todo reminders: announce failed: %v", err)
    return [compat(i) for i in fresh]
