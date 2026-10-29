"""Sessions shared over a peer link (docs/peer-link.md, "Shared sessions —
CONTRACT"): the ``/api/peer/links/{id}/sessions*`` routes and the plumbing
for a peer's sessions on this rail.

A connected peer's shared sessions are rows in ``GET /api/instances`` titled
``<peer>::<title>`` (:func:`peer_rows`), the way another tailnet device's
are. Requests for them never reach the device proxy: :class:`PeerRowsMiddleware`
(mounted inside the auth gate, outside ``core.remote``'s proxy) rewrites
``/api/instances/<peer>::<title>/…`` to ``/api/peer/rows/<link id>/<title>/…``,
where the row answers, its terminal socket rides the peer link
(``term_open`` / ``term_in`` / ``term_out``), ``/focus`` tells the peer
which row this page selected, ``POST …/messages`` sends an agent's message
over the link (``agent_msg``), ``…/chat`` is the session's people chat
(``chat_msg`` out, ``chat_out`` copies in: ``backend.peer.chat_bridge``) —
and every other call is a 409: the session lives on the peer's machine.

Like the other peer routes, none of this answers another MindFlock relaying
(X-MindFlock-Remote) or a guest link, and a change needs the owner
(``auth.may_configure``). The work lives in ``backend.peer.service``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import quote

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from backend.web.core import remote as _remote
from backend.web.routes import _server
from backend.web.routes.peer import _peer_body, _peer_call, _peer_remote_refusal

router = APIRouter()

INSTANCES_PREFIX = "/api/instances/"
#: Where the middleware sends a peer row's requests (``<link id>/<title>/…``).
ROWS_PREFIX = "/api/peer/rows/"
#: How long ``GET …/sessions`` waits for the peer's fresh list.
_REFRESH_WAIT_S = 5.0
#: Websocket close codes the web terminal understands (lib/terminals.ts
#: closeAction): 4404 gone (stop), 4409 refused (stop); 1000 reconnects.
_WS_GONE = 4404
_WS_REFUSED = 4409


def _service():
    """The PeerService, read off the server namespace (tests patch it)."""
    return _server()._peer_service()


def peer_rows() -> list:
    """Connected peers' shared sessions as ``GET /api/instances`` rows — an
    in-memory read (nothing while peer links are off)."""
    try:
        svc = _service()
        if getattr(svc, "_transport", None) is None:
            return []
        return svc.peer_rows()
    except Exception:  # noqa: BLE001 — a peer never fails the listing
        return []


# --------------------------------------------------------------------------- #
# Routing a <peer>::<title> request here instead of to a device
# --------------------------------------------------------------------------- #
def split_row_path(path: str):
    """``(prefix, title, tail)`` for ``/api/instances/<prefix>::<title><tail>``,
    else None."""
    if not path.startswith(INSTANCES_PREFIX):
        return None
    seg, slash, tail = path[len(INSTANCES_PREFIX) :].partition("/")
    if _remote.NS not in seg:
        return None
    prefix, _, title = seg.partition(_remote.NS)
    if not prefix or not title or title in (".", ".."):
        return None
    return prefix, title, (slash + tail) if slash else ""


def _rewrite(scope: dict):
    hit = split_row_path(scope.get("path", ""))
    if hit is None:
        return None
    prefix, title, tail = hit
    try:
        link_id = _service().row_prefixes().get(prefix)
    except Exception:  # noqa: BLE001
        link_id = None
    if not link_id:
        return None  # not a peer: a tailnet device's row (core.remote)
    path = ROWS_PREFIX + link_id + "/" + title + tail
    return dict(scope, path=path, raw_path=quote(path).encode("ascii"))


class PeerRowsMiddleware:
    """Answer ``/api/instances/<peer>::<title>/…`` here (see the module
    docstring); anything else passes through untouched."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            moved = _rewrite(scope)
            if moved is not None:
                scope = moved
        await self.app(scope, receive, send)


def _lives_there(link_id: str) -> str:
    try:
        name = _service().peer_name(link_id)
    except Exception:  # noqa: BLE001
        name = "the peer"
    return "This session lives on %s's machine — open its terminal to work in it" % name


def _refused_here(scope) -> str:
    """Why a peer row's request is refused outright: another MindFlock
    relaying, or a guest link (neither may reach a peer's sessions)."""
    if _remote._has_remote_header(scope):
        return "a peer's shared sessions are only reachable on this device"
    try:
        from backend.web.core import guests as _guests

        if _guests.scope_guest(scope) is not None:
            return "not on your link"
    except Exception:  # noqa: BLE001
        pass
    return ""


