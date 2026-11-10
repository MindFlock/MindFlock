"""Flights: ``/api/flights*`` (docs/flights.md "Contract: the HTTP API").

A plan is a row of blocks, a flight one run of it bound to one session. The
stores and the pure planner live in ``core.flights``; the loop, the step
executors and the operations these routes call live in ``core.flight_driver``;
the describe-a-step one-shot in ``core.flight_describe``. Static paths come
before ``/api/flights/{flight_id}`` so they are never read as an id.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from backend.web.core import auth as _web_auth
from backend.web.core import flight_describe as _describe
from backend.web.core import flight_driver as _driver
from backend.web.core import flight_intake_default as _intake_default
from backend.web.core import flight_kinds as _kinds
from backend.web.core import flight_triggers as _triggers
from backend.web.core import flight_when as _when
from backend.web.core import flights as _flights

router = APIRouter()
_log = logging.getLogger(__name__)


#: The body field a refusal is about, by its machine word, so the editor
#: (and an agent) can point at it. A ``FlightError`` that carries its own
#: ``field`` wins.
_ERROR_FIELD = {
    "unknown_plan": "plan_id",
    "unknown_session": "title",
    "in_flight": "title",
    "needs_title": "title",
    "no_repo": "repo_path",
    "unknown_step": "step",
    "in_progress": "step",
    "behind": "step",
    "not_waiting": "step",
    "bad_at": "at",
    "too_many_steps": "steps",
    "needs_steps": "steps",
    "bad_start_on": "start_on",
    "bad_group": "group",
    "unknown_group": "group",
    "bad_layout": "positions",
    "bad_edits_default": "edits_default",
    "bad_subject": "subject",
    "bad_name": "plan_name",
    "bad_parallel": "parallel",
}


def _err(err: _flights.FlightError, fields: Optional[dict] = None) -> JSONResponse:
    """``{error, detail, field}`` (``field`` is ``""`` when the refusal is
    about the route as a whole, not one of the body's fields; ``fields``
    overrides the word → field map for one route)."""
    field = str(
        getattr(err, "field", "")
        or (fields or {}).get(err.error)
        or _ERROR_FIELD.get(err.error, "")
    )
    return JSONResponse(
        {"error": err.error, "detail": err.detail, "field": field},
        status_code=err.status,
    )


@router.get("/api/flights/kinds")
def flights_kinds() -> JSONResponse:
    """The step registry: ``{"kinds": [{kind, label, actor, summary, fields,
    default_title}], "on_fail"}``."""
    return JSONResponse(_flights.kinds_dto())


@router.get("/api/flights/when")
def flights_when(expr: str = "") -> JSONResponse:
    """Validate a step's ``when`` for the editor → ``{ok, canonical, error,
    field, hint}`` (``field`` is ``when`` on an error; an empty expression
    is ok with an empty canonical form)."""
    try:
        canonical = _flights._normalize_when(expr, True, "")
    except _flights.PlanError as err:
        return JSONResponse(
            {
                "ok": False,
                "canonical": "",
                "error": err.message,
                "field": "when",
                "hint": _when._HINT,
            }
        )
    return JSONResponse({"ok": True, "canonical": canonical, "error": "", "field": ""})


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
        # ``error`` stays the sentence (the shelf reads it); ``detail`` is
        # the same sentence in the shape every other flights refusal has.
        return JSONResponse(
            {"error": err.message, "detail": err.message, "field": err.field},
            status_code=400,
        )
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


@router.post("/api/hooks/flyway/{token}")
async def flights_hook(token: str, request: Request) -> JSONResponse:
    """An outside system fires the flyway whose ``hook`` trigger minted
    ``token`` (public: auth._PUBLIC_FLYWAY_HOOK). The body — JSON with a
    ``title`` / ``text``, or any text — names the flight's subject. 202 with
    the flight; 404 for a token no plan holds, 429 inside the plan's quiet
    minutes, 409 while its previous flight is still in the air."""
    raw = await request.body()
    status, body = await _triggers.hook_fire(token, raw[: _triggers.HOOK_BODY_MAX])
    return JSONResponse(body, status_code=status)


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
        return _err(_flights.FlightError("unknown_plan", 404, "no such saved route"))
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
        {
            "error": getattr(err, "code", "") or "bad_step",
            "detail": err.message,
            "field": err.field,
        },
        status_code=400,
    )


async def _ground_editor(body: dict, ref: dict) -> dict:
    """:func:`flight_driver.ground` for a chain that edits ``ref``'s default
    stops: the same grounding, with ``edits_default`` stored on it."""
    plan = None
    plan_id = str(body.get("plan_id") or "")
    if plan_id:
        plan = _flights.get_plan(plan_id)
        if plan is None:
            raise _flights.FlightError("unknown_plan", 404, "no such saved route")
    flight = await asyncio.to_thread(
        _flights.ground,
        plan,
        body.get("steps"),
        str(body.get("name") or ""),
        str(body.get("by") or "user"),
        None,
        ref,
    )
    _driver._emit("flight.changed", flight)
    return flight


async def _start_grounded(body: dict) -> JSONResponse:
    """``{grounded: true, plan_id? | steps?, name?, edits_default?}`` → 202
    ``{"flight"}``: a row of stops with no session (state ``grounded``) that
    nothing drives until ``POST /api/flights/{id}/attach``. With
    ``edits_default`` (``{kind: tickets|prs|issues, target}``; 400
    ``bad_edits_default`` when malformed) it EDITS that source's default
    stops and is never attached (409 ``edits_default``)."""
    if "steps" in body and not isinstance(body["steps"], list):
        return _bad_step(_flights.PlanError("steps is a list", "steps"))
    ref = None
    if body.get("edits_default") is not None:
        ref = _flights.normalize_edits_default(body.get("edits_default"))
        if ref is None:
            return _err(
                _flights.FlightError(
                    "bad_edits_default",
                    400,
                    "edits_default is {kind: tickets|prs|issues, target}",
                )
            )
    try:
        if ref is not None:
            flight = await _ground_editor(body, ref)
        else:
            flight = await _driver.ground(body)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight}, status_code=202)


# --- The pre-flight check (``POST /api/flights/check``): what a start with
# the same body would fly, from the same strict path, without creating
# anything — so a dry run that says "ok" means the start will be accepted.


def _problem(field: str, detail: str, error: str = "", session: str = "") -> dict:
    out = {"field": str(field or ""), "detail": str(detail or "")}
    if error:
        out["error"] = str(error)
    if session:
        out["session"] = str(session)
    return out


def _plan_problem(err: _flights.PlanError) -> dict:
    return _problem(err.field, err.message, getattr(err, "code", "") or "bad_step")


def _flight_problem(err: _flights.FlightError) -> dict:
    field = str(getattr(err, "field", "") or _ERROR_FIELD.get(err.error, ""))
    return _problem(field, err.detail, err.error)


def _start_plan(body: dict) -> Tuple[dict, str, str, str]:
    """``(plan, title, start_at, start_on)`` for a start ``body``, built the
    way :func:`flight_driver.start` builds it (strict: an ad-hoc ``steps``
    list goes through :func:`flights.adhoc_plan`). Raises what a start
    raises for the same body (:class:`flights.PlanError` /
    :class:`flights.FlightError`)."""
    start_on = _flights.normalize_start_on(body.get("start_on"))
    plan_id = str(body.get("plan_id") or "")
    title = str(body.get("title") or "").strip()
    start_at = str(body.get("start_at") or "").strip()
    adhoc = body.get("steps") is not None
    plan = _flights.get_plan(plan_id) if plan_id or not adhoc else None
    if plan is None and (plan_id or not adhoc):
        raise _flights.FlightError("unknown_plan", 404, "no such saved route")
    if adhoc:
        if plan is None and not title and not str(body.get("repo_path") or "").strip():
            raise _flights.FlightError(
                "needs_title", 400, "an ad-hoc route needs a session (or a folder)"
            )
        plan = _flights.adhoc_plan(
            body.get("steps"), plan, "Route on %s" % title if title else ""
        )
    return plan, title, start_at, start_on


def _repo_for(body: dict, plan: dict, title: str) -> str:
    """The repo a start flies in: the session's, else the body's folder,
    else the saved route's."""
    if title:
        try:
            root = _driver._repo_root(title)
        except Exception:  # noqa: BLE001 — unknown repo: the checks that need it skip
            root = ""
        if root:
            return root
    return str(
        body.get("repo_path") or (plan.get("session") or {}).get("repo_path") or ""
    ).strip()


