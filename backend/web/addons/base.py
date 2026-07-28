"""The addon (plugin) interface.

A feature like the MindFlock pipeline control or the Assistant is an :class:`Addon`:
a self-contained module that owns its routes/websockets (on an ``APIRouter`` the
host ``include_router``s before the static mount), its lifecycle hooks, and a
declarative description of the UI it contributes. Adding a new feature is then:
one backend module + one frontend descriptor + one line in the registry — with
zero edits to the core server.

Optionally an addon also satisfies the structural :class:`ManagedProcess`
protocol (start/stop/status), which lets a generic start/stop/logs UI drive it.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional, Protocol, runtime_checkable

from fastapi import APIRouter

from backend.web.core import events as _events


@dataclass
class FrontendDescriptor:
    """One UI contribution, serialized verbatim into ``/api/addons`` for the SPA.

    ``where`` ∈ ``sidebar-bar`` | ``grid-pane`` | ``dialog`` | ``pane-tab`` |
    ``settings``. ``module`` is the URL of the ES module that renders it
    (``core/slots.js`` imports it and calls
    ``window.mindflockAddons[<addon id>].init(ctx)`` — see docs/extensions.md);
    ``None`` when the addon has no loadable module (built-in hand-wired UI).
    """

    id: str
    label: str
    where: str
    module: Optional[str] = None
    ws_path: Optional[str] = None
    api_base: Optional[str] = None
    poll_ms: Optional[int] = None
    read_only: bool = False
    order: int = 100
    available_flag: Optional[str] = None
    # True when the addon ships a bespoke hand-wired bar in app.js/index.html
    # (MindFlock, Assistant) rather than relying on the generic slot renderer. The
    # SPA's slots.js skips these. New addons leave it False to auto-surface.
    builtin_ui: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class ManagedProcess(Protocol):
    """OPTIONAL contract. An addon that implements it gets a generic
    start/stop/logs treatment. ``status()`` MUST include
    ``{"running": bool, "available": bool}``."""

    def start(self) -> bool: ...
    def stop(self) -> bool: ...
    def status(self) -> dict: ...
    def is_running(self) -> bool: ...


#: Namespace every addon-originated event lives under (``emit()`` auto-prefixes).
ADDON_EVENT_PREFIX = "addon."
# The namespace reserved for core lifecycle events (web/core/events.py); an
# addon must never spoof those.
_RESERVED_EVENT_PREFIX = "session."


@dataclass
class AppContext:
    """Handed to each addon's lifecycle hooks. Kept intentionally small; addons
    import core/engine helpers directly for everything else.

    v2 (roadmap B4) adds the event-bus seam, so in-process addons react to
    session events — and publish their own — without importing engine
    internals: :meth:`subscribe`, :meth:`emit`, :meth:`sessions`.
    """

    engine: object  # the process-wide Engine singleton
    register_task: (
        Callable  # register_task(coro) -> asyncio.Task (cancelled on shutdown)
    )
    log: object = None  # backend.log module (ErrorLog/InfoLog), best-effort

    # --- event-bus seam (Addon API v2) -------------------------------------- #
    def subscribe(
        self, event_name: str, callback: Callable[[dict], None]
    ) -> Callable[[], None]:
        """Register ``callback(envelope)`` for bus events named ``event_name``
        (``"*"`` matches every event). Returns an unsubscribe callable.

        A thin name-filter over :data:`backend.web.core.events.BUS`. The
        callback runs synchronously on whatever thread emits — keep it tiny
        (see ``EventBus.subscribe``)."""
        if event_name == "*":
            return _events.BUS.subscribe(callback)

        def _filtered(envelope: dict) -> None:
            if envelope.get("event") == event_name:
                callback(envelope)

        return _events.BUS.subscribe(_filtered)

    def emit(
        self,
        event: str,
        session: str = "",
        old=None,
        new=None,
        data: Optional[dict] = None,
    ) -> dict:
        """Publish an addon-originated event on the shared bus; returns the
        sequenced envelope (same shape ``/api/events`` streams).

        Namespacing rule: the core's ``session.*`` vocabulary is reserved —
        emitting into it raises ``ValueError``. Any other name is auto-prefixed
        with ``addon.`` when it doesn't already carry it, so
        ``ctx.emit("notify.ping")`` publishes ``addon.notify.ping``.
        Convention: name events ``<addon_id>.<what>``."""
        name = (event or "").strip()
        if not name:
            raise ValueError("event name is required")
        if name.startswith(_RESERVED_EVENT_PREFIX):
            raise ValueError(
                "the %r namespace is reserved for core session events"
                % _RESERVED_EVENT_PREFIX
            )
        if not name.startswith(ADDON_EVENT_PREFIX):
            name = ADDON_EVENT_PREFIX + name
        return _events.BUS.emit(name, session=session, old=old, new=new, data=data)

    def sessions(self) -> List[dict]:
        """Read-only snapshot of the sessions as last computed by the
        ``/api/instances`` poll — the same dicts the SPA sees (title, status,
        activity, stage, tokens…). Empty until the first poll after startup;
        the dicts are copies, so mutating them affects nothing."""
        return _events.sessions_snapshot()


class Addon(abc.ABC):
    """Base class for a self-registering feature."""

    id: str = ""
    label: str = ""

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        self.ctx = ctx

    @property
    @abc.abstractmethod
    def router(self) -> APIRouter:
        """An APIRouter carrying this addon's routes + websockets. The host
        includes it BEFORE the static mount, so the mount-last invariant holds
        by construction."""

    def frontend(self) -> List[FrontendDescriptor]:
        """UI contributions (0..n). Default: none."""
        return []

    async def on_startup(self, ctx: AppContext) -> None:
        """Run once at app startup (idempotent). Default: no-op."""

    async def on_shutdown(self, ctx: AppContext) -> None:
        """Run once at app shutdown (cleanup). Default: no-op."""

    def manifest(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "managed": isinstance(self, ManagedProcess),
            "frontend": [d.to_dict() for d in self.frontend()],
        }