# --------------------------------------------------------------------------- #
# People: what I share on a link, what they share with me
# --------------------------------------------------------------------------- #
@router.get("/api/peer/links/{link_id}/sessions")
async def peer_sessions(request: Request, link_id: str) -> JSONResponse:
    """``{mine: [{title, mode, since, source, live, open, focused}], theirs:
    [{title, name, status, activity, stage, branch, folder, mode, program}],
    connected, their_unsupported}`` — theirs asked afresh when connected."""

    async def view():
        svc = _service()
        svc._link_or_404(link_id)
        if svc.is_connected(link_id):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    svc.refresh_their_sessions(link_id), _REFRESH_WAIT_S
                )
        return svc.sessions_view(link_id)

    return await _peer_call(request, view)


@router.post("/api/peer/links/{link_id}/sessions")
async def peer_share_session(request: Request, link_id: str) -> JSONResponse:
    """``{title, mode: "gated"|"full"}`` → the view above. ``full`` lets them
    type into that session as it is (a guest link's trust); ``gated`` makes
    the link's sandboxed shared folder from it. 409 before *It matches*."""
    body = await _peer_body(request)
    return await _peer_call(
        request,
        _service().share_session,
        link_id,
        body.get("title"),
        body.get("mode") or "gated",
    )


@router.delete("/api/peer/links/{link_id}/sessions/{title}")
async def peer_unshare_session(
    request: Request, link_id: str, title: str
) -> JSONResponse:
    """Withdraw one shared session: their terminal on it ends; a full one is
    untouched, a gated one closed (folder kept)."""
    return await _peer_call(request, _service().unshare_session, link_id, title)


# --------------------------------------------------------------------------- #
# A peer's row on this rail
# --------------------------------------------------------------------------- #
def _row(link_id: str, title: str):
    svc = _service()
    for prefix, lid in svc.row_prefixes().items():
        if lid != link_id:
            continue
        for item in svc.their_sessions(lid):
            if item.get("title") == title:
                return svc._peer_row(lid, prefix, svc.peer_name(lid), item)
    return None


@router.get(ROWS_PREFIX + "{link_id}/{title}")
async def peer_row(request: Request, link_id: str, title: str) -> JSONResponse:
    """``GET /api/instances/<peer>::<title>``: the row."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    row = _row(link_id, title)
    if row is None:
        return JSONResponse({"error": "not shared with you"}, status_code=404)
    return JSONResponse(row)


@router.post(ROWS_PREFIX + "{link_id}/{title}/focus")
async def peer_row_focus(request: Request, link_id: str, title: str) -> JSONResponse:
    """This page selected the row (every 30 s while it stays selected) — or
    ``{"left": true}``: it moved off. Their rail shows a pill with our name
    (``guest.focus_changed`` there)."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    refused = await _peer_remote_refusal(request)
    if refused is not None:
        return refused
    body = await _peer_body(request)
    from backend.peer.service import PeerServiceError

    try:
        await _service().focus_their(link_id, "" if body.get("left") else title)
    except PeerServiceError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    return JSONResponse({"ok": True, "peer": True})


@router.post(ROWS_PREFIX + "{link_id}/{title}/messages")
async def peer_row_message(request: Request, link_id: str, title: str):
    """``POST /api/instances/<peer>::<title>/messages``: an agent in one of my
    sessions (``from``) messaging their shared session — sent over the link
    (``agent_msg``), stored in THEIR mailbox and typed in there as another
    person's agent. Body as the local route's (``text``, ``from``,
    ``reply_to``); it is always ``auto`` there. 202 ``{ok, id, their_id,
    peer: true}``; 409 not shared with me, 503 the peer is offline."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    refused = await _peer_remote_refusal(request)
    if refused is not None:
        return refused
    body = await _peer_body(request)
    text, sender = body.get("text", ""), body.get("from") or ""
    reply_to = body.get("reply_to") or None
    if not isinstance(text, str) or not isinstance(sender, str):
        return JSONResponse({"error": "text and from must be strings"}, 400)
    if reply_to is not None and not isinstance(reply_to, str):
        return JSONResponse({"error": "reply_to must be a message id"}, 400)
    if (body.get("kind") or "message") != "message":
        return JSONResponse(
            {"error": "only messages reach another person's session"}, 400
        )
    from backend.peer.service import PeerServiceError

    try:
        out = await _service().send_agent_msg(link_id, title, text, sender, reply_to)
    except PeerServiceError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    if (body.get("delivery") or "auto") != "auto":
        out["detail"] = (
            "sent as auto: their MindFlock types it in when that agent is idle"
        )
    return JSONResponse(out, status_code=202)


@router.get(ROWS_PREFIX + "{link_id}/{title}/messages")
async def peer_row_messages(request: Request, link_id: str, title: str):
    """A peer's session's inbox is not read from here."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    try:
        name = _service().peer_name(link_id)
    except Exception:  # noqa: BLE001
        name = "the peer"
    return JSONResponse(
        {"error": "their inbox lives on their machine (%s's)" % name},
        status_code=409,
    )