def _preflight(
    plan: dict, *, repo: str, session: str, attach: bool, start_at: str
) -> List[dict]:
    """The driver's pre-check of a route before take-off
    (``flight_driver.preflight``) as ``[{field, detail, session?, error?}]``;
    no such function (an older driver) or a check that broke = no problems."""
    fn = getattr(_driver, "preflight", None)
    if not callable(fn):
        return []
    try:
        found = fn(
            plan,
            repo=repo,
            session=session or None,
            attach=attach,
            start_at=start_at or None,
        )
    except Exception as err:  # noqa: BLE001 — a broken check never blocks a start
        _log.warning("flights preflight failed: %s", err)
        return []
    out = []
    for p in found or []:
        if isinstance(p, dict) and p.get("detail"):
            out.append(
                _problem(
                    str(p.get("field") or ""),
                    str(p["detail"]),
                    str(p.get("error") or ""),
                    str(p.get("session") or ""),
                )
            )
    return out


def _skip_reason(step: dict, start_at: str) -> str:
    if start_at:
        return "before the stop it starts at (%s)" % start_at
    if step.get("kind") == "agent":
        return (
            "on an existing session the work so far counts as this Agent stop: "
            "its prompt is not sent (start at %s to send it)" % step.get("id")
        )
    return "before where the route starts"


