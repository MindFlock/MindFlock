"""Disk cleanup: the worktree garbage collector's routes
(``core.worktree_gc_driver``; docs/web-api.md, *Disk cleanup*).

* ``GET /api/cleanup`` — the last report (``?refresh=1`` starts a fresh scan;
  ``fetch=0``: without refreshing the base branches first, ``size=0``:
  cached sizes only).
* ``POST /api/cleanup/collect`` — remove every provably safe item now.
* ``POST /api/cleanup/remove`` — remove one item; one that holds work needs
  ``confirm`` and has its work saved first.
* ``POST /api/cleanup/settings`` — ``cleanup.mode`` / ``cleanup.after_days``.

Everything that removes or configures is owner-only, like the update routes.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import worktree_gc_driver as _driver
from backend.web.routes import _server

router = APIRouter()


async def _owner(request: Request) -> bool:
    try:
        from backend.web.core import auth as _auth

        return bool(await _auth.privileged(request.scope))
    except Exception:  # noqa: BLE001 — fail closed
        return False


def _refused() -> JSONResponse:
    return JSONResponse(
        {"error": "disk cleanup can only be run by this device's owner"},
        status_code=403,
    )


@router.get("/api/cleanup")
async def get_cleanup(refresh: int = 0, fetch: int = 1, size: int = 1) -> JSONResponse:
    rep = await asyncio.to_thread(
        _driver.report,
        _server(),
        refresh=bool(refresh),
        fetch=bool(fetch),
        measure=bool(size),
    )
    return JSONResponse(rep)


@router.post("/api/cleanup/collect")
async def post_cleanup_collect(request: Request) -> JSONResponse:
    if not await _owner(request):
        return _refused()
    srv = _server()
    res = await asyncio.to_thread(_driver.collect_now, srv)
    return JSONResponse({"ok": True, **res})


@router.post("/api/cleanup/remove")
async def post_cleanup_remove(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    if not await _owner(request):
        return _refused()
    body = payload if isinstance(payload, dict) else {}
    item_id = str(body.get("id") or "")
    if not item_id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    res = await asyncio.to_thread(
        _driver.remove_item, _server(), item_id, confirm=bool(body.get("confirm"))
    )
    if res.get("missing"):
        return JSONResponse(res, status_code=404)
    if res.get("needs_confirm"):
        return JSONResponse(res, status_code=409)
    return JSONResponse(res, status_code=200 if res.get("ok") else 422)


@router.post("/api/cleanup/settings")
async def post_cleanup_settings(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    if not await _owner(request):
        return _refused()
    body = payload if isinstance(payload, dict) else {}
    from backend.config.settings import SettingsUnreadable, UNREADABLE_HINT

    try:
        out = await asyncio.to_thread(
            _driver.set_settings, body.get("mode"), body.get("after_days")
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except SettingsUnreadable:
        return JSONResponse({"error": UNREADABLE_HINT}, status_code=409)
    return JSONResponse(out)
