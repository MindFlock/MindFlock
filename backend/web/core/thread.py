"""A session's family thread: who it works with, and what passed between them.

An orchestrator agent and the workers it spawned talk through the MindFlock
MCP — spawns, ``send_message`` and ``report_result`` — and none of that is on
any one pane. This module assembles it for a person, behind two surfaces:

* the row's ``last_report`` (:func:`last_report`): a worker's newest
  ``kind == "result"`` message to its CURRENT parent, consumed or not — the
  rail's "✓ reported" and the parent's "2 of 3 reported" roll-up;
* ``GET /api/instances/{title}/thread`` (:func:`thread`): the family — the
  session, its live parent, its live children — as member rows, plus one
  chronological log of the spawn records and every message any two members
  exchanged, both directions.

Everything here is READ-ONLY on the mailbox (``mailbox.between`` /
``mailbox.last_result``): reading the thread never marks a message read and
never claims a pending delivery, so a person looking at it cannot cancel a
typing or eat the report an orchestrator is long-polling for.

The routes in ``server.py`` own the 404s; the registry, the rows and the
activity probes are read back through the server namespace (``_server()``)
so tests patch them in one place.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Mapping, Optional, Tuple

from backend.web.core import lineage as _lineage
from backend.web.core import mailbox as _mailbox

__all__ = [
    "SUMMARY_CHARS",
    "SEED_CHARS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "summary",
    "report_json",
    "last_report",
    "note_seed",
    "seed_text",
    "parse_limit",
    "thread",
]

#: A report's one-line summary on a row (the rail line, the Thread worker row).
SUMMARY_CHARS = 140
#: How much of a worker's seed prompt a spawn record carries.
SEED_CHARS = 300
#: A message body in the log (the full text stays in the recipient's inbox).
TEXT_CHARS = 4000
#: Items per page, and the ceiling on ``limit``.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: ``report_result``'s separator between the summary and its details.
_DETAILS_SEP = "\n\nDetails:"
#: Seed prompts remembered at create time (see :func:`note_seed`).
_SEEDS_MAX = 512


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def summary(text: str) -> str:
    """A report body as one sanitized line of at most :data:`SUMMARY_CHARS`:
    the summary ``report_result`` was given, without its ``Details:`` block."""
    head = str(text or "").split(_DETAILS_SEP, 1)[0]
    return _cap(_mailbox.sanitize(head), SUMMARY_CHARS)


def _status(msg: Mapping) -> str:
    """A result's ``data.status`` (done / blocked / failed), sanitized the way
    the ``session.message`` event carries it; "" when it has none."""
    data = msg.get("data") if isinstance(msg.get("data"), Mapping) else {}
    raw = data.get("status")
    if not isinstance(raw, str):
        return ""
    return _mailbox.sanitize(raw)[:24]


def report_json(msg: Optional[Mapping]) -> Optional[dict]:
    """A stored result message as the row's ``last_report``, or None."""
    if not msg:
        return None
    return {
        "id": str(msg.get("id") or ""),
        "status": _status(msg),
        "summary": summary(str(msg.get("text") or "")),
        "ts": float(msg.get("ts") or 0.0),
    }


def _created_epoch(inst) -> Optional[float]:
    created = getattr(inst, "CreatedAt", None)
    try:
        return float(created.timestamp()) if created is not None else None
    except Exception:  # noqa: BLE001 — unknown reads as "no guard"
        return None


def last_report(instances: Mapping, title: str) -> Optional[dict]:
    """``title``'s newest report to its current live parent, or None (a root,
    a dead parent link, or no report yet).

    Only reports sent since this session was created count: a title can be
    reused, and the parent's box may still hold the namesake's report."""
    parent = _lineage.live_parent(instances, title)
    if not parent:
        return None
    since = _created_epoch(instances.get(title))
    return report_json(_mailbox.last_result(parent, title, since=since))


# --------------------------------------------------------------------------- #
# Seed prompts
# --------------------------------------------------------------------------- #
_SEEDS: Dict[str, Tuple[Optional[float], str]] = {}
_SEEDS_LOCK = threading.Lock()