def _environments(repo: str, steps: List[dict]) -> Optional[List[str]]:
    """The repo's Verify environment names, when a Verify stop needs one."""
    if not repo or not any(s.get("kind") == "verify" for s in steps):
        return None
    try:
        from backend.web.core import test_plans as _tp

        return [str(e.get("name")) for e in _tp.environments(repo) if e.get("name")]
    except Exception:  # noqa: BLE001 — unreadable: say nothing
        return None


def _stop_ref(steps: List[dict], i: int) -> Optional[dict]:
    if not 0 <= i < len(steps):
        return None
    s = steps[i]
    return {"index": i, "id": s["id"], "kind": s["kind"], "title": s["title"]}


def _strict_refusal(raw, i: int, taken=None):
    """``(PlanError, incomplete)`` when ``raw`` (one stop) fails the strict
    check, else None. ``incomplete``: the refusal is about a required option
    left empty — what a block on the ground may wait to fill in."""
    try:
        _flights.normalize_step(
            copy.deepcopy(raw), i, strict=True, taken=set(taken or ())
        )
    except _flights.PlanError as err:
        m = re.match(r"^steps\[\d+\]\.config\.(\w+)$", str(err.field or ""))
        cfg = raw.get("config") if isinstance(raw, dict) else None
        cfg = cfg if isinstance(cfg, dict) else {}
        return err, bool(m) and cfg.get(m.group(1)) in (None, "", [], {})
    return None


