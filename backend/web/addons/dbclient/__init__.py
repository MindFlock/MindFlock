"""Database Client — the first Addon API v3 extension (id ``dbclient``).

A DBeaver-style client inside MindFlock: connection profiles (SQLite,
PostgreSQL, MySQL), a lazy schema tree, an editable table grid and a SQL
query pad. This package is the backend half; the frontend is the plain ES
module tree under ``backend/web/static/extensions/dbclient/`` that the host
imports on first use. The two meet in :meth:`DbClientAddon.extension` — the
static manifest (bar, buttons, commands, surfaces) the host renders without
running any extension code — and in the REST shapes under ``/api/dbclient``
that the JS calls.

Layout: ``store.py`` keeps the profiles (own 0600 file, never settings.json),
``adapters.py`` speaks each engine's dialect, ``service.py`` is the single
execution chokepoint (pool + locks, statement guards, value codec, identifier
validation). This module only assembles the router: every handler is
``async def`` and pushes adapter work through ``asyncio.to_thread`` (the
doctor/assistant precedent) so a slow database never stalls the event loop.

Error shapes, for the JS on the other side: a bad request is a 400
``{"error": …}`` (plus ``"field"`` for profile validation); an unknown
connection is a 404; a database failure while introspecting is a 502; and a
database failure while running SQL is a 200 ``{"ok": false, "error": …}`` so
the query pad shows it inline like any other result.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..base import (
    Addon,
    AppContext,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
)
from . import store
from .adapters import DriverMissing, driver_report
from .service import DbClientService, RequestError, content_disposition, error_text


class _NotFound(LookupError):
    """No stored profile has that id."""


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class DbClientAddon(Addon):
    id = "dbclient"
    label = "Database Client"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self.service = DbClientService()
        self._router = self._build_router()

    # --- manifest --------------------------------------------------------- #
    def extension(self) -> ExtensionSpec:
        return ExtensionSpec(
            module="/extensions/dbclient/index.js",
            bar_label="Database",
            buttons=[
                ExtensionButton(
                    command="dbclient.explorer",
                    label="Explorer",
                    title="Browse connections, tables and data",
                ),
                ExtensionButton(
                    command="dbclient.sql",
                    label="SQL",
                    title="Open the SQL query pad",
                ),
            ],
            commands=[
                # Declarative: the host opens the dialog before the module loads.
                ExtensionCommand(
                    id="dbclient.explorer", title="Database: Explorer", surface="main"
                ),
                ExtensionCommand(
                    id="dbclient.add-connection",
                    title="Database: Add connection",
                    surface="main",
                    ref="new",
                ),
                # Code-backed: focus-or-open vs always-new are module decisions.
                ExtensionCommand(id="dbclient.sql", title="Database: SQL query pad"),
                ExtensionCommand(id="dbclient.new-query", title="Database: New query"),
            ],
            surfaces=[
                ExtensionSurface(id="main", kind="dialog", title="Database Client"),
                ExtensionSurface(
                    id="query",
                    kind="pane",
                    title="SQL",
                    multi=True,
                    back_command="dbclient.explorer",
                ),
                ExtensionSurface(
                    id="table",
                    kind="pane",
                    title="Table",
                    multi=True,
                    back_command="dbclient.explorer",
                ),
            ],
            stylesheet=True,
            api_version=1,
        )

    # --- lifecycle -------------------------------------------------------- #
    async def on_shutdown(self, ctx: AppContext) -> None:
        await asyncio.to_thread(self.service.close_all)

    # --- routes ------------------------------------------------------------ #
    @property
    def router(self) -> APIRouter:
        return self._router

    def _profile(self, cid: str) -> dict:
        profile = store.get_profile(cid)
        if profile is None:
            raise _NotFound("no connection with id %r" % cid)
        return profile

    def _emit_query(self, cid: str, result: dict) -> None:
        """``addon.dbclient.query`` — timing and outcome only, never SQL text."""
        if self.ctx is None:
            return
        try:
            self.ctx.emit(
                "dbclient.query",
                data={
                    "connection": cid,
                    "elapsed_ms": result.get("elapsed_ms"),
                    "ok": bool(result.get("ok")),
                },
            )
        except Exception:  # noqa: BLE001 — the bus must never break a response
            pass

    async def _run(
        self, cid: str, fn: Callable[..., Any], *args: Any, sql_shape: bool = False
    ) -> Response:
        """Resolve the profile, run ``fn(profile, *args)`` off the loop, and map
        failures to the error shapes documented at the top of this module.
        ``sql_shape`` selects the 200 ``{ok: false}`` form for engine errors."""
        try:
            profile = self._profile(cid)
            result = await asyncio.to_thread(fn, profile, *args)
        except _NotFound as err:
            return _error(404, str(err))
        except RequestError as err:
            return _error(400, str(err))
        except DriverMissing as err:
            if sql_shape:
                return JSONResponse(
                    {"ok": False, "error": str(err), "install_hint": err.install_hint}
                )
            return _error(400, str(err), install_hint=err.install_hint)
        except Exception as err:  # noqa: BLE001 — every driver has its own errors
            if sql_shape:
                return JSONResponse({"ok": False, "error": error_text(err)})
            return _error(502, error_text(err))
        return JSONResponse(result)

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/dbclient")

        @router.get("/drivers")
        async def get_drivers() -> JSONResponse:
            return JSONResponse({"drivers": await asyncio.to_thread(driver_report)})

        # --- profiles ----------------------------------------------------- #
        @router.get("/connections")
        async def list_connections() -> JSONResponse:
            profiles = await asyncio.to_thread(store.load_profiles)
            return JSONResponse({"connections": [store.masked(p) for p in profiles]})

        @router.post("/connections")
        async def save_connection(body: dict) -> JSONResponse:
            raw = (
                body.get("connection")
                if isinstance(body.get("connection"), dict)
                else body
            )
            try:
                existing = (
                    store.get_profile(str(raw.get("id", "") or ""))
                    if isinstance(raw, dict)
                    else None
                )
                clean = store.normalize_profile(raw, existing)
                await asyncio.to_thread(store.upsert_profile, clean)
            except store.ProfileError as err:
                return _error(400, str(err), field=err.field)
            # The saved profile may point somewhere else now: pooled connections
            # built from the old one are dead weight (and possibly wrong).
            await asyncio.to_thread(self.service.drop_profile, clean["id"])
            return JSONResponse({"ok": True, "connection": store.masked(clean)})

        @router.delete("/connections/{cid}")
        async def delete_connection(cid: str) -> JSONResponse:
            removed = await asyncio.to_thread(store.delete_profile, cid)
            if not removed:
                return _error(404, "no connection with id %r" % cid)
            await asyncio.to_thread(self.service.drop_profile, cid)
            return JSONResponse({"ok": True})

        @router.post("/connections/{cid}/test")
        async def test_connection(cid: str) -> Response:
            try:
                profile = self._profile(cid)
            except _NotFound as err:
                return _error(404, str(err))
            return JSONResponse(
                await asyncio.to_thread(self.service.test_connection, profile)
            )

        # --- introspection ------------------------------------------------ #
        @router.get("/connections/{cid}/tree")
        async def get_tree(cid: str, database: str = "", schema: str = "") -> Response:
            return await self._run(cid, self.service.tree, database, schema)

        @router.get("/connections/{cid}/table")
        async def get_table(
            cid: str, table: str = "", database: str = "", schema: str = ""
        ) -> Response:
            return await self._run(
                cid, self.service.table_info, database, schema, table
            )

        # --- SQL ----------------------------------------------------------- #
        @router.post("/connections/{cid}/query")
        async def post_query(cid: str, body: dict) -> Response:
            sql = str(body.get("sql") or "")
            if not sql.strip():
                return _error(400, "sql is required")

            def run(profile: dict) -> dict:
                result = self.service.run_sql(
                    profile,
                    sql,
                    database=body.get("database"),
                    schema=body.get("schema"),
                    max_rows=body.get("max_rows"),
                    timeout_s=body.get("timeout_s"),
                    confirm=bool(body.get("confirm")),
                )
                if not result.get("needs_confirm"):
                    self._emit_query(cid, result)
                return result

            return await self._run(cid, run, sql_shape=True)

        @router.post("/connections/{cid}/table-data")
        async def post_table_data(cid: str, body: dict) -> Response:
            return await self._run(cid, self.service.table_data, body, sql_shape=True)

        @router.post("/connections/{cid}/rows")
        async def post_rows(cid: str, body: dict) -> Response:
            return await self._run(cid, self.service.rows, body, sql_shape=True)

        # --- export -------------------------------------------------------- #
        @router.get("/connections/{cid}/export")
        async def get_export(
            cid: str,
            table: str = "",
            database: str = "",
            schema: str = "",
            format: str = "csv",
        ) -> Response:
            # A plain GET so the table pane's anchor streams the file to disk
            # with the session cookie riding along — no blob buffering.
            try:
                profile = self._profile(cid)
                plan = await asyncio.to_thread(
                    self.service.prepare_table_export,
                    profile,
                    database,
                    schema,
                    table,
                    format,
                )
            except _NotFound as err:
                return _error(404, str(err))
            except RequestError as err:
                return _error(400, str(err))
            except Exception as err:  # noqa: BLE001
                return _error(502, error_text(err))
            return StreamingResponse(
                self.service.stream_export(plan),
                media_type=plan.content_type,
                headers={"Content-Disposition": content_disposition(plan.filename)},
            )

        @router.post("/connections/{cid}/export")
        async def post_export(cid: str, body: dict) -> Response:
            sql = str(body.get("sql") or "")
            if not sql.strip():
                return _error(400, "sql is required")
            try:
                profile = self._profile(cid)
                res = await asyncio.to_thread(
                    self.service.export_sql,
                    profile,
                    sql,
                    body.get("database"),
                    body.get("schema"),
                    body.get("format"),
                    body.get("timeout_s"),
                )
            except _NotFound as err:
                return _error(404, str(err))
            except RequestError as err:
                return _error(400, str(err))
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": error_text(err)})
            if not res.get("ok"):
                # 200 + {ok:false}: the query pad sniffs this shape (a JSON
                # export and a JSON error share a content type).
                return JSONResponse(res)
            self._emit_query(cid, {"ok": True, "elapsed_ms": None})
            return Response(
                content=res["body"],
                media_type=res["content_type"],
                headers={"Content-Disposition": content_disposition(res["filename"])},
            )

        return router
