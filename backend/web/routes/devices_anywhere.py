"""Reach my devices from anywhere: the ``/api/devices/anywhere`` routes.

The switch on Settings → Your devices and its status line
(:mod:`backend.web.core.devices_anywhere`, ``docs/devices-anywhere.md``).
Reading the status is open to whoever passed the gate; changing it is this
device's own business — never another MindFlock relaying, and it needs
``auth.may_configure`` (it opens a tunnel and writes to the user's GitHub).

Adding a NEW device without Tailscale (``core.devices_pair``,
docs/devices-anywhere.md §7) lives here too: ``/invite`` makes, shows and
cancels the long code on the inviter; ``/join`` takes it on the new
computer (409 ``needs_confirm`` until the person confirmed); ``/pair-alerts``
lists, dismisses and answers (Change key) the alarm a used-but-unfinished
code raises. They are never relayed and never served through the tunnel, and
they need :func:`auth.privileged`: a browser on this computer, a caller with
this device's access token or the devices' key (a signed-in phone), or one
of the owner's trusted Tailscale devices. Every one of those can already do
more than a code does (the token drives this computer; the devices' key IS
what a code hands out), so the live code is theirs to read too — it is not
limited to the browser that made it.
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
        st = await _pair.join(
            code[:4096],
            reachable=body.get("reachable") is not False,
            confirm=body.get("confirm") is True,
            leave_group=body.get("leave_group") is True,
        )
    except _pair.NeedsConfirm as err:
        # Nothing was sent: the person confirms first (in-page, or the CLI's
        # prompt) with the safety number in front of them.
        return JSONResponse({"error": str(err), **err.info}, status_code=409)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(st)


@router.get("/api/devices/anywhere/pair-alerts")
async def anywhere_pair_alerts(request: Request) -> JSONResponse:
    """``{pending: [...], alerts: [...]}`` — a new device pairing right now,
    and codes used without the join finishing (the key may be out)."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    return JSONResponse(_pair.pairing_status())


@router.post("/api/devices/anywhere/pair-alerts/change-key")
async def anywhere_change_key(request: Request) -> JSONResponse:
    """The alarm's **Change key**: a new devices' key for every member (and
    new access tokens, as removing a device does); the alarms clear."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    try:
        out = await _pair.change_key()
    except Exception as err:  # noqa: BLE001
        return JSONResponse(
            {"error": "couldn't change the key: %s" % err}, status_code=500
        )
    return JSONResponse({"ok": True, **out})


@router.delete("/api/devices/anywhere/pair-alerts")
async def anywhere_dismiss_alerts(request: Request) -> JSONResponse:
    """ "It was me": ``?id=<alert id>`` drops one alarm, no id drops all."""
    refused = await _owner_only(request)
    if refused is not None:
        return refused
    from backend.web.core import devices_pair as _pair

    n = _pair.dismiss_alerts(str(request.query_params.get("id") or "")[:32])
    return JSONResponse({"dismissed": n, **_pair.pairing_status()})
