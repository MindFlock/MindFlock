"""Flock mode: ``POST /api/instances/{title}/flock`` — every prompt to the
session asks for MindFlock workers.

* ``POST /api/instances/{title}/flock {on: bool}`` → ``{flock: bool}``.
  Turning it on writes the session's ``.flock`` file holding the current
  request text (``core.flock_mode``) and, for a CLI whose prompt hook injects
  context (Claude), re-pins the activity hooks so a session launched by an
  older build gets the hook that prints it — Claude Code hot-reloads the file,
  no relaunch. Off removes the file. 400 without a boolean ``on``, 404 unknown
  session, 409 for another device's ``<device>::<title>`` row or a request
  another MindFlock relayed (the flag is this device's file). A guest link is
  refused by the gate (``flock`` is not a guest session write).

The row's ``flock`` field (``GET /api/instances``) is how clients read it;
there is no event — the rows poll. Routes only translate HTTP into core
calls; the registry lookup and the providers are read through the server
module at call time, so tests that patch ``server._x`` reach them.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.session import tmux
from backend.web.core import flock_mode as _flock_mode
from backend.web.core import remote as _remote
from backend.web.routes import _server

router = APIRouter()


def _set(inst, title: str, on: bool) -> bool:
    name = tmux.to_mindflock_tmux_name(title)
    state = _flock_mode.set_on(name, on)
    if on:
        try:
            prov = _server().providers.resolve(getattr(inst, "Program", "") or "")
            if prov.voice_prompt_hook():
                wt = inst.GetWorktreePath() or ""
                if wt:
                    prov.install_activity_hooks(wt, name)
        except Exception:  # noqa: BLE001 — the flag is set; hooks are enrichment
            pass
    return state


@router.post("/api/instances/{title}/flock")
async def flock_set(title: str, request: Request) -> JSONResponse:
    """Turn Flock mode on or off: ``{"on": bool}`` → ``{"flock": bool}``."""
    if _remote.is_remote_title(title) or _remote.from_remote(request):
        return JSONResponse(
            {"error": "Flock mode is set on the device that runs the session"},
            status_code=409,
        )
    inst, err = _server()._inst_or_404(title)
    if err is not None:
        return err
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: a bad body, not a 422
        payload = None
    on = payload.get("on") if isinstance(payload, dict) else None
    if not isinstance(on, bool):
        return JSONResponse({"error": "on must be true or false"}, status_code=400)
    try:
        state = await asyncio.to_thread(_set, inst, title, on)
    except OSError as oerr:
        return JSONResponse(
            {"error": "could not write the flock flag: %s" % oerr}, status_code=500
        )
    return JSONResponse({"flock": state})
