"""Per-link message log (``~/.mindflock/peer/messages/<link_id>.json``, 0600).

Every message that crosses a link — theirs (``in``) and ours (``out``, sent by
the shared agent or typed by you) — is kept here, so the people can follow
the conversation and take part in it without a shared session: a Mac or any
side that hasn't shared a folder (no sandbox) used to drop everything the
peer sent. An inbound message that no shared session took waits here
(``delivered_to: None``) and is handed to the link's session once one is
bound (:meth:`PeerService.share`).

The peer's text is untrusted: it is stored as received (the wire already
refused control characters and capped the length) and only ever rendered as
text. Bounded: :data:`MAX_ENTRIES` per link, oldest first out. Single
process (the server's one PeerService), so a thread lock plus atomic writes
are enough.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from typing import Iterable, List, Optional

from backend.peer import paths

__all__ = [
    "record",
    "entries",
    "unread_count",
    "undelivered",
    "mark_delivered",
    "mark_read",
    "drop",
]

MAX_ENTRIES = 200
MAX_TEXT = 20_000
BYS = ("peer", "you", "agent")
_LINK_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_LOCK = threading.Lock()


def _dir() -> str:
    return paths.ensure_dir(os.path.join(paths.peer_root(), "messages"))


def _path(link_id: str) -> str:
    if not isinstance(link_id, str) or not _LINK_ID_RE.match(link_id):
        raise ValueError("bad link id")
    return os.path.join(_dir(), link_id + ".json")


def _load(link_id: str) -> List[dict]:
    try:
        fd = os.open(_path(link_id), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        with os.fdopen(fd, "rb") as f:
            doc = json.loads(f.read(8 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    items = doc.get("messages") if isinstance(doc, dict) else None
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _save(link_id: str, items: List[dict]) -> None:
    path = _path(link_id)
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), os.urandom(4).hex())
    data = json.dumps({"version": 1, "messages": items[-MAX_ENTRIES:]}).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record(
    link_id: str,
    direction: str,
    text: str,
    *,
    by: str,
    msg_id: str = "",
    reply_to: Optional[str] = None,
    delivered_to: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Append one message (``direction`` ``in`` | ``out``); returns it."""
    if direction not in ("in", "out") or by not in BYS:
        raise ValueError("bad message direction or sender")
    with _LOCK:
        items = _load(link_id)
        seq = (items[-1].get("id", 0) if items else 0) + 1
        entry = {
            "id": int(seq),
            "dir": direction,
            "by": by,
            "text": str(text or "")[:MAX_TEXT],
            "ts": time.time() if now is None else float(now),
            "msg_id": str(msg_id or "")[:64],
            "reply_to": str(reply_to)[:64] if reply_to else None,
            "delivered_to": delivered_to,
            # Ours, and theirs that a shared session took, need no reading.
            "read": direction == "out" or bool(delivered_to),
        }
        items.append(entry)
        _save(link_id, items)
        return dict(entry)


def entries(link_id: str, limit: int = 100) -> dict:
    """``{"messages": [oldest→newest], "unread": n}`` (read-only)."""
    with _LOCK:
        items = _load(link_id)
    limit = max(1, min(int(limit or 100), MAX_ENTRIES))
    return {
        "messages": items[-limit:],
        "unread": sum(1 for m in items if not m.get("read")),
    }


def unread_count(link_id: str) -> int:
    with _LOCK:
        return sum(1 for m in _load(link_id) if not m.get("read"))


def undelivered(link_id: str) -> List[dict]:
    """Inbound messages no shared session took yet, oldest first."""
    with _LOCK:
        return [
            m
            for m in _load(link_id)
            if m.get("dir") == "in" and not m.get("delivered_to")
        ]


def mark_delivered(link_id: str, ids: Iterable[int], title: str) -> None:
    wanted = set(ids)
    with _LOCK:
        items = _load(link_id)
        for m in items:
            if m.get("id") in wanted:
                m["delivered_to"] = title
        _save(link_id, items)


def mark_read(link_id: str) -> int:
    """Mark every message read; returns how many were unread."""
    with _LOCK:
        items = _load(link_id)
        n = 0
        for m in items:
            if not m.get("read"):
                m["read"] = True
                n += 1
        if n:
            _save(link_id, items)
        return n


def drop(link_id: str) -> None:
    """Forget a link's messages (it was unlinked)."""
    with _LOCK, contextlib.suppress(OSError, ValueError):
        os.unlink(_path(link_id))
