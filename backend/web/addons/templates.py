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

Two ways to start one WITHOUT the dialog — **Run now** (``POST
/api/templates/{name}/run``, also ``mindflock run <name>``) and an optional
per-template **schedule** (``{"at": "HH:MM", "days": "daily" | "weekdays"}``
or a cron expression, ``{"cron": "0 21 * * 1-5"}``) — live in
:mod:`backend.web.core.template_schedule`; both go through the same
``core.session_create`` path, so session creation still has one code path.
A template may also carry ``autopilot`` (``commit`` / ``push`` / ``pr``): how
far each run's session carries its work on its own.
"""

from __future__ import annotations

import asyncio
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
    # Same rule for the autopilot depth: only a template that has one stores it.
    try:
        depth = _schedule.normalize_autopilot(body.get("autopilot"))
    except ValueError:
        depth = ""
    if depth:
        out["autopilot"] = depth
    return out


# --------------------------------------------------------------------------- #
# Launch preconditions (round 5): said before a session is made, never after
# --------------------------------------------------------------------------- #
#: Fast-track depths that need GitHub to finish what they start.
_GITHUB_DEPTHS = ("pr", "merge")


def agent_of(tpl: dict) -> str:
    """The agent a template launches: its own ``program``, else the flock's
    default (what the create path would pick)."""
    program = str((tpl or {}).get("program") or "").strip()
    if program:
        return program
    try:
        from backend.web import server

        return str(server.ENGINE.default_program() or "").strip() or "claude"
    except Exception:  # noqa: BLE001 — no engine (CLI import): the registry default
        from backend import providers

        return providers.DEFAULT_PROVIDER


def _agent_name(program: str) -> str:
    """The word a person knows the agent by: the command's first token, as a
    file name ("codex", not "/opt/bin/codex --fast")."""
    head = (program or "").strip().split()
    return os.path.basename(head[0]) if head else ""


def _agent_installed(program: str) -> bool:
    """Whether ``program`` can launch on this machine. A registered agent is
    asked through its own binary resolution (settings/env override, the
    provider's ``binary_path``); anything else is a command, looked up as one.
    Never raises; an unknowable answer is "installed" (never block on a guess)."""
    from backend.web.core import settings_hooks

    head = (program or "").strip().split()
    if not head:
        return True
    try:
        from backend import providers

        if providers.get(head[0]) is not None:
            return settings_hooks.provider_installed(head[0])
        return bool(settings_hooks.installed_path(head[0]))
    except Exception:  # noqa: BLE001
        return True


def _github_connected() -> bool:
    """Whether a GitHub credential resolves (token, env, ``gh``). Never raises."""
    try:
        from backend.web.core import github_auth

        _source, tok = github_auth.resolve_full_chain()
        return bool(tok)
    except (
        Exception
    ):  # noqa: BLE001 — unknowable reads as connected: never block on a guess
        return True


def launch_refusal(tpl: dict) -> Optional[dict]:
    """Why ``tpl`` can't start a session right now, as the run route's 409 body
    ``{"error", "detail", "reason"}`` — ``None`` when it can. ``reason`` is
    ``"no_folder"`` or ``"agent_missing"``."""
    name = str((tpl or {}).get("name") or "").strip() or "this template"
    problem = _schedule.run_problem(tpl)
    if problem:
        return {
            "error": problem,
            "detail": "Couldn't start %s: it has no folder — edit it and pick one"
            % name,
            "reason": "no_folder",
        }
    program = agent_of(tpl)
    if not _agent_installed(program):
        agent = _agent_name(program)
        return {
            "error": "%s isn't installed" % agent,
            "detail": "Couldn't start %s: %s isn't installed" % (name, agent),
            "reason": "agent_missing",
            "agent": agent,
        }
    return None


def save_refusal(body: dict) -> Optional[dict]:
    """Why saving ``body`` would leave a template that can't do its job, as a
    409 body ``{"error", "detail", "reason"}`` naming the fix — ``None`` when it
    can be saved. A SCHEDULED template whose agent isn't installed would fail
    every night with nobody watching (``agent_missing``); a Fast-track of
    "Open a PR" / "Merge" can't finish without GitHub (``github_required``)."""
    if _schedule.schedule_or_none(body.get("schedule")):
        program = agent_of(body)
        if not _agent_installed(program):
            agent = _agent_name(program)
            return {
                "error": "%s isn't installed" % agent,
                "detail": "%s isn't installed — install it or pick another agent "
                "before scheduling this template" % agent,
                "reason": "agent_missing",
                "agent": agent,
            }
    depth = str(body.get("autopilot") or "").strip().lower()
    if depth in _GITHUB_DEPTHS and not _github_connected():
        return {
            "error": "Fast-track to a PR needs GitHub connected",
            "detail": "Fast-track “%s” needs GitHub — connect GitHub, or pick "
            "Commit or Push"
            % ("Merge when green" if depth == "merge" else "Open a PR"),
            "reason": "github_required",
        }
    return None


def record_refused_run(tpl: dict, refusal: dict, trigger: str = "manual") -> None:
    """Put a run that never started into the template's history as a FAILED
    run (``status: "failed"``, ``reason``), so the card says "Last run failed"
    and the top-bar badge counts it. Never raises."""
    import time as _time

    now = _time.time()
    err = str(refusal.get("error") or "")
    try:
        _schedule.record_run(
            _schedule._key(tpl.get("name")),
            {
                "at": now,
                "trigger": trigger,
                "status": "failed",
                "reason": str(refusal.get("reason") or ""),
                "error": err,
            },
            last_error=err,
            last_error_at=now,
            last_reason=str(refusal.get("reason") or ""),
        )
    except Exception:  # noqa: BLE001 — the refusal still answers
        pass


def _history_status(entry: dict) -> str:
    """One history entry's outcome: ``failed`` / ``skipped`` / ``started``."""
    if entry.get("status"):
        return str(entry["status"])
    if entry.get("error"):
        return "failed"
    if entry.get("skipped"):
        return "skipped"
    return "started"


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
            status = _schedule.status_for(items)
            # Every history entry says how it ended (``status``), and the row
            # says whether its newest run failed (``last_status``): the card's
            # "Last run failed: …" and the top-bar badge read these.
            for row in (status.get("runs") or {}).values():
                hist = [dict(h) for h in row.get("history") or []]
                for h in hist:
                    h["status"] = _history_status(h)
                row["history"] = hist
                row["last_status"] = hist[0]["status"] if hist else ""
                if hist and hist[0]["status"] == "failed":
                    row["last_reason"] = str(hist[0].get("reason") or "launch_failed")
            # ``agent_installed`` per template: a response field only, never
            # stored (the stored shape is what settings sync compares).
            shown = [
                {**t, "agent_installed": _agent_installed(agent_of(t))} for t in items
            ]
            return JSONResponse({"templates": shown, **status})

        # What a cron field means, as it is typed: its words and next runs
        # (or why it can't be saved). Read-only.
        @router.get("/cron-preview")
        def cron_preview(expr: str = "") -> JSONResponse:
            return JSONResponse(_schedule.preview(expr))

        # Run now: a fresh session from the template, its prompt already
        # sent — the same create path as the New dialog (no may_configure:
        # starting a session isn't configuring anything).
        #
        # A template that can't launch (no folder, its agent not installed) is
        # refused BEFORE a session is made — 409 ``{error, detail, reason}`` —
        # and the attempt goes into its history as a failed run. Before this,
        # Run now answered "Started …" while the session died on its own a
        # second later and the card still read as a success.
        @router.post("/{name}/run")
        async def run_template(name: str) -> JSONResponse:
            key = _schedule._key(name)
            tpl = next(
                (t for t in list_templates() if _schedule._key(t.get("name")) == key),
                None,
            )
            if tpl is not None:
                refusal = await asyncio.to_thread(launch_refusal, tpl)
                if refusal is not None:
                    await asyncio.to_thread(record_refused_run, tpl, refusal)
                    return JSONResponse(refusal, status_code=409)
            status, body = await _schedule.run_template(name)
            if status >= 400 and isinstance(body, dict) and not body.get("detail"):
                # The create's own refusal, said as one sentence for the toast.
                shown = str((tpl or {}).get("name") or name)
                err = str(body.get("error") or "it couldn't start").strip()
                body = {
                    **body,
                    "detail": (
                        "There's no template named %s." % shown
                        if status == 404
                        else "Couldn't start %s: %s" % (shown, err)
                    ),
                    "reason": body.get("reason")
                    or ("not_found" if status == 404 else "launch_failed"),
                }
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
            try:
                sched = _schedule.normalize_schedule(body.get("schedule"))
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            try:
                _schedule.normalize_autopilot(body.get("autopilot"))
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
            refusal = save_refusal(body)
            if refusal is not None:
                return JSONResponse(refusal, status_code=409)
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
