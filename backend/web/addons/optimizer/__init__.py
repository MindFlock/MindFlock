"""Usage Optimizer — an Addon API v3 extension (id ``optimizer``).

Tracks how well the plan windows of every subscription CLI (Claude, Codex,
Antigravity) are being used and says what to do about it. The thesis: a plan
window is **use it or lose it** — a 5-hour window that resets at 40 % threw
60 % away — so the extension scores *capacity used* over time, charts the
sessions' uptime (working / idle / locked out) against whether a person was
at the keyboard, and turns the live burn rate into advice: "you are draining
faster than the window, switch routine work to a cheaper model" while you are
working, and "this window will reset half-empty, queue something" when you
are not.

Three parts: :mod:`sampler` records one sample per provider per minute (the
same window reading the cost panel shows, plus session counts and the
"someone typed recently" bit), :mod:`store` keeps them in the extension's
own JSONL file, and :mod:`analysis` (pure) turns them into the score, the
pace, the timeline and the advice. This module is the manifest and the
routes under ``/api/optimizer``; the UI is the ES module under
``backend/web/static/extensions/optimizer/``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..base import (
    Addon,
    AppContext,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
)
from . import analysis, sampler, store

__all__ = ["OptimizerAddon", "build_summary"]

_RANGES = {"24h": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}
_BUCKETS = {"24h": 900.0, "7d": 3600.0, "30d": 4 * 3600.0}


def build_summary(range_key: str = "7d", now: Optional[float] = None) -> dict:
    """The whole dashboard payload: one report per watched provider plus the
    headline score (the mean of the providers that have one)."""
    now = now or time.time()
    range_key = range_key if range_key in _RANGES else "7d"
    period = _RANGES[range_key]
    bucket = _BUCKETS[range_key]
    att = sampler.attended_now(now)
    reports = []
    rows = store.read(since=now - period - 2 * 86400.0)
    by_p: dict = {}
    for r in rows:
        by_p.setdefault(str(r.get("p")), []).append(r)
    for p in sampler.providers_watched():
        try:
            uw = p.usage_window() or {}
        except Exception:  # noqa: BLE001
            uw = {}
        try:
            mode = p.usage_mode()
        except Exception:  # noqa: BLE001
            mode = "metered"
        try:
            from backend.web.core.usage_api import _provider_label

            label = _provider_label(p.name)
        except Exception:  # noqa: BLE001
            label = p.name
        rep = analysis.provider_report(
            p.name,
            label,
            by_p.get(p.name, []),
            float(uw.get("hours") or 0.0),
            float(uw.get("weekly_hours") or 0.0),
            now,
            period,
            bucket,
            attended_now=att,
            mode=mode,
        )
        rep["window_note"] = str(uw.get("note") or "")
        reports.append(rep)
    scored = [
        r["score"]["score"] for r in reports if r["score"].get("score") is not None
    ]
    overall = round(sum(scored) / len(scored), 1) if scored else None
    return {
        "now": now,
        "range": range_key,
        "period_s": period,
        "bucket_s": bucket,
        "attended_now": att,
        "attended_s": analysis.ATTENDED_S,
        "overall": overall,
        "providers": reports,
        "store": store.path(),
    }


class OptimizerAddon(Addon):
    id = "optimizer"
    label = "Usage Optimizer"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    # --- manifest --------------------------------------------------------- #
    def extension(self) -> ExtensionSpec:
        return ExtensionSpec(
            module="/extensions/optimizer/index.js",
            bar_label="Usage",
            buttons=[
                ExtensionButton(
                    command="optimizer.open",
                    label="Optimizer",
                    title="Usage score, window pacing and uptime across providers",
                ),
            ],
            commands=[
                ExtensionCommand(
                    id="optimizer.open", title="Usage: Optimizer", surface="main"
                ),
                ExtensionCommand(
                    id="optimizer.window",
                    title="Usage: Optimizer as a window",
                    surface="board",
                ),
                ExtensionCommand(
                    id="optimizer.sample", title="Usage: Record a sample now"
                ),
            ],
            surfaces=[
                ExtensionSurface(id="main", kind="dialog", title="Usage Optimizer"),
                ExtensionSurface(id="board", kind="pane", title="Usage Optimizer"),
            ],
            stylesheet=True,
            api_version=1,
        )

    # --- lifecycle -------------------------------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        ctx.register_task(sampler.loop())

    # --- routes ----------------------------------------------------------- #
    @property
    def router(self) -> APIRouter:
        return self._router

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/optimizer")

        @router.get("/summary")
        async def summary(
            range: str = "7d",
        ) -> JSONResponse:  # noqa: A002 — the query key
            try:
                return JSONResponse(await asyncio.to_thread(build_summary, range))
            except Exception as err:  # noqa: BLE001 — never 500 the dashboard
                return JSONResponse(
                    {"error": str(err), "providers": []}, status_code=200
                )

        @router.post("/sample")
        async def sample() -> JSONResponse:
            try:
                written = await asyncio.to_thread(sampler.sample_once, None, True)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": str(err)}, status_code=200)
            return JSONResponse(
                {"ok": True, "written": len(written), "samples": written}
            )

        @router.get("/samples")
        async def samples(provider: str = "", since: float = 0.0) -> JSONResponse:
            rows = await asyncio.to_thread(store.read, provider or None, since or None)
            return JSONResponse({"samples": rows})

        return router
