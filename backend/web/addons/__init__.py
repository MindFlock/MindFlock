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

Addon API v3 adds *discovered* extensions: ``discover_extensions`` scans
``$MINDFLOCK_EXTENSIONS_DIR`` (default ``~/.mindflock/extensions/``) for
``<dir>/extension.py`` modules exposing ``build(ctx) -> Addon``, and
``register_addons`` appends them after the built-ins (and mounts each dir's
``frontend/`` at ``/extensions/<id>``). Discovery runs once at server import —
a new extension needs a restart, and a discovered extension's backend stays
loaded until one (stated in the UI, not hidden).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config.config import GetConfigDir

from .base import (
    Addon,
    AppContext,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
    FrontendDescriptor,
    ManagedProcess,
    EXTENSION_ID_RE,
    validate_extension_spec,
)
from .assistant import AssistantAddon
from .connections import ConnectionsAddon
from .dbclient import DbClientAddon
from .doctor import DoctorAddon
from .notify import NotifyAddon
from .settings import SettingsAddon
from .ticket_ingestion import TicketIngestionAddon
from .templates import TemplatesAddon
from .traffic import TrafficAddon

_logger = logging.getLogger(__name__)

__all__ = [
    "Addon",
    "AppContext",
    "ExtensionButton",
    "ExtensionCommand",
    "ExtensionSpec",
    "ExtensionSurface",
    "FrontendDescriptor",
    "ManagedProcess",
    "RESERVED_EXTENSION_IDS",
    "build_addons",
    "discover_extensions",
    "extensions_dir",
    "register_addons",
    "validate_extension_spec",
]

#: Ids a discovered extension may never claim. An extension's id doubles as a
#: URL namespace (``/api/<id>/``, ``/extensions/<id>/``) and an event prefix,
#: so a claim on any core segment would shadow or spoof the app itself. Covers
#: the core /api segments and namespaces plus every built-in addon id (those
#: are also rejected by the live collision check, but belt-and-braces keeps a
#: bare ``discover_extensions()`` call safe too).
RESERVED_EXTENSION_IDS = frozenset(
    {
        "addons",
        "instances",
        "settings",
        "events",
        "config",
        "providers",
        "devices",
        "logs",
        "aliases",
        "extensions",
        "assistant",
        "server",
        "auth",
        "core",
        "vendor",
        "static",
        "api",
        "m",
        # Built-in addon ids not already covered above.
        "mindflock",
        "doctor",
        "connections",
        "templates",
        "notify",
        "traffic",
        "dbclient",
    }
)


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
        TrafficAddon(ctx),
        # The first Addon API v3 extension (sidebar bar + dialog/pane surfaces).
        DbClientAddon(ctx),
    ]


def extensions_dir() -> Path:
    """Directory user extensions are discovered in.

    Honors ``$MINDFLOCK_EXTENSIONS_DIR`` (tests and ad-hoc TestClient scripts
    MUST point it at a tmp dir, alongside ``MINDFLOCK_SETTINGS_FILE`` — see
    tests/conftest.py); otherwise ``~/.mindflock/extensions/``.
    """
    env = os.environ.get("MINDFLOCK_EXTENSIONS_DIR")
    if env:
        return Path(env)
    return Path(GetConfigDir()) / "extensions"


