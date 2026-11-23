"""Shared files' routes (internally "the hub": :mod:`backend.web.core.hub`;
docs/web-api.md "Shared files"). The paths keep ``hub`` — clients of every
version and agents' tools call them.

The owner's shared files, from the app, the phone page and the CLI:

* ``GET /api/hub`` — the items, newest first, with the quota.
* ``POST /api/hub/items?name=&note=&source=&secret_ok=`` — add a file: the
  raw bytes are the body (``Content-Length`` required). 201 the item.
* ``POST /api/hub/text`` — add a text snippet ``{text, name?, note?,
  source?}``. 201 the item.
* ``GET /api/hub/items/{id}`` — one item; ``…/raw`` its bytes (an
  allow-listed image inline, everything else as a download); ``…/text`` the
  text of a snippet or text file.
* ``PATCH /api/hub/items/{id}`` — ``{name?, note?, pinned?}``.
* ``DELETE /api/hub/items/{id}`` — remove (Undo: ``POST …/restore``).
* ``POST /api/hub/clear`` — Disk cleanup: remove every unpinned item.
* ``POST /api/hub/batch`` — ``{action, ids, session?}``: pin / unpin /
  remove / restore / copy several items in one request, answered per item.
* ``POST /api/hub/expand`` — ``{text}`` → the text with its references
  expanded (what the app pastes into an agent's terminal).

* ``POST /api/hub/items/{id}/copy-to`` — ``{session, path?, replace?}``: the
  owner's copy into a session's folder (``GET …/free?session=&path=`` says
  where it would land); ``POST /api/hub/rebuild`` — a new list from the
  folders on disk when hub.json can't be read.

A session's own agent (the MCP ``hub`` tool) — every call carries that
session's key (``backend.session_key``), so an agent can't act for another:

* ``POST /api/instances/{title}/hub/put`` — ``{path}`` (one file inside that
  session's worktree) or ``{text}``; ``name?``, ``note?``.
* ``POST /api/instances/{title}/hub/take`` — ``{id, path?}``: copy an item
  INTO that session's folder, only under ``hub/``, never over a file.
* ``POST /api/instances/{title}/hub/remove`` — ``{id}``: remove an item that
  session added (only those).

Nothing the hub writes into a worktree is ever a file agents follow or that
runs by itself (CLAUDE.md, AGENTS.md, .mcp.json, git hooks, CI, Makefile,
package.json, conftest.py … — core.hub._dest_refusal), for anyone.

Never for a guest link (the auth middleware refuses every ``/api/hub`` path
and the ``hub`` session sub-route to a guest; checked here again), a request
relayed by another MindFlock, or a shared-folder (peer) session. Every
refusal is ``{"error", "code"}`` with the next step.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.web.core import hub as _hub
from backend.web.core.send_session import Refusal
from backend.web.routes import _server

router = APIRouter()

#: The upload sources a client may claim (who added it): the app, the phone.
_SOURCES = {"you": "you", "phone": "phone", "": "you"}


def _refusal(request: Optional[Request]) -> Optional[JSONResponse]:
    """A guest link or a request another MindFlock relayed gets nothing."""
    if request is None:
        return None
    from backend.web.core import guests as _guests
    from backend.web.core import remote as _remote

    if _guests.scope_guest(request) is not None:
        return JSONResponse(
            {"error": "Shared files are not part of a guest invite", "code": "guest"},
            status_code=403,
        )
    if _remote.from_remote(request):
        return JSONResponse(
            {
                "error": "Shared files are used on the device they live on — "
                "open MindFlock there (each device has its own)",
                "code": "remote",
            },
            status_code=403,
        )
    return None


def _answer(fn, *args, status: int = 200, **kw) -> JSONResponse:
    try:
        return JSONResponse(fn(*args, **kw), status_code=status)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)


async def _run(fn, *args, status: int = 200, **kw) -> JSONResponse:
    return await asyncio.to_thread(_answer, fn, *args, status=status, **kw)


def _bad(msg: str, code: str = "bad_request") -> JSONResponse:
    return JSONResponse({"error": msg, "code": code}, status_code=400)


# --------------------------------------------------------------------------- #
# the owner's hub
# --------------------------------------------------------------------------- #
@router.get("/api/hub")
async def hub_list(request: Request) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.list_items)


@router.post("/api/hub/items")
async def hub_add_file(
    request: Request,
    name: str = "",
    note: str = "",
    source: str = "",
    secret_ok: int = 0,
) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    if source not in _SOURCES:
        return _bad("source is you or phone")
    raw_len = request.headers.get("content-length")
    try:
        declared = int(raw_len) if raw_len is not None else None
    except ValueError:
        declared = None
    if declared is None:
        return JSONResponse(
            {
                "error": "the upload needs its size (Content-Length) — add the "
                "file again",
                "code": "length_required",
            },
            status_code=411,
        )
    if declared == 0:
        return _bad("that file is empty — there is nothing to add", "empty")
    if not secret_ok:
        bad = _hub.secret_refusal(name)
        if bad is not None:
            return JSONResponse(
                {**bad.body(), "can_confirm": True}, status_code=bad.status
            )
    try:
        rec = await _hub.add_stream_async(
            request.stream(),
            name or "file",
            declared=declared,
            by=_SOURCES[source],
            note=note,
        )
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return JSONResponse(rec, status_code=201)


@router.post("/api/hub/text")
async def hub_add_text(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    text, name = body.get("text"), body.get("name", "")
    note, source = body.get("note", ""), body.get("source", "")
    if not isinstance(name, str) or not isinstance(note, str) or source not in _SOURCES:
        return _bad("name and note are text, source is you or phone")
    if not isinstance(text, str):
        return _bad("text is the snippet to add")
    return await _run(
        _hub.add_text, text, name=name, note=note, by=_SOURCES[source], status=201
    )


@router.post("/api/hub/expand")
async def hub_expand(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """``{text}`` → ``{text, unknown}``: the text with its references
    expanded as a send would — what the app pastes into an agent's terminal
    when the pasted text holds a code (``file:`` or the first ``hub:``)."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    text = body.get("text")
    if not isinstance(text, str) or len(text) > 200000:
        return _bad("text is the text to expand")

    def run() -> dict:
        out, _used, unknown = _hub.expand(text)
        return {"text": out, "unknown": unknown, "note": _hub.unknown_note(unknown)}

    return await _run(run)