def _check_grounded(body: dict, out: dict) -> dict:
    """A grounded start's check: the stops ``flights.ground`` would store
    (``s1..sN``; a block may still be incomplete there — what must be filled
    in before it can fly is a warning)."""
    plan = None
    plan_id = str(body.get("plan_id") or "")
    if plan_id:
        plan = _flights.get_plan(plan_id)
        if plan is None:
            out["problems"].append(
                _problem("plan_id", "no such saved route", "unknown_plan")
            )
            return out
    steps: List[dict] = copy.deepcopy(plan["steps"]) if plan else []
    if body.get("steps") is not None:
        stripped = [dict(s, id="") if isinstance(s, dict) else s for s in body["steps"]]
        try:
            given = _flights.normalize_steps(stripped, strict=True, config_strict=False)
        except _flights.PlanError as err:
            out["problems"].append(_plan_problem(err))
            return out
        # On the ground a block may wait to be filled in, but a bad VALUE
        # would be turned into its default without a word: a problem.
        for i, raw in enumerate(stripped):
            refusal = _strict_refusal(raw, i)
            if refusal is None:
                continue
            err, incomplete = refusal
            if incomplete:
                out["warnings"].append(
                    _problem(
                        err.field, "before it can fly: " + err.message, "incomplete"
                    )
                )
            else:
                out["problems"].append(_plan_problem(err))
        steps += given
    if not steps:
        out["problems"].append(
            _problem("steps", "a route needs at least one stop", "needs_steps")
        )
        return out
    for k, s in enumerate(steps):
        s["id"] = "s%d" % (k + 1)
    out["steps"] = steps
    out["start"] = _stop_ref(steps, 0)
    return out


