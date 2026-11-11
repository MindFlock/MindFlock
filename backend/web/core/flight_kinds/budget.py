"""The ``budget`` stop (docs/flights.md "Round 15"): a spending check on the
flight's session — dollars (the usage estimate the session's row shows),
tokens, or the flight's elapsed minutes.

At its place in the plan it passes at once when within budget and fails
"over budget: $5.20 of $5" otherwise (the kind's ``default_on_fail`` is
``you``, so the owner is perched with Done / Retry). With ``scope: flight``
(the default) it keeps watching once it has passed: the core asks its
:func:`_guard` before every later step is started or judged, and a trip fails
the CURRENT step with the budget's reason and the budget stop's ``on_fail``.

A trip records ``detail.tripped_at`` on the budget step (``{"usd": 5.2}``:
the figure it tripped at); the guard does not fire again on that figure
until it has grown by :data:`RETRIP` (10 %), so pressing Done / Retry on the
perched step carries on instead of tripping straight back.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from backend.web.core import flights as F
from backend.web.core.flight_kinds import Kind, register

SCOPES = ("flight", "here")
#: A tripped guard fires again once its figure has grown by this factor.
RETRIP = 1.10

#: (config key, observed key) in the order the receipt and a trip name them.
_MEASURES = (
    ("max_usd", "usd"),
    ("max_minutes", "elapsed_min"),
    ("max_tokens", "tokens"),
)


def _config(raw: dict, strict: bool, where: str) -> dict:
    usd = _money(raw.get("max_usd"))
    if strict and (usd is None or usd < 0):
        # Never saved as "no limit" (0) when the person typed a limit.
        raise F.PlanError(
            "max_usd on a Budget stop is an amount like 5 or $2.50 — 0 or empty "
            "for none (got %r)" % F._text(raw.get("max_usd"), 40),
            where + ".max_usd",
        )
    usd = usd or 0.0
    scope = F._text(raw.get("scope"), 8).lower() or "flight"
    if scope not in SCOPES:
        if strict:
            raise F.PlanError(
                "scope is flight (the whole route) or here", where + ".scope"
            )
        scope = "flight"
    # No limit at all is allowed (a block just placed): it passes, "no limit set".
    return {
        "max_usd": round(max(0.0, min(usd, 1e6)), 2),
        "max_minutes": F._int(raw.get("max_minutes"), 0, 0, 7 * 24 * 60),
        "max_tokens": F._int(raw.get("max_tokens"), 0, 0, 10**12),
        "scope": scope,
    }


def _money(value) -> Optional[float]:
    """A dollar limit: a number, or text like "5", "$2.50", "1,000"; 0 when
    empty; None when it can't be read."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = F._text(value, 40).replace(",", "").replace(" ", "")
    if text.startswith("$"):
        text = text[1:]
    if not text:
        return 0.0
    try:
        out = float(text)
    except ValueError:
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _title(config: dict) -> str:
    if config.get("max_usd"):
        return "Budget %s" % _usd(config["max_usd"])
    if config.get("max_minutes"):
        return "Budget %d min" % config["max_minutes"]
    if config.get("max_tokens"):
        return "Budget %s tokens" % _tokens(config["max_tokens"])
    return "Budget"


def _usd(v: float) -> str:
    v = float(v)
    return "$%d" % v if v == int(v) else "$%.2f" % v


def _tokens(n: float) -> str:
    n = float(n)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%dk" % (n // 1e3)
    return "%d" % n


def _figure(key: str, v: float) -> str:
    if key == "usd":
        return _usd(v)
    if key == "elapsed_min":
        return "%d min" % int(v)
    return _tokens(v)


def _of(key: str, v: float, limit: float) -> str:
    """ "$1.20 of $5", "12 min of 60", "1.2M of 2.0M"."""
    cap = "%d" % int(limit) if key == "elapsed_min" else _figure(key, limit)
    return "%s of %s" % (_figure(key, v), cap)


def _read(config: dict, seen: dict) -> list:
    """``[(observed key, figure, limit)]`` for every limit set, in order."""
    out = []
    for cfg_key, key in _MEASURES:
        limit = float(config.get(cfg_key) or 0)
        if limit > 0:
            out.append((key, float(seen.get(key) or 0.0), limit))
    return out


def _over(key: str, v: float, limit: float) -> str:
    return "over budget: " + _of(key, v, limit)


def _judge(flight, step, ss, obs, now) -> dict:
    seen = obs.get("budget")
    if not isinstance(seen, dict):
        return F._wait("reading the session's usage")
    rows = _read(step["config"], seen)
    for key, v, limit in rows:
        if v > limit:
            return {"action": "fail", "reason": _over(key, v, limit)}
    if not rows:
        return {"action": "done", "note": "no limit set"}
    key, v, limit = rows[0]
    receipt = _of(key, v, limit)
    return {"action": "done", "note": receipt, "detail": {"receipt": receipt}}


def _guard(flight, step, obs, now) -> Optional[dict]:
    if step["config"].get("scope") != "flight":
        return None
    seen = (obs.get("guard") or {}).get(step["id"])
    if not isinstance(seen, dict) or seen.get("failed"):
        return None
    tripped = flight["step_states"][step["id"]]["detail"].get("tripped_at") or {}
    for key, v, limit in _read(step["config"], seen):
        if v <= limit:
            continue
        last = float(tripped.get(key) or 0.0)
        if last and v < last * RETRIP:
            continue  # tripped here already; the owner carried on
        return {
            "action": "fail",
            "reason": _over(key, v, limit),
            "guard_detail": {"tripped_at": dict(tripped, **{key: round(v, 4)})},
        }
    return None


async def _run(flight, step, action, now) -> dict:
    return dict(action, detail={})


def _usage(title: str) -> dict:
    """The session's estimated USD + tokens from its transcripts (the same
    figures its row shows; :func:`session_stats._session_tokens`)."""
    from backend.web.core import flight_driver as D
    from backend.web.core import session_stats

    inst = D._server().ENGINE.instances.get(title) if title else None
    if inst is None:
        return {"usd": 0.0, "tokens": 0}
    t = session_stats._session_tokens(inst) or {}
    # Fresh tokens only: a cache READ re-reads the same context every turn
    # and would trip any token limit within minutes.
    tokens = sum(int(t.get(k) or 0) for k in ("in", "out", "cache_write"))
    return {"usd": float(t.get("cost") or 0.0), "tokens": tokens}


async def _observe(flight, step, ss, sess, now) -> dict:
    out = await asyncio.to_thread(_usage, flight["session"])
    started = float(flight.get("started") or 0.0)
    out["elapsed_min"] = max(0.0, now - started) / 60.0 if started else 0.0
    return out


KIND = register(
    Kind(
        kind="budget",
        spec={
            "label": "Budget",
            "actor": "robot",
            "summary": "Check the route is within a budget (dollars, minutes or "
            "tokens); checking the whole route, it keeps watching every later stop.",
            "default_title": "Budget",
            "family": "outside",
            "risk": [],
            "default_on_fail": "you",
            "fields": [
                F._f(
                    "max_usd",
                    "text",
                    "Most dollars (estimated)",
                    default="",
                    placeholder="e.g. 5 — empty or 0 = no limit",
                ),
                F._f("max_minutes", "int", "Most minutes running", default=0),
                F._f("max_tokens", "int", "Most tokens", default=0),
                F._f("scope", "select", "Checks", default="flight", options=SCOPES),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        needs_session=False,
        default_title=_title,
        guard=_guard,
    )
)
