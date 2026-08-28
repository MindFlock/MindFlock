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
import re
from dataclasses import asdict, dataclass, field
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


# --------------------------------------------------------------------------- #
# Addon API v3: extensions. A VSCode-like contribution model layered over the
# v2 descriptors — an extension declares ONE sidebar bar (label + buttons),
# commands, and dialog/pane surfaces in a static manifest; the host renders all
# chrome and lazily imports the extension's ES module on first use. Everything
# here is data, not behaviour: the SPA host (frontend/src/extensions/) owns the
# lifecycle. slots.js ignores the new ``extension`` manifest key, so v2 addons
# are untouched.
# --------------------------------------------------------------------------- #

#: Shape of an extension id and a surface id. Deliberately narrow (lowercase
#: slug, no dots): the id doubles as a URL segment (``/extensions/<id>/``), an
#: event namespace (``addon.<id>.*``) and a CSS prefix (``.mfx-<id>``).
EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Vocabulary of a surface's ``kind`` — the house words (a *dialog* is the
#: modal popup, a *pane* is a grid window), never "popup"/"window".
EXTENSION_SURFACE_KINDS = ("dialog", "pane")


@dataclass
class ExtensionButton:
    """One button on the extension's sidebar bar. Buttons never carry code —
    they reference a command id, the universal verb."""

    command: str  # command id this button runs
    label: str
    title: str = ""  # tooltip

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtensionCommand:
    """One command. ``id`` is ``<ext-id>.<verb>``. A command with ``surface``
    set is *declarative*: the host opens that surface without loading the
    extension's module first (``ref`` optionally names the instance). Without
    ``surface`` the command runs code the module registers at activation."""

    id: str  # "<ext-id>.<verb>" — see validate_extension_spec
    title: str  # palette text, "Database: Explorer" style
    surface: Optional[str] = None  # declarative: surface id to open
    ref: Optional[str] = None  # declarative: instance ref (surface must be set)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtensionSurface:
    """One host-owned container the extension renders into: a ``dialog``
    (modal popup) or a ``pane`` (grid window). ``multi`` panes support many
    live instances (the host mints refs); ``back_command`` makes the host draw
    a back button in the pane head running that command (the verify-pane-back
    precedent)."""

    id: str
    kind: str  # "dialog" | "pane" (house vocabulary, NOT popup/window)
    title: str  # default chrome title
    multi: bool = False  # pane only: many instances (host mints refs)
    back_command: Optional[str] = None  # pane only: back button in the head

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtensionSpec:
    """The whole static manifest of one extension — everything the host needs
    to render its chrome without executing extension code. Serialized verbatim
    into ``/api/addons`` under the ``extension`` key."""

    module: str  # e.g. "/extensions/dbclient/index.js"
    bar_label: str
    buttons: List[ExtensionButton] = field(default_factory=list)
    commands: List[ExtensionCommand] = field(default_factory=list)
    surfaces: List[ExtensionSurface] = field(default_factory=list)
    #: Host injects ``<module dir>/style.css`` into ``layer(components)`` (so a
    #: sloppy selector loses to the theme layer instead of beating the app).
    stylesheet: bool = False
    #: MINIMUM host API level required — the host refuses activation when its
    #: own level is lower.
    api_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def validate_extension_spec(addon_id: str, spec: ExtensionSpec) -> List[str]:
    """Every problem with ``spec`` (empty list = valid).

    Returns problems rather than raising because the two callers want opposite
    severities: a *built-in* spec is developer error (register_addons raises on
    a non-empty list), while a *discovered* extension's bad spec must only skip
    that extension, never take the server down.
    """
    problems: List[str] = []
    if not EXTENSION_ID_RE.match(addon_id or ""):
        problems.append(
            "extension id %r must match %s" % (addon_id, EXTENSION_ID_RE.pattern)
        )
    # Command ids are namespaced under the extension id: "<ext-id>.<verb>".
    command_re = re.compile(r"^" + re.escape(addon_id or "") + r"\.[a-z0-9][a-z0-9-]*$")

    surfaces_by_id = {}
    for s in spec.surfaces:
        if not EXTENSION_ID_RE.match(s.id or ""):
            problems.append(
                "surface id %r must match %s" % (s.id, EXTENSION_ID_RE.pattern)
            )
        if s.id in surfaces_by_id:
            problems.append("duplicate surface id %r" % s.id)
        surfaces_by_id[s.id] = s
        if s.kind not in EXTENSION_SURFACE_KINDS:
            problems.append(
                "surface %r has kind %r (expected one of %s)"
                % (s.id, s.kind, ", ".join(EXTENSION_SURFACE_KINDS))
            )
        if s.multi and s.kind != "pane":
            problems.append("surface %r: multi is pane-only" % s.id)

    command_ids = set()
    for c in spec.commands:
        if not command_re.match(c.id or ""):
            problems.append(
                "command id %r must match '%s.<verb>' (verb: [a-z0-9][a-z0-9-]*)"
                % (c.id, addon_id)
            )
        if c.id in command_ids:
            problems.append("duplicate command id %r" % c.id)
        command_ids.add(c.id)

    for c in spec.commands:
        if c.ref is not None and c.surface is None:
            problems.append("command %r has a ref but no surface" % c.id)
        if c.surface is None:
            continue
        surface = surfaces_by_id.get(c.surface)
        if surface is None:
            problems.append("command %r opens unknown surface %r" % (c.id, c.surface))
        elif surface.multi and c.ref is None:
            # A multi surface has no single instance a declarative open could
            # mean, and the manifest defines no ref-minting — the command must
            # pin one explicitly (or run code that calls openPane itself).
            problems.append(
                "command %r opens multi surface %r without a ref" % (c.id, c.surface)
            )

    for b in spec.buttons:
        if b.command not in command_ids:
            problems.append("button %r references unknown command" % b.command)

    for s in spec.surfaces:
        if s.back_command is None:
            continue
        if s.kind != "pane":
            problems.append("surface %r: back_command is pane-only" % s.id)
        if s.back_command not in command_ids:
            problems.append(
                "surface %r has unknown back_command %r" % (s.id, s.back_command)
            )
    return problems