def check_start(body: dict) -> dict:
    """``POST /api/flights/check`` (blocking): ``{ok, steps, start, skipped,
    in_flight, problems, warnings, start_on, environments?, absorbed?}`` for
    a start with this body. ``steps`` are the strictly normalised stops (the
    ids the server gives them), ``start`` the stop it takes off at,
    ``skipped`` the stops before it with why, ``in_flight`` the session's
    current route, ``problems`` every refusal the start would answer (its
    first one at least) and the driver's pre-check — each ``{field, detail,
    error?, session?}``. ``ok`` = no problems: the real start is accepted."""
    out: dict = {
        "ok": False,
        "steps": [],
        "start": None,
        "skipped": [],
        "in_flight": None,
        "problems": [],
        "warnings": [],
        "start_on": "now",
    }
    problems: List[dict] = out["problems"]
    if "steps" in body and not isinstance(body["steps"], list):
        problems.append(_problem("steps", "steps is a list", "bad_step"))
        return out
    subject = body.get("subject")
    if (
        isinstance(subject, dict)
        and str(subject.get("kind") or "").strip().lower() in _INTAKE_SUBJECTS
    ):
        plan = _flights.get_plan(str(body.get("plan_id") or ""))
        if plan is None:
            problems.append(_problem("plan_id", "no such saved route", "unknown_plan"))
        else:
            out["steps"] = plan["steps"]
            out["start"] = _stop_ref(plan["steps"], 0)
        out["ok"] = not problems
        return out
    if body.get("grounded"):
        _check_grounded(body, out)
        out["ok"] = not problems
        return out
    try:
        plan, title, start_at, start_on = _start_plan(body)
    except _flights.PlanError as err:
        problems.append(_plan_problem(err))
        return out
    except _flights.FlightError as err:
        problems.append(_flight_problem(err))
        return out
    from backend.web import server as srv

    out["start_on"] = start_on
    ready = start_on != "now"
    live = bool(title) and title in srv.ENGINE.instances
    if title and not live:
        problems.append(
            _problem("title", "no session %s" % title, "unknown_session", title)
        )
    take = body.get("absorb_fasttrack")
    if live and (take is None or bool(take)):
        try:
            rec = _driver._fasttrack_to_absorb(title)
            if rec is not None:
                lead = _flights.absorb_steps(rec, _driver._stage_of(title))
                plan, start_at = _flights.with_lead(
                    plan, lead, "" if ready else start_at, attach=not ready
                )
                out["absorbed"] = [s["config"]["lane"] for s in lead]
        except (_flights.PlanError, _flights.FlightError):
            pass  # the start_cursor check below names it
        except Exception as err:  # noqa: BLE001 — read-only peek
            _log.info("flights check: fast-track peek failed: %s", err)
    steps = plan["steps"]
    out["steps"] = steps
    cursor = 0
    if not ready:
        try:
            cursor = _flights.start_cursor(plan, start_at, attach=bool(title))
        except _flights.FlightError as err:
            problems.append(_problem("start_at", err.detail, err.error))
        if (
            live
            and not start_at
            and "absorbed" not in out
            and steps
            and steps[0]["kind"] == "agent"
        ):
            try:
                busy = _driver._session_busy(title)
            except Exception:  # noqa: BLE001
                busy = ""
            if busy:
                cursor = 0
                out["warnings"].append(
                    _problem(
                        "start_at",
                        "%s is busy (%s): its work in progress is the first Agent "
                        "stop — nothing is sent, the stop ends with that turn"
                        % (title, busy),
                        "in_progress",
                    )
                )
    out["start"] = _stop_ref(steps, cursor)
    out["skipped"] = [
        dict(_stop_ref(steps, i), reason=_skip_reason(steps[i], start_at))
        for i in range(min(cursor, len(steps)))
    ]
    if steps and cursor >= len(steps):
        out["warnings"].append(
            _problem(
                "start_at",
                "every stop is before where it starts: the route ends at once",
                "nothing_to_fly",
            )
        )
    if live:
        held = _flights.for_session(title)
        if held is not None and held.get("state") in _flights.HOLDING:
            out["in_flight"] = {
                "id": held["id"],
                "name": held.get("plan_name") or "",
                "state": held["state"],
            }
            problems.append(
                _problem(
                    "title",
                    "%s is already on a route (%s, %s) — cancel it, or edit that "
                    "route instead" % (title, held.get("plan_name"), held["state"]),
                    "in_flight",
                    title,
                )
            )
        elif held is not None and held.get("state") == "halted":
            out["warnings"].append(
                _problem(
                    "title",
                    "replaces %s's halted route %s (%s)"
                    % (title, held["id"], held.get("plan_name")),
                    "replaces",
                    title,
                )
            )
    repo = _repo_for(body, plan, title)
    if not title and not repo:
        problems.append(
            _problem("repo_path", "pick a folder for the new session", "no_repo")
        )
    if not title or live:
        problems.extend(
            _preflight(
                plan, repo=repo, session=title, attach=bool(title), start_at=start_at
            )
        )
    envs = _environments(repo, steps)
    if envs is not None:
        out["environments"] = envs
    out["ok"] = not problems
    return out


@router.post("/api/flights/check")
async def flights_check(payload: Optional[dict] = None) -> JSONResponse:
    """The body of a ``POST /api/flights/start`` → 200 :func:`check_start`
    (``ok`` false with ``problems`` when the start would be refused) — no
    session, no route, nothing written."""
    return JSONResponse(await asyncio.to_thread(check_start, payload or {}))


def _preflight_refusal(body: dict) -> Optional[JSONResponse]:
    """The driver's pre-check before a real start: its first problem as 409
    (it names a session) or 400 ``{error, detail, field, session?,
    problems}``; None to go ahead. A body the start itself refuses is left
    to the start (same answer as before)."""
    if not callable(getattr(_driver, "preflight", None)):
        return None
    try:
        plan, title, start_at, _start_on = _start_plan(body)
    except (_flights.PlanError, _flights.FlightError):
        return None
    from backend.web import server as srv

    if title and title not in srv.ENGINE.instances:
        return None
    found = _preflight(
        plan,
        repo=_repo_for(body, plan, title),
        session=title,
        attach=bool(title),
        start_at=start_at,
    )
    if not found:
        return None
    first = found[0]
    out = {
        "error": first.get("error") or "preflight",
        "detail": first["detail"],
        "field": first["field"],
        "problems": found,
    }
    if first.get("session"):
        out["session"] = first["session"]
    return JSONResponse(out, status_code=409 if first.get("session") else 400)


