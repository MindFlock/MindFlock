"""Reach my devices from anywhere: the ``/api/devices/anywhere`` routes.

The switch on Settings → Your devices and its status line
(:mod:`backend.web.core.devices_anywhere`, ``docs/devices-anywhere.md``).
Reading the status is open to whoever passed the gate; changing it is this
device's own business — never another MindFlock relaying, and it needs
``auth.may_configure`` (it opens a tunnel and writes to the user's GitHub).

Adding a NEW device without Tailscale (``core.devices_pair``,
docs/devices-anywhere.md §7) lives here too: ``/invite`` makes, shows and
cancels the long code on the inviter; ``/join`` takes it on the new
computer. Both are the owner's own browser only (privileged, never relayed,
never through the tunnel): a code is the group's key in waiting.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import auth as _auth
from backend.web.core import devices_anywhere as _anywhere
from backend.web.core import remote as _remote

router = APIRouter()


async def _refusal(request: Request) -> Optional[JSONResponse]:
    if _remote.from_remote(request) or _auth.tunneled(request.scope):
        return JSONResponse(
            {"error": "this can only be changed on the device itself"},
            status_code=403,
        )
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    return None


async def _owner_only(request: Request) -> Optional[JSONResponse]:
    refused = await _refusal(request)
    if refused is not None:
        return refused
    try:
        ok = bool(await _auth.privileged(request.scope))
    except Exception:  # noqa: BLE001 — fail closed
        ok = False
    if not ok:
        return JSONResponse(
            {"error": "open this on the device itself"}, status_code=403
        )
    return None


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    return body if isinstance(body, dict) else {}


@router.get("/api/devices/anywhere")
async def anywhere_status() -> JSONResponse:
    """``{on, state, text, retry, install, installed, tunnel, github:{state,
    text}, published_at, found, via_tunnel}`` — ``text`` is the plain status
    line under the switch. No address, token or key is ever in it."""
    return JSONResponse(_anywhere.status())


@router.post("/api/devices/anywhere")
async def anywhere_set(request: Request) -> JSONResponse:
    """``{on: bool}`` → the status after applying it. Off stops the tunnel
    and deletes this device's record from GitHub."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        return JSONResponse({"error": 'send {"on": true|false}'}, status_code=400)
    try:
        _anywhere.save_switch(body["on"])
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": "couldn't save: %s" % err}, status_code=500)
    return JSONResponse(await _anywhere.apply())


@router.post("/api/devices/anywhere/retry")
async def anywhere_retry(request: Request) -> JSONResponse:
    """Try the tunnel and GitHub again now (the status line's Retry)."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    return JSONResponse(await _anywhere.retry())


@router.get("/api/devices/anywhere/invite")
async def anywhere_invite(request: Request) -> JSONResponse:
    """``{invite: {code, expires_at, host, qr_svg} | null, gone}`` — the live
    code for a new device not on this network (``gone``: why the last one
    stopped working)."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    return JSONResponse(_pair.invite_status())


@router.post("/api/devices/anywhere/invite")
async def anywhere_invite_new(request: Request) -> JSONResponse:
    """``{turn_on?: bool}`` → a fresh code (409 ``needs_switch`` while Reach
    my devices from anywhere is off and ``turn_on`` isn't set; 503 when the
    tunnel doesn't come up, with the status line)."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    body = await _body(request)
    try:
        inv = await _pair.create_invite(turn_on=body.get("turn_on") is True)
    except _pair.NeedsSwitch as err:
        return JSONResponse({"error": str(err), "needs_switch": True}, status_code=409)
    except _pair.NotUp as err:
        return JSONResponse(
            {"error": "Couldn't start the private tunnel — %s" % err}, status_code=503
        )
    except Exception as err:  # noqa: BLE001
        return JSONResponse(
            {"error": "couldn't make a code: %s" % err}, status_code=500
        )
    return JSONResponse({"invite": inv, "gone": ""})


@router.delete("/api/devices/anywhere/invite")
async def anywhere_invite_cancel(request: Request) -> JSONResponse:
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    _pair.cancel_invite()
    return JSONResponse({"invite": None, "gone": ""})


@router.post("/api/devices/anywhere/join")
async def anywhere_join(request: Request) -> JSONResponse:
    """``{code, reachable?: bool (default true)}`` on the NEW computer →
    the join status (it goes on in the background: ``GET /api/fleet``'s
    ``join``). 400 for a code that doesn't parse or while a join runs."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    body = await _body(request)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return JSONResponse(
            {"error": "paste the code from your other computer"}, status_code=400
        )
    try:
        st = await _pair.join(code[:4096], reachable=body.get("reachable") is not False)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(st)
