"""Workflow Optimizer — an Addon API v3 extension (id ``workflow``).

Where does the time go between starting a piece of work and the commit that
finishes it? The extension records every session's phase — setup, agent
working, waiting on your answer, your turn, pre-commit hooks, recovering from
failed hooks, usage limit, and after the commit: committed-not-pushed,
pushed-no-PR, PR open — plus each branch's commits, and turns that into a
per-unit breakdown (first real work → the commit), the median time to
commit, which phase is the bottleneck and what to change about it.
By default the person's own time (questions, your turn, idle while away) is
left out of the clock — "Include my time" in the dashboard puts it back
(:data:`analysis.PERSON_PHASES`; the setting is :func:`store.load_settings`).

Three parts, the same shape as the Usage Optimizer: :mod:`recorder` samples
the session rows (plus the bus's exact stage / merge moments) into
:mod:`store`, the extension's own JSONL file, and :mod:`analysis` (pure)
builds the report. This module is the manifest and the routes under
``/api/workflow``; the UI is the ES module under
``backend/web/static/extensions/workflow/``.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

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
from . import analysis, recorder, store

__all__ = ["WorkflowAddon", "build_summary", "build_item"]

RANGES = {"7d": 7 * 86400.0, "30d": 30 * 86400.0, "90d": 90 * 86400.0}


def _live_titles() -> List[str]:
    try:
        from backend.web.core import events

        return [
            str(r.get("title") or "")
            for r in events.sessions_snapshot() or []
            if isinstance(r, dict)
        ]
    except Exception:  # noqa: BLE001
        return []


def _rows(period: float, now: float) -> List[dict]:
    # A work item can start well before the range it ends in: read a margin.
    return store.read(since=now - period - 30 * 86400.0)


def _include_me(override: Optional[bool]) -> bool:
    """The explicit override when given, else the saved setting."""
    if override is not None:
        return bool(override)
    return bool(store.load_settings()["include_me"])


def _flag(raw: Optional[str]) -> Optional[bool]:
    """A query-string bool (``1``/``true``/``on``, ``0``/``false``/``off``);
    anything else is no override."""
    v = str(raw or "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return None


def build_summary(
    range_key: str = "7d",
    now: Optional[float] = None,
    include_me: Optional[bool] = None,
) -> dict:
    """The dashboard payload (items without their timelines — those come one
    at a time from :func:`build_item`). ``include_me`` ``None`` = the saved
    setting; the answer says which it used."""
    now = time.time() if now is None else now
    range_key = range_key if range_key in RANGES else "7d"
    period = RANGES[range_key]
    mine = _include_me(include_me)
    rep = analysis.report(_rows(period, now), now, period, _live_titles(), mine)
    rep["range"] = range_key
    rep["include_me"] = mine
    rep["items"] = [
        {k: v for k, v in it.items() if k != "timeline"} for it in rep["items"]
    ]
    rep["store"] = store.path()
    return rep


def build_item(
    item_id: str, now: Optional[float] = None, include_me: Optional[bool] = None
) -> Optional[dict]:
    """One work item with its phase timeline (``None`` when unknown) —
    compressed, without the person's phases, unless their time counts."""
    now = time.time() if now is None else now
    rows = store.read(since=now - store.RETENTION_S)
    mine = _include_me(include_me)
    for it in analysis.build_items(rows, now, _live_titles(), mine):
        if it["id"] == item_id:
            return it
    return None


class WorkflowAddon(Addon):
    id = "workflow"
    label = "Workflow Optimizer"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()
        self._unsub = None

    # --- manifest --------------------------------------------------------- #
    def extension(self) -> ExtensionSpec:
        return ExtensionSpec(
            module="/extensions/workflow/index.js",
            bar_label="Workflow",
            buttons=[
                ExtensionButton(
                    command="workflow.open",
                    label="Bottlenecks",
                    title="Where each unit of work's time goes, start to commit",
                ),
            ],
            commands=[
                ExtensionCommand(
                    id="workflow.open", title="Workflow: Optimizer", surface="main"
                ),
                ExtensionCommand(
                    id="workflow.window",
                    title="Workflow: Optimizer as a window",
                    surface="board",
                ),
            ],
            surfaces=[
                ExtensionSurface(id="main", kind="dialog", title="Workflow Optimizer"),
                ExtensionSurface(id="board", kind="pane", title="Workflow Optimizer"),
            ],
            stylesheet=True,
            api_version=1,
        )

    # --- lifecycle -------------------------------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        # Recording is off under pytest: tests drive Recorder.observe /
        # on_event directly, and the engine there is the developer's own.
        from backend.web.core import restart as _restart

        if _restart._under_pytest():
            return
        self._unsub = ctx.subscribe("*", recorder.RECORDER.on_event)
        ctx.register_task(recorder.loop())

    async def on_shutdown(self, ctx: AppContext) -> None:
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                pass
            self._unsub = None

    # --- routes ----------------------------------------------------------- #
    @property
    def router(self) -> APIRouter:
        return self._router

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/workflow")

        @router.get("/summary")
        async def summary(
            range: str = "7d",  # noqa: A002
            include_me: Optional[str] = None,
        ) -> JSONResponse:
            try:
                return JSONResponse(
                    await asyncio.to_thread(
                        build_summary, range, None, _flag(include_me)
                    )
                )
            except Exception as err:  # noqa: BLE001 — never 500 the dashboard
                return JSONResponse({"error": str(err), "items": []}, status_code=200)

        @router.get("/item")
        async def item(
            id: str = "",  # noqa: A002
            include_me: Optional[str] = None,
        ) -> JSONResponse:
            it = await asyncio.to_thread(build_item, id, None, _flag(include_me))
            if it is None:
                return JSONResponse({"error": "no such work item"}, status_code=404)
            return JSONResponse(it)

        @router.get("/settings")
        async def get_settings() -> JSONResponse:
            return JSONResponse(await asyncio.to_thread(store.load_settings))

        @router.post("/settings")
        async def post_settings(body: dict) -> JSONResponse:
            """Merge ``{include_me: bool}``; other keys / types are ignored."""
            try:
                return JSONResponse(await asyncio.to_thread(store.save_settings, body))
            except OSError as err:
                return JSONResponse({"error": str(err)}, status_code=500)

        return router
