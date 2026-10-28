"""Guest links (docs/people.md): the ``/api/guests*`` routes.

A link lets someone else type into a few of this device's sessions
(:mod:`backend.web.core.guests`). The routes only translate: the owner lists,
makes, edits and revokes links; the gate itself lives in ``core.auth``. Every
route is the owner's — never a guest's (the middleware refuses them here),
never another MindFlock's relay, and a change needs ``auth.may_configure``
like any other setting. The token appears only inside the URL this hands the
owner to copy; the list never carries it on its own.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import auth as _auth
from backend.web.core import guests as _guests
from backend.web.core import remote as _remote

router = APIRouter()


async def _refusal(request: Request) -> Optional[JSONResponse]:
    if _remote.from_remote(request):
        return JSONResponse(
            {"error": "guest links can only be managed on this device"},
            status_code=403,
        )
    if request.method != "GET" and not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    return None


def _base_url(request: Request) -> str:
    """Where a guest opens the link: the tailnet/shared phone URL this device
    advertises (its root, not ``/m``), else the origin this request came in
    on. A plain ``http://`` LAN address is what it is — the owner sees the
    URL before sending it."""
    try:
        from backend.web.core import mobile_access as _mobile

        url, _live = _mobile.tailnet_url()
    except Exception:  # noqa: BLE001
        url = None
    if url:
        base = url[:-2] if url.endswith("/m") else url
    else:
        base = str(request.base_url)
    return base.rstrip("/") + "/"


def _with_url(request: Request, row: dict, token: str) -> dict:
    from urllib.parse import quote

    base = _base_url(request)
    q = "?token=" + quote(token, safe="")
    return {**row, "url": base + q, "phone_url": base + "m" + q}


@router.get("/api/guests")
async def list_guests(request: Request) -> JSONResponse:
    """Every link with its URL (the owner's Copy button). ``gate_on`` says
    whether links can work at all right now."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    rows = [
        _with_url(request, r, _guests.token_of(r["id"])) for r in _guests.list_links()
    ]
    return JSONResponse({"links": rows, "gate_on": _auth.auth_enabled()})


@router.post("/api/guests")
async def create_guest(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """``{label?, sessions: [title, …]}`` → **201** the link with its URL.
    409 while the access gate is off: without it the link is just the
    address everyone can already open, and it would promise a limit the
    server isn't enforcing."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    if not _auth.auth_enabled():
        return JSONResponse(
            {
                "error": "Turn the access gate on first (Settings → Security) — "
                "a guest link only limits anything while every visitor needs a token."
            },
            status_code=409,
        )
    body = payload or {}
    try:
        rec = _guests.create(str(body.get("label") or ""), body.get("sessions") or [])
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    token = rec.pop("token")
    return JSONResponse(_with_url(request, rec, token), status_code=201)


@router.post("/api/guests/{link_id}")
async def update_guest(
    request: Request, link_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{label?, sessions?}`` — change what the link opens; the URL stays."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    body = payload or {}
    try:
        row = _guests.update(
            link_id,
            label=str(body["label"]) if "label" in body else None,
            sessions=body.get("sessions") if "sessions" in body else None,
        )
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if row is None:
        return JSONResponse({"error": "no such guest link"}, status_code=404)
    return JSONResponse(_with_url(request, row, _guests.token_of(link_id)))


@router.delete("/api/guests/{link_id}")
async def revoke_guest(request: Request, link_id: str) -> JSONResponse:
    """Revoke: the link's browser is signed out on its next request."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    if not _guests.revoke(link_id):
        return JSONResponse({"error": "no such guest link"}, status_code=404)
    return JSONResponse({"ok": True})
