"""Guest links (docs/people.md): the ``/api/guests*`` routes.

A link lets someone else type into a few of this device's sessions
(:mod:`backend.web.core.guests`), opened at a public address — a Cloudflare
quick tunnel to this server (:mod:`backend.web.core.guest_tunnel`) — because
the guest is on some other network. The routes only translate: the owner
lists, makes, edits and revokes links and asks for the tunnel; the gate
itself lives in ``core.auth``. Every route is the owner's — never a guest's
(the middleware refuses them here), never another MindFlock's relay, and a
change needs ``auth.may_configure``. The token appears only inside the URLs
this hands the owner to copy; the list never carries it on its own.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import auth as _auth
from backend.web.core import guest_tunnel as _tunnel
from backend.web.core import guests as _guests
from backend.web.core import remote as _remote

router = APIRouter()

GATE_OFF = (
    "Turn the access gate on first (Settings → Security) — a guest link only "
    "limits anything while every visitor needs a token."
)


async def _refusal(request: Request) -> Optional[JSONResponse]:
    if _remote.from_remote(request):
        return JSONResponse(
            {"error": "guest links can only be managed on this device"},
            status_code=403,
        )
    if request.method != "GET" and not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    return None


def _local_base(request: Request) -> str:
    """This device's own address — the tailnet/shared phone URL it advertises
    (its root, not ``/m``), else the origin this request came in on. Only for
    someone already on the owner's network."""
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


def _with_urls(request: Request, row: dict, token: str) -> dict:
    from urllib.parse import quote

    q = "?token=" + quote(token, safe="")
    public = _tunnel.url()
    return {
        **row,
        "url": public + q if public else None,
        "phone_url": public + "m" + q if public else None,
        "local_url": _local_base(request) + q,
    }


def _tunnel_status() -> dict:
    """The tunnel, kept up while links exist and the gate is on."""
    if _auth.auth_enabled() and _guests.any_links():
        return _tunnel.ensure()
    return _tunnel.status()


@router.get("/api/guests")
async def list_guests(request: Request) -> JSONResponse:
    """Every link with its URLs (the owner's Copy button), the tunnel's state,
    and whether links can work at all right now (``gate_on``)."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    tunnel = _tunnel_status()
    rows = [
        _with_urls(request, r, _guests.token_of(r["id"])) for r in _guests.list_links()
    ]
    return JSONResponse(
        {"links": rows, "gate_on": _auth.auth_enabled(), "tunnel": tunnel}
    )


@router.post("/api/guests")
async def create_guest(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """``{label?, sessions: [title, …]}`` → **201** the link with its URLs and
    the tunnel's state (``url`` is null until the tunnel is up — the screen
    polls GET). 409 while the access gate is off: without it the link is just
    the address everyone can already open, and the tunnel would put this
    whole machine on the internet."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    if not _auth.auth_enabled():
        return JSONResponse({"error": GATE_OFF}, status_code=409)
    body = payload or {}
    try:
        rec = _guests.create(str(body.get("label") or ""), body.get("sessions") or [])
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    token = rec.pop("token")
    tunnel = _tunnel.ensure()
    return JSONResponse(
        {**_with_urls(request, rec, token), "tunnel": tunnel}, status_code=201
    )


@router.post("/api/instances/{title}/focus")
async def guest_focus(request: Request, title: str) -> JSONResponse:
    """A GUEST's page says which session it has selected (on every change,
    and every 30 s while it stays open) — the one call here that is the
    guest's, not the owner's; the gate has already checked ``title`` is on
    their link. The owner's rail shows a pill with their name on that row
    (``guest.focus_changed``). From the owner's own browser it is a no-op:
    where the owner looks is nobody's business."""
    guest = _guests.scope_guest(request)
    if guest is None:
        return JSONResponse({"ok": True, "guest": False})
    return JSONResponse(
        {"ok": True, "guest": True, "focus": _guests.note_focus(guest, title)}
    )


@router.post("/api/guest/name")
async def guest_name(request: Request, payload: Optional[dict] = None) -> JSONResponse:
    """The GUEST introduces themselves: ``{name}`` → ``{name}``. Required
    before their pill shows on the owner's rail (the page asks on open), and
    unique among the people on it — another guest's name or the owner's own
    is **409** ``{error, taken: true}``; empty is 400. The owner's browser
    has no link to name: 403."""
    guest = _guests.scope_guest(request)
    if guest is None:
        return JSONResponse({"error": "not a guest link"}, status_code=403)
    name = str((payload or {}).get("name") or "")
    try:
        stored = _guests.set_name(str(guest.get("id") or ""), name)
    except ValueError as err:
        taken = "already goes by" in str(err)
        return JSONResponse(
            {"error": str(err), "taken": taken}, status_code=409 if taken else 400
        )
    return JSONResponse({"name": stored})


@router.post("/api/guests/tunnel")
async def start_tunnel(request: Request) -> JSONResponse:
    """Ask for the public address (start the tunnel, or say what is missing:
    ``installed: false`` → the doctor's install plan now offers cloudflared).
    409 with the gate off."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    if not _auth.auth_enabled():
        return JSONResponse({"error": GATE_OFF}, status_code=409)
    _tunnel.want()
    return JSONResponse(_tunnel.ensure())


@router.delete("/api/guests/tunnel")
async def kill_guests(request: Request) -> JSONResponse:
    """The kill switch: every link revoked (their open terminals close on
    the next frame), the public address taken down. → ``{ok, revoked}``."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    n = _guests.kill_all()
    await _tunnel.stop()
    return JSONResponse({"ok": True, "revoked": n})


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
    return JSONResponse(_with_urls(request, row, _guests.token_of(link_id)))


@router.delete("/api/guests/{link_id}")
async def revoke_guest(request: Request, link_id: str) -> JSONResponse:
    """Revoke: the link's browser is signed out on its next request. The
    last link takes the tunnel down with it."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    if not _guests.revoke(link_id):
        return JSONResponse({"error": "no such guest link"}, status_code=404)
    await _tunnel.stop_if_unused()
    return JSONResponse({"ok": True})