@router.get("/api/hub/items/{iid}")
async def hub_get(iid: str, request: Request) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.get, iid)


@router.get("/api/hub/items/{iid}/text")
async def hub_text(iid: str, request: Request) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.read_text, iid)


def _disposition(kind: str, name: str) -> str:
    ascii_name = "".join(
        c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name
    )
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
        kind,
        ascii_name or "item",
        urllib.parse.quote(name, safe=""),
    )


@router.get("/api/hub/items/{iid}/raw")
async def hub_raw(iid: str, request: Request, download: int = 0) -> Response:
    """The item's bytes. Only an allow-listed image type (sniffed from its
    bytes when it was added) is served with its own content type, inline;
    everything else — SVG, HTML, scripts, PDFs, text — is
    ``application/octet-stream`` as an attachment. Always ``nosniff`` and a
    sandboxing CSP, so nothing from the hub can run in the app's origin."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    try:
        rec, fd = await asyncio.to_thread(_hub.open_item, iid)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    etag = '"%s"' % rec["sha256"]
    if request.headers.get("if-none-match") == etag:
        # Revalidated every time (no-cache): a removed item is 410 above,
        # never a day-old copy from the browser's cache.
        os.close(fd)
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    inline = (
        bool(rec.get("mime"))
        and rec["mime"] in _hub.IMAGE_TYPES.values()
        and not download
    )
    headers = {
        "Content-Disposition": _disposition(
            "inline" if inline else "attachment", rec["name"]
        ),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, no-cache",
        "ETag": etag,
        "Content-Length": str(os.fstat(fd).st_size),
    }

    def chunks():
        with os.fdopen(fd, "rb") as f:
            while True:
                b = f.read(256 * 1024)
                if not b:
                    return
                yield b

    return StreamingResponse(
        chunks(),
        media_type=rec["mime"] if inline else "application/octet-stream",
        headers=headers,
    )


@router.patch("/api/hub/items/{iid}")
async def hub_update(
    iid: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    pinned = body.get("pinned")
    if pinned is not None and not isinstance(pinned, bool):
        return _bad("pinned is true or false")
    return await _run(
        _hub.update, iid, name=body.get("name"), note=body.get("note"), pinned=pinned
    )


@router.delete("/api/hub/items/{iid}")
async def hub_remove(iid: str, request: Request) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.remove, iid)


@router.post("/api/hub/items/{iid}/restore")
async def hub_restore(iid: str, request: Request) -> JSONResponse:
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.restore, iid)


@router.post("/api/hub/clear")
async def hub_clear(request: Request) -> JSONResponse:
    """Disk cleanup's hub line: remove every unpinned item now."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.clear_unpinned)