@runtime_checkable
class ManagedProcess(Protocol):
    """OPTIONAL contract. An addon that implements it gets a generic
    start/stop/logs treatment. ``status()`` MUST include
    ``{"running": bool, "available": bool}``."""

    def start(self) -> bool: ...
    def stop(self) -> bool: ...
    def status(self) -> dict: ...
    def is_running(self) -> bool: ...


#: What a GET returns in place of a stored credential — it says "a value is
#: saved" without ever transmitting the value. Writing it back (or writing "")
#: means "keep the saved one". Shared so every addon that handles a credential
#: agrees on the sentinel; the frontend's counterpart is ``SECRET_MASK`` in
#: settings/useSettings.tsx.
SECRET_MASK = "•••set"  # pragma: allowlist secret — it IS the placeholder

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
    #: Where the addon came from: ``"builtin"`` (shipped in this package) or
    #: ``"user"`` (discovered under ``~/.mindflock/extensions/``). The registrar
    #: (``discover_extensions``) stamps discovered addons ``"user"``; the class
    #: default keeps every existing addon a builtin with zero edits.
    origin: str = "builtin"

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

    def extension(self) -> Optional[ExtensionSpec]:
        """The Addon API v3 contribution (sidebar bar + commands + surfaces),
        or ``None`` for addons without one. Default: none."""
        return None

    async def on_startup(self, ctx: AppContext) -> None:
        """Run once at app startup (idempotent). Default: no-op."""

    async def on_shutdown(self, ctx: AppContext) -> None:
        """Run once at app shutdown (cleanup). Default: no-op."""

    def manifest(self) -> dict:
        # Local import — the house pattern for settings readers: a module-level
        # name here would be one refactor away from the silent-fallback
        # NameError trap (see backend/web/server.py's settings readers), and
        # manifest() must always read the store fresh so a toggle flip lands on
        # the next /api/addons fetch without a restart.
        from backend.config.settings import load_settings

        spec = self.extension()
        return {
            "id": self.id,
            "label": self.label,
            "managed": isinstance(self, ManagedProcess),
            "frontend": [d.to_dict() for d in self.frontend()],
            "extension": spec.to_dict() if spec else None,
            # Enabled is the absence of an opt-out: extensions are on by
            # default, and the Settings screen only ever writes the OFF list.
            "enabled": self.id not in load_settings().extensions.disabled,
            "origin": self.origin,
        }
