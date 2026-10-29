"""The session chat across a peer link (docs/people.md "A small chat";
docs/peer-link.md "Shared sessions — CONTRACT"): the joiner and the sharer
of a shared session talk in the same little window. Agents are never in it.

The chat lives with its session, on the sharer's machine
(:mod:`backend.web.core.chat`):

- **Sharer.** ``chat_msg {title, text, from_name}`` from the peer is
  :func:`inbound_chat`: refused unless ``title`` is shared on that link, else
  posted as ``peer:<link id>`` under the peer's name. Every line of a shared
  session's chat (``session.chat`` on the bus — mine, a guest's, the peer's
  own) goes to each connected link it is shared on as ``chat_out {title,
  msg}`` (:func:`fan_out`); on the receiver's own lines ``from`` is
  ``"you"``, since link ids are per side.
- **Opener.** :func:`send` is ``POST /api/instances/<peer>::<title>/chat``:
  ``chat_msg`` over the link. ``chat_out`` is :func:`inbound_out`: a copy
  kept under the row's title (``chat.receive``, which emits ``session.chat``
  there so the panel shows it). ``from`` is rewritten so the panel can tell
  whose line it is: ``"you"`` becomes my own id, anything else
  ``peer:<link id>:<their id>`` (never mine). The copies are forgotten when
  the link drops or they stop sharing that session.

:func:`install` subscribes to the bus once the peer service runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import Dict, Optional

from backend.web.core import chat as _chat

_log = logging.getLogger("mindflock.peer")

#: The wire's limits on a ``chat_out`` line (backend.peer.wire).
_NAME_MAX = 64
_FROM_MAX = 128

_LOCK = threading.Lock()
#: Opener side: ``{link id: {their title: row title}}`` — the chats copied
#: here, to forget when the link drops.
_ROWS: Dict[str, Dict[str, str]] = {}
_STATE: dict = {"service": None, "loop": None, "unsub": None}


def _lid(link) -> str:
    if isinstance(link, str):
        return link
    if isinstance(link, dict):
        return str(link.get("link_id") or "")
    return str(getattr(link, "link_id", "") or "")


def _peer_name(link, default: str = "peer") -> str:
    raw = (
        link.get("peer_name")
        if isinstance(link, dict)
        else getattr(link, "peer_name", "")
    )
    return str(raw or "").strip() or default


def _op_error(message: str) -> Exception:
    from backend.peer.transport import PeerOpError

    return PeerOpError(message)


def _speaks_chat(service, link_id: str) -> bool:
    """The peer advertised ``chat_msg`` (an older one doesn't know
    ``chat_out``)."""
    t = getattr(service, "_transport", None)
    try:
        info = t.peer_info(link_id) if t is not None else {}
    except Exception:  # noqa: BLE001
        return False
    return "chat_msg" in (info.get("caps") or ())


# --------------------------------------------------------------------------- #
# The sharer: their lines in, every line out
# --------------------------------------------------------------------------- #
def inbound_chat(service, link, p: dict) -> dict:
    """``chat_msg``: the peer's line into my chat for ``title`` → ``{ok,
    id}``. Never a session not shared on this link."""
    title = str(p.get("title") or "")
    if title not in service._my_shared(link):
        raise _op_error("not shared")
    name = _chat.clean_text(p.get("from_name")).replace("\n", " ")[:_NAME_MAX]
    who = {"id": "peer:" + _lid(link), "name": name or _peer_name(link)}
    try:
        msg = _chat.post(title, who, p.get("text"))
    except ValueError as err:
        raise _op_error("nothing to send") from err
    return {"ok": True, "id": msg["id"]}


def _wire_line(msg: dict, link_id: str) -> dict:
    sender = str(msg.get("from") or "")
    return {
        "id": str(msg.get("id") or ""),
        "from": "you" if sender == "peer:" + link_id else sender[:_FROM_MAX],
        "name": str(msg.get("name") or "")[:_NAME_MAX],
        "text": str(msg.get("text") or ""),
        "ts": float(msg.get("ts") or 0),
    }


async def fan_out(service, title: str, msg: dict) -> int:
    """Push one line of ``title``'s chat to every connected link it is
    shared on (whose peer speaks the chat) → how many it reached."""
    if not title or "::" in title:
        return 0
    try:
        links = list(service.store.list())
    except Exception:  # noqa: BLE001
        return 0
    sent = 0
    for link in links:
        lid = _lid(link)
        if not lid or not service.is_connected(lid) or not _speaks_chat(service, lid):
            continue
        try:
            if title not in service._my_shared(link):
                continue
            await service._push(
                lid, "chat_out", {"title": title, "msg": _wire_line(msg, lid)}
            )
            sent += 1
        except Exception as err:  # noqa: BLE001 — one link never stops the rest
            _log.debug("peer: chat line not pushed: %s", type(err).__name__)
    return sent


# --------------------------------------------------------------------------- #
# The opener: my lines out, theirs in
# --------------------------------------------------------------------------- #
def _shares(service, link_id: str, title: str) -> bool:
    return any(i.get("title") == title for i in service.their_sessions(link_id))


def row_title(service, link_id: str, title: str) -> str:
    """``<peer>::<title>``: the row this chat is kept under here."""
    return service._row_prefix(link_id) + "::" + title


def _remember(link_id: str, title: str, row: str) -> None:
    with _LOCK:
        _ROWS.setdefault(link_id, {})[title] = row


async def inbound_out(service, link, p: dict) -> Optional[dict]:
    """``chat_out``: a line of THEIR shared session's chat → my copy under
    the row (``session.chat`` there). Dropped for a session they don't share
    with me."""
    lid = _lid(link)
    title = str(p.get("title") or "")
    if not lid or not _shares(service, lid, title):
        return None
    msg = dict(p.get("msg") or {})
    sender = str(msg.get("from") or "")
    if sender == "you":
        msg["from"] = _chat.owner_identity()["id"]
    else:
        msg["from"] = "peer:%s:%s" % (lid, sender)
    row = row_title(service, lid, title)
    _remember(lid, title, row)
    return _chat.receive(row, msg)


async def send(service, link_id: str, title: str, text) -> dict:
    """Me saying something in THEIR shared session's chat (my row
    ``<peer>::<title>``) → the message as kept here; raises
    ``PeerServiceError``: 400 blank, 409 not shared with me (or their
    MindFlock is too old), 503 they're offline."""
    from backend.peer.service import PeerServiceError

    link = service._link_or_404(link_id)
    name = _peer_name(link, "they")
    body = _chat.clean_text(text)
    if not body:
        raise PeerServiceError("nothing to send")
    if not service.is_connected(link_id):
        raise PeerServiceError(
            "%s isn't connected right now — the message was not sent" % name, 503
        )
    if not _shares(service, link_id, title):
        raise PeerServiceError("%s doesn't share %r with you" % (name, title), 409)
    me = _chat.owner_identity()
    p = {
        "title": title,
        "text": body,
        "from_name": me["name"].replace("\n", " ")[:_NAME_MAX],
    }
    try:
        res = await service.request(link_id, "chat_msg", p, timeout=15.0)
    except Exception as err:  # noqa: BLE001 — PeerUnavailable, PeerOpError
        if "not shared" in str(err):
            service._schedule(service.refresh_their_sessions(link_id))
            raise PeerServiceError(
                "%s stopped sharing %r — the message was not sent" % (name, title),
                409,
            ) from err
        mapped = service._peer_error(link, err, "chat_msg")
        if mapped.status == 502 and "isn't connected" in mapped.message:
            mapped.status = 503
        raise mapped from err
    msg = {
        "id": str(res.get("id") or ""),
        "from": me["id"],
        "name": me["name"],
        "text": body,
        "ts": time.time(),
    }
    row = row_title(service, link_id, title)
    _remember(link_id, title, row)
    _chat.receive(row, msg)  # None when its echo came first: same line
    return dict(msg)