@router.post("/api/flights/start")
async def flights_start(payload: Optional[dict] = None) -> JSONResponse:
    """Take off → 202 ``{"flight", "absorbed"}``. ``title`` attaches to an
    existing session; without it the session is created from the plan.
    ``steps`` draws the flight here (after the plan's when ``plan_id`` comes
    too); the session's own running fast-track becomes its leading ship
    steps unless ``absorb_fasttrack`` is false. A ticket / PR / issue
    ``subject`` (``{kind, source, id}``) starts through Intake.
    ``start_on`` (round 19; absent = ``"now"``): ``"prompt"`` / ``"run"``
    store the flight ``ready`` instead of starting it (400
    ``bad_start_on``). Before anything is created the driver's pre-check
    (``flight_driver.preflight``) runs: its first problem is a 400 / 409
    ``{error, detail, field, session?, problems}``. ``POST
    /api/flights/check`` answers the same questions without starting."""
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
    refused = await asyncio.to_thread(_preflight_refusal, body)
    if refused is not None:
        return refused
    absorbed: list = []
    try:
        flight = await _driver.start(body, absorbed)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err, {"unknown_step": "start_at"})
    return JSONResponse({"flight": flight, "absorbed": absorbed}, status_code=202)


@router.get("/api/flights/default-place")
def flights_default_place() -> JSONResponse:
    """The default folder (``{folder}``, ``~`` folded): where a chain runs
    when no session is picked — for the place picker."""
    import os

    from backend.config.settings import load_settings

    try:
        folder = str(load_settings().general.default_folder or "").strip()
    except Exception:  # noqa: BLE001
        folder = ""
    shown = folder or _driver.DEFAULT_PLACE
    home = os.path.expanduser("~")
    if shown.startswith(home):
        shown = "~" + shown[len(home) :]
    return JSONResponse({"folder": shown})


# --- An intake source's default stops (docs/flights.md "A source's default
# stops, edited in the Sky (2026-11-02)"); the logic is core.flight_intake_default.


@router.get("/api/flights/intake-default")
def flights_intake_default(kind: str = "", target: str = "") -> JSONResponse:
    """``?kind=tickets|prs|issues&target=<source id | owner/repo>`` →
    ``{kind, target, label, plan_id, name, own, steps}``: what a new item
    from that source flies now. 400 ``bad_kind``, 404 ``unknown_source``."""
    try:
        return JSONResponse(_intake_default.resolve(kind, target))
    except _flights.FlightError as err:
        return _err(err)


@router.put("/api/flights/intake-default")
def flights_intake_default_put(
    payload: Optional[dict] = None,
    allowed: bool = Depends(_web_auth.configure_allowed),
) -> JSONResponse:
    """Set a source's default stops: ``{kind, target, steps, name?}`` (saved
    as its own saved stop), ``{kind, target, plan_id}`` (``""`` = our
    built-in) or ``{from_flight}`` → the GET shape plus ``previous_plan_id``.
    400 ``bad_step`` with the field; 404 ``unknown_source`` /
    ``unknown_plan`` / ``unknown_flight`` / ``no_source``. Owner only, like
    the Intake dialog's saves (a source decides what its tickets run)."""
    if not allowed:
        return _web_auth.configure_refused()
    try:
        return JSONResponse(_intake_default.put(payload or {}))
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)


@router.get("/api/flights/intake-default/for-flight/{flight_id}")
def flights_intake_default_for_flight(flight_id: str) -> JSONResponse:
    """The GET shape for the source ``flight_id`` came from (its subject, or
    the source an editor chain edits); 404 ``unknown_flight`` /
    ``no_source``."""
    try:
        return JSONResponse(_intake_default.for_flight(flight_id))
    except _flights.FlightError as err:
        return _err(err)


