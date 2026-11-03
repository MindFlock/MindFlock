"""The ``handoff`` stop, "Start a route" (docs/flights.md "Round 18: One chain
triggers another"): one chain starts another chain on a session — an existing
one, or a new worker of this flight's session — and either carries on at once
or waits for that chain to land.

Config:

* ``target``: ``"flight:<id>"`` — a stops-only (grounded) chain on the board,
  consumed: it takes off on the session; or ``"plan:<id>"`` — a saved
  stop/flyway, a fresh grounded copy each time (the reusable module).
  Required to take off.
* ``session``: an existing session title, or ``"new"`` (the default): a new
  session in this flight's repo on its session's CLI — the Split's
  :func:`flight_driver.spawn_worker` path, its first agent stop's prompt as
  the worker's initial prompt.
* ``wait``: false (the default) — done once the target has taken off; true —
  running until the target lands (``done``), failing with the target's reason
  when it halts or is cancelled (or is deleted).

The run records ``detail.child`` (the started flight's id), ``detail.session``
(where it runs), ``detail.child_name`` and the receipt "started <name> on
<session>". The started flight carries ``triggered_by: {flight, step,
session}`` (:func:`flights.op_attach`), so a retry of this stop that finds
its own child already flying adopts it instead of failing.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional, Tuple

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as F

NEW = "new"
_TARGET_RE = re.compile(r"^(flight|plan):([A-Za-z0-9_:@.-]{1,64})$")


def parse_target(text: str) -> Tuple[str, str]:
    """``"flight:<id>"`` → ``("flight", id)``, ``"plan:<id>"`` → ``("plan",
    id)``; ``("", "")`` for anything else."""
    m = _TARGET_RE.match(str(text or ""))
    return (m.group(1), m.group(2)) if m else ("", "")


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    target = F._text(c.get("target"), 80)
    if target and not parse_target(target)[0]:
        if strict:
            raise F.PlanError(
                'target is "flight:<id>" or "plan:<id>"', where + ".target"
            )
        target = ""
    if strict and not target:
        raise F.PlanError("pick the route to start", where + ".target")
    session = F._text(c.get("session"), 300) or NEW
    return {
        "target": target,
        "session": NEW if session.lower() == NEW else session,
        "wait": F._bool(c.get("wait"), False),
    }


def _title(config: dict) -> str:
    sess = config.get("session") or NEW
    head = "Start a route" + ("" if sess == NEW else " on %s" % sess)
    return head + (" and wait" if config.get("wait") else "")


def _name(f: dict) -> str:
    return f.get("plan_name") or f.get("id") or "the route"


def _judge(flight, step, ss, obs, now) -> dict:
    """Asked while the stop is ``running`` (``wait`` on): the started chain's
    state, from ``obs["handoff"]``."""
    d = ss["detail"]
    child = str(d.get("child") or "")
    if not step["config"].get("wait"):
        receipt = str(d.get("receipt") or "started")
        return F._receipt({"action": "done", "note": receipt}, receipt)
    if not child:
        return {"action": "fail", "reason": "no route was started"}
    o = obs.get("handoff")
    if not isinstance(o, dict):
        return F._wait("checking the route it started")
    if o.get("failed"):
        return {"action": "fail", "reason": str(o["failed"])}
    name = str(o.get("name") or d.get("child_name") or child)
    if not o.get("present"):
        return {"action": "fail", "reason": "%s is gone" % name}
    state = str(o.get("state") or "")
    reason = str(o.get("reason") or "")
    if state == "done":
        receipt = "%s finished" % name
        return F._receipt({"action": "done", "note": receipt}, receipt)
    if state in ("halted", "cancelled"):
        word = "was cancelled" if state == "cancelled" else "halted"
        return {
            "action": "fail",
            "reason": "%s %s%s" % (name, word, ": %s" % reason if reason else ""),
        }
    return F._wait("waiting for %s to finish" % name)


def _ours(target: dict, flight: dict, step: dict) -> bool:
    trig = target.get("triggered_by") or {}
    return trig.get("flight") == flight["id"] and trig.get("step") == step["id"]


def _first_prompt(target: dict) -> Tuple[bool, str]:
    """``(first_agent, prompt)``: whether the chain opens on an agent stop
    (not a halt point) and that stop's prompt filled — the new session's
    initial prompt, as ``attach`` with no title does."""
    first = F._current(target)
    if first is None or first["kind"] != "agent" or first.get("halt"):
        return False, ""
    text = F.fill_prompt(first["config"].get("prompt") or "", target)
    return True, text.strip()


def _started(child: dict, title: str, wait: bool, action: dict) -> dict:
    name = _name(child)
    receipt = ("started %s on %s" % (name, title))[: F.MAX_RECEIPT]
    detail = {
        "child": child["id"],
        "child_name": name,
        "session": title,
        "receipt": receipt,
    }
    if wait:
        return dict(action, note="waiting for %s to finish" % name, detail=detail)
    return {"action": "done", "note": receipt, "detail": detail}


async def _run(flight, step, action, now) -> dict:
    from backend.web.core import flight_driver as D

    cfg = step["config"]
    kind, ident = parse_target(cfg.get("target") or "")
    if not kind:
        raise _kinds.StepFailed("pick the route to start")
    wait = bool(cfg.get("wait"))
    target: Optional[dict] = None
    plan: Optional[dict] = None
    if kind == "flight":
        target = await asyncio.to_thread(F.get, ident)
        if target is None:
            raise _kinds.StepFailed("the route to start is gone")
        if target["state"] != F.GROUNDED:
            if _ours(target, flight, step):
                # A retry (or a restart mid-run): it is already ours.
                return _started(target, target["session"], wait, action)
            raise _kinds.StepFailed(
                "%s is already %s"
                % (
                    _name(target),
                    "over" if target["state"] in F.FINAL else "running",
                )
            )
        try:
            F.validate_for_takeoff(target)
        except F.PlanError as err:
            raise _kinds.StepFailed("%s: %s" % (_name(target), err.message))
    else:
        plan = await asyncio.to_thread(F.get_plan, ident)
        if plan is None:
            raise _kinds.StepFailed("the saved route %s is gone" % ident)
    srv = D._server()
    title = str(cfg.get("session") or NEW)
    if title != NEW:
        if title not in srv.ENGINE.instances:
            raise _kinds.StepFailed("no session %s" % title)
        if await asyncio.to_thread(F.for_session, title) is not None:
            raise _kinds.StepFailed("%s is already on a route" % title)
    if plan is not None:
        target = await asyncio.to_thread(F.ground, plan, None, "", flight["by"])
    assert target is not None
    seeded = no_prompt = False
    if title == NEW:
        first_agent, prompt = _first_prompt(target)
        try:
            title = await D.spawn_worker(
                flight,
                step,
                prompt,
                report_back=False,
                title_prefix=_name(target),
            )
        except _kinds.StepFailed:
            if plan is not None:
                await asyncio.to_thread(F.delete, target["id"])
            raise
        seeded = first_agent and bool(prompt)
        no_prompt = first_agent and not prompt
    inst = srv.ENGINE.instances.get(title)
    root = await asyncio.to_thread(D._repo_root, title)
    trig = {"flight": flight["id"], "step": step["id"], "session": flight["session"]}
    try:
        child = await asyncio.to_thread(
            F.attach,
            target["id"],
            title,
            root,
            str(getattr(inst, "Branch", "") or ""),
            seeded,
            None,
            trig,
        )
    except F.FlightError as err:
        raise _kinds.StepFailed(err.detail or err.error, {"session": title})
    if no_prompt:
        child = (
            await asyncio.to_thread(
                F.mutate,
                child["id"],
                lambda x: x["step_states"][x["steps"][0]["id"]]["detail"].update(
                    no_prompt=True
                ),
            )
            or child
        )
    D._emit("flight.changed", child)
    D.wake()
    return _started(child, title, wait, action)


async def _observe(flight, step, ss, sess, now) -> dict:
    """The started chain as the judge needs it: ``{present, state, reason,
    name}`` (``present`` false once it is deleted)."""
    child = str(ss["detail"].get("child") or "")
    if not child or not step["config"].get("wait"):
        return {}
    f = await asyncio.to_thread(F.get, child)
    if f is None:
        return {"present": False, "name": ss["detail"].get("child_name") or child}
    return {
        "present": True,
        "state": f["state"],
        "reason": f["reason"],
        "name": _name(f),
    }


KIND = _kinds.register(
    _kinds.Kind(
        kind="handoff",
        spec={
            "label": "Start a route",
            "actor": "robot",
            "summary": "Start another route on a session (a new one by default), "
            "and carry on or wait for it to finish",
            "default_title": "Start a route",
            "family": "flow",
            "risk": [],
            "default_on_fail": "halt",
            "fields": [
                F._f(
                    "target",
                    "select",
                    "Route to start",
                    default="",
                    options=[],
                    dynamic="chains",
                ),
                F._f(
                    "session",
                    "select",
                    "Run it in",
                    default=NEW,
                    options=[NEW],
                    dynamic="sessions",
                ),
                F._f("wait", "bool", "Wait for it to finish", default=False),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        default_title=_title,
    )
)
