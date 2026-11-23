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

* ``POST /api/instances/{title}/hub/fetch`` — ``{id}`` (``file@mac-mini:
  k7m2``, or a ``file:`` code only another device holds): bring an item of
  another of the person's devices here (logged with the session); never a
  secret-looking one.

Shared files on the person's OTHER devices (core.hub_remote — on demand,
never a sync; every request to another device rides ``/api/devices/<device>/
fwd/…``):

* ``GET /api/hub/devices?fresh=`` — what each of the person's other devices
  offers right now, or why it can't be asked.
* ``POST /api/hub/devices/fetch`` — ``{device?, id, secret_ok?}``: bring one
  here (a normal item that remembers where it came from; found, not fetched
  again, on a second use).
* ``GET /api/hub/devices/{device}/items/{id}/thumb`` — a small picture's
  thumbnail (checked here: its type sniffed, its checksum the listed one).

What THIS device offers them — answered only to another of the person's own
devices over the device link (the remote marker AND this device's token or
the fleet key, remote control on, never a guest; ``server.
_device_link_refusal``), only while ``hub.offer_to_devices`` is on, and never
an item kept on this device only or one fetched from elsewhere. GET only,
this store only (no path parameter reaches the disk); these three are the
forwarding path's hub allow-list (core.remote):

* ``GET /api/hub/offer`` — the list.
* ``GET /api/hub/offer/{id}/raw?offset=&length=&sha256=`` — a range of one
  item's bytes (at most core.hub_remote.CHUNK).
* ``GET /api/hub/offer/{id}/thumb`` — a picture small enough to be its own
  thumbnail.

Nothing the hub writes into a worktree is ever a file agents follow or that
runs by itself (CLAUDE.md, AGENTS.md, .mcp.json, git hooks, CI, Makefile,
package.json, conftest.py … — core.hub._dest_refusal), for anyone.

