"""Reach my devices from anywhere: the ``/api/devices/anywhere`` routes.

The switch on Settings → Your devices and its status line
(:mod:`backend.web.core.devices_anywhere`, ``docs/devices-anywhere.md``).
Reading the status is open to whoever passed the gate; changing it is this
device's own business — never another MindFlock relaying, and it needs
``auth.may_configure`` (it opens a tunnel and writes to the user's GitHub).
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
    from backend.config import settings as settings_store
    from backend.web.core import settings_sync as _sync

    try:
        settings_store.update_settings(general={"devices_anywhere": body["on"]})
        _sync.local_change()
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
