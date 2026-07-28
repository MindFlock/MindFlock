"""Per-session cost budgets (J5): the guardrail notice and the input lock.

Two related mechanisms, one shared config:

* **Guardrail** — one ``session.budget_exceeded`` event per session per budget
  value (:func:`_check_session_budget`), re-armed when the budget changes.
* **Lock** — when a session's estimated cost reaches its *effective* budget,
  user input to that window is blocked server-side (:func:`_budget_locked`)
  until the budget is raised. A raise can be temporary (expires after N hours)
  or forever, and is persisted in ``<config dir>/budget_overrides.json`` so
  "forever" and in-flight temporary raises survive a restart.

The global defaults come from Settings (``session_budget_usd`` per session,
``window_budget_usd`` for the plan-window estimate in the top bar).

Split out of ``backend.web.server`` (which re-imports these names — the
budget routes, the tick loops, and tests reference them through the server
namespace).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Optional

from backend import config, log
from backend.web.core import events as _events


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


# J5 cost guardrail: one ``session.budget_exceeded`` per session per budget
# value. Re-arms when the budget is changed (raised past the fired level — or
# lowered, which is a new threshold being crossed); cleared on server restart
# and on session delete.
_BUDGET_FIRED: Dict[str, float] = {}  # title -> budget value announced at


def _session_budget_usd() -> float:
    """The configured per-session budget in USD (0 = guardrail off)."""
    try:
        from backend.config import settings as _settings_store

        v = _settings_store.load_settings().general.session_budget_usd
        return float(v) if v else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _window_budget_usd() -> float:
    """The user's plan-window allowance estimate in API-equivalent USD
    (0 = unknown — the UI then shows only the reset countdown, no percent)."""
    try:
        from backend.config import settings as _settings_store

        v = _settings_store.load_settings().general.window_budget_usd
        return float(v) if v else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _check_session_budget(title: str, cost: float) -> None:
    """Emit ``session.budget_exceeded`` on the first crossing (deduped)."""
    budget = _server()._session_budget_usd()
    if budget <= 0 or cost < budget:
        return
    if _BUDGET_FIRED.get(title) == budget:
        return  # already announced at this budget level
    _BUDGET_FIRED[title] = budget
    _events.BUS.emit(
        "session.budget_exceeded",
        session=title,
        data={"cost": float(cost), "budget": float(budget)},
    )


# --- Per-session budget LOCK -------------------------------------------------
# When a session's estimated cost reaches its effective budget, user input to
# that window is blocked (server-side, authoritative) until the budget is
# raised. A raise can be temporary (expires after N hours) or forever; it is
# persisted so "forever" and in-flight temporary raises survive a restart.
_BUDGET_OV_LOCK = threading.Lock()
_BUDGET_OV: Optional[Dict[str, dict]] = (
    None  # title -> {"limit": float, "expires": epoch|None}
)


def _budget_overrides_path() -> str:
    return os.path.join(config.GetConfigDir(), "budget_overrides.json")


def _budget_overrides() -> Dict[str, dict]:
    """In-memory override map (loaded once from disk). Callers must not mutate it
    outside the lock; use :func:`_set_budget_override` / :func:`_forget_budget`."""
    global _BUDGET_OV
    if _BUDGET_OV is None:
        try:
            with open(_server()._budget_overrides_path()) as f:
                d = json.load(f)
            _BUDGET_OV = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _BUDGET_OV = {}
    return _BUDGET_OV


def _persist_budget_overrides() -> None:
    try:
        with open(_server()._budget_overrides_path(), "w") as f:
            json.dump(_BUDGET_OV or {}, f)
    except OSError as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save budget overrides: %v", err)


def _set_budget_override(title: str, limit: float, expires: Optional[float]) -> None:
    srv = _server()
    with _BUDGET_OV_LOCK:
        ov = srv._budget_overrides()
        ov[title] = {"limit": float(limit), "expires": expires}
        srv._persist_budget_overrides()
    _BUDGET_FIRED.pop(title, None)  # re-arm the "exceeded" notice for the new level


def _forget_budget(title: str) -> None:
    srv = _server()
    with _BUDGET_OV_LOCK:
        ov = srv._budget_overrides()
        if title in ov:
            ov.pop(title, None)
            srv._persist_budget_overrides()


def _effective_budget(title: str) -> float:
    """Cost ceiling in USD for this session (0 = no ceiling). An active raise
    (temporary until its expiry, or forever) overrides the global default."""
    srv = _server()
    base = srv._session_budget_usd()
    ov = srv._budget_overrides().get(title)
    if isinstance(ov, dict):
        exp = ov.get("expires")
        if exp is None or (isinstance(exp, (int, float)) and exp > time.time()):
            try:
                return float(ov.get("limit") or 0.0)
            except (TypeError, ValueError):
                return base
    return base


def _budget_status_for(title: str, cost: float) -> dict:
    """Budget snapshot for the UI given an already-computed session cost."""
    srv = _server()
    limit = srv._effective_budget(title)
    ov = srv._budget_overrides().get(title) or {}
    return {
        "cost": round(float(cost), 4),
        "base": srv._session_budget_usd(),
        "limit": limit,
        "expires": ov.get("expires") if isinstance(ov, dict) else None,
        "locked": bool(limit > 0 and cost >= limit),
    }


def _budget_locked(title: str) -> bool:
    """True when the session is at/over its effective budget (input blocked)."""
    srv = _server()
    limit = srv._effective_budget(title)
    if limit <= 0:
        return False
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return False
    try:
        cost = float(srv._session_tokens(inst).get("cost", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return False
    return cost >= limit
