"""Doctor addon: dependency preflight over HTTP.

Exposes the shared dependency doctor (:mod:`backend.doctor`) as
``GET /api/doctor`` so the SPA can surface missing tmux/gh/claude (and their
per-platform fixes) up front instead of the user discovering them as cryptic
errors at session-create or push time.

Results are cached for ~30s (the checks shell out to ``git``/``gh``/``tmux``);
pass ``?refresh=1`` to force a re-probe after installing something.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend import doctor

from .base import Addon, AppContext, FrontendDescriptor

#: How long a doctor run stays fresh. Keeps the endpoint cheap under the SPA's
#: polling without hiding a just-installed dependency for long.
_CACHE_TTL_S = 30.0


class DoctorAddon(Addon):
    id = "doctor"
    label = "Doctor"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._cached_payload: Optional[dict] = None
        self._cached_at: float = 0.0
        self._router = self._build_router()

    def _payload(self, refresh: bool = False) -> dict:
        now = time.monotonic()
        if (
            not refresh
            and self._cached_payload is not None
            and now - self._cached_at < _CACHE_TTL_S
        ):
            return self._cached_payload
        payload = doctor.to_payload(doctor.run_checks())
        self._cached_payload = payload
        self._cached_at = now
        return payload

    # --- routes ----------------------------------------------------------- #
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api")

        @router.get("/doctor")
        def get_doctor(refresh: bool = False) -> JSONResponse:
            # sync def -> FastAPI runs it in the threadpool, so the (bounded)
            # subprocess probes never block the event loop.
            return JSONResponse(self._payload(refresh=refresh))

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- lifecycle -------------------------------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        """Print failed checks once at startup so an interactive launch shows
        what's missing immediately (best-effort; the API is the primary
        surface). Skipped when stdout isn't a terminal — under tests / service
        managers nobody reads the print and the probes cost subprocesses."""
        try:
            if not sys.stdout.isatty():
                return
            payload = await asyncio.to_thread(self._payload)
        except Exception:  # noqa: BLE001 — the doctor must never break startup
            return
        for c in payload["checks"]:
            if c["status"] == "fail":
                line = f"doctor: {c['label']} — {c['detail']}"
                if c.get("fix"):
                    line += f"  (fix: {c['fix']})"
                print(line)

    # --- frontend --------------------------------------------------------- #
    def frontend(self):
        return [
            FrontendDescriptor(
                id="doctor",
                label="Doctor",
                where="settings",
                module=None,  # panel is hand-wired into the settings dialog
                api_base="/api/doctor",
                order=90,
                # Wave 2 wires the panel into the settings dialog by hand; keep
                # the generic slot renderer away until then.
                builtin_ui=True,
            )
        ]
