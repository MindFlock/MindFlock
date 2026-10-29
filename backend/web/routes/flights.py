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
    """``{"plans": [Plan]}``, built-ins first."""
    return JSONResponse({"plans": _flights.list_plans()})


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


@router.post("/api/flights/start")
async def flights_start(payload: Optional[dict] = None) -> JSONResponse:
    """Take off → 202 ``{"flight"}``. ``title`` attaches to an existing
    session; without it the session is created from the plan. The flight
    records whatever ``subject`` it is given."""
    try:
        flight = await _driver.start(payload or {})
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight}, status_code=202)


@router.get("/api/flights/{flight_id}")
def flights_get(flight_id: str) -> JSONResponse:
    f = _flights.get(flight_id)
    if f is None:
        return JSONResponse({"error": "unknown_flight"}, status_code=404)
    return JSONResponse({"flight": f})


@router.delete("/api/flights/{flight_id}")
def flights_delete(flight_id: str) -> Response:
    """204 (a flight still in the air must be cancelled first: 409)."""
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
