"""Session Templates addon: reusable session recipes.

A template bundles the New-session inputs a user repeats — program, repo,
provisioning, and a seed prompt — under a name, so a power user launches a
familiar run in one click instead of re-filling the dialog. The New dialog's
Templates strip applies one (and remembers the last one used per repo, so
Name → Enter reuses it); a template that is only a prompt is what the
sidebar Prompts bar and the Prompt fold's "Saved prompt…" list show. The
list is one list everywhere: settings sync carries it between devices as the
``store:templates`` unit (:mod:`backend.web.core.sync_stores`).

Storage is a small JSON file (``~/.mindflock/session_templates.json``), atomic
writes, tolerant of a missing/corrupt file — the same pattern as
``web/core/prompt_queue.py``. This addon ONLY stores templates; launching a
session from the New dialog stays with the existing ``POST /api/instances``
(the frontend posts the template's fields there).

A template may also carry a **fast-track** (``fasttrack``: ``commit`` /
``push`` / ``pr`` / ``merge``, plus ``ask_first``), so a session started from it
— by hand or on its schedule — ships itself that far, e.g. a nightly chore that
ends in a PR. Off is stored as no key at all.

Two ways to start one WITHOUT the dialog — **Run now** (``POST
/api/templates/{name}/run``) and an optional per-template **schedule**
(``{"at": "HH:MM", "days": "daily" | "weekdays"}``) — live in
:mod:`backend.web.core.template_schedule`; both go through the same
``core.session_create`` path, so session creation still has one code path.
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.fsutil import atomic_write_json, read_json
from backend.config.config import GetConfigDir
from backend.web.core import auth as _web_auth
from backend.web.core import template_schedule as _schedule

from .base import Addon, AppContext, FrontendDescriptor

_FileName = "session_templates.json"
_LOCK = threading.Lock()
# Cap so a runaway client can't grow the file without bound.
_MAX_TEMPLATES = 100
_VALID_STRATEGIES = ("worktree", "clone")
# Sanity backstops so a bulk import can't bloat the store (rejected, not
# silently truncated — a truncated prompt would corrupt intent).
_MAX_NAME = 100
_MAX_PROMPT = 20000
_MAX_FIELD = 1000  # program / repo_path

# The New-session payload subset a template carries. Anything else in a POST
# body is ignored, so the stored shape stays stable as the create endpoint grows.
_STR_FIELDS = ("program", "repo_path", "prompt", "workspace_strategy")
_BOOL_FIELDS = ("provisioned", "in_place", "init_repo")


def templates_path() -> str:
    """Path to the template store.

    Honors ``$MINDFLOCK_TEMPLATES_FILE`` (tests point it at a tmp file);
    otherwise ``<config dir>/session_templates.json``.
    """
    env = os.environ.get("MINDFLOCK_TEMPLATES_FILE")
    if env:
        from backend.config.home_guard import guard

        return guard(env, "template store")
    return os.path.join(GetConfigDir(), _FileName)


def _load() -> List[dict]:
    data = read_json(templates_path(), {}, expect=dict)
    items = data.get("templates")
    return items if isinstance(items, list) else []


def _save(items: List[dict]) -> None:
    atomic_write_json(templates_path(), {"templates": items}, prefix=".tpl.")


def _clean(body: dict) -> dict:
    """Coerce a POST body to the stored template shape (known fields only)."""
    out = {"name": str(body.get("name", "") or "").strip()}
    for f in _STR_FIELDS:
        out[f] = str(body.get(f, "") or "").strip()
    for f in _BOOL_FIELDS:
        out[f] = bool(body.get(f, False))
    if out["workspace_strategy"] not in _VALID_STRATEGIES:
        out["workspace_strategy"] = "worktree"
    # Only a template WITH a schedule carries the key: a device that predates
    # schedules stores none, and settings sync must not read the two shapes
    # of "no schedule" as an edit.
    sched = _schedule.schedule_or_none(body.get("schedule"))
    if sched:
        out["schedule"] = sched
    # Fast-track: how far a session started from this template ships itself
    # (commit / push / pr / merge), plus "ask me before it ships". Same rule
    # as the schedule — only a template that HAS one carries the keys, so an
    # older device's template and settings sync see no edit.
    lane = _schedule.template_lane(body)
    if lane:
        out["fasttrack"] = lane
        if body.get("ask_first") is True:
            out["ask_first"] = True
    return out


def list_templates() -> List[dict]:
    with _LOCK:
        return _load()


def save_template(body: dict) -> dict:
    """Upsert a template by name (case-insensitive match). Returns the saved one."""
    tpl = _clean(body)
    with _LOCK:
        items = _load()
        key = tpl["name"].lower()
        replaced = False
        for i, existing in enumerate(items):
            if str(existing.get("name", "")).lower() == key:
                items[i] = tpl
                replaced = True
                break
        if not replaced:
            items.append(tpl)
        if len(items) > _MAX_TEMPLATES:
            items = items[-_MAX_TEMPLATES:]
        _save(items)
    return tpl


def delete_template(name: str) -> bool:
    key = (name or "").strip().lower()
    with _LOCK:
        items = _load()
        kept = [t for t in items if str(t.get("name", "")).lower() != key]
        if len(kept) == len(items):
            return False
        _save(kept)
    return True


class TemplatesAddon(Addon):
    id = "templates"
    label = "Templates"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/templates")

        @router.get("")
        def get_templates() -> JSONResponse:
            items = list_templates()
            # ``runs``: next/last run per template (by lowercased name), from
            # this device's run record; ``runs_here`` / ``runs_on``: whether
            # schedules fire here or on which other device.
            return JSONResponse({"templates": items, **_schedule.status_for(items)})

        # Run now: a fresh session from the template, its prompt already
        # sent — the same create path as the New dialog (no may_configure:
        # starting a session isn't configuring anything).
        @router.post("/{name}/run")
        async def run_template(name: str) -> JSONResponse:
            status, body = await _schedule.run_template(name)
            return JSONResponse(body, status_code=status)

        # A template names a program and a prompt that start sessions, and
        # settings sync spreads templates: saving or deleting one is the
        # owner's (auth.may_configure).
        @router.post("")
        def post_template(
            body: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            if not allowed:
                return _web_auth.configure_refused()
            body = body or {}
            name = str(body.get("name", "") or "").strip()
            if not name:
                return JSONResponse(
                    {"error": "template name is required"}, status_code=400
                )
            strategy = str(body.get("workspace_strategy", "") or "").strip()
            if strategy and strategy not in _VALID_STRATEGIES:
                return JSONResponse(
                    {"error": "workspace_strategy must be 'worktree' or 'clone'"},
                    status_code=400,
                )
            if len(name) > _MAX_NAME:
                return JSONResponse(
                    {"error": f"name too long (max {_MAX_NAME} chars)"}, status_code=400
                )
            if len(str(body.get("prompt", "") or "")) > _MAX_PROMPT:
                return JSONResponse(
                    {"error": f"prompt too long (max {_MAX_PROMPT} chars)"},
                    status_code=400,
                )
            for f in ("program", "repo_path"):
                if len(str(body.get(f, "") or "")) > _MAX_FIELD:
                    return JSONResponse(
                        {"error": f"{f} too long (max {_MAX_FIELD} chars)"},
                        status_code=400,
                    )
            raw_lane = body.get("fasttrack")
            if raw_lane not in (None, "") and not _schedule.lane_name(raw_lane):
                return JSONResponse(
                    {"error": "fasttrack must be one of: off, commit, push, pr, merge"},
                    status_code=400,
                )
            try:
                sched = _schedule.normalize_schedule(body.get("schedule"))
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            if sched:
                problem = _schedule.run_problem(body)
                if not problem and not str(body.get("prompt", "") or "").strip():
                    problem = "a scheduled template needs a prompt"
                if problem:
                    return JSONResponse(
                        {"error": "can't schedule it: " + problem}, status_code=400
                    )
            saved = save_template(body)
            return JSONResponse({"template": saved, "templates": list_templates()})

        @router.delete("/{name}")
        def del_template(
            name: str, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            if not allowed:
                return _web_auth.configure_refused()
            return JSONResponse({"deleted": delete_template(name)})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    async def on_startup(self, ctx: AppContext) -> None:
        # The schedule loop. Off under pytest: tests drive template_schedule.tick
        # with an injected clock, and the engine there is the developer's own.
        from backend.web.core import restart as _restart

        if _restart._under_pytest():
            return
        ctx.register_task(_schedule.run_loop())

    def frontend(self) -> List[FrontendDescriptor]:
        return [
            FrontendDescriptor(
                id="templates",
                label="Templates",
                where="dialog",
                api_base="/api/templates",
                order=35,
                # No module and no sidebar bar: the manager is the SPA's own
                # TemplatesDialog (New → Templates → Manage…, or the command
                # palette's "Templates…"), which talks to this router
                # directly. builtin_ui keeps slots.js from looking for one.
                builtin_ui=True,
            )
        ]