def _load_extension_dir(ctx: AppContext, ext_dir: Path) -> Addon:
    """Import ``<ext_dir>/extension.py`` and build its addon. Raises on any
    problem — the caller owns the per-directory containment."""
    module_name = "mindflock_ext_%s" % ext_dir.name
    spec = importlib.util.spec_from_file_location(
        module_name, str(ext_dir / "extension.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("could not build an import spec for extension.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec (the standard importlib recipe), so the module can
    # see itself under its own name while its top level runs.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    build = getattr(module, "build", None)
    if not callable(build):
        raise TypeError("extension.py must expose `def build(ctx) -> Addon`")
    addon = build(ctx)
    if not isinstance(addon, Addon):
        raise TypeError("build(ctx) returned %r, not an Addon" % (addon,))
    # Touch the router now so a broken one is contained here (skip this dir)
    # instead of raising later inside register_addons, past the containment.
    _ = addon.router
    return addon


def discover_extensions(
    ctx: AppContext, taken_ids: Optional[Iterable[str]] = None
) -> List[Addon]:
    """Load user extensions from :func:`extensions_dir`, in dir-name order.

    Each subdirectory containing ``extension.py`` is imported under the module
    name ``mindflock_ext_<dirname>`` and must expose ``build(ctx) -> Addon``.
    Containment is per directory: any failure — import error, a ``build`` that
    raises, an unsafe id, an invalid :class:`ExtensionSpec` — logs and skips
    that extension, never the server. ``taken_ids`` carries the already-
    registered addon ids (register_addons passes the built-ins) so a user
    extension can never shadow one.
    """
    taken = set(taken_ids or ())
    root = extensions_dir()
    try:
        subdirs = sorted(
            (d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name
        )
    except OSError:
        return []  # no extensions dir — the common, quiet case

    found: List[Addon] = []
    for ext_dir in subdirs:
        if not (ext_dir / "extension.py").is_file():
            continue
        try:
            addon = _load_extension_dir(ctx, ext_dir)
            addon_id = str(getattr(addon, "id", "") or "")
            if not EXTENSION_ID_RE.match(addon_id):
                raise ValueError(
                    "id %r must match %s" % (addon_id, EXTENSION_ID_RE.pattern)
                )
            if addon_id in RESERVED_EXTENSION_IDS:
                raise ValueError("id %r is reserved" % addon_id)
            if addon_id in taken:
                raise ValueError("id %r is already registered" % addon_id)
            ext_spec = addon.extension()
            if ext_spec is not None:
                problems = validate_extension_spec(addon_id, ext_spec)
                if problems:
                    raise ValueError("invalid ExtensionSpec: " + "; ".join(problems))
        except Exception as err:  # noqa: BLE001 — containment IS the contract
            _logger.warning("extension %s failed to load: %s", ext_dir.name, err)
            continue
        # Stamped by the registrar, not declared by the extension: origin is a
        # fact about where the code came from, and the manifest reports it.
        addon.origin = "user"
        addon.extension_dir = ext_dir
        taken.add(addon_id)
        found.append(addon)
    return found


def register_addons(app: FastAPI, ctx: AppContext) -> List[Addon]:
    """Build addons, mount their routers, and expose the ``/api/addons`` manifest.

    MUST be called before the static-files mount so addon ``/api/*`` and ``ws``
    routes win over the catch-all — which is also what lets the per-extension
    ``/extensions/<id>`` mounts here beat it. Built-ins first, then discovered
    extensions in dir-name order.
    """
    addons = build_addons(ctx)
    # A built-in's bad spec is developer error in THIS repo — fail the import
    # loudly rather than serving a manifest the SPA host can't act on.
    for addon in addons:
        spec = addon.extension()
        if spec is not None:
            problems = validate_extension_spec(addon.id, spec)
            if problems:
                raise ValueError(
                    "addon %r has an invalid ExtensionSpec: %s"
                    % (addon.id, "; ".join(problems))
                )
    discovered = discover_extensions(ctx, taken_ids={a.id for a in addons})
    addons.extend(discovered)
    for addon in addons:
        app.include_router(addon.router)
    # A discovered extension serves its ES module + assets from its own
    # ``frontend/`` dir; built-in extension frontends live under
    # ``backend/web/static/extensions/<id>/`` and ride the main static mount.
    for addon in discovered:
        frontend_dir = getattr(addon, "extension_dir", None)
        frontend_dir = frontend_dir / "frontend" if frontend_dir else None
        if frontend_dir is not None and frontend_dir.is_dir():
            app.mount(
                f"/extensions/{addon.id}",
                StaticFiles(directory=str(frontend_dir)),
                name=f"ext-{addon.id}",
            )

    @app.get("/api/addons")
    def addons_manifest() -> JSONResponse:
        return JSONResponse({"addons": [a.manifest() for a in addons]})

    return addons