@router.get("/api/flights/{flight_id}")
def flights_get(flight_id: str) -> JSONResponse:
    f = _flights.get(flight_id)
    if f is None:
        return JSONResponse({"error": "unknown_flight"}, status_code=404)
    return JSONResponse({"flight": _driver.flight_dto(f)})


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
    several, one new group when ``group`` is given. ``{"step", "parallel_with":
    sid, "at"?}`` (round 20) puts the step in ``sid``'s parallel group (one
    is made when ``sid`` has none): after its last member, or at ``at``
    within it; 400 ``not_parallel_safe`` names a kind that can't."""
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
    if body.get("parallel_with"):
        return _change(
            flight_id,
            "add_parallel",
            str(body["parallel_with"]),
            raw=body["step"],
            at=body.get("at"),
        )
    return _change(flight_id, "add", raw=body["step"], at=body.get("at"))


@router.patch("/api/flights/{flight_id}/steps/{step_id}")
def flights_step_edit(
    flight_id: str, step_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{title?, config?, on_fail?, halt?, when?, group?}`` on a step still ahead →
    ``{"flight"}``; ``{halt}`` alone may also mark / clear the current step;
    ``group: {name}`` renames the step's group, ``null`` takes the step out.
    ``{pos: {x, y} | null}`` is layout only: allowed on ANY step.
    ``{parallel: null}`` takes the step out of its parallel group (it flies
    right after it); ``{parallel_with: sid}`` moves it into ``sid``'s group
    (making one) — round 20, steps still ahead only.

    Every option is checked as strictly as a plan's when it is saved, a
    grounded route's too (400 ``bad_step`` with ``field`` = ``step.config.…``):
    on the ground only an option still to be filled in (an empty prompt or
    command) may wait until take-off."""
    refused = _grounded_patch_refusal(flight_id, step_id, payload)
    if refused is not None:
        return refused
    return _change(flight_id, "edit", step_id, raw=payload or {})


def _grounded_patch_refusal(
    flight_id: str, step_id: str, patch
) -> Optional[JSONResponse]:
    """A grounded route checks a stop's config leniently (a block may be
    incomplete until take-off), which silently turned a bad option into its
    default. Refuse a bad VALUE now, with its field; only a required option
    left empty may wait for take-off."""
    if not isinstance(patch, dict) or not (
        set(patch) & {"title", "config", "on_fail", "when", "halt"}
    ):
        return None
    f = _flights.get(flight_id)
    if f is None or f.get("state") != _flights.GROUNDED:
        return None
    ids = [s["id"] for s in f["steps"]]
    if step_id not in ids:
        return None
    i = ids.index(step_id)
    raw = copy.deepcopy(f["steps"][i])
    for key in ("title", "config", "on_fail", "halt", "when"):
        if key in patch:
            raw[key] = patch[key]
    if "when" in patch and not str(patch["when"] or "").strip():
        raw.pop("when", None)
    refusal = _strict_refusal(raw, i, set(ids) - {step_id})
    if refusal is None or refusal[1]:
        return None  # fine, or still to be filled in: take-off checks it
    err = refusal[0]
    return _bad_step(
        _flights.PlanError(
            err.message,
            re.sub(r"^steps\[\d+\]", "step", str(err.field or "")),
            getattr(err, "code", "") or "bad_step",
        )
    )


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
    "in_place"?, "prompt"?}`` creates the session first. ``start_on``
    (round 19; absent = ``"now"``): ``"prompt"`` / ``"run"`` bind it
    ``ready`` — nothing starts, a created session gets no prompt."""
    try:
        flight = await _driver.attach(flight_id, payload or {})
    except _flights.PlanError as err:
        # An incomplete block (a Split with no prompts): named by field, so
        # the board can open that stop's dialog.
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


