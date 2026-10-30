"""Flights: ``/api/flights*`` (docs/flights.md "Contract: the HTTP API").

A plan is a row of blocks, a flight one run of it bound to one session. The
stores and the pure planner live in ``core.flights``; the loop, the step
executors and the operations these routes call live in ``core.flight_driver``;
the describe-a-step one-shot in ``core.flight_describe``. Static paths come
before ``/api/flights/{flight_id}`` so they are never read as an id.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from backend.web.core import flight_describe as _describe
from backend.web.core import flight_driver as _driver
from backend.web.core import flights as _flights

router = APIRouter()


def _err(err: _flights.FlightError) -> JSONResponse:
    return JSONResponse(
        {"error": err.error, "detail": err.detail}, status_code=err.status
    )


@router.get("/api/flights/kinds")
def flights_kinds() -> JSONResponse:
    """The step registry: ``{"kinds": [{kind, label, actor, summary, fields,
    default_title}], "on_fail"}``."""
    return JSONResponse(_flights.kinds_dto())


@router.get("/api/flights/plans")
def flights_plans() -> JSONResponse:
    """``{"plans": [Plan]}``, built-ins first; a scheduled plan carries
    ``runs`` (``template_schedule.flight_plans_status``)."""
    from backend.web.core import template_schedule as _schedule

    return JSONResponse({"plans": _schedule.flight_plans_status(_flights.list_plans())})


@router.post("/api/flights/plans")
def flights_plan_save(payload: Optional[dict] = None) -> JSONResponse:
    """Create (no ``id``) or replace (``id``) a plan → ``{"plan"}``; 400
    ``{"error", "field"}`` on a bad step."""
    try:
        plan = _flights.save_plan(payload or {})
    except _flights.PlanError as err:
        return JSONResponse({"error": err.message, "field": err.field}, status_code=400)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"plan": plan})


@router.delete("/api/flights/plans/{plan_id}")
def flights_plan_delete(plan_id: str) -> Response:
    """204; a built-in is reset to its default instead of removed."""
    if not _flights.delete_plan(plan_id):
        return JSONResponse({"error": "unknown_plan"}, status_code=404)
    return Response(status_code=204)


@router.post("/api/flights/plans/{plan_id}/reset")
def flights_plan_reset(plan_id: str) -> JSONResponse:
    """A built-in back to its default → ``{"plan"}`` (404 for anything else)."""
    plan = _flights.reset_plan(plan_id)
    if plan is None:
        return JSONResponse({"error": "not_builtin"}, status_code=404)
    return JSONResponse({"plan": plan})


@router.post("/api/flights/describe")
async def flights_describe(payload: Optional[dict] = None) -> JSONResponse:
    """``{text, want: "step"|"plan"}`` → ``{"steps", "model"}``; 409
    ``no_model``, 422 ``could_not_describe``."""
    body = payload or {}
    status, out = await asyncio.to_thread(
        _describe.safe_describe,
        str(body.get("text") or ""),
        str(body.get("want") or "step"),
    )
    return JSONResponse(out, status_code=status)


@router.get("/api/flights")
def flights_list(all: int = 0) -> JSONResponse:  # noqa: A002 — the query name
    """``{"flights"}``: unfinished + the last 20 finished (``?all=1``: all)."""
    return JSONResponse({"flights": _driver.list_dto(bool(all))})


#: Subject kinds that take off through an Intake start path.
_INTAKE_SUBJECTS = ("ticket", "pr", "issue")


async def _start_intake(body: dict, subject: dict) -> JSONResponse:
    """A ticket / PR / issue flight: the Intake start body for that kind
    (``ticket_start.launch`` / ``pr_review.launch`` / ``issue_start.launch``)
    with ``plan`` = the plan, which arms the flight on the deterministic title
    before the session exists → 202 ``{"flight", "title"}`` (the start's own
    refusals pass through as they are: 404 / 409 / 502)."""
    from backend.web import server as srv
    from backend.web.core import issue_start, pr_review, ticket_start

    plan_id = str(body.get("plan_id") or "")
    if _flights.get_plan(plan_id) is None:
        return _err(_flights.FlightError("unknown_plan", 404, "no such plan"))
    if not srv.git_available():
        return srv._no_git_response()
    kind = str(subject.get("kind") or "").strip().lower()
    source = str(subject.get("source") or "").strip()
    ident = str(subject.get("id") or "").strip()
    if not source or not ident:
        return _err(
            _flights.FlightError(
                "bad_subject", 400, "a %s subject needs its source and id" % kind
            )
        )
    try:
        if kind == "ticket":
            out = await ticket_start.launch(source, ident, plan=plan_id)
        else:
            try:
                number = int(ident.lstrip("#"))
            except ValueError:
                return _err(
                    _flights.FlightError(
                        "bad_subject", 400, "%s is not a number" % ident
                    )
                )
            launch = pr_review.launch if kind == "pr" else issue_start.launch
            out = await launch(source, number, plan=plan_id)
    except ticket_start.LaunchError as err:
        return JSONResponse(err.body, status_code=err.status)
    title = str(out.get("title") or "")
    return JSONResponse(
        {"flight": _flights.for_session(title), "title": title}, status_code=202
    )


def _bad_step(err: _flights.PlanError) -> JSONResponse:
    return JSONResponse(
        {"error": "bad_step", "detail": err.message, "field": err.field},
        status_code=400,
    )


async def _start_grounded(body: dict) -> JSONResponse:
    """``{grounded: true, plan_id? | steps?, name?}`` → 202 ``{"flight"}``: a
    row of stops with no session (state ``grounded``) that nothing drives
    until ``POST /api/flights/{id}/attach``."""
    if "steps" in body and not isinstance(body["steps"], list):
        return _bad_step(_flights.PlanError("steps is a list", "steps"))
    try:
        flight = await _driver.ground(body)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight}, status_code=202)


@router.post("/api/flights/start")
async def flights_start(payload: Optional[dict] = None) -> JSONResponse:
    """Take off → 202 ``{"flight", "absorbed"}``. ``title`` attaches to an
    existing session; without it the session is created from the plan.
    ``steps`` draws the flight here (after the plan's when ``plan_id`` comes
    too); the session's own running fast-track becomes its leading ship
    steps unless ``absorb_fasttrack`` is false. A ticket / PR / issue
    ``subject`` (``{kind, source, id}``) starts through Intake."""
    body = payload or {}
    if body.get("grounded"):
        return await _start_grounded(body)
    subject = body.get("subject")
    if (
        isinstance(subject, dict)
        and str(subject.get("kind") or "").strip().lower() in _INTAKE_SUBJECTS
    ):
        return await _start_intake(body, subject)
    if "steps" in body and not isinstance(body["steps"], list):
        return _bad_step(_flights.PlanError("steps is a list", "steps"))
    absorbed: list = []
    try:
        flight = await _driver.start(body, absorbed)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight, "absorbed": absorbed}, status_code=202)


@router.get("/api/flights/{flight_id}")
def flights_get(flight_id: str) -> JSONResponse:
    f = _flights.get(flight_id)
    if f is None:
        return JSONResponse({"error": "unknown_flight"}, status_code=404)
    return JSONResponse({"flight": f})


@router.delete("/api/flights/{flight_id}")
def flights_delete(flight_id: str) -> Response:
    """204 (a flight still in the air must be cancelled first: 409; a grounded
    one goes at once)."""
    try:
        _driver.remove(flight_id)
    except _flights.FlightError as err:
        return _err(err)
    return Response(status_code=204)


def _op(flight_id: str, op: str, step: str = "", note: str = "") -> JSONResponse:
    try:
        flight = _driver.operate(flight_id, op, step, note)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


def _change(
    flight_id: str, op: str, step: str = "", raw=None, at=None, group=None
) -> JSONResponse:
    try:
        flight = _driver.change_steps(flight_id, op, step, raw, at, group)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


@router.post("/api/flights/{flight_id}/steps")
def flights_step_add(flight_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """``{"step": Step, "at"?: int}`` → ``{"flight"}``: appended, or inserted
    at ``at`` (after the current step: 409 ``in_progress``); 409 ``finished``
    unless the flight is running, waiting on you, paused or grounded.
    ``{"plan_id", "at"?}`` adds that saved stop's steps as one new group (404
    ``unknown_plan``); ``{"steps": [Step], "at"?, "group"?: {name}}`` adds
    several, one new group when ``group`` is given."""
    body = payload or {}
    if body.get("plan_id"):
        return _change(
            flight_id, "add_plan", raw=str(body["plan_id"]), at=body.get("at")
        )
    if "steps" in body:
        if body.get("group") is not None and not isinstance(body["group"], dict):
            return _err(_flights.FlightError("bad_group", 400, "group is {name}"))
        return _change(
            flight_id,
            "add_steps",
            raw=body["steps"],
            at=body.get("at"),
            group=body.get("group"),
        )
    if not isinstance(body.get("step"), dict):
        return _bad_step(_flights.PlanError("step is an object", "step"))
    return _change(flight_id, "add", raw=body["step"], at=body.get("at"))


@router.patch("/api/flights/{flight_id}/steps/{step_id}")
def flights_step_edit(
    flight_id: str, step_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{title?, config?, on_fail?, halt?, group?}`` on a step still ahead →
    ``{"flight"}``; ``{halt}`` alone may also mark / clear the current step;
    ``group: {name}`` renames the step's group, ``null`` takes the step out."""
    return _change(flight_id, "edit", step_id, raw=payload or {})


