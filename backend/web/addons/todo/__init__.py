"""To-do — the built-in extension (id ``todo``) for everything a PERSON has
to do by hand.

The owner runs many agent sessions at once; the small manual jobs they leave
behind (a Verify step to check, a route parked on them, a thing an agent asks
of them, a note to self) land in ONE list, each entry saying which session it
came from. This package is the backend half: ``store.py`` is the one store
(``~/.mindflock-assistant/todos.json`` + ``todos-history.json``, shared with
the Assistant, which edits the list by hand), ``auto.py`` adds and resolves
the items the server can see for itself, and this module is the manifest and
the ``/api/todo`` routes. The frontend is the plain ES module tree under
``backend/web/static/extensions/todo/``; the MCP tools (``add_todo`` …) speak
these same routes.

Every change, whoever made it, emits ``addon.todo.changed`` (``data:
{"reason", "id", "open", "needs_you"}``); clients refetch on it. Errors are
``{"error": "…"}`` with 400 (bad input), 404 (no such item) or 500 (the file
could not be read or written).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..base import (
    Addon,
    AppContext,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
)
from . import auto, store


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request):
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 — empty or not JSON
        return None


async def _run(fn, *args):
    """``fn(*args)`` off the loop; a broken file is a 500 with its reason."""
    try:
        return await asyncio.to_thread(fn, *args), None
    except store.StoreError as err:
        return None, _error(500, str(err))
    except OSError as err:
        return None, _error(500, "couldn't save the to-do list: %s" % err)


class TodoAddon(Addon):
    id = "todo"
    label = "To-do"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self.auto = auto.AutoSources()
        self._router = self._build_router()

    # --- manifest --------------------------------------------------------- #
    def extension(self) -> ExtensionSpec:
        return ExtensionSpec(
            module="/extensions/todo/index.js",
            bar_label="To-do",
            buttons=[
                ExtensionButton(
                    command="todo.open", label="Open", title="Open the to-do list"
                ),
                ExtensionButton(command="todo.add", label="Add", title="Add a to-do"),
            ],
            commands=[
                ExtensionCommand(
                    id="todo.open", title="To-do: Open the list", surface="list"
                ),
                ExtensionCommand(
                    id="todo.add", title="To-do: Add an item", surface="add"
                ),
                # Code-backed: the module opens the list on its history view.
                ExtensionCommand(id="todo.history", title="To-do: History"),
            ],
            surfaces=[
                ExtensionSurface(id="list", kind="pane", title="To-do"),
                ExtensionSurface(id="add", kind="dialog", title="Add a to-do"),
            ],
            stylesheet=True,
            api_version=1,
            badge="/api/todo/badge",
        )

    # --- lifecycle -------------------------------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        try:
            await asyncio.to_thread(store.archive_now)
        except Exception:  # noqa: BLE001 — a broken file must not stop the server
            pass
        self.auto.start(ctx.subscribe)

    async def on_shutdown(self, ctx: AppContext) -> None:
        self.auto.stop()

    # --- routes ----------------------------------------------------------- #
    @property
    def router(self) -> APIRouter:
        return self._router

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/todo")

        @router.get("")
        async def get_todos() -> JSONResponse:
            return JSONResponse(await asyncio.to_thread(store.snapshot))

        @router.post("/items")
        async def add_items(request: Request) -> JSONResponse:
            body = await _body(request)
            if not isinstance(body, dict):
                return _error(400, 'send an item, or {"items": [...]}')
            raw = body.get("items") if "items" in body else [body]
            if not isinstance(raw, list) or not raw:
                return _error(400, "items must be a non-empty list")
            if len(raw) > store.MAX_BATCH:
                return _error(400, "at most %d items at once" % store.MAX_BATCH)
            try:
                entries = [store.clean_new(r) for r in raw]
            except ValueError as err:
                return _error(400, str(err))
            out, err = await _run(store.add_items, entries)
            if err is not None:
                return err
            added, updated = out
            counts = store.counts(await asyncio.to_thread(store.list_items))
            return JSONResponse({"added": added, "updated": updated, "counts": counts})

        @router.patch("/items/{todo_id}")
        async def patch_item(todo_id: str, request: Request) -> JSONResponse:
            try:
                changes = store.clean_patch(await _body(request))
            except ValueError as err:
                return _error(400, str(err))
            item, err = await _run(store.patch_item, todo_id, changes)
            if err is not None:
                return err
            if item is None:
                return _error(404, "no such to-do")
            return JSONResponse({"todo": item})

        @router.delete("/items/{todo_id}")
        async def delete_item(todo_id: str) -> JSONResponse:
            ok, err = await _run(store.delete_item, todo_id)
            if err is not None:
                return err
            if not ok:
                return _error(404, "no such to-do")
            return JSONResponse({"ok": True})

        @router.post("/order")
        async def set_order(request: Request) -> JSONResponse:
            body = await _body(request)
            ids = body.get("ids") if isinstance(body, dict) else None
            if not isinstance(ids, list):
                return _error(400, "ids must be a list")
            todos, err = await _run(store.set_order, ids)
            if err is not None:
                return err
            return JSONResponse({"todos": todos})

        @router.get("/history")
        async def get_history(limit: str = "200", q: str = "") -> JSONResponse:
            try:
                n = int(limit)
            except ValueError:
                return _error(400, "limit must be a number")
            if n < 1:
                return _error(400, "limit must be at least 1")
            rows = await asyncio.to_thread(store.history, n, q)
            return JSONResponse({"history": rows})

        @router.post("/history/{todo_id}/restore")
        async def restore(todo_id: str) -> JSONResponse:
            item, err = await _run(store.restore, todo_id)
            if err is not None:
                return err
            if item is None:
                return _error(404, "no such history entry")
            return JSONResponse({"todo": item})

        @router.delete("/history/{todo_id}")
        async def forget(todo_id: str) -> JSONResponse:
            ok, err = await _run(store.forget, todo_id)
            if err is not None:
                return err
            if not ok:
                return _error(404, "no such history entry")
            return JSONResponse({"ok": True})

        @router.get("/badge")
        async def badge() -> JSONResponse:
            return JSONResponse(await asyncio.to_thread(store.badge))

        return router
