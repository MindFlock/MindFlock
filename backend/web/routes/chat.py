"""Session chat (docs/people.md, "A small chat"): the people on one session
— its owner, a guest on it, a peer with it open — talking to each other in a
little window; ``/api/instances/{title}/chat``.

The routes only translate (:mod:`backend.web.core.chat` keeps the messages).
"Me" is the request's person: a guest link's browser is that link
(``guest:<id>``, the name they picked), anyone else is the owner. The gate
has already limited a guest to the sessions their link reaches (``chat`` is
in ``guests.SESSION_WRITE_OK``). A chat lives where its session lives: a
``<device>::<title>`` row is 409, a session this device doesn't have 404.
A peer's shared session (``<peer>::<title>``) never reaches here: the peer
rows middleware answers it (routes/peer_sessions.py, over the peer link).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import chat as _chat
from backend.web.core import guests as _guests
from backend.web.routes import _server

router = APIRouter()

# A deleted session's chat goes with it.
_chat.subscribe()


def _me(request: Request) -> dict:
    guest = _guests.scope_guest(request)
    if guest is not None:
        return _chat.guest_identity(guest)
    return _chat.owner_identity()


def _refusal(title: str) -> Optional[JSONResponse]:
    if "::" in title:
        return JSONResponse(
            {"error": "that session's chat is on its own device"}, status_code=409
        )
    if title not in _server().ENGINE.instances:
        return JSONResponse({"error": "no such session"}, status_code=404)
    return None


@router.get("/api/instances/{title}/chat")
async def chat_messages(request: Request, title: str, since: str = "") -> JSONResponse:
    """``{messages, me: {id, name}}`` — the session's chat (after ``since``,
    a message id, when given) and who this browser is in it."""
    refused = _refusal(title)
    if refused is not None:
        return refused
    return JSONResponse({"messages": _chat.messages(title, since), "me": _me(request)})


@router.post("/api/instances/{title}/chat")
async def chat_post(
    request: Request, title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{text}`` → **201** the message (``session.chat`` tells everyone on
    the session); a blank one is 400."""
    refused = _refusal(title)
    if refused is not None:
        return refused
    try:
        msg = _chat.post(title, _me(request), (payload or {}).get("text"))
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(msg, status_code=201)