@router.delete("/api/flights/{flight_id}/steps/{step_id}")
def flights_step_remove(flight_id: str, step_id: str, group: int = 0) -> JSONResponse:
    """A step out of the flight → ``{"flight"}``: one ahead just goes, a flown
    one shifts the cursor down, the CURRENT one pauses the flight right before
    the step that followed (``reason: "stop removed"``) and stops its side
    effects. ``?group=1`` removes every step of the step's group."""
    return _change(flight_id, "remove_group" if group else "remove", step_id)


@router.post("/api/flights/{flight_id}/group")
def flights_group(flight_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """``{"steps": [sid], "name"}`` → ``{"flight", "group"}``: steps side by
    side, each able to change, become one stop (409 ``in_progress``, 400
    ``bad_group``)."""
    body = payload or {}
    try:
        flight, group = _driver.group_steps(
            flight_id, body.get("steps"), body.get("name")
        )
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight, "group": group})


@router.post("/api/flights/{flight_id}/ungroup")
def flights_ungroup(flight_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """``{"group": gid}`` → ``{"flight"}``: every step of the group loses it
    (404 ``unknown_group``)."""
    return _change(flight_id, "ungroup", str((payload or {}).get("group") or ""))


@router.post("/api/flights/{flight_id}/attach")
async def flights_attach(
    flight_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{"title"}`` → ``{"flight"}``: a grounded flight bound to that live
    session takes off (404 ``unknown_session``; 409 ``in_flight`` /
    ``not_grounded``). ``{"title": "", "repo_path", "program"?, "model"?,
    "in_place"?, "prompt"?}`` creates the session first."""
    try:
        flight = await _driver.attach(flight_id, payload or {})
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


@router.post("/api/flights/{flight_id}/steps/{step_id}/move")
def flights_step_move(
    flight_id: str, step_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{"at": int}`` → ``{"flight"}``: a step still ahead moved to index
    ``at`` of the final list (past the end = last); the step and ``at`` both
    after the current step (409 ``in_progress``)."""
    return _change(flight_id, "move", step_id, at=(payload or {}).get("at"))


@router.post("/api/flights/{flight_id}/steps/{step_id}/done")
def flights_step_done(
    flight_id: str, step_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Done on a step waiting on you (a verify step's open human items are
    recorded as passed)."""
    return _op(flight_id, "done", step_id, str((payload or {}).get("note") or ""))


@router.post("/api/flights/{flight_id}/steps/{step_id}/skip")
def flights_step_skip(flight_id: str, step_id: str) -> JSONResponse:
    return _op(flight_id, "skip", step_id)


@router.post("/api/flights/{flight_id}/steps/{step_id}/retry")
def flights_step_retry(flight_id: str, step_id: str) -> JSONResponse:
    return _op(flight_id, "retry", step_id)


@router.post("/api/flights/{flight_id}/jump")
def flights_jump(flight_id: str, payload: Optional[dict] = None) -> JSONResponse:
    step = str((payload or {}).get("step") or "")
    if not step:
        return JSONResponse({"error": "step required"}, status_code=400)
    return _op(flight_id, "jump", step)


@router.post("/api/flights/{flight_id}/pause")
def flights_pause(flight_id: str) -> JSONResponse:
    return _op(flight_id, "pause")


@router.post("/api/flights/{flight_id}/resume")
def flights_resume(flight_id: str) -> JSONResponse:
    return _op(flight_id, "resume")


@router.post("/api/flights/{flight_id}/cancel")
def flights_cancel(flight_id: str) -> JSONResponse:
    return _op(flight_id, "cancel")
