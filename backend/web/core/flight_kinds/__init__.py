"""Flight stop kinds as plug-ins (docs/flights.md "Round 15").

The built-in kinds (agent, split, join, ship, verify, you, wait, reviews,
checks, command, ticket, notify) live in ``core/flights.py`` and
``core/flight_driver.py`` as before. Every kind added since is one module in
this package that calls :func:`register` at import time with a :class:`Kind`;
:func:`load` imports every module here once, and the core consults the
registry wherever it used to consult its own tables:

* ``flights.STEP_KINDS`` carries the kind's ``spec`` (so ``GET
  /api/flights/kinds`` lists it and the palette / forms show it),
* ``flights._step_config`` falls through to ``Kind.config``,
* ``flights._default_title`` to ``Kind.default_title``,
* ``flights.next_action`` to ``Kind.judge`` for a running step,
* ``flight_driver.observe`` adds ``obs[kind] = await Kind.observe(...)``,
* ``flight_driver._perform`` hands a ``run`` action to ``Kind.run``,
* ``POST /api/flights/{id}/steps/{sid}/op/{name}`` calls ``Kind.ops[name]``,
* ``routes/flights.py`` includes every ``Kind.router``.

A plug-in module may import ``backend.web.core.flights`` and
``backend.web.core.flight_driver`` and use their helpers (``_text``, ``_int``,
``_bool``, ``_f``, ``_wait``, ``fill_prompt``; ``run_shell``, ``job_of``,
``spawn_worker``, ``worktree_of``, ``_deliver``): the registry is installed at
the very end of ``flights.py``, after everything a plug-in needs is defined.
The driver is imported lazily inside functions (it imports ``flights`` itself).

Failing to import one plug-in logs and skips it: a broken kind never takes
the flights store down with it.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend import log

__all__ = ["Kind", "StepFailed", "register", "get", "all_kinds", "load", "routers"]


class StepFailed(Exception):
    """An executor's refusal: becomes a ``fail`` action with this reason (and
    ``detail``, what the step still records — a split's workers made). Raise
    it from ``Kind.run`` or ``Kind.observe``; ``flight_driver._StepFailed`` is
    this class."""

    def __init__(self, reason: str, detail: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.detail = detail


Judge = Callable[[dict, dict, dict, dict, float], dict]
Runner = Callable[[dict, dict, dict, float], Awaitable[dict]]
Observer = Callable[[dict, dict, dict, dict, float], Awaitable[Any]]
Op = Callable[[dict, dict, dict], dict]


@dataclass(frozen=True)
class Kind:
    """One stop kind.

    ``spec`` is the ``STEP_KINDS`` entry: ``label``, ``actor`` (``robot`` |
    ``you``), ``summary``, ``default_title``, ``fields`` (``flights._f`` rows)
    and ``risk`` (a list drawn from ``remote`` — touches the shared remote,
    ``irreversible`` — Ctrl+Z cannot undo it, ``spends`` — costs money outside
    the agent's own usage; the Sky draws a stripe for each), ``family``
    (``work`` | ``ship`` | ``outside`` | ``people``: the palette's grouping)
    and ``default_on_fail`` (what a step of this kind does on failure when
    the plan says nothing: ``halt`` unless set — a budget check perches).

    ``config(raw, strict, where) -> dict`` coerces a step's config exactly as
    ``flights._step_config`` does for the built-ins (raise
    ``flights.PlanError(message, where + ".field")`` in strict mode).

    ``judge(flight, step, ss, obs, now) -> action`` is pure (no I/O) and is
    asked while the step is ``running``: ``{"action": "wait", "note": …}`` /
    ``done`` / ``fail`` (+ ``reason``) / ``run`` (do the side effect again) /
    ``you`` (park on the owner), with ``detail`` merged into the step's
    detail. Reads ``obs["session"]`` and ``obs[kind]`` (what ``observe``
    returned this pass). Set ``detail["receipt"]`` (≤ 80 chars, what it did:
    "312 passed", "PR #291") on ``done`` — the box shows it after the stop
    has flown.

    ``run(flight, step, action, now) -> action`` is awaited for a ``run``
    action: start the side effect (a thread, a job, a request) and return the
    action amended (``detail``), or turned into ``done`` / ``fail`` /
    ``advance``. Raise :class:`StepFailed` to fail with a reason.

    ``observe(flight, step, ss, sess, now)`` (optional, awaited while the
    step runs or waits on you) gathers what ``judge`` needs into
    ``obs[kind]``; ``sess`` is the session observation (``present``,
    ``folder``, ``branch``, ``pr_url``, ``activity`` …).

    ``needs_session``: the step needs the flight's session to exist (and not
    be paused) — the driver holds it otherwise, as for ``agent``.
    ``types_to_agent``: the step types into the agent (the driver wakes it
    first and watches the agent's turn, as for ``agent`` / ``reviews``).

    ``default_title(config)`` (optional) names a step from its config, as
    "Wait 10 min" does. ``ops`` are ``name -> fn(flight, step, body) -> dict``
    behind ``POST /api/flights/{id}/steps/{sid}/op/{name}`` (mutate through
    ``flights.mutate``; raise ``flights.FlightError`` to refuse). ``router``
    is a FastAPI ``APIRouter`` of the kind's own routes (an inbound hook, a
    listing), included next to the flights routes.
    """

    kind: str
    spec: dict
    config: Callable[[dict, bool, str], dict]
    judge: Judge
    run: Runner
    observe: Optional[Observer] = None
    needs_session: bool = True
    types_to_agent: bool = False
    default_title: Optional[Callable[[dict], str]] = None
    ops: Dict[str, Op] = field(default_factory=dict)
    router: Any = None


_REGISTRY: Dict[str, Kind] = {}
_LOADED = False


def register(kind: Kind) -> Kind:
    """Add ``kind`` (a module calls this at import). Re-registering the same
    kind id replaces it (a test's fake, a reload)."""
    if not kind.kind or kind.kind != kind.kind.lower() or len(kind.kind) > 16:
        raise ValueError("a kind id is 1–16 lower-case chars, not %r" % (kind.kind,))
    spec = dict(kind.spec)
    spec.setdefault("fields", [])
    spec.setdefault("risk", [])
    spec.setdefault("actor", "robot")
    spec.setdefault("default_title", spec.get("label") or kind.kind.title())
    spec.setdefault("summary", "")
    spec.setdefault("label", kind.kind.title())
    spec.setdefault("family", "outside")
    spec.setdefault("default_on_fail", "halt")
    object.__setattr__(kind, "spec", spec)
    _REGISTRY[kind.kind] = kind
    return kind


def get(kind: str) -> Optional[Kind]:
    return _REGISTRY.get(kind)


def all_kinds() -> List[Kind]:
    """Every registered kind, in registration (module) order."""
    return list(_REGISTRY.values())


def routers() -> list:
    return [k.router for k in _REGISTRY.values() if k.router is not None]


def load() -> None:
    """Import every module of this package once (idempotent); a module that
    fails to import is logged and skipped."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    for mod in sorted(m.name for m in pkgutil.iter_modules(__path__)):
        if mod.startswith("_"):
            continue
        try:
            importlib.import_module("%s.%s" % (__name__, mod))
        except Exception as err:  # noqa: BLE001 — one bad kind, not no flights
            log.ErrorLog.Printf("flight kind %s not loaded: %v", mod, err)