def messages(service, link_id: str, title: str, since: str = "") -> list:
    """My copy of THEIR shared session's chat (since the row appeared)."""
    return _chat.messages(row_title(service, link_id, title), since)


def forget_link(link_id: str, title: str = "") -> None:
    """Drop my copies of a link's chats (one session's, or all of them)."""
    with _LOCK:
        rows = _ROWS.get(link_id) or {}
        drop = [rows.pop(title, "")] if title else list(rows.values())
        if not title or not rows:
            _ROWS.pop(link_id, None)
    for row in drop:
        if row:
            _chat.forget(row)


# --------------------------------------------------------------------------- #
# The bus
# --------------------------------------------------------------------------- #
def _on_event(env: dict) -> None:
    event = env.get("event")
    data = env.get("data") or {}
    if event == "session.chat":
        title = str(env.get("session") or "")
        svc, loop = _STATE["service"], _STATE["loop"]
        if svc is None or loop is None or not title or "::" in title:
            return
        if loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(fan_out(svc, title, dict(data)), loop)
    elif event == "peer.state" and data.get("connected") is False:
        forget_link(str(data.get("link_id") or ""))
    elif event == "peer.link_removed":
        forget_link(str(data.get("link_id") or ""))
    elif event == "peer.session_unshared" and data.get("theirs"):
        title = str(data.get("title") or "")
        if title:
            forget_link(str(data.get("link_id") or ""), title)


def install(service) -> None:
    """Start carrying chats over ``service``'s links (idempotent; call it on
    the server's event loop)."""
    with contextlib.suppress(RuntimeError):
        _STATE["loop"] = asyncio.get_running_loop()
    _STATE["service"] = service
    if _STATE["unsub"] is None:
        from backend.web.core import events as _events

        _STATE["unsub"] = _events.BUS.subscribe(_on_event)


def _reset() -> None:
    """Test seam: unsubscribe and forget every copy."""
    unsub = _STATE["unsub"]
    if unsub is not None:
        unsub()
    _STATE.update(service=None, loop=None, unsub=None)
    with _LOCK:
        _ROWS.clear()
