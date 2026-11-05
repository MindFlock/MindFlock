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


def _estimate_percent(win: Optional[dict]) -> Optional[float]:
    """Percent of the configured window budget the transcript estimate has
    spent, or None when there is no window or no budget to measure against."""
    if not win or win.get("cost") is None:
        return None
    budget = _server()._window_budget_usd()
    if budget <= 0:
        return None
    return min(100.0, round(100.0 * win["cost"] / budget, 1))


def _window_account(p) -> Optional[str]:
    """Which identity's transcripts the plan window should be estimated from.

    A plan window belongs to ONE subscription, and ``usage_live()`` reads
    whatever credentials the SERVER sees — the CLI's ambient login. With claude
    account profiles configured, an unscoped transcript estimate would price
    that plan's window with turns billed to a different account and anchor it
    on a message the plan never saw. Scope it to the ambient login so the
    dollar estimate and the live percentage describe the same subscription; the
    other identities have their own rows in the "By account" table.

    ``None`` (= the machine-wide walk, unchanged) when no account profile
    exists, which is every install that does not use the feature.
    """
    if p.name != "claude":
        return None
    try:
        from backend.providers import auth_profiles, usage_history

        if auth_profiles.claude_account_root_map():
            return usage_history.AMBIENT_ACCOUNT
    except Exception:  # noqa: BLE001 — scoping is enrichment only
        pass
    return None


#: The window note for a plan with a weekly limit and no 5-hour window.
WEEKLY_ONLY_NOTE = "Your plan has a weekly limit and no 5-hour window."

_WEEK = 7 * 86400.0


def _plan_shape(p) -> Optional[dict]:
    """The provider's remembered plan shape (``{"session": bool,
    "weekly_end": epoch|None}``), or None when unknown."""
    try:
        shape = p.usage_plan_shape()
    except Exception:  # noqa: BLE001 — shape memory is enrichment only
        return None
    return shape if isinstance(shape, dict) else None


