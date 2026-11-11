"""What a queued start waits for, in the words an agent reads.

The server's launch gate (``core/launch_gate.py``) queues an automatic start
for several reasons, not only low memory. Its record, ``waiting_for_memory``
(``{kind, since, reason, position, creates, why}``), rides on a queued
create's 202 body and on the row of a session waiting in the queue; ``why`` is
one of :data:`QUEUED_STATUS`'s keys. A create that waited and then failed
leaves its row behind with ``start_failed: {error, at, prompt_preview?}``.

Stdlib only, like the rest of ``backend.mcp``; every read is defensive (an
older server sends no ``why``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: The default status: what an older server's record (no ``why``) means.
QUEUED_DEFAULT = "queued: waiting for memory"

#: ``why`` -> the queued result's ``status``. ``queue`` is worded from the
#: position (:func:`queued_status`).
QUEUED_STATUS = {
    "memory": QUEUED_DEFAULT,
    "warming": QUEUED_DEFAULT,
    "spacing": "queued: starting shortly (automatic starts are staggered)",
    "cap": "queued: waiting for a free agent slot",
    "restart": "queued: waiting to restart",
}


#: How a queued result explains itself (spawn_session, spawn_ticket_session).
QUEUED_HINT = (
    "MindFlock starts it on its own, one at a time, oldest first. `why` says "
    "what it waits for: memory | warming (memory), cap (the live-agent cap), "
    "spacing (automatic starts are a few seconds apart), queue (behind "
    "others); `reason` in words."
)


def gate_record(src: Any) -> Optional[dict]:
    """The launch gate's record from a queued create's body or a session row
    (its ``waiting_for_memory``), as ``{why, position, reason}`` with only the
    keys that are set; None when ``src`` is not waiting."""
    if not isinstance(src, dict):
        return None
    rec = src.get("waiting_for_memory")
    rec = rec if isinstance(rec, dict) else {}
    if not rec and not src.get("queued"):
        return None
    out: Dict[str, Any] = {}
    why = rec.get("why") or src.get("why")
    if why:
        out["why"] = str(why)
    position = rec.get("position")
    if position is None:
        position = src.get("position")
    if position is not None:
        out["position"] = position
    # A ticket start's body says "memory_reason" ("reason" is its report-back
    # note there); a spawn's says "reason".
    reason = rec.get("reason") or src.get("memory_reason")
    if not reason and src.get("queued") and "memory_reason" not in src:
        reason = src.get("reason")
    if reason:
        out["reason"] = str(reason)
    return out


def queued_status(rec: Optional[dict]) -> str:
    """The ``status`` line of a queued start, from its record's ``why``."""
    rec = rec or {}
    why = str(rec.get("why") or "")
    if why == "queue":
        pos = rec.get("position")
        if isinstance(pos, int) and pos > 1:
            return "queued: %d ahead of it" % (pos - 1)
        return "queued: next in line"
    return QUEUED_STATUS.get(why, QUEUED_DEFAULT)


def queued_fields(body: Any) -> Dict[str, Any]:
    """The stable machine fields of a queued start's result: ``status``,
    ``queued: true``, ``why`` (None when the server sent none), ``position``
    and ``reason``."""
    rec = gate_record(body) or {}
    return {
        "status": queued_status(rec),
        "queued": True,
        "why": rec.get("why"),
        "position": rec.get("position"),
        "reason": rec.get("reason") or "",
    }


def start_failure(row: Any) -> Optional[dict]:
    """A row's ``start_failed`` record (``{error, at, prompt_preview?}``), or
    None. A record without an error still counts: the start failed."""
    if not isinstance(row, dict):
        return None
    rec = row.get("start_failed")
    if isinstance(rec, dict):
        return rec
    if rec:
        return {"error": str(rec)}
    return None