@router.post("/api/hub/rebuild")
async def hub_rebuild(request: Request) -> JSONResponse:
    """A list (hub.json) that can't be read: make a new one from the item
    folders on disk (core.hub.rebuild). Nothing is deleted."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    return await _run(_hub.rebuild)


# --------------------------------------------------------------------------- #
# the owner's copy into a session (the app's Send to session…, the CLI)
# --------------------------------------------------------------------------- #
def _target(title: str, what: str):
    """``(worktree, None)`` or ``(None, error response)`` for a session the
    owner copies INTO (no key: the owner names any session of theirs)."""
    srv = _server()
    inst, err = srv._inst_or_404(title)
    if err is not None:
        return None, srv._closed_send_refusal(title) or err
    from backend.web.core import peer_guard as _peer_guard

    peer_err = _peer_guard.refusal(inst, what)
    if peer_err is not None:
        return None, peer_err
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return None, JSONResponse(
            {
                "error": "%s has no folder on disk yet (it may still be starting) "
                "— try again once it is running" % title,
                "code": "no_workspace",
            },
            status_code=409,
        )
    return wt, None


@router.post("/api/hub/items/{iid}/copy-to")
async def hub_copy_to(
    iid: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """``{session, path?, replace?}``: save a copy of an item in that
    session's folder (default ``hub/<name>``) — the app's Send to session…
    and ``mindflock hub cp --to``. Any path inside the folder except what the
    hub never writes (core.hub._dest_refusal); ``replace`` only on purpose."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    title, dest, replace = (
        body.get("session"),
        body.get("path"),
        body.get("replace", False),
    )
    if (
        not isinstance(title, str)
        or not title
        or (dest is not None and not isinstance(dest, str))
    ):
        return _bad("session is the session's name, path where in its folder")
    if not isinstance(replace, bool):
        return _bad("replace is true or false")
    wt, err = _target(title, "copying a shared file in")
    if err is not None:
        return err
    return await _run(_hub.take, iid, wt, dest, replace=replace)


