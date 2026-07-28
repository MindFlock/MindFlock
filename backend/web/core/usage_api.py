"""Per-provider usage descriptors for ``/api/usage`` (the cost panel).

How each active CLI's usage is *paid for* — ``"metered"`` (own API key; dollar
estimates are real marginal spend) vs ``"windowed"`` (subscription plan; lead
with percent/reset) — and, for windowed providers, the state of the active
window (:func:`_usage_window_for`): live from the provider's own telemetry
when available, else the transcript-derived estimate measured against the
user's configured window budget.

Split out of ``backend.web.server`` (which re-imports these names — the
``/api/usage`` route and tests reference them through the server namespace).
"""

from __future__ import annotations

import time
from typing import Optional


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


_PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "aider": "Aider",
}


def _provider_label(name: str) -> str:
    name = name or ""
    return _PROVIDER_LABELS.get(name) or (name[:1].upper() + name[1:] if name else "")


def _usage_window_for(p) -> Optional[dict]:
    """The active usage window for provider ``p`` (or None) — the same
    computation the default provider has always used, factored out so it can run
    per-provider. ``None`` when the provider is metered, idle past its window, or
    has no MindFlock-managed window."""
    from backend.providers import usage_history

    if p.usage_mode() != "windowed":
        return None
    uw = p.usage_window() or {}
    kind = uw.get("kind") or ""
    if kind == "rolling" and uw.get("hours"):
        # The transcript estimate is derived from CLAUDE transcripts
        # (~/.claude*/projects); for any other rolling provider it would dress
        # Claude's activity up as that provider's window. So non-Claude
        # providers get a window only from their own usage_live() (Codex reads
        # its on-disk rate_limits snapshot).
        win = (
            usage_history.current_window(float(uw["hours"]))
            if p.name == "claude"
            else None
        )
        live = None
        try:
            live = p.usage_live()
        except Exception:  # noqa: BLE001 — live is enrichment only
            live = None
        # Engage the live reading when it carries ANY usable signal — a reset
        # time, a percent, or per-group quotas. Gating solely on ``end`` dropped
        # an otherwise-valid ``percent_used`` (a build that reports utilization
        # but no reset field) back to the transcript estimate, blanking the
        # window pill for codex/antigravity despite real live data.
        if live and (
            live.get("end")
            or live.get("percent_used") is not None
            or live.get("groups")
        ):
            win = dict(win or {})
            win["source"] = "live"
            if live.get("end"):
                win["end"] = live["end"]
            win["percent_used"] = live.get("percent_used")
            win.setdefault("budget", 0.0)
            if live.get("weekly"):
                win["weekly"] = live["weekly"]
            if live.get("groups"):
                win["groups"] = live["groups"]
            if live.get("extra"):
                win["extra"] = live["extra"]
            if live.get("plan"):
                win["plan"] = live["plan"]
            return win
        if win:
            win["source"] = "estimate"
            budget = _server()._window_budget_usd()
            win["budget"] = budget
            win["percent_used"] = (
                min(100.0, round(100.0 * win["cost"] / budget, 1))
                if budget > 0
                else None
            )
            return win
        return None
    if kind == "daily":
        # Fixed per-day quota: countdown-only (request-count quotas aren't
        # $-meterable). Local midnight anchors the day for daily-quota CLIs.
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return {
            "anchor": midnight,
            "end": midnight + 86400.0,
            "cost": None,
            "tokens": None,
            "budget": 0,
            "percent_used": None,
        }
    return None


def _provider_usage_entry(p) -> dict:
    """Per-provider usage descriptor for the ``providers`` list in /api/usage."""
    srv = _server()
    entry = {
        "name": p.name,
        "label": srv._provider_label(p.name),
        "mode": "metered",
        "window": None,
        "window_note": "",
        "periods": None,
    }
    try:
        entry["mode"] = p.usage_mode()
        entry["window_note"] = (p.usage_window() or {}).get("note") or ""
        entry["window"] = srv._usage_window_for(p)
    except Exception:  # noqa: BLE001 — a provider's usage is enrichment only
        pass
    try:
        entry["periods"] = p.usage_periods()
    except Exception:  # noqa: BLE001 — history is enrichment only
        entry["periods"] = None
    return entry