def _weekly_estimate(p, weekly_end, now: Optional[float] = None) -> dict:
    """The WEEKLY window estimated from the record, for a plan with no 5-hour
    window: ends at the last known weekly reset rolled forward by whole weeks
    to the next one still ahead (None when no reset was ever read), and its
    cost/tokens are the rolling week's. Percent is unknown — the weekly cap's
    size is not something the transcripts can tell."""
    from backend.providers import usage_history

    now = time.time() if now is None else now
    end = None
    try:
        end = float(weekly_end) if weekly_end else None
    except (TypeError, ValueError):
        end = None
    if end:
        if end <= now:
            end += _WEEK * (int((now - end) // _WEEK) + 1)
    week: dict = {}
    try:
        account = _window_account(p)
        if account is not None:
            week = (usage_history.windows_by_account().get(account) or {}).get(
                "week"
            ) or {}
        else:
            week = (usage_history.windows() or {}).get("week") or {}
    except Exception:  # noqa: BLE001 — totals are enrichment only
        week = {}
    tokens = sum(
        int(week.get(k) or 0) for k in ("in", "out", "cache_read", "cache_write")
    )
    return {
        "anchor": (end - _WEEK) if end else None,
        "end": end,
        "cost": float(week.get("cost") or 0.0) if week else None,
        "tokens": tokens if week else None,
        "window_kind": "weekly",
    }


def _usage_window_for(p) -> Optional[dict]:
    """The active usage window for provider ``p`` (or None) — the same
    computation the default provider has always used, factored out so it can run
    per-provider. ``None`` when the provider is metered, idle past its window, or
    has no MindFlock-managed window.

    ``window_kind`` says which window the headline (``percent_used``/``end``)
    is: ``"session"`` (the 5-hour window, when the plan has one) or
    ``"weekly"`` (a plan with only a weekly limit — then no 5-hour window is
    ever modelled, live or estimated)."""
    from backend.providers import usage_history

    if p.usage_mode() != "windowed":
        return None
    uw = p.usage_window() or {}
    kind = uw.get("kind") or ""
    if kind == "rolling" and uw.get("hours"):
        live = None
        try:
            # The display reading (may be stale through a 429 backoff; then
            # it says so) — providers without one fall back to usage_live().
            display = getattr(p, "usage_live_display", None)
            live = display() if callable(display) else p.usage_live()
        except Exception:  # noqa: BLE001 — live is enrichment only
            live = None
        usable = bool(
            live
            and (
                live.get("end")
                or live.get("percent_used") is not None
                or live.get("groups")
            )
        )
        shape = _plan_shape(p)
        weekly_only = (
            live.get("window_kind") == "weekly"
            if usable
            else bool(shape and shape.get("session") is False)
        )
        # The transcript estimate is derived from CLAUDE transcripts
        # (~/.claude*/projects); for any other rolling provider it would dress
        # Claude's activity up as that provider's window. So non-Claude
        # providers get a window only from their own usage_live() (Codex reads
        # its on-disk rate_limits snapshot). A weekly-only plan is estimated
        # as its WEEK — chaining 5-hour windows from the first message is what
        # showed "resets at 3pm" on a plan that has no such window.
        if p.name != "claude":
            win = None
        elif weekly_only:
            wk_end = ((live or {}).get("weekly") or {}).get("end") if usable else None
            win = _weekly_estimate(p, wk_end or (shape or {}).get("weekly_end"))
        else:
            win = usage_history.current_window(float(uw["hours"]), _window_account(p))
        # Engage the live reading when it carries ANY usable signal — a reset
        # time, a percent, or per-group quotas. Gating solely on ``end`` dropped
        # an otherwise-valid ``percent_used`` (a build that reports utilization
        # but no reset field) back to the transcript estimate, blanking the
        # window pill for codex/antigravity despite real live data.
        if usable:
            win = dict(win or {})
            win["source"] = "live"
            win["window_kind"] = "weekly" if weekly_only else "session"
            # A STALE reading (served through a backoff) whose headline window
            # has reset since it was taken describes a window that is over: keep
            # the estimate's end (the weekly one is rolled forward) and drop the
            # old percent rather than show "resets now" at yesterday's figure.
            try:
                expired = bool(
                    live.get("stale")
                    and live.get("end")
                    and float(live["end"]) <= time.time()
                )
            except (TypeError, ValueError):
                expired = False
            if live.get("end") and not expired:
                win["end"] = live["end"]
            win["percent_used"] = None if expired else live.get("percent_used")
            # A live reading can carry a reset time but no utilization (an
            # endpoint shape without it, or a window the provider reports only a
            # deadline for). Taking it verbatim then blanked a percentage the
            # transcript estimate could still supply — the UI showed a reset
            # countdown with no "% used" row at all. Fall back to the estimate
            # and say so, rather than showing nothing. (The budget is a 5-hour
            # one: it says nothing about a weekly cap.)
            if (
                win["percent_used"] is None
                and not live.get("groups")
                and not weekly_only
            ):
                estimate_pct = _estimate_percent(win)
                if estimate_pct is not None:
                    win["percent_used"] = estimate_pct
                    win["source"] = "estimate"
            win.setdefault("budget", 0.0)
            for key in (
                "session",
                "weekly",
                "weekly_opus",
                "weekly_models",
                "groups",
                "extra",
                "plan",
            ):
                if live.get(key):
                    win[key] = live[key]
            # How old the shared reading is: through a 429 backoff the last
            # good reading keeps being served, and the UI says "as of …".
            for key in ("fetched_at", "stale", "backoff_until"):
                if key in live:
                    win[key] = live[key]
            return win
        if win:
            win["source"] = "estimate"
            if weekly_only:
                win["window_kind"] = "weekly"
                win["budget"] = 0.0
                win["percent_used"] = None
            else:
                win["window_kind"] = "session"
                win["budget"] = _server()._window_budget_usd()
                win["percent_used"] = _estimate_percent(win)
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


def _account_usage_entries(p) -> Optional[list]:
    """Per-ACCOUNT period totals for provider ``p``, or None.

    Only claude has per-account transcript attribution today (each auth-profile
    account dir is its own scan root), and the breakdown only appears once at
    least one claude account profile exists — a single ambient identity has
    nothing to break down. Entries: ``{id, label, periods}``, ambient first.
    """
    if p.name != "claude":
        return None
    from backend.providers import auth_profiles, usage_history

    profiles = [
        pr
        for pr in auth_profiles.load_profiles()
        if pr.kind == "account" and pr.resolved_provider() == "claude"
    ]
    if not profiles:
        return None
    per = usage_history.windows_by_account()
    out = [
        {
            "id": auth_profiles.AMBIENT_ID,
            "label": "Default login",
            "periods": per.get(usage_history.AMBIENT_ACCOUNT) or None,
        }
    ]
    for pr in profiles:
        out.append(
            {
                "id": pr.id,
                "label": pr.display_label(),
                "periods": per.get(pr.id) or None,
            }
        )
    return out


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
        if (entry["window"] or {}).get("window_kind") == "weekly":
            entry["window_note"] = WEEKLY_ONLY_NOTE
    except Exception:  # noqa: BLE001 — a provider's usage is enrichment only
        pass
    try:
        entry["periods"] = p.usage_periods()
    except Exception:  # noqa: BLE001 — history is enrichment only
        entry["periods"] = None
    try:
        est = p.usage_estimated_before()
        if est:
            entry["estimated_before"] = est
    except Exception:  # noqa: BLE001 — the caveat is enrichment only
        pass
    try:
        accounts = _account_usage_entries(p)
        if accounts:
            entry["accounts"] = accounts
    except Exception:  # noqa: BLE001 — the breakdown is enrichment only
        pass
    return entry