@router.post("/api/hub/batch")
async def hub_batch(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """``{action, ids, session?}``: several items in ONE request — the app's
    selection bar (Pin, Unpin, Remove and its Undo, Save copies in a
    session's folder). ``action`` is pin | unpin | remove | restore | copy
    (``copy`` needs ``session``: each item lands at ``hub/<name>`` there, or
    the next free path, never over a file). Each item is checked as its
    one-item route checks it and answered on its own: 200 ``{action,
    results: [{id, ref, ok, …} | {id, ok: false, error, code}], done,
    failed}`` — a partial batch says which items, and why."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    action, ids, title = body.get("action"), body.get("ids"), body.get("session")
    if action not in _hub.BATCH_ACTIONS:
        return _bad("action is one of " + ", ".join(_hub.BATCH_ACTIONS))
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > _hub.BATCH_MAX
        or not all(isinstance(i, str) for i in ids)
    ):
        return _bad("ids is a list of 1 to %d codes (file:k7m2)" % _hub.BATCH_MAX)
    wt = None
    if action == "copy":
        if not isinstance(title, str) or not title:
            return _bad("session is the session to copy into")
        wt, err = _target(title, "copying shared files in")
        if err is not None:
            return err
    elif title is not None:
        return _bad("session is only for action=copy")
    return await _run(_hub.batch, action, ids, wt=wt)


@router.get("/api/hub/items/{iid}/free")
async def hub_free(
    iid: str, request: Request, session: str = "", path: str = ""
) -> JSONResponse:
    """Where a copy into ``session``'s folder would land without replacing
    anything: ``{path, wanted, taken, root}`` (``path`` default
    ``hub/<name>``); a path the hub never writes is the refusal."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    wt, err = _target(session, "copying a shared file in")
    if err is not None:
        return err

    def free() -> dict:
        rec = _hub.get(iid)
        want = path or _hub.default_dest(rec)
        got = _hub.free_dest(wt, want)
        return {"path": got, "wanted": want, "taken": got != want, "root": wt}

    return await _run(free)


# --------------------------------------------------------------------------- #
# a session's own agent (the MCP hub tool) — bound to that session
# --------------------------------------------------------------------------- #
def _session(title: str, request: Request, what: str, body: dict):
    """``(inst, worktree, None)`` or ``(None, None, error response)``. The
    request must carry THIS session's key (``backend.session_key``): the one
    its own MindFlock MCP was handed at launch — so an agent can't act for
    another session by writing that session's name in the URL."""
    refused = _refusal(request)
    if refused is not None:
        return None, None, refused
    srv = _server()
    inst, err = srv._inst_or_404(title)
    if err is not None:
        return None, None, srv._closed_send_refusal(title) or err
    from backend.web.core import peer_guard as _peer_guard

    peer_err = _peer_guard.refusal(inst, what)
    if peer_err is not None:
        return None, None, peer_err
    from backend import session_key as _session_key

    if not _session_key.verify(title, body.get("key")):
        return (
            None,
            None,
            JSONResponse(
                {
                    "error": "this answers only %s's own MindFlock tool (shared "
                    "files, from inside that session). A session started before this MindFlock "
                    "version gets its key when it is restarted" % title,
                    "code": "not_this_session",
                },
                status_code=403,
            ),
        )
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return (
            None,
            None,
            JSONResponse(
                {
                    "error": "%s has no folder on disk yet (it may still be starting) "
                    "— try again once it is running" % title,
                    "code": "no_workspace",
                },
                status_code=409,
            ),
        )
    return inst, wt, None


@router.post("/api/instances/{title}/hub/put")
async def hub_put(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    body = payload if isinstance(payload, dict) else {}
    _inst, wt, err = _session(title, request, "shared files", body)
    if err is not None:
        return err
    path, text = body.get("path"), body.get("text")
    name, note = body.get("name", "") or "", body.get("note", "") or ""
    if not isinstance(name, str) or not isinstance(note, str):
        return _bad("name and note are text")
    if (path is None) == (text is None):
        return _bad("give path (one file in your worktree) or text — one of them")
    if text is not None:
        if not isinstance(text, str):
            return _bad("text is the snippet to add")
        return await _run(
            _hub.add_text,
            text,
            name=name,
            note=note,
            by="session",
            by_name=title,
            status=201,
        )
    if not isinstance(path, str):
        return _bad("path names one file in your worktree")
    from backend.web.core.red_zone_driver import zone_hits_for_paths

    return await _run(
        _hub.add_from_worktree,
        wt,
        path,
        session=title,
        note=note,
        name=name,
        zone_hits=zone_hits_for_paths,
        status=201,
    )


@router.post("/api/instances/{title}/hub/take")
async def hub_take(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """An agent's copy into ITS OWN folder: only under ``hub/``, never over a
    file (no ``replace`` here — that is the owner's, on /copy-to), never in a
    red zone or outside its fence."""
    body = payload if isinstance(payload, dict) else {}
    _inst, wt, err = _session(title, request, "copying a shared file in", body)
    if err is not None:
        return err
    iid, dest = body.get("id"), body.get("path")
    if not isinstance(iid, str) or (dest is not None and not isinstance(dest, str)):
        return _bad("id is the item (file:k7m2), path where under hub/ in your folder")
    from backend.web.core.red_zone_driver import zone_hits_for_paths

    return await _run(
        _hub.take, iid, wt, dest, zone_hits=zone_hits_for_paths, agent=True
    )


@router.post("/api/instances/{title}/hub/remove")
async def hub_remove_own(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Remove an item THIS session added (only those)."""
    body = payload if isinstance(payload, dict) else {}
    _inst, _wt, err = _session(title, request, "shared files", body)
    if err is not None:
        return err
    iid = body.get("id")
    if not isinstance(iid, str):
        return _bad("id is the item (file:k7m2)")
    return await _run(_hub.remove, iid, session=title)