Never for a guest link (the auth middleware refuses every ``/api/hub`` path
and the ``hub`` session sub-route to a guest; checked here again), a request
relayed by another MindFlock (except the "offered" three, which answer ONLY
those), or a shared-folder (peer) session. Every refusal is ``{"error",
"code"}`` with the next step.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.web.core import hub as _hub
from backend.web.core import hub_remote as _hub_remote
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
    bad = _hub.secret_refusal(name)
    if bad is not None and not secret_ok:
        return JSONResponse({**bad.body(), "can_confirm": True}, status_code=bad.status)
    try:
        rec = await _hub.add_stream_async(
            request.stream(),
            name or "file",
            declared=declared,
            by=_SOURCES[source],
            note=note,
            secret=bad is not None,
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

    why = await _hub_remote.prepare_codes(request.scope, text)

    def run() -> dict:
        out, _used, unknown = _hub.expand(text)
        return {
            "text": out,
            "unknown": unknown,
            "note": _hub.unknown_note(unknown, why),
        }

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
    pinned, local_only = body.get("pinned"), body.get("local_only")
    if pinned is not None and not isinstance(pinned, bool):
        return _bad("pinned is true or false")
    if local_only is not None and not isinstance(local_only, bool):
        return _bad("local_only is true or false")
    return await _run(
        _hub.update,
        iid,
        name=body.get("name"),
        note=body.get("note"),
        pinned=pinned,
        local_only=local_only,
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
    actions = _hub.BATCH_ACTIONS + ("fetch",)
    if action not in actions:
        return _bad("action is one of " + ", ".join(actions))
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
    remote = _remote_ids(ids)
    if not remote and action != "fetch":
        return await _run(_hub.batch, action, ids, wt=wt)
    return await _mixed_batch(request, action, ids, remote, wt)


def _remote_ids(ids) -> dict:
    """``{code as given: (device, id)}`` for the codes that name ANOTHER
    device's item (``file@mac-mini:k7m2``)."""
    me = _hub.self_device()
    out = {}
    for raw in ids:
        try:
            dev, iid = _hub.split_code(raw)
        except Refusal:
            continue
        if dev and dev != me:
            out[raw] = (dev, iid)
    return out


async def _mixed_batch(request: Request, action: str, ids, remote: dict, wt):
    """A selection holding items of other devices: ``fetch`` / ``copy`` bring
    each here first (one at a time, each answered on its own); pin, unpin,
    remove and restore are this device's — an item of another device is
    read-only from here and says so. The local rest goes through
    :func:`hub.batch` as before; the answer keeps the order asked."""
    results: dict = {}
    local: list = []
    for raw in ids:
        key = str(raw)[:80]
        if key in results:
            continue
        if raw in remote:
            dev, iid = remote[raw]
            ref = _hub.device_code(dev, iid)
            if action not in ("fetch", "copy"):
                results[key] = {
                    "id": key,
                    "ref": ref,
                    "ok": False,
                    "code": "read_only",
                    "error": "%s lives on another of your devices — from here it is "
                    "read-only; save a copy here first to %s it" % (ref, action),
                }
                continue
            try:
                got = await _hub_remote.fetch(request.scope, dev, iid)
            except Refusal as r:
                results[key] = {"id": key, "ref": ref, "ok": False, **r.body()}
                continue
            if action == "fetch":
                results[key] = {
                    "id": key,
                    "ref": ref,
                    "ok": True,
                    "item": got,
                    "already": got.get("already", False),
                }
                continue
            results[key] = {"id": key, "ref": ref, "local": got["id"]}
            local.append(got["id"])
        elif action == "fetch":
            try:
                rec = await asyncio.to_thread(_hub.get, raw)
                results[key] = {
                    "id": key,
                    "ref": rec["ref"],
                    "ok": True,
                    "item": rec,
                    "already": True,
                }
            except Refusal as r:
                results[key] = {"id": key, "ok": False, **r.body()}
        else:
            results[key] = {"id": key, "local": None}
            local.append(raw)
    if local and action != "fetch":
        try:
            done = await asyncio.to_thread(_hub.batch, action, local, wt=wt)
        except Refusal as r:
            return JSONResponse(r.body(), status_code=r.status)
        by_id = {r["id"]: r for r in done["results"]}
        for key, res in results.items():
            if "ok" in res:
                continue
            want = res.get("local") or key
            try:
                want = _hub.check_id(want)
            except Refusal:
                pass
            hit = by_id.get(want) or by_id.get(str(want)[:40])
            res.pop("local", None)
            if hit is None:
                res.update(ok=False, code="not_found", error="%s was not done" % key)
            else:
                res.update({k: v for k, v in hit.items() if k not in ("id",)})
    out = [results[str(k)[:80]] for k in dict.fromkeys(str(i)[:80] for i in ids)]
    for res in out:
        res.pop("local", None)
    done_n = sum(1 for r in out if r.get("ok"))
    return JSONResponse(
        {"action": action, "results": out, "done": done_n, "failed": len(out) - done_n}
    )


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


@router.post("/api/instances/{title}/hub/fetch")
async def hub_fetch_own(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """An agent brings an item of another of the person's devices HERE (into
    this device's shared files; then ``take`` copies it into its folder).
    ``{id}`` — ``file@mac-mini:k7m2``, or ``file:k7m2`` when exactly one
    reachable device holds it. Logged with the session; never a
    secret-looking item; nothing on the other device changes."""
    body = payload if isinstance(payload, dict) else {}
    _inst, _wt, err = _session(title, request, "shared files", body)
    if err is not None:
        return err
    raw = body.get("id")
    if not isinstance(raw, str):
        return _bad("id is the item (file@mac-mini:k7m2)")
    try:
        dev, iid = await _hub_remote.locate(request.scope, raw)
        rec = await _hub_remote.fetch(request.scope, dev, iid, session=title)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return JSONResponse(rec)


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


# --------------------------------------------------------------------------- #
# the owner's view of their OTHER devices' shared files (core.hub_remote)
# --------------------------------------------------------------------------- #
@router.get("/api/hub/devices")
async def hub_devices(request: Request, fresh: int = 0) -> JSONResponse:
    """``{self, devices: [{device, name, state, note, items: [...], dropped,
    truncated}]}`` — each of the person's other devices and what it offers
    right now (``state`` ``ok``, or why it can't be asked: ``offline`` /
    ``old`` / ``off`` / ``private`` / ``not_linked`` / ``refused`` /
    ``error``, with ``note`` the sentence to show). Items carry ``ref`` (the
    code to type), ``copy`` (the local copy's id once fetched), ``secret``
    and ``too_big``."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    try:
        return JSONResponse(
            await _hub_remote.list_all(request.scope, fresh=bool(fresh))
        )
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)


@router.post("/api/hub/devices/fetch")
async def hub_devices_fetch(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """``{id, device?, secret_ok?}`` → the local item (``already`` when it
    was here already, ``from`` the device's name). ``id`` may carry the
    device (``file@mac-mini:k7m2``). A secret-looking item needs
    ``secret_ok`` — the person's confirmation for that one item."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    body = payload if isinstance(payload, dict) else {}
    raw, device, secret_ok = body.get("id"), body.get("device"), body.get("secret_ok")
    if not isinstance(raw, str) or (device is not None and not isinstance(device, str)):
        return _bad("id is the item (file@mac-mini:k7m2), device the device's name")
    if secret_ok is not None and not isinstance(secret_ok, bool):
        return _bad("secret_ok is true or false")
    try:
        if device:
            dev, iid = device, _hub.split_code(raw)[1]
        else:
            dev, iid = await _hub_remote.locate(request.scope, raw)
        rec = await _hub_remote.fetch(
            request.scope, dev, iid, secret_ok=bool(secret_ok)
        )
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return JSONResponse(rec)


_IMG_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, no-cache",
}


@router.get("/api/hub/devices/{device}/items/{iid}/thumb")
async def hub_devices_thumb(device: str, iid: str, request: Request) -> Response:
    """A picture of another device, small enough to be its own thumbnail —
    its type sniffed HERE from the bytes (an allow-listed image type only),
    its bytes checked against the listed checksum. 404 ``no_thumb``
    otherwise (the drawer draws an icon)."""
    refused = _refusal(request)
    if refused is not None:
        return refused
    try:
        mime, data = await _hub_remote.thumb(request.scope, device, iid)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return Response(
        data,
        media_type=mime,
        headers={**_IMG_HEADERS, "Content-Disposition": "inline"},
    )


# --------------------------------------------------------------------------- #
# what THIS device offers the person's other devices (the forwarding path's
# hub allow-list: these three GETs, nothing else)
# --------------------------------------------------------------------------- #
def _offer_refusal(request: Request) -> Optional[JSONResponse]:
    """Only another of the person's own devices, over the device link
    (server._device_link_refusal: the remote marker AND this device's token
    or the fleet key, remote control on, never a guest, one hop, the same
    person), and only while this device offers its shared files."""
    refused = _server()._device_link_refusal(
        request,
        guest="Shared files are not part of a guest invite",
        not_device="this answers only your other devices — open Shared files "
        "in the app to see this device's",
        remote_off="Allow remote control is off on this device — turn it on in "
        "Settings → Your devices → Advanced so your other devices see its "
        "shared files",
        same_person=True,
    )
    if refused is not None:
        return refused
    from backend.config import settings as _settings

    try:
        on = _settings.load_settings().hub.offer_to_devices is not False
    except Exception:  # noqa: BLE001 — unreadable settings: offer nothing
        on = False
    if not on:
        return JSONResponse(
            {
                "error": "this device keeps its shared files to itself (Settings → "
                "Shared files → Offer to my other devices)",
                "code": "not_offered",
            },
            status_code=403,
        )
    return None


@router.get("/api/hub/offer")
async def hub_offer(request: Request) -> JSONResponse:
    """``{v, device, items: [{id, name, kind, mime, size, sha256, by,
    by_name, added, note, textual, preview}]}`` — never a path on this
    disk."""
    refused = _offer_refusal(request)
    if refused is not None:
        return refused

    def run() -> dict:
        items = _hub.offered()[: _hub_remote.LIST_ITEMS_MAX]
        return {"v": _hub_remote.OFFER_V, "device": _hub.self_device(), "items": items}

    return await _run(run)


@router.get("/api/hub/offer/{iid}/raw")
async def hub_offer_raw(
    iid: str,
    request: Request,
    offset: int = 0,
    length: int = 0,
    sha256: str = "",
) -> Response:
    """``length`` bytes (1 to core.hub_remote.CHUNK) of an offered item from
    ``offset``; with ``sha256`` only while the item still has those bytes
    (409 ``changed``). Always ``application/octet-stream``, an attachment,
    ``nosniff``."""
    refused = _offer_refusal(request)
    if refused is not None:
        return refused
    if offset < 0 or not 0 < length <= _hub_remote.CHUNK:
        return _bad("offset is 0 or more, length 1 to %d" % _hub_remote.CHUNK)

    def read() -> bytes:
        rec, fd = _hub.open_offered(iid)
        try:
            if sha256 and sha256 != rec["sha256"]:
                raise Refusal(
                    409, "changed", "file:%s is not the item it was" % rec["id"]
                )
            size = os.fstat(fd).st_size
            if size != rec["size"]:
                raise Refusal(
                    409, "changed", "file:%s is not the item it was" % rec["id"]
                )
            if offset + length > size:
                raise Refusal(416, "bad_range", "that range is past the item's end")
            data = os.pread(fd, length, offset) if hasattr(os, "pread") else None
            if data is None:
                os.lseek(fd, offset, os.SEEK_SET)
                data = os.read(fd, length)
        finally:
            os.close(fd)
        if offset == 0:
            _hub.touch([rec["id"]])
            _hub_remote._info(
                "shared files: file:%s offered to device %s",
                rec["id"],
                clean_device(request),
            )
        return data

    try:
        data = await asyncio.to_thread(read)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "attachment",
            "Cache-Control": "no-store",
        },
    )


def clean_device(request: Request) -> str:
    """Who asked (the device the link says, else what the marker claims)."""
    from backend.web.core.send_session import clean_line

    sender, verified = _server()._transfer_sender(request)
    return clean_line(sender or "?", 64) + ("" if verified else " (claimed)")


@router.get("/api/hub/offer/{iid}/thumb")
async def hub_offer_thumb(iid: str, request: Request) -> Response:
    """An offered picture at most core.hub_remote.THUMB_MAX — the picture
    itself (no image library makes a smaller one); 404 ``no_thumb`` for any
    other item."""
    refused = _offer_refusal(request)
    if refused is not None:
        return refused

    def read():
        rec, fd = _hub.open_offered(iid)
        try:
            if (
                rec.get("kind") != "image"
                or rec.get("mime") not in _hub.IMAGE_TYPES.values()
                or rec["size"] > _hub_remote.THUMB_MAX
            ):
                raise Refusal(404, "no_thumb", "no thumbnail for that item")
            data = os.read(fd, _hub_remote.THUMB_MAX + 1)
        finally:
            os.close(fd)
        return rec, data

    try:
        rec, data = await asyncio.to_thread(read)
    except Refusal as r:
        return JSONResponse(r.body(), status_code=r.status)
    return Response(
        data[: _hub_remote.THUMB_MAX],
        media_type=rec["mime"],
        headers={**_IMG_HEADERS, "Content-Disposition": "inline"},
    )
