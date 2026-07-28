"""Addon registry.

``build_addons(ctx)`` returns the ordered list of addons; ``register_addons``
includes each addon's router on the app (BEFORE the static mount, so the
mount-last invariant holds) and exposes ``GET /api/addons`` — the manifest the
SPA iterates to render addon UI. The app lifespan drives each addon's
``on_startup`` / ``on_shutdown``.

Adding a feature is: write ``addons/<name>.py`` (subclass :class:`Addon`), write
``static/addons/<name>.js`` (the ES module ``core/slots.js`` loads via the
descriptor's ``module`` URL — it registers ``window.mindflockAddons[<id>]``),
and add one line to ``build_addons`` — no edits to the core server. The notify
addon (``addons/notify.py`` + ``static/addons/notify.js``) is the worked
example; the full guide is docs/extensions.md.
"""

from __future__ import annotations

from typing import List

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .base import Addon, AppContext, FrontendDescriptor, ManagedProcess
from .assistant import AssistantAddon
from .connections import ConnectionsAddon
from .doctor import DoctorAddon
from .notify import NotifyAddon
from .settings import SettingsAddon
from .ticket_ingestion import TicketIngestionAddon
from .templates import TemplatesAddon

__all__ = [
    "Addon",
    "AppContext",
    "FrontendDescriptor",
    "ManagedProcess",
    "build_addons",
    "register_addons",
]


def build_addons(ctx: AppContext) -> List[Addon]:
    """The ordered list of registered addons. One line per addon — the only
    edit needed to add a new one."""
    return [
        TicketIngestionAddon(ctx),
        AssistantAddon(ctx),
        SettingsAddon(ctx),
        DoctorAddon(ctx),
        ConnectionsAddon(ctx),
        TemplatesAddon(ctx),
        NotifyAddon(ctx),
    ]


def register_addons(app: FastAPI, ctx: AppContext) -> List[Addon]:
    """Build addons, mount their routers, and expose the ``/api/addons`` manifest.

    MUST be called before the static-files mount so addon ``/api/*`` and ``ws``
    routes win over the catch-all.
    """
    addons = build_addons(ctx)
    for addon in addons:
        app.include_router(addon.router)

    @app.get("/api/addons")
    def addons_manifest() -> JSONResponse:
        return JSONResponse({"addons": [a.manifest() for a in addons]})

    return addons
