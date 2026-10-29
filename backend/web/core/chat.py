"""Session chat (docs/people.md, "A small chat"): the PEOPLE on one session
talking to each other in a little window — its owner, a guest who came in
through a guest link (:mod:`backend.web.core.guests`), a peer with the
session open from their own MindFlock. Agents are never in it.

In memory only: the last :data:`KEEP` messages per session, gone on a
restart and when the session is deleted (``session.deleted`` on the bus,
:func:`subscribe`). Every message is ``{id, from, name, text, ts}`` —
``id`` is ``"c<seq>"`` (monotonic across sessions, so ``since`` is a plain
comparison), ``from`` the person id, ``"guest:<link id>"`` or
``"peer:<link id>"`` — and :func:`post` emits ``session.chat`` with it.

The peer link carries a shared session's chat both ways
(:mod:`backend.peer.chat_bridge`). On the sharer's side a peer's line is an
ordinary post::

    chat.post(title, {"id": "peer:<link id>", "name": peer_name}, text)

On the opener's side the sharer's lines are copies, kept under the row's
``<peer>::<title>`` and added with :func:`receive` (their ids are the
sharer's, so ``since`` still compares within that chat).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

#: Messages kept per session.
KEEP = 200
#: Longest message, in characters (the rest is cut).
MAX_TEXT = 2000

_LOCK = threading.Lock()
_SEQ = 0
_CHATS: Dict[str, Deque[dict]] = {}
_UNSUB: Optional[Callable[[], None]] = None

# C0 controls except tab and newline, DEL, and the C1 block.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(text: object) -> str:
    """``text`` as a message body: control characters stripped (``\\r\\n``
    becomes ``\\n``), outer blank lines trimmed, cut to :data:`MAX_TEXT`.
    ``""`` means nothing to send."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _CONTROL.sub("", s).strip()
    return s[:MAX_TEXT]


def _seq_of(msg_id: str) -> int:
    try:
        return int(str(msg_id or "").lstrip("c") or 0)
    except ValueError:
        return 0


def post(title: str, who: dict, text: object) -> dict:
    """Add a message from ``who`` (``{id, name}``) to ``title``'s chat and
    emit ``session.chat``. Raises ``ValueError`` for a blank message."""
    global _SEQ
    body = clean_text(text)
    if not body:
        raise ValueError("nothing to send")
    title = str(title or "")
    with _LOCK:
        _SEQ += 1
        msg = {
            "id": f"c{_SEQ}",
            "from": str(who.get("id") or ""),
            "name": str(who.get("name") or "").strip() or "Someone",
            "text": body,
            "ts": time.time(),
        }
        _CHATS.setdefault(title, deque(maxlen=KEEP)).append(msg)
    try:
        from backend.web.core import events as _events

        _events.BUS.emit("session.chat", session=title, data=dict(msg))
    except Exception:  # noqa: BLE001 — the message is kept either way
        pass
    return dict(msg)


def receive(title: str, msg: dict) -> Optional[dict]:
    """Keep a copy of another machine's message (``{id, from, name, text,
    ts}``, id included) in ``title``'s chat, in id order, and emit
    ``session.chat`` with it. None — and no event — when that id is already
    here or the message is blank."""
    body = clean_text(msg.get("text"))
    mid = str(msg.get("id") or "")
    if not body or not _seq_of(mid):
        return None
    try:
        ts = float(msg.get("ts") or 0) or time.time()
    except (TypeError, ValueError):
        ts = time.time()
    copy = {
        "id": mid,
        "from": str(msg.get("from") or ""),
        "name": clean_text(msg.get("name")).replace("\n", " ")[:64] or "Someone",
        "text": body,
        "ts": ts,
    }
    title = str(title or "")
    with _LOCK:
        chat = _CHATS.setdefault(title, deque(maxlen=KEEP))
        if any(m["id"] == mid for m in chat):
            return None
        seq = _seq_of(mid)
        if chat and _seq_of(chat[-1]["id"]) > seq:
            rows = sorted([*chat, copy], key=lambda m: _seq_of(m["id"]))
            chat.clear()
            chat.extend(rows[-KEEP:])
        else:
            chat.append(copy)
    try:
        from backend.web.core import events as _events

        _events.BUS.emit("session.chat", session=title, data=dict(copy))
    except Exception:  # noqa: BLE001 — the message is kept either way
        pass
    return dict(copy)


def messages(title: str, since_id: str = "") -> List[dict]:
    """``title``'s messages, oldest first; only those after ``since_id``
    when given."""
    since = _seq_of(since_id)
    with _LOCK:
        rows = list(_CHATS.get(str(title or ""), ()))
    return [dict(m) for m in rows if _seq_of(m["id"]) > since]


def forget(title: str = "") -> None:
    """Drop one session's chat (deleted), or every chat."""
    with _LOCK:
        if title:
            _CHATS.pop(title, None)
        else:
            _CHATS.clear()


def _on_event(env: dict) -> None:
    if env.get("event") == "session.deleted" and env.get("session"):
        forget(str(env["session"]))


def subscribe() -> None:
    """Forget a session's chat when it is deleted (idempotent)."""
    global _UNSUB
    if _UNSUB is None:
        from backend.web.core import events as _events

        _UNSUB = _events.BUS.subscribe(_on_event)


def owner_identity() -> dict:
    """Who the owner's browser is: ``{id, name}`` — their person record,
    named by its name, else this device's name, else "Owner"."""
    pid, name = "", ""
    try:
        from backend.web.core import person as _person

        rec = _person.current()
        pid, name = str(rec.get("id") or ""), str(rec.get("name") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if not name:
        try:
            from backend.web.core import remote as _remote

            name = str(_remote.self_identity().get("host") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
    return {"id": pid or "owner", "name": name or "Owner"}


def guest_identity(guest: dict) -> dict:
    """Who a guest link's browser is: ``{"id": "guest:<link id>", name}``."""
    from backend.web.core import guests as _guests

    return {"id": f"guest:{guest.get('id') or ''}", "name": _guests.display_name(guest)}


def _reset() -> None:
    """Test seam: empty every chat and restart the ids."""
    global _SEQ
    with _LOCK:
        _CHATS.clear()
        _SEQ = 0