@router.post("/api/flights/{flight_id}/layout")
def flights_layout(flight_id: str, payload: Optional[dict] = None) -> JSONResponse:
    """``{"positions": {<sid>: {x, y} | null}}`` → ``{"flight"}``: free
    positions on the Sky (docs/flights.md "Round 18: Free positions") —
    layout only, so any step of any flight; ``null`` clears one, unknown ids
    are ignored (400 ``bad_layout`` when ``positions`` is not an object)."""
    try:
        flight = _driver.layout(flight_id, (payload or {}).get("positions"))
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


@router.post("/api/flights/{flight_id}/steps/{step_id}/split_off")
def flights_step_split_off(
    flight_id: str, step_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``{"pos"?: {x, y}}`` → ``{"flight", "chain"}``: the step and every one
    after it leave into a new grounded flight (``chain``), the first one at
    ``pos`` (docs/flights.md "Round 18: Split off"). 409 ``not_pending`` when
    any of them has started, 409 ``whole_chain`` for the first step."""
    try:
        flight, chain = _driver.split_off(
            flight_id, step_id, (payload or {}).get("pos")
        )
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight, "chain": chain})


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
    """Done on a step waiting on you → ``{"flight"}``. A Wait for me stop is
    ticked off (``note`` goes to its history); a Verify stop waiting for your
    confirm is "Confirm verified"; a Verify stop parked because you had to do
    something runs again ("I did it"). Done never passes a Verify stop's open
    steps: one still waiting on its steps is 409 (answer them, or Skip it);
    a Verify stop whose failure was handed to you takes Done. 409
    ``not_waiting`` on a stop that does not need you now."""
    return _op(flight_id, "done", step_id, str((payload or {}).get("note") or ""))


@router.post("/api/flights/{flight_id}/steps/{step_id}/skip")
def flights_step_skip(flight_id: str, step_id: str) -> JSONResponse:
    return _op(flight_id, "skip", step_id)


@router.post("/api/flights/{flight_id}/steps/{step_id}/retry")
def flights_step_retry(flight_id: str, step_id: str) -> JSONResponse:
    return _op(flight_id, "retry", step_id)


@router.post("/api/flights/{flight_id}/steps/{step_id}/op/{name}")
def flights_step_op(
    flight_id: str, step_id: str, name: str, payload: Optional[dict] = None
) -> JSONResponse:
    """A plug-in kind's own operation on one of its stops (docs/flights.md
    "Round 15": ``Kind.ops``); the body is the operation's own."""
    try:
        return JSONResponse(_driver.step_op(flight_id, step_id, name, payload))
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)


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


@router.post("/api/flights/{flight_id}/run")
def flights_run(flight_id: str) -> JSONResponse:
    """▶ Run (docs/flights.md "Round 19"): a ``ready`` flight takes off at
    its first stop → ``{"flight"}`` (409 ``not_ready`` otherwise; 400
    ``bad_step`` naming a stop not ready to fly)."""
    try:
        flight = _driver.run(flight_id)
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


@router.patch("/api/flights/{flight_id}")
def flights_rename(flight_id: str, payload: dict) -> JSONResponse:
    """The flight's own name (docs/flights.md "Round 13"): ``{plan_name}``;
    and how a ready flight starts (round 19): ``{start_on: "prompt" | "run"
    | "now"}`` — ready only (409 ``not_ready``), ``"now"`` runs it."""
    body = payload or {}
    if "start_on" not in body:
        return _op(flight_id, "rename", "", str(body.get("plan_name") or ""))
    if "plan_name" in body:
        renamed = _op(flight_id, "rename", "", str(body.get("plan_name") or ""))
        if renamed.status_code != 200:
            return renamed
    try:
        flight = _driver.set_start_on(flight_id, body.get("start_on"))
    except _flights.PlanError as err:
        return _bad_step(err)
    except _flights.FlightError as err:
        return _err(err)
    return JSONResponse({"flight": flight})


# A plug-in kind's own routes (an inbound hook, a listing), next to these.
for _plug_router in _kinds.routers():
    router.include_router(_plug_router)
