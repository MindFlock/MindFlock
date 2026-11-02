"""The ``loop`` stop (docs/flights.md "Round 16"): send the flight back to an
earlier stop, N times, at most every so often, waiting out usage limits,
clearing the agent's context first when asked — then let it carry on past.

Placed AFTER the stops it repeats. ``detail.runs`` counts the runs completed
so far (the first pass is run 1, so a Loop with ``times: 3`` sends the flight
back twice); ``detail.last_start`` is when the current run began.

The flow through the core:

* first arrival (the step is pending): the core's generic ``run`` starts it;
  :func:`_run` (no phase) only records ``runs: 0`` and the first run's start.
* every arrival after (the step is ``running``): :func:`_judge` decides —
  ``done`` (count or ``until`` met, with a receipt), ``wait`` (the usage
  limit, the interval), or ``run`` with ``phase: "loop"`` (a phase, so the
  core does not restart the step and wipe its detail).
* :func:`_run` with that phase: delivers the provider's
  ``clear_context_command()`` when ``fresh`` (nothing when it has none), then
  returns ``{"action": "loop", "to": <stop id>}`` — the store applies it
  through ``flights.apply`` → :func:`flights.loop_back` (the run plug-ins get
  is a copy of the flight, so the jump must happen there). The loop step
  stays ``running``, so the judge is asked again when the flight comes round.

Skip on a current Loop carries on past it, Remove is the usual "stop
removed" pause, and a backward jump resets it (``runs`` from 0): no special
casing anywhere.
"""

from __future__ import annotations

import asyncio
import math
from typing import Optional

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flight_when as _when
from backend.web.core import flights as F

MAX_TIMES = 1000
MAX_EVERY = 7 * 24 * 60


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    back_to = F._text(c.get("back_to"), 32)
    if back_to and not F._ID_RE.match(back_to):
        if strict:
            raise F.PlanError("back_to names a stop of this line", where + ".back_to")
        back_to = ""
    times = F._int(c.get("times"), 3, -1, MAX_TIMES + 1)
    if strict and not 0 <= times <= MAX_TIMES:
        raise F.PlanError(
            "times is 0 (until stopped) to %d" % MAX_TIMES, where + ".times"
        )
    every = F._int(c.get("every_minutes"), 0, -1, MAX_EVERY + 1)
    if strict and not 0 <= every <= MAX_EVERY:
        raise F.PlanError(
            "every_minutes is 0 to %d (7 days)" % MAX_EVERY, where + ".every_minutes"
        )
    until = str(c.get("until") if c.get("until") is not None else "")
    until = until.replace("\x00", "").strip()
    if until:
        try:
            if len(until) > _when.MAX_LEN:
                raise _when.WhenError(
                    "until is at most %d characters" % _when.MAX_LEN, _when._HINT
                )
            until = _when.canonical(_when.parse(until))
        except _when.WhenError as err:
            if strict:
                raise F.PlanError(err.message, where + ".until")
            # A block just placed may hold a half-typed condition: kept as
            # typed (the takeoff check is strict), the judge refuses it.
            until = until[: _when.MAX_LEN]
    return {
        "back_to": back_to,
        "times": max(0, min(times, MAX_TIMES)),
        "every_minutes": max(0, min(every, MAX_EVERY)),
        "wait_limits": F._bool(c.get("wait_limits"), True),
        "fresh": F._bool(c.get("fresh"), False),
        "until": until,
    }