def note_seed(title: str, created_at: Optional[float], prompt: str) -> None:
    """Remember the prompt a session was created with (the create route calls
    it), so its spawn record can show what the worker was asked to do even
    when the prompt went through the queue rather than the launch command.
    In memory and bounded: after a restart a spawn record falls back to what
    the instance itself still holds."""
    text = str(prompt or "").strip()
    if not title or not text:
        return
    with _SEEDS_LOCK:
        _SEEDS.pop(title, None)
        if len(_SEEDS) >= _SEEDS_MAX:
            _SEEDS.pop(next(iter(_SEEDS)))
        _SEEDS[title] = (created_at, text[: SEED_CHARS * 4])


def seed_text(inst) -> str:
    """The first :data:`SEED_CHARS` characters of ``inst``'s seed prompt, or
    "" when it can't be found: the create-time memo (same session — matched
    on its creation time), else the instance's own ``Prompt``, else a
    provisioned workspace's prompt file."""
    title = str(getattr(inst, "Title", "") or "")
    created = _created_epoch(inst)
    with _SEEDS_LOCK:
        hit = _SEEDS.get(title)
    text = ""
    if hit is not None and (hit[0] is None or created is None or hit[0] == created):
        text = hit[1]
    if not text:
        text = str(getattr(inst, "Prompt", "") or "").strip()
    if not text and getattr(inst, "Provisioned", False):
        try:
            from backend.session import provisioned as _provisioning

            path = os.path.join(inst.GetWorktreePath(), _provisioning.PROMPT_BASENAME)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(SEED_CHARS * 4).strip()
        except Exception:  # noqa: BLE001 — no prompt file reads as unknown
            text = ""
    return _cap(text, SEED_CHARS) if text else ""


# --------------------------------------------------------------------------- #
# The thread
# --------------------------------------------------------------------------- #
def parse_limit(raw) -> Tuple[int, Optional[str]]:
    """``(limit, error)`` from a query value: default when absent, clamped to
    :data:`MAX_LIMIT`, an error for a non-integer or non-positive one."""
    if raw is None or raw == "":
        return DEFAULT_LIMIT, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0, "limit must be an integer"
    if value <= 0:
        return 0, "limit must be positive"
    return min(value, MAX_LIMIT), None


def _base_sha(inst) -> Optional[str]:
    """The commit ``inst``'s worktree was cut from, or None when unrecorded."""
    try:
        sha = inst.GetGitWorktree().GetBaseCommitSHA()
    except Exception:  # noqa: BLE001 — not started / no worktree
        return None
    return str(sha) if sha else None


def _member(inst, role: str, rows: Mapping) -> dict:
    """One member row: the tick snapshot's fields for it when it has one (≤ a
    tick old), else the cheap row plus a memoized activity probe."""
    srv = _server()
    title = inst.Title
    row = rows.get(title)
    if row is None:
        row = srv._instance_json(inst, cheap=True)
        try:
            row["activity"] = srv._agent_activity_cached(inst, title)
        except Exception:  # noqa: BLE001 — enrichment only
            row["activity"] = "unknown"
        row["activity_since"] = srv._agent_state.state_since(title)
    return {
        "title": title,
        "role": role,
        "status": row.get("status"),
        "activity": row.get("activity"),
        "activity_since": row.get("activity_since"),
        "branch": row.get("branch") or "",
        "diff_stat": row.get("diff_stat"),
        "created_at": _created_epoch(inst),
        "last_report": last_report(srv.ENGINE.instances, title),
        "base_sha": _base_sha(inst),
    }


def _spawn_item(parent: str, child) -> dict:
    created = _created_epoch(child)
    return {
        "type": "spawn",
        "id": "spawn:%s:%d" % (child.Title, int((created or 0) * 1000)),
        "ts": created or 0.0,
        "from": parent,
        "to": child.Title,
        "text": seed_text(child),
        "status": None,
        "state": None,
        "base_sha": _base_sha(child),
    }


