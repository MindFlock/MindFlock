"""Per-session memory: ``GET /api/memory`` and ``GET /api/memory/history``.

* ``GET /api/memory`` → ``{"system": {"total_mb", "available_mb",
  "swap_total_mb", "swap_used_mb", "psi_some_avg10", "psi_full_avg10",
  "level"}, "sessions": [{"title", "mb", "peak_mb", "agent_mb", "mcp_mb",
  "other_mb", "procs", "at"}] (biggest first), "others": [{"pid", "name",
  "cmd", "mb"}] (the top processes in no session's tree), "at", "tick_ms",
  "tick_cpu_ms"}`` — the sampler's last reading (``core.memory_watch``),
  taken on demand when it is older than two ticks.
* ``GET /api/memory/history?minutes=60`` → ``{"samples": [{"at", "system",
  "sessions": {title: mb}, "others"}]}``, oldest first, from
  ``<config dir>/logs/memory.jsonl`` (and its rotated ``.1``). ``minutes``
  is clamped to 1..10080 (a week); 400 when it is not a number.

Not on a guest link's allow-list (core.guests): the readings name every
session and other processes' command lines. Routes only translate HTTP into
core calls.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.web.core import memory_watch as _memory_watch
from backend.web.routes import _server

router = APIRouter()

_MAX_MINUTES = 7 * 24 * 60


@router.get("/api/memory")
async def get_memory() -> JSONResponse:
    snap = await asyncio.to_thread(_memory_watch.snapshot, _server())
    return JSONResponse(snap)


@router.get("/api/memory/history")
async def get_memory_history(minutes: Optional[str] = None) -> JSONResponse:
    try:
        mins = 60.0 if minutes in (None, "") else float(minutes)
    except (TypeError, ValueError):
        return JSONResponse({"error": "minutes must be a number"}, status_code=400)
    if mins != mins:  # NaN
        return JSONResponse({"error": "minutes must be a number"}, status_code=400)
    mins = min(float(_MAX_MINUTES), max(1.0, mins))
    samples = await asyncio.to_thread(_memory_watch.history, mins)
    return JSONResponse({"samples": samples})