def _every(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        return "%d h" % (minutes // 60)
    return "%d min" % minutes


def _title(config: dict) -> str:
    if config.get("until"):
        return "Loop until %s" % config["until"]
    times = int(config.get("times") or 0)
    every = int(config.get("every_minutes") or 0)
    head = "Loop ×%d" % times if times else ("Loop" if every else "Loop forever")
    return head + (" every %s" % _every(every) if every else "")


def _first_start(flight: dict, to: int) -> float:
    """When run 1 began: the target stop's start, else the flight's."""
    since = float(flight["step_states"][flight["steps"][to]["id"]].get("since") or 0)
    return since or float(flight.get("started") or 0.0)


def _until(cfg: dict, obs: dict) -> Optional[dict]:
    """None when ``until`` is not met (or not set); a ``done`` once it is; a
    wait while its facts are not gathered; a fail for an unreadable one."""
    text = cfg.get("until") or ""
    if not text:
        return None
    try:
        cond = _when.parse(text)
    except _when.WhenError as err:
        return {"action": "fail", "reason": "until: %s" % err.message}
    facts = (obs.get("loop") or {}).get("facts")
    if not isinstance(facts, dict):
        return F._wait("checking %s" % text)
    ok, _why = _when.evaluate(cond, facts)
    if not ok:
        return None
    receipt = "stopped by: %s" % text
    return F._receipt({"action": "done", "note": receipt}, receipt)


def _judge(flight, step, ss, obs, now) -> dict:
    cfg = step["config"]
    to = F.loop_target(flight, step)
    if to is None:
        return {"action": "fail", "reason": F.NOTHING_TO_REPEAT}
    d = ss["detail"]
    runs = int(d.get("runs") or 0)
    times = int(cfg.get("times") or 0)
    met = _until(cfg, obs)
    if met is not None:
        return met
    if times > 0 and runs + 1 >= times:
        receipt = "looped %s" % F.plural(runs + 1, "time")
        return F._receipt({"action": "done", "note": receipt}, receipt)
    sess = obs.get("session") or {}
    limited = sess.get("limited") or (obs.get("loop") or {}).get("limited")
    if cfg.get("wait_limits", True) and limited:
        return F._wait("waiting on the usage limit", next_at=None)
    every = int(cfg.get("every_minutes") or 0) * 60
    last = float(d.get("last_start") or 0.0) or _first_start(flight, to)
    if every and last and now - last < every:
        left = int(math.ceil((last + every - now) / 60.0))
        return F._wait("next run in %d min" % max(1, left), next_at=last + every)
    target = flight["steps"][to]
    return {
        "action": "run",
        "phase": "loop",
        "note": "run %d%s — back to %s"
        % (runs + 2, " of %d" % times if times else "", target["title"]),
        "detail": {"target": target["id"]},
    }


def _provider_of(title: str):
    """The session's provider (tests monkeypatch this), None when gone."""
    from backend import providers
    from backend.web.core import flight_driver as D

    inst = D._server().ENGINE.instances.get(title) if title else None
    if inst is None:
        return None
    return providers.resolve(getattr(inst, "Program", "") or "")


def _clear_command(title: str) -> Optional[str]:
    prov = _provider_of(title)
    fn = getattr(prov, "clear_context_command", None)
    return fn() if callable(fn) else None


async def _run(flight, step, action, now) -> dict:
    if action.get("phase") != "loop":
        # First arrival: count run 1 and when it began; the judge decides.
        to = F.loop_target(flight, step)
        start = _first_start(flight, to) if to is not None else 0.0
        return dict(action, detail={"runs": 0, "last_start": start or now})
    from backend.web.core import flight_driver as D

    to = F.loop_target(flight, step)
    if to is None:
        raise _kinds.StepFailed(F.NOTHING_TO_REPEAT)
    sent = False
    if step["config"].get("fresh"):
        cmd = await asyncio.to_thread(_clear_command, flight["session"])
        if cmd:
            await asyncio.to_thread(D._deliver, flight["session"], cmd)
            sent = True
    return {
        "action": "loop",
        "to": flight["steps"][to]["id"],
        "note": action.get("note") or "",
        "cleared": sent,
    }


async def _observe(flight, step, ss, sess, now) -> dict:
    """What the judge needs beyond the session row: the ``until`` facts (the
    same a step's ``when`` sees) and, for ``wait_limits``, whether the
    session is usage-limited (the row the driver read for a non-agent step
    does not carry it)."""
    from backend.web.core import flight_driver as D

    cfg = step["config"]
    out: dict = {}
    if cfg.get("until"):
        out["facts"] = await D.when_facts(
            flight, {"id": step["id"], "when": cfg["until"]}, sess, now
        )
    if cfg.get("wait_limits", True) and "limited" not in sess and sess.get("present"):
        full = await asyncio.to_thread(D._session_obs, flight, now, True)
        out["limited"] = bool(full.get("limited"))
    return out


KIND = _kinds.register(
    _kinds.Kind(
        kind="loop",
        spec={
            "label": "Loop",
            "actor": "robot",
            "summary": "Send the flight back to an earlier stop, N times, every so often",
            "default_title": "Loop ×3",
            "family": "work",
            "risk": [],
            "default_on_fail": "halt",
            "fields": [
                F._f(
                    "back_to",
                    "select",
                    "Repeat from",
                    default="",
                    options=[],
                    dynamic="stops_before",
                ),
                F._f("times", "int", "Runs in all (0 = until stopped)", default=3),
                F._f(
                    "every_minutes",
                    "int",
                    "At most every N minutes (0 = straight away)",
                    default=0,
                ),
                F._f("wait_limits", "bool", "Wait out usage limits", default=True),
                F._f(
                    "fresh", "bool", "Clear the agent's context each run", default=False
                ),
                F._f(
                    "until",
                    "text",
                    "Stop looping when",
                    default="",
                    placeholder="e.g. judge is yes (empty = by count)",
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        default_title=_title,
    )
)
