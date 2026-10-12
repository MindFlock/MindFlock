"""Team runs + the outbox: ``/api/runs*`` and ``GET /api/outbox``.

Team runs are "work on these together" (docs/team-runs.md). The record and the
pure planner live in ``core.team_runs``; the loop, the side effects and the
operations these routes call live in ``core.team_run_driver``. A run never
commits, pushes or opens a PR itself — it arms each member's lane, and the
autopilot carries it out. ``GET /api/outbox`` is ``core.outbox``.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.web.core import outbox as _outbox
from backend.web.core import team_run_driver as _team_run_driver
from backend.web.core import team_runs as _team_runs
from backend.web.routes import _server

router = APIRouter()


def _run_error(err) -> JSONResponse:
    body = {"error": err.message}
    body.update(getattr(err, "extra", {}) or {})
    return JSONResponse(body, status_code=err.status)


@router.post("/api/runs/preview")
async def runs_preview(payload: Optional[dict] = None) -> JSONResponse:
    """Parse a goal into items — ``{"text", "repo_path", "program"}`` →
    ``{"items", "name_suggestion", "lane_default", "warnings"}``. No side
    effects. A ticket that does not resolve comes back with an ``error`` and
    is never silently turned into a task."""
    body = payload or {}
    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"error": "text must be a string"}, status_code=400)
    out = await _team_run_driver.preview(
        text[:20000], str(body.get("repo_path") or ""), str(body.get("program") or "")
    )
    return JSONResponse(out)


@router.post("/api/runs")
async def runs_create(payload: Optional[dict] = None) -> JSONResponse:
    """Create a group and start it → ``201 {"run": RunDTO, "warnings"}``.

    ``{"name", "items": [{"kind": "ticket", "source", "id"} | {"kind": "task",
    "text"}], "policy": {"lane", "ask_first", "grouping", "release"},
    "concurrency", "program", "repo_path", "budget_usd", "split"}``. An item
    whose session already exists is adopted (a warning), not a 409."""
    if not _server().git_available():
        return _server()._no_git_response()
    try:
        run, warnings = await _team_run_driver.create_run(payload or {})
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run, "warnings": warnings}, status_code=201)


@router.get("/api/runs")
def runs_list(active: int = 0) -> JSONResponse:
    """``{"runs": [RunSummary]}``, newest first (``?active=1``: unfinished)."""
    runs = _team_runs.list_runs(include_finished=not active)
    return JSONResponse({"runs": [_team_runs.summary_dto(r) for r in runs]})


@router.get("/api/runs/{run_id}")
async def runs_get(
    run_id: str, wait: float = 0.0, until: str = "change", rev: int = -1
) -> JSONResponse:
    """``{"run": RunDTO}``. With ``wait`` (seconds, ≤60) it long-polls until
    ``until`` holds — ``change`` (``rev`` moved past the given one),
    ``needs_you`` or ``done`` — and adds ``"reason"`` (that, or ``timeout``)."""
    try:
        if wait and wait > 0:
            if until not in ("change", "needs_you", "done"):
                return JSONResponse(
                    {"error": "until must be change, needs_you or done"},
                    status_code=400,
                )
            return JSONResponse(
                await _team_run_driver.wait_run(run_id, until, rev, wait)
            )
        run = await asyncio.to_thread(_team_run_driver.get_dto, run_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/pause")
def runs_pause(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """Hold the group: nothing new starts and nothing ships; agents keep
    working. ``{"reason": "user"}`` → ``{"run": RunSummary}``."""
    try:
        summary = _team_run_driver.pause(
            run_id, str((payload or {}).get("reason") or "user")
        )
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": summary})


@router.post("/api/runs/{run_id}/resume")
def runs_resume(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """Re-arm what the pause held. ``{"budget_usd": N}`` raises the budget in
    the same click (the bell's "Raise to $N"). → ``{"run": RunSummary}``."""
    try:
        summary = _team_run_driver.resume(run_id, (payload or {}).get("budget_usd"))
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": summary})


@router.post("/api/runs/{run_id}/cancel")
def runs_cancel(run_id: str) -> JSONResponse:
    """Stop starting new work and stop shipping. Sessions and branches are
    kept; queued tickets go back to ingestion. → ``{"run", "kept_sessions"}``."""
    try:
        summary, kept = _team_run_driver.cancel(run_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": summary, "kept_sessions": kept})


@router.post("/api/runs/{run_id}/tasks")
async def runs_add_tasks(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """Add lines to a group: ``{"items": [...]}`` (as at create) →
    ``{"run": RunDTO}``."""
    try:
        run = await _team_run_driver.add_tasks(run_id, (payload or {}).get("items"))
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/tasks/{task_id}/retry")
def runs_task_retry(
    run_id: str, task_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Retry a failed task, or one waiting on you: ``{"fresh": false}``
    re-arms (or re-creates) it; ``fresh: true`` starts ``<title>-2`` on a new
    branch and keeps the old one. 409 for any other state."""
    fresh = (payload or {}).get("fresh", False)
    if not isinstance(fresh, bool):
        return JSONResponse({"error": "fresh must be a boolean"}, status_code=400)
    try:
        task = _team_run_driver.retry(run_id, task_id, fresh)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"task": task})


@router.post("/api/runs/{run_id}/tasks/{task_id}/start-now")
def runs_task_start_now(run_id: str, task_id: str) -> JSONResponse:
    """Start a queued task on the next pass, past the concurrency cap once."""
    try:
        task = _team_run_driver.start_now(run_id, task_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"task": task})


@router.post("/api/runs/{run_id}/tasks/{task_id}/skip")
def runs_task_skip(run_id: str, task_id: str) -> JSONResponse:
    """Remove a queued task, or detach a running one (its session stays and
    its lane is unchanged)."""
    try:
        task = _team_run_driver.skip(run_id, task_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"task": task})


@router.post("/api/runs/{run_id}/adopt")
def runs_adopt(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """Add a live session to the group: ``{"title"}`` → ``{"task": Task}``;
    409 ``branch already in team run <name>`` when a team run owns its branch."""
    try:
        task = _team_run_driver.adopt(run_id, str((payload or {}).get("title") or ""))
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"task": task})


@router.post("/api/runs/{run_id}/plan")
async def runs_plan(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """A split's plan — the lead proposes it through its MCP tools, or you
    edit it: ``{"pieces": [{"title", "prompt", "paths": [...]}], "why",
    "from"?}`` → ``{"plan", "problems": []}``. 422 with ``problems`` (two
    pieces sharing a file, a piece wholly in a red zone, too many pieces…);
    409 when ``from`` is a session other than the lead, when the group is
    not a split or its plan is already approved."""
    try:
        out = await asyncio.to_thread(
            _team_run_driver.propose_plan, run_id, payload or {}
        )
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse(out)


@router.post("/api/runs/{run_id}/plan/approve")
async def runs_plan_approve(
    run_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Approve a split's plan: ``{"mode": "worktrees" | "same_folder"}``
    (default ``worktrees``), recorded on the run. ``worktrees``: every piece
    a worker of the lead in its own worktree, forked from its HEAD, fenced to
    its paths and merged back — a lead that works directly in its folder (or
    sits on its trunk) first gets a NEW lead of its own, started from its
    last commit (the original is never merged into, switched or pushed).
    ``same_folder``: every piece an extra agent in the lead's own folder,
    fenced to its paths per session; MindFlock commits each piece's paths
    when it is done (one commit per piece, nothing to merge).
    → ``{"run": RunDTO}``; 400 for another mode; 409 without a proposed
    plan, while the lead (or, for a new lead, the original) has uncommitted
    changes (``code: "lead_dirty"`` / ``"origin_dirty"``), or — same folder
    — while the lead's folder is on its trunk (``code: "trunk"``: start a
    branch there first, ``POST /lead/branch``)."""
    mode = (payload or {}).get("mode") or ""
    if not isinstance(mode, str):
        return JSONResponse({"error": "mode must be a string"}, status_code=400)
    try:
        run = await _team_run_driver.approve(run_id, mode)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/lead/branch")
def runs_lead_branch(run_id: str) -> JSONResponse:
    """ "Start a branch here first" — a split whose lead's folder is on its
    trunk, before "in this folder": ``git switch -c <prefix><lead>-split``
    in that folder (only on this click; uncommitted changes come along) and
    the group's branch is the new one. → ``{"run": RunDTO}``; 409 once the
    pieces started, when the lead is already on its own branch, or mid git
    operation."""
    try:
        run = _team_run_driver.lead_branch(run_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/plan/reject")
async def runs_plan_reject(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """ "Ask for a different split": ``{"note"?}`` is passed to the lead, which
    proposes again. → ``{"run": RunDTO}``."""
    try:
        run = await _team_run_driver.reject_plan(
            run_id, str((payload or {}).get("note") or "")
        )
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/integrated")
def runs_integrated(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """The lead merged a member by hand (it resolved a conflict):
    ``{"task_id", "head_sha", "from"?}`` → ``{"ok", "verified"}``. The server
    re-checks ancestry; ``verified: false`` keeps it in the merge queue."""
    body = payload or {}
    try:
        out = _team_run_driver.report_integrated(
            run_id,
            str(body.get("task_id") or ""),
            str(body.get("head_sha") or ""),
            body.get("from") or "",
        )
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse(out)


@router.post("/api/runs/{run_id}/check")
def runs_check(run_id: str) -> JSONResponse:
    """Run the check on a one-for-all group's merged branch again (409 unless
    it is checking). → ``{"run": RunDTO}``."""
    try:
        run = _team_run_driver.recheck(run_id)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": run})


@router.post("/api/runs/{run_id}/release")
def runs_release(run_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """Release a one-for-all group: open its ONE PR (title and body built
    from the pieces), or ``{"merge_when_green": true}`` to merge it once
    checks pass. Allowed only in ``release_ready``. → ``{"run": RunSummary}``."""
    merge = (payload or {}).get("merge_when_green", False)
    if not isinstance(merge, bool):
        return JSONResponse(
            {"error": "merge_when_green must be a boolean"}, status_code=400
        )
    try:
        summary = _team_run_driver.release(run_id, merge)
    except _team_run_driver.RunError as err:
        return _run_error(err)
    return JSONResponse({"run": summary})


@router.get("/api/outbox")
async def outbox(group: str = "all") -> JSONResponse:
    """What is waiting on you, shipping, shipped today and queued — for every
    session, de-duplicated on (repo, branch). ``?group=<run id>|own|all``."""
    return JSONResponse(await asyncio.to_thread(_outbox.outbox, group))