@router.get(ROWS_PREFIX + "{link_id}/{title}/chat")
async def peer_row_chat(request: Request, link_id: str, title: str, since: str = ""):
    """``GET /api/instances/<peer>::<title>/chat``: ``{messages, me}`` like
    a local session's — my copy of their chat (``chat_out`` lines since the
    row appeared; the chat itself lives on their machine). 409 not shared
    with me, 503 the peer is offline."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    from backend.peer import chat_bridge
    from backend.web.core import chat as _chat

    svc = _service()
    if not svc.is_connected(link_id):
        return JSONResponse(
            {"error": "%s isn't connected right now" % svc.peer_name(link_id)}, 503
        )
    if _row(link_id, title) is None:
        return JSONResponse({"error": "not shared with you"}, status_code=409)
    return JSONResponse(
        {
            "messages": chat_bridge.messages(svc, link_id, title, since),
            "me": _chat.owner_identity(),
        }
    )


@router.post(ROWS_PREFIX + "{link_id}/{title}/chat")
async def peer_row_chat_post(request: Request, link_id: str, title: str):
    """``POST /api/instances/<peer>::<title>/chat``: ``{text}`` → sent over
    the link (``chat_msg``) into THEIR chat for the session — 202 the message
    as kept here (``session.chat`` on the row tells my other windows; their
    echo carries the same id). 400 blank, 409 not shared with me, 503 the
    peer is offline."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    refused = await _peer_remote_refusal(request)
    if refused is not None:
        return refused
    body = await _peer_body(request)
    text = body.get("text", "")
    if not isinstance(text, str):
        return JSONResponse({"error": "text must be a string"}, 400)
    from backend.peer import chat_bridge
    from backend.peer.service import PeerServiceError

    try:
        msg = await chat_bridge.send(_service(), link_id, title, text)
    except PeerServiceError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    return JSONResponse(msg, status_code=202)


@router.websocket(ROWS_PREFIX + "{link_id}/{title}/terminal")
async def peer_row_terminal(ws: WebSocket, link_id: str, title: str) -> None:
    """The row's terminal: this socket joins the peer's terminal on the
    session (one per row, shared by every window here that opens it) —
    their output in, keystrokes and resizes out."""
    await ws.accept()
    why = _refused_here(ws.scope)
    if why or _row(link_id, title) is None:
        await ws.close(code=_WS_GONE)
        return
    from backend.peer.service import PeerServiceError

    svc = _service()
    try:
        term, sink = await svc.open_their_term(link_id, title, 80, 24)
    except PeerServiceError as err:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"type": "error", "message": err.message}))
        await ws.close(code=_WS_GONE if err.status == 404 else _WS_REFUSED)
        return
    bridge = svc.bridge

    async def pump_out() -> None:
        while True:
            data = await sink.get()
            if data is None:
                break
            await ws.send_bytes(data)
        code = _WS_GONE if term.reason == "withdrawn" else 1000
        with contextlib.suppress(Exception):
            await ws.close(code=code)

    sender = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            text = msg.get("text")
            if text is not None:
                try:
                    j = json.loads(text)
                except (ValueError, TypeError):
                    j = None
                if isinstance(j, dict) and j.get("type") == "resize":
                    try:
                        cols = max(1, min(1000, int(j["cols"])))
                        rows = max(1, min(1000, int(j["rows"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                    await bridge.resize_theirs(term, cols, rows)
                    continue
                data = text.encode("utf-8")
            if data:
                await bridge.type_theirs(term, data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as err:  # noqa: BLE001 — the link dropped mid-keystroke
        with contextlib.suppress(Exception):
            msg = getattr(err, "message", "") or "the peer link dropped"
            await ws.send_text(json.dumps({"type": "error", "message": msg}))
    finally:
        sender.cancel()
        await bridge.detach_theirs(term, sink)
        with contextlib.suppress(Exception):
            await ws.close()


@router.websocket(ROWS_PREFIX + "{link_id}/{title}/{rest:path}")
async def peer_row_other_socket(ws: WebSocket, link_id: str, title: str, rest: str):
    """A shell or log stream of a peer's session: not offered."""
    await ws.accept()
    with contextlib.suppress(Exception):
        await ws.send_text(
            json.dumps({"type": "error", "message": _lives_there(link_id)})
        )
    await ws.close(code=_WS_REFUSED)


@router.api_route(
    ROWS_PREFIX + "{link_id}/{title}/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def peer_row_other(request: Request, link_id: str, title: str, rest: str):
    """Everything else on a peer's row: a 409 — commit, push, rename, close,
    send, … belong to the person whose machine it runs on."""
    why = _refused_here(request.scope)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    return JSONResponse({"error": _lives_there(link_id)}, status_code=409)