def _message_item(msg: Mapping) -> dict:
    result = msg.get("kind") == "result"
    return {
        "type": "result" if result else "message",
        "id": str(msg.get("id") or ""),
        "ts": float(msg.get("ts") or 0.0),
        "from": str(msg.get("from") or ""),
        "to": str(msg.get("to") or ""),
        "text": _cap(str(msg.get("text") or ""), TEXT_CHARS),
        "status": (_status(msg) or None) if result else None,
        "state": msg.get("state"),
        "base_sha": None,
    }


#: The creation time (epoch ms) an item id carries: a message's
#: ``m<ms>_<seq>``, a spawn record's ``spawn:<title>:<ms>``.
_ID_MS = re.compile(r"^m(\d+)_\d+$|^spawn:.*:(\d+)$")


def _current(msg: Mapping, created: Mapping) -> bool:
    """Whether ``msg`` was sent since both its sender and its recipient (as
    the sessions they are now) were created."""
    floors = [
        c for c in (created.get(msg.get("from")), created.get(msg.get("to"))) if c
    ]
    return not floors or float(msg.get("ts") or 0.0) >= max(floors)


def _page(items: List[dict], limit: int, before: Optional[str]):
    """The newest ``limit`` items older than ``before`` (an item id) →
    ``(items, more, error)``; ``more`` says older ones exist beyond them.

    An inbox drops its oldest messages over its caps, so the anchor a client
    pages back from ("load older" sends the oldest item it has) can be gone
    by the time it asks: the time its id carries then stands in. Only an id
    that is neither present nor time-stamped is the error."""
    end = len(items)
    if before:
        idx = next((i for i, it in enumerate(items) if it["id"] == before), None)
        if idx is None:
            m = _ID_MS.match(before)
            if m is None:
                return [], False, "unknown item: %s" % before
            cut = int(m.group(1) or m.group(2)) / 1000.0
            idx = next((i for i, it in enumerate(items) if it["ts"] >= cut), end)
        end = idx
    start = max(0, end - limit)
    return items[start:end], start > 0, None


def thread(title: str, limit: int = DEFAULT_LIMIT, before: Optional[str] = None):
    """``title``'s family thread → ``(body, error)``. Blocking (it may probe a
    member's activity) — call via ``asyncio.to_thread``. The caller has
    already checked that ``title`` is a live session.

    ``body``: ``{"title", "parent", "members", "items", "more"}`` — members
    are the session (role ``self``), its live parent and its live children;
    items (oldest first, newest last) are a spawn record per parent→child
    edge in the family plus every message between two members sent since
    both were created (a reused title doesn't inherit its namesake's mail).
    ``before`` pages back from an item id — one evicted meanwhile by the
    time its id carries; an id with neither is the error."""
    srv = _server()
    with srv.ENGINE.lock:
        instances = dict(srv.ENGINE.instances)
    me = instances.get(title)
    if me is None:
        return None, "instance not found: %s" % title
    parent = _lineage.live_parent(instances, title)
    children = [instances[c] for c in _lineage.children_of(instances, title)]
    rows = {d.get("title"): d for d in srv._events.sessions_snapshot()}
    members = [_member(me, "self", rows)]
    if parent:
        members.append(_member(instances[parent], "parent", rows))
    members.extend(_member(c, "child", rows) for c in children)

    items: List[dict] = []
    if parent:
        items.append(_spawn_item(parent, me))
    items.extend(_spawn_item(title, c) for c in children)
    family = [m["title"] for m in members]
    # A title can be reused: the parent's box may still hold reports from a
    # deleted namesake. A message counts only when it is no older than BOTH
    # ends of it — the same rule ``last_report`` applies to the row.
    created = {t: _created_epoch(instances.get(t)) for t in family}
    items.extend(
        _message_item(m) for m in _mailbox.between(family) if _current(m, created)
    )
    # Stable: a spawn and the first message in the same instant keep the
    # spawn first (it was appended first).
    items.sort(key=lambda it: it["ts"])
    page, more, err = _page(items, limit, before)
    if err is not None:
        return None, err
    return {
        "title": title,
        "parent": parent,
        "members": members,
        "items": page,
        "more": more,
    }, None
