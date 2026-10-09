"""Doctor addon: dependency preflight over HTTP.

Exposes the shared dependency doctor (:mod:`backend.doctor`) as
``GET /api/doctor`` so the SPA can surface missing tmux/git/claude (and their
per-platform fixes) up front instead of the user discovering them as cryptic
errors at session-create time. Optional tools (``gh``, ``uv``, ``tailscale``)
are reported too, but as ``info``/``warn`` — never as a blocker.

``/api/doctor/install-terminal`` runs everything missing in one go (see
:mod:`backend.web.core.setup_install`): a browser terminal, so the one sudo
prompt has somewhere to go.

Results are cached for ~30s (the checks shell out to ``git``/``tmux``, plus the
optional ``gh``/``tailscale`` probes); pass ``?refresh=1`` to force a re-probe
after installing something.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

from fastapi import APIRouter, WebSocket
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
        #: The install result PATH was last refreshed for (see install-state).
        self._refreshed_for: Optional[dict] = None
        self._router = self._build_router()

    def _payload(self, refresh: bool = False) -> dict:
        now = time.monotonic()
        if (
            not refresh
            and self._cached_payload is not None
            and now - self._cached_at < _CACHE_TTL_S
        ):
            return self._cached_payload
        if refresh:
            # A re-probe is asked for after installing something: re-read the
            # PATH first, or a tool in a directory created since boot (Homebrew,
            # ~/.opencode/bin) still reads as missing until a restart.
            from backend import pathenv

            pathenv.refresh()
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

        @router.post("/doctor/ack-state-notice")
        def ack_state_notice() -> JSONResponse:
            """Dismiss the downgrade notice (the user read the banner).

            Clears the cache too: the notice is embedded in the cached payload,
            so without this the banner would come back on reload for up to the
            cache TTL and look like the dismiss did nothing.
            """
            from backend.config import state as state_mod

            state_mod.clear_downgrade_notice()
            self._cached_payload = None
            return JSONResponse({"ok": True})

        @router.websocket("/doctor/install-terminal")
        async def install_terminal(ws: WebSocket) -> None:
            """A browser terminal running the one-shot install script (rebuilt
            server-side from a fresh doctor run — the client sends no command).
            Reattaches to a run still in progress. Works without tmux (a plain
            PTY — tmux is usually one of the things being installed).

            Only for the person at this device (:func:`privileged`): it runs
            installers and sudo, so an anonymous caller of an exposed gate-off
            server, or another MindFlock relaying, is refused."""
            import json

            from backend.web.core import auth, pty_run, setup_install

            await ws.accept()
            if not await auth.privileged(ws.scope):
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "installing is only allowed from this "
                            "computer or a signed-in device",
                        }
                    )
                )
                await ws.close(code=4403)
                return
            session, err = await asyncio.to_thread(setup_install.ensure_session)
            if err is not None:
                await ws.send_text(json.dumps({"type": "error", "message": err}))
                await ws.close(code=4500)
                return
            self._cached_payload = None  # whatever it installs, re-probe after
            await pty_run.serve(ws, session)

        @router.get("/doctor/install-state")
        async def install_state() -> JSONResponse:
            """``{running, exit_code}`` of the install terminal's script."""
            from backend.web.core import setup_install

            st = await asyncio.to_thread(setup_install.state)
            if st["exit_code"] is not None and self._refreshed_for != st:
                # The script just finished: pick up whatever it put on disk
                # (once per result — the UI polls this every second).
                self._refreshed_for = st
                from backend import pathenv

                await asyncio.to_thread(pathenv.refresh)
                self._cached_payload = None
            elif st["exit_code"] is None:
                self._refreshed_for = None
            return JSONResponse(st)

        @router.post("/doctor/install-close")
        def install_close() -> JSONResponse:
            """Close the install terminal — only once its script finished; a
            run in progress keeps going (``closed: false``)."""
            from backend.web.core import setup_install

            self._cached_payload = None
            return JSONResponse({"closed": setup_install.close()})

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
