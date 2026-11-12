"""The flight tools: ``describe_flight``, ``start_flight``, ``flight_status``,
``control_flight``, ``edit_flight``, ``save_flight_plan`` and
``run_pr_review``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart for size), driving
the same ``/api/flights*`` routes the Sky does (docs/flights.md "Contract: the
HTTP API"). A FLIGHT is a row of stops (steps) the server's flight driver flies
one session through: an agent prompt, a test run, a ship rung, a wait, a perch
for the user, a Loop back to an earlier stop… The kinds are plug-ins the
server serves at ``GET /api/flights/kinds``; nothing here hard-codes the list,
so a kind added on the server (round 16's ``loop``) is usable at once.

The path from a sentence to a running flight is ``describe_flight`` (the
server's one-shot model turns prose into normalised steps, and the stop
cookbook comes along — when each kind fits, the common routes, the order —
so the agent can hand-write or fix them) → ``start_flight(dry_run=true)``
(the server's own check of the same start, ``POST /api/flights/check``: the
stops as it stores them, where it starts, what it skips and every refusal —
read back as one sentence for the person to OK) → ``start_flight``.
The cookbook (:mod:`backend.web.core.flight_cookbook`, pure stdlib) is the
same one the server's one-shot prompt reads, so the Sky's sentence box and an
agent agree; its :func:`~backend.web.core.flight_cookbook.lint` turns a route
that can't work (a Join with no Split, Merge before Open PR …) into a refusal
that says what to do instead.

Scope: a session flies, steers and edits flights on ITSELF or on one of its
descendants — the rule ``ship_session`` / ``set_autopilot`` follow
(:meth:`backend.mcp.policy.Policy.require_ship`), including "a team run's
member ships with its run" and "a stop that cannot be undone needs
``confirm_merge``". A grounded flight (stops on the board, no session) belongs
to nobody yet: any caller with write scope may make or edit one, and the user
attaches it. These tools are local-only: peer mode serves its own toolset
(:mod:`backend.mcp.peer_tools`), so they are never offered to a peer.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from backend import client
from backend.mcp.policy import Flock
from backend.mcp.protocol import ToolContext, ToolError
from backend.web.core import flight_cookbook as cookbook

_log = logging.getLogger(__name__)

__all__ = [
    "FlightTools",
    "CONTROL_OPS",
    "STEP_OPS",
    "compact_flight",
    "compact_kinds",
    "cookbook_view",
    "find_plan",
    "read_back",
    "saved_lines",
    "step_line",
]

#: control_flight's ops: the flight-wide ones and the per-step ones.
FLIGHT_OPS = ("pause", "resume", "cancel", "run", "again")
STEP_OPS = ("skip", "retry", "jump", "done")
CONTROL_OPS = FLIGHT_OPS + STEP_OPS
#: Flights still in the air (or on the board): docs/flights.md "Flight".
UNFINISHED = ("grounded", "ready", "running", "waiting_you", "paused", "halted")
#: The one-shot describe runs ≤60 s server-side.
DESCRIBE_TIMEOUT_S = 90.0
START_TIMEOUT_S = 120.0
#: flight_status stays about this small (JSON characters).
STATUS_BUDGET = 2000
#: describe_flight with the whole cookbook stays under this (JSON characters).
DESCRIBE_BUDGET = 12000
HISTORY_LINES = 10
MAX_STEPS = 40

D_DESCRIBE = (
    "Turn your user's description of a route into its stops. A ROUTE is the "
    "row of stops MindFlock carries one session through (agent, tests, Open "
    "PR, CI checks, Wait for me, Merge when green…). text: the route in plain "
    "words, as your user said it; want: plan (default) or step. Returns steps "
    "{id, kind, title, config, on_fail, when?} from the server's one-shot "
    "model, sentence (the route read back in one line), problems / warnings "
    "(what to fix, and how), and the COOKBOOK: stops (per kind: options with "
    "defaults and allowed values, use / not / waits; RARE kinds only when "
    "your user named them), recipes "
    "(common routes, terse) and rules (order, on_fail, when to ask); saved "
    "lists your user's saved routes (id · name · starts · stops) for 'like "
    "last time'. "
    "kinds=false skips the cookbook. Check the steps against the cookbook "
    "and fix them yourself; if the request is ambiguous (merge with no OK "
    "named, no schedule time, a URL or command not given) ask your user "
    "instead. error no_model / could_not_describe: write the steps from the "
    "cookbook. Nothing starts: start_flight(dry_run=true) reads it back, "
    "then start_flight (or save_flight_plan)."
)
D_START = (
    "Start a route. steps: [{kind, title?, config?, on_fail?, when?, halt?}] "
    "(from describe_flight, or by hand from its cookbook) and/or plan_id (a "
    "saved route's id or name; its steps come first). title: the session that runs it — "
    "default yourself; else any local session. On an existing session "
    "the work so far counts as the first agent stop (still on its turn: the "
    "stop waits for it to end). Work still ahead? at='s1' (the agent "
    "stop's prompt is queued to that session and sent when its turn ends), or "
    "start_on='prompt' (takes off with your user's next prompt) / 'run' "
    "(waits for Run). grounded=true puts the stops on your user's Sky with no "
    "session. name: the route's name. A stop that can't be undone (ship lane "
    "merge, a kind whose risk is irreversible) needs confirm_merge=true. One "
    "route per session: 409 in_flight means it has one (flight_status). A "
    'loop\'s config.back_to is an earlier stop id, or "agent". dry_run=true '
    "starts nothing: the sentence to read back to your user, problems / "
    "warnings, needs_confirm_merge, merge_blocked (you may not merge "
    "yourself: start it grounded). A route that can't work (Join with no "
    "Split, Merge before Open PR, a Loop to a later stop…) is refused with "
    "what to do instead. Returns {id, state, steps, session, sentence, "
    "warnings?}; then flight_status / control_flight."
)
#: What dry_run=true answers.
D_DRY_RUN = (
    "start nothing: the server's own check of the same start — ok (the real "
    "start will be accepted), lines (each stop as the server stores it: ▶ "
    "where it starts, skipped stops with why), in_flight, problems (each "
    "field: what is wrong and what to change, with the allowed values)"
)
D_STATUS = (
    "A route, compact: state (grounded|ready|running|waiting_you|paused|"
    "halted|done|cancelled; ready = built on its session, not started), cursor (the current stop), reason, every stop as one "
    "line (id, state, kind, title, note, receipt, when) and the last 10 "
    "history lines. id, or title (that session's live route, else its "
    "latest); neither = your own session's route."
)
D_CONTROL = (
    "Steer a route on any local session (or a grounded one). op: run (a "
    "ready route starts now) | pause | resume (Carry on: a paused route "
    "where it was, a halted one runs its stop again) | cancel (Stop the "
    "route; the session is kept) | skip (a stop) | retry (a failed or halted "
    "stop) | jump (Jump here: move the cursor to step) | done (a Wait for me "
    "stop, with an optional note; on a Verify stop waiting for a confirm it "
    "is Confirm verified — it never passes a Verify stop's open steps) | "
    "again (fly an ENDED route — stopped, halted or landed — again as a new "
    "route on its session, from the stop it had reached; step = start there "
    "instead; its session gone → the one holding its branch, or its closed "
    "session reopened; a stop that can't be undone needs confirm_merge). "
    "skip/retry/done default to the current stop; jump needs step. id, or "
    "title for that session's live route (else its latest). Never press done "
    "on a stop your user should judge unless they said so."
)
D_EDIT = (
    "Fix a route's stops (the Sky does the rest): add [steps] (inserted at "
    "index at, else appended; only after the current stop), remove [step ids] "
    "(a stop ahead just goes; removing the running stop pauses the route "
    "right before the next; the last stop stays), edit [{step, title?, "
    "config?, on_fail?, when?, halt?}] (a stop still ahead; config holds the "
    "options to change, merged into the stop's own; halt alone may also "
    "change on the current stop; when '' clears it), rename = the route's "
    "name. id, or title for that session's route (default: yours). The whole "
    "request is checked before anything is written; a write that still "
    "fails says what was applied and what was not. Same scope and "
    "confirm_merge rule as start_flight. Returns the route as flight_status "
    "does."
)
D_SAVE_PLAN = (
    "Keep a row of stops as a saved route on your user's shelf (the store "
    "calls it a plan): name, steps (as start_flight takes them), description, "
    "trigger {kind: manual|ticket|pr|issue|schedule, schedule?: {at:'HH:MM', "
    "days:'daily'|'weekdays'} | {cron} | {once}, paused?} — a schedule "
    "trigger starts it by itself (ask your user for the time if they gave "
    "none). id: a saved route's id or name to REPLACE (its trigger and "
    "description are kept unless you pass new ones); without it a new one "
    "is made. dry_run=true saves nothing and reads it back. Returns {plan_id, "
    "name, steps, sentence}; start_flight(plan_id=…) runs it."
)

D_PR_REVIEW = (
    "Run your user's PR review route (their saved review stops: a standards "
    "or security review, the PR's review-comment loop, a verdict…) on an "
    "open PR — also one a session is already working on. session: the "
    "session that has the PR (you or a descendant; default you when no "
    "pr_url), and/or pr_url: the PR (its session is found — the one on its "
    "branch — or made as Intake's PRs tab makes one). plan: a saved route's "
    "id or name (default: your user's Settings flights.pr_review_plan). No "
    "route on the session: it starts at the route's first stop (the work is "
    "already done). A route already flying: the stops are inserted after the "
    "current one (stops it already has are left out; before a reviews / "
    "ship stop ahead). The session's own agent makes the fixes. A stop that "
    "can't be undone needs confirm_merge=true. Returns {mode: started|"
    "inserted|created, session, flight, stops}; then flight_status."
)


def _obj(props: dict, required: Tuple[str, ...] = ()) -> dict:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


_FLIGHT_ID = {"type": "string", "minLength": 1, "maxLength": 40}
_STEP_ID = {"type": "string", "minLength": 1, "maxLength": 32}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 200}
#: One step as a client draws it; the server validates (400 bad_step + field).
_STEP = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40,
            "description": "a kind from describe_flight's kinds",
        },
        "title": {"type": "string", "maxLength": 120},
        "config": {"type": "object", "description": "the kind's fields"},
        "on_fail": {"type": "string", "enum": ["halt", "agent", "skip", "you"]},
        "when": {
            "type": "string",
            "maxLength": 300,
            "description": "condition; false = the stop is skipped",
        },
        "halt": {"type": "boolean", "description": "stop before this stop"},
        "id": _STEP_ID,
        "group": {"type": "object"},
        "parallel": {
            "type": "object",
            "description": (
                "{id: 'p_<hex>'}: consecutive stops sharing it run side by side "
                "(only kinds with parallel_safe; not agent/ship/split…)"
            ),
        },
    },
    "required": ["kind"],
}
_STEPS = {"type": "array", "minItems": 1, "maxItems": MAX_STEPS, "items": _STEP}

S_DESCRIBE = _obj(
    {
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "want": {"type": "string", "enum": ["plan", "step"], "default": "plan"},
        "kinds": {"type": "boolean", "default": True},
    },
    ("text",),
)
S_START = _obj(
    {
        "steps": _STEPS,
        "plan_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": dict(_TITLE, description="session that runs it (default: you)"),
        "grounded": {"type": "boolean", "default": False},
        "name": {"type": "string", "maxLength": 80},
        "at": dict(
            _STEP_ID,
            description="stop id to start at, or 'resume': where this route's "
            "last run on that session stopped (else the normal start)",
        ),
        "confirm_merge": {"type": "boolean", "default": False},
        "dry_run": {"type": "boolean", "default": False, "description": D_DRY_RUN},
        "start_on": {
            "type": "string",
            "enum": ["now", "prompt", "run"],
            "default": "now",
            "description": "now | prompt (the session's next prompt) | run (Run)",
        },
    }
)
S_STATUS = _obj({"id": _FLIGHT_ID, "title": _TITLE})
S_CONTROL = _obj(
    {
        "id": _FLIGHT_ID,
        "title": _TITLE,
        "op": {"type": "string", "enum": list(CONTROL_OPS)},
        "step": _STEP_ID,
        "note": {"type": "string", "maxLength": 2000},
        "confirm_merge": {"type": "boolean", "default": False},
    },
    ("op",),
)
_EDIT_STOP = {
    "type": "object",
    "properties": {
        "step": dict(_STEP_ID, description="the stop id (flight_status)"),
        "title": {"type": "string", "maxLength": 120},
        "config": {
            "type": "object",
            "description": "options to change, merged into the stop's own",
        },
        "on_fail": _STEP["properties"]["on_fail"],
        "when": {
            "type": "string",
            "maxLength": 300,
            "description": "condition; '' clears it",
        },
        "halt": {"type": "boolean", "description": "a halt point before this stop"},
    },
    "required": ["step"],
    "additionalProperties": False,
}
S_EDIT = _obj(
    {
        "id": _FLIGHT_ID,
        "title": dict(_TITLE, description="that session's route (default: yours)"),
        "add": _STEPS,
        "edit": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": _EDIT_STOP,
        },
        "at": {"type": "integer", "minimum": 0, "maximum": MAX_STEPS},
        "remove": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": _STEP_ID,
        },
        "rename": {"type": "string", "minLength": 1, "maxLength": 80},
        "confirm_merge": {"type": "boolean", "default": False},
    }
)
S_PR_REVIEW = _obj(
    {
        "session": dict(_TITLE, description="the PR's session (default: you)"),
        "pr_url": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "https://github.com/<owner>/<repo>/pull/<n>",
        },
        "plan": {"type": "string", "minLength": 1, "maxLength": 80},
        "confirm_merge": {"type": "boolean", "default": False},
    }
)
S_SAVE_PLAN = _obj(
    {
        "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "a saved route's id or name to replace",
        },
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "steps": _STEPS,
        "description": {"type": "string", "maxLength": 2000},
        "trigger": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["manual", "ticket", "pr", "issue", "schedule"],
                },
                "schedule": {"type": ["object", "null"]},
                "paused": {"type": "boolean"},
            },
            "required": ["kind"],
        },
        "dry_run": {"type": "boolean", "default": False},
    },
    ("name", "steps"),
)


# --------------------------------------------------------------------------- #
# Compact views
# --------------------------------------------------------------------------- #
def _cut(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def compact_kinds(dto: dict) -> List[str]:
    """``GET /api/flights/kinds`` as one line per kind: ``kind · label ·
    summary · fields`` (+ ``[risk]`` when the kind carries any)."""
    out: List[str] = []
    for k in dto.get("kinds") or []:
        if not isinstance(k, dict) or not k.get("kind"):
            continue
        fields = [
            str(f.get("name"))
            for f in k.get("fields") or []
            if isinstance(f, dict) and f.get("name")
        ]
        line = "%s · %s · %s · fields: %s" % (
            k.get("kind"),
            k.get("label") or k.get("kind"),
            _cut(k.get("summary"), 110),
            (cookbook.options_line(k) if fields else "") or "none",
        )
        if k.get("actor") == "you":
            line += " · waits on a person"
        risk = [str(r) for r in k.get("risk") or []]
        if risk:
            line += " · [%s]" % ", ".join(risk)
        out.append(line)
    return out


def cookbook_view(dto: dict, budget: int = DESCRIBE_BUDGET) -> dict:
    """The stop cookbook for the kinds THIS server serves: ``stops`` (one line
    per kind — its options with their defaults and allowed values straight
    from the kinds list, then use / not / waits — or, for a kind newer than
    the cookbook, the server's own summary line), ``recipes`` (common routes
    in the terse ``kind(cfg)!on_fail`` notation), ``rules``. The prose
    shrinks until the whole view fits ``budget`` JSON characters; the
    options never do."""
    kinds = [k for k in dto.get("kinds") or [] if isinstance(k, dict) and k.get("kind")]
    served = {str(k["kind"]) for k in kinds}

    def build(width: int) -> dict:
        stops: List[str] = []
        for k in kinds:
            kind = str(k["kind"])
            opts = cookbook.options_line(k)
            line = cookbook.stop_line(kind, str(k.get("label") or ""), opts)
            if not line:
                line = compact_kinds({"kinds": [k]})[0]
            elif "irreversible" in [str(r) for r in k.get("risk") or []]:
                line += " · [irreversible]"
            # The options come first and are never cut: they are the kind's
            # own definition, what start_flight is checked against.
            head = len(line.split(" · use: ", 1)[0]) if " · use: " in line else 0
            stops.append(_cut(line, max(width, head + 40)))
        recipes = [
            _cut(cookbook.recipe_line(r), width + 120)
            for r in cookbook.RECIPES
            if all(s["kind"] in served for s in r["steps"])
        ]
        return {"stops": stops, "recipes": recipes, "rules": list(cookbook.RULES)}

    width = 1400
    out = build(width)
    while len(json.dumps(out, ensure_ascii=False)) > budget and width > 80:
        width -= 40
        out = build(width)
    return out


#: What to do when a session may not merge itself (scope children): the same
#: stops, grounded — they wait on the user's Sky and the user's attach is the OK.
GROUNDED_WAY = (
    "Start the same steps with grounded=true (and confirm_merge=true, a name) "
    "instead — no need to ask again: they wait on your user's Sky until they "
    "attach this session, and that attach is their OK for the merge"
)

#: describe_flight lists at most this many saved routes.
SAVED_MAX = 15


def saved_lines(plans: List[dict], limit: int = SAVED_MAX) -> List[str]:
    """The shelf, one line per saved route: ``id · name · starts: trigger ·
    kind(cfg) → …`` — so "like last time" can find it (``plan_id`` takes the
    id or the name)."""
    out: List[str] = []
    for p in plans[:limit]:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        trig = p.get("trigger") if isinstance(p.get("trigger"), dict) else {}
        kind = str(trig.get("kind") or "manual")
        sched = trig.get("schedule") if isinstance(trig.get("schedule"), dict) else {}
        if kind == "schedule" and sched:
            kind += " " + _cut(
                " ".join(str(v) for v in sched.values() if v not in ("", None)), 30
            )
        steps = [s for s in p.get("steps") or [] if isinstance(s, dict)]
        out.append(
            _cut(
                "%s · %s · starts: %s · %s"
                % (
                    p["id"],
                    p.get("name") or p["id"],
                    kind,
                    cookbook.notation(steps, 24),
                ),
                260,
            )
        )
    if len(plans) > limit:
        out.append("… %d more saved routes" % (len(plans) - limit))
    return out


def find_plan(plans: List[dict], ref: str) -> Optional[dict]:
    """A saved route by id, else by name (any case), else by a unique name
    prefix; None when nothing (or more than one) matches."""
    ref = str(ref or "").strip()
    rows = [p for p in plans if isinstance(p, dict) and p.get("id")]
    for p in rows:
        if p["id"] == ref:
            return p
    key = ref.casefold()
    named = [p for p in rows if str(p.get("name") or "").casefold() == key]
    if len(named) == 1:
        return named[0]
    pre = [p for p in rows if str(p.get("name") or "").casefold().startswith(key)]
    return pre[0] if len(pre) == 1 and key else None


def _labels(dto: dict) -> Dict[str, str]:
    return {
        str(k.get("kind")): str(k.get("label") or k.get("kind"))
        for k in dto.get("kinds") or []
        if isinstance(k, dict) and k.get("kind")
    }


def read_back(
    steps: List[dict],
    dto: dict,
    on_session: bool = True,
    has_pr: bool = False,
    renumber: Optional[int] = None,
) -> Dict[str, Any]:
    """``{sentence, problems?, warnings?}`` for ``steps``: the route in one
    line to read back to the person, and the cookbook's lint (errors =
    problems, the tools refuse those; warnings = odd, worth saying) — an
    option a kind does not have included, checked against the served kinds.
    ``renumber``: the stops from that index on get the server's ids."""
    known = [str(k.get("kind")) for k in dto.get("kinds") or [] if isinstance(k, dict)]
    issues = cookbook.lint(
        steps,
        known=known or None,
        on_session=on_session,
        has_pr=has_pr,
        fields=cookbook.kind_fields(dto) or None,
        labels=_labels(dto),
        renumber=renumber,
    )
    out: Dict[str, Any] = {"sentence": cookbook.sentence(steps, _labels(dto))}
    probs = [cookbook.issue_line(x) for x in issues if x["level"] == "error"]
    warns = [cookbook.issue_line(x) for x in issues if x["level"] != "error"]
    if probs:
        out["problems"] = probs
    if warns:
        out["warnings"] = warns
    return out


def _refuse(what: str, view: Dict[str, Any]) -> ToolError:
    return ToolError(
        "%s: this route can't work as written — %s. (Route: %s.) Fix those "
        "stops and try again; start_flight(dry_run=true) checks without "
        "starting." % (what, "; ".join(view["problems"]), view["sentence"])
    )


def step_line(step: dict, state: Optional[dict] = None, width: int = 60) -> str:
    """One stop as a line: ``s3 running ship "Open PR" · note … · receipt …``.
    Without ``state`` (a plan's step): ``s3 ship "Open PR" · on_fail agent``."""
    sid = str(step.get("id") or "?")
    parts = [sid]
    if state is not None:
        parts.append(str(state.get("state") or "pending"))
    parts.append(str(step.get("kind") or "?"))
    parts.append('"%s"' % _cut(step.get("title"), width))
    line = " ".join(parts)
    cfg = step.get("config") if isinstance(step.get("config"), dict) else {}
    if state is None:
        bits = [
            "%s=%s" % (k, _cut(v, 40)) for k, v in cfg.items() if v not in ("", None)
        ]
        if bits:
            line += " · " + "; ".join(bits[:4])
        if step.get("on_fail") and step.get("on_fail") != "halt":
            line += " · on_fail " + str(step["on_fail"])
    if step.get("when"):
        line += " · when " + _cut(step["when"], 60)
    if step.get("halt"):
        line += " · halt point"
    if isinstance(step.get("group"), dict) and step["group"].get("name"):
        line += " · in " + _cut(step["group"]["name"], 30)
    if state is not None:
        detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
        if state.get("note"):
            line += " · " + _cut(state["note"], width + 20)
        if detail.get("receipt"):
            line += " · ✓ " + _cut(detail["receipt"], max(12, width - 10))
    return line


def _history_line(h: dict, now: float, width: int = 90) -> str:
    try:
        ago = max(0, int(now - float(h.get("at") or 0)))
    except (TypeError, ValueError):
        ago = 0
    when = (
        "%ds" % ago
        if ago < 90
        else "%dm" % (ago // 60) if ago < 5400 else "%dh" % (ago // 3600)
    )
    line = "-%s %s %s" % (when, h.get("step") or "-", h.get("event") or "?")
    if h.get("note"):
        line += " — " + _cut(h["note"], width)
    return line


def compact_flight(f: dict, now: float, budget: int = STATUS_BUDGET) -> dict:
    """A flight as the tools return it, about ``budget`` JSON characters:
    flown stops far behind the cursor fold into a count, then titles, notes
    and history shrink, then the stops far ahead fold too."""
    steps = [s for s in f.get("steps") or [] if isinstance(s, dict)]
    states = f.get("step_states") if isinstance(f.get("step_states"), dict) else {}
    try:
        cursor = int(f.get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    current = steps[cursor] if 0 <= cursor < len(steps) else None
    head: Dict[str, Any] = {
        "id": f.get("id"),
        "name": f.get("plan_name"),
        "session": f.get("session") or None,
        "state": f.get("state"),
        "cursor": (
            "%d/%d %s" % (cursor + 1, len(steps), current.get("id"))
            if current
            else "%d/%d (past the last stop)" % (cursor, len(steps))
        ),
    }
    if f.get("reason"):
        head["reason"] = _cut(f["reason"], 200)
    if f.get("by") and f.get("by") != "user":
        head["by"] = f["by"]
    if f.get("state") == "ready":
        head["start_on"] = f.get("start_on") or "prompt"
    history = [h for h in f.get("history") or [] if isinstance(h, dict)]
    keep = HISTORY_LINES

    def fold(part: List[dict]) -> str:
        counts: Dict[str, int] = {}
        for s in part:
            st = str((states.get(s.get("id")) or {}).get("state") or "pending")
            counts[st] = counts.get(st, 0) + 1
        return "%s–%s: %s" % (
            part[0].get("id"),
            part[-1].get("id"),
            ", ".join("%d %s" % (n, st) for st, n in counts.items()),
        )

    def render(width: int, before: int, after: int) -> dict:
        lines: List[str] = [fold(steps[:before])] if before > 0 else []
        for s in steps[before:after]:
            lines.append(step_line(s, states.get(s.get("id")) or {}, width))
        if after < len(steps):
            lines.append(fold(steps[after:]))
        hist = [_history_line(h, now, width + 30) for h in history[-keep:]]
        return dict(head, steps=lines, history=hist)

    before, after, width = 0, len(steps), 60
    out = render(width, before, after)
    while len(json.dumps(out, ensure_ascii=False)) > budget:
        if before < cursor - 1:
            before = cursor - 1  # flown stops far behind fold into a count
        elif width > 12:
            width -= 12
        elif keep > 3:
            keep = 3
        elif after > cursor + 6:
            after = cursor + 6  # and, last, the stops far ahead
        else:
            break
        out = render(width, before, after)
    return out


def _irreversible(steps: List[dict], kinds: Dict[str, dict]) -> List[str]:
    """Titles of the stops that cannot be undone: the kind's ``risk`` says
    ``irreversible`` (served by the kinds list), or a ship stop's lane is
    merge (the frontend's ``riskOf`` rule)."""
    out: List[str] = []
    for s in steps:
        kind = str(s.get("kind") or "")
        cfg = s.get("config") if isinstance(s.get("config"), dict) else {}
        risk = (kinds.get(kind) or {}).get("risk") or []
        if "irreversible" in risk or (kind == "ship" and cfg.get("lane") == "merge"):
            out.append(str(s.get("title") or kind))
    return out


#: What to do after a refusal, by its machine word.
ERROR_HINTS = {
    "in_flight": "that session already has a route running: "
    "flight_status(title=…) shows it; cancel it first or edit_flight it",
    "in_progress": "only stops after the current one can change",
    "needs_title": "name the session (title) or start it grounded=true",
    "unknown_flight": "flight_status(title=…) finds a session's route",
    "unknown_step": "flight_status lists the route's stop ids",
    "unknown_session": "list_sessions shows the sessions you may use",
    "last_step": "a route keeps its last stop: cancel the route instead",
    "finished": "that route is over",
    "grounded": "a grounded route does nothing until your user attaches "
    "a session to it (only cancel works)",
    "not_waiting": "that stop is not waiting on a person: flight_status "
    "shows what it waits on (retry / skip / resume may be what you want)",
    "answer_behaviors": "a Verify stop waiting on its steps moves on when "
    "each step has an answer (Confirm verified / Not verified… on its card); "
    "ask your user, or skip it if they said so",
    "not_needs_you": "that Verify stop is not waiting for a confirm now",
    "preflight": "fix it, then start_flight(dry_run=true) to check again",
}

_FIELD_RE = re.compile(r"^(?:steps\[(\d+)\]|step)\.(\w+)(?:\.(\w+))?")


def choices_for(
    dto: dict, field: str, detail: str = "", steps: Optional[List[dict]] = None
) -> str:
    """The allowed values behind a refused ``field`` (``steps[2].kind``,
    ``steps[0].config.lane``, ``step.config.who``, ``…on_fail``), from the
    kinds ``dto``: ``""`` when it is not a closed list. ``steps``: the stops
    the field indexes (``step.…`` = the first), to know an option's kind."""
    m = _FIELD_RE.match(str(field or ""))
    if not m:
        return ""
    idx = int(m.group(1)) if m.group(1) is not None else 0
    part, sub = m.group(2) or "", m.group(3) or ""
    kinds = {
        str(k.get("kind")): k
        for k in dto.get("kinds") or []
        if isinstance(k, dict) and k.get("kind")
    }
    if part == "kind":
        return "stop kinds: %s" % ", ".join(kinds) if kinds else ""
    if part == "on_fail":
        return "on_fail is one of: %s" % ", ".join(
            dto.get("on_fail") or ["halt", "agent", "skip", "you"]
        )
    if part != "config" or not sub:
        return ""
    rows = [x for x in steps or [] if isinstance(x, dict)]
    kind = str(rows[idx].get("kind") or "") if 0 <= idx < len(rows) else ""
    spec = kinds.get(kind)
    if spec is None:
        return ""
    fields = [f for f in spec.get("fields") or [] if isinstance(f, dict)]
    hit = next((f for f in fields if f.get("name") == sub), None)
    if hit is None:
        if "it has:" in detail:
            return ""  # the server listed them already
        return "%s options: %s" % (
            spec.get("label") or kind,
            cookbook.options_line(spec),
        )
    opts = [str(o) for o in hit.get("options") or []]
    if not opts:
        return ""
    words = (
        hit.get("option_labels") if isinstance(hit.get("option_labels"), dict) else {}
    )
    return "%s is one of: %s" % (
        sub,
        " | ".join("%s (%s)" % (o, words[o]) if words.get(o) else o for o in opts),
    )


def problem_line(
    p: dict,
    dto: dict,
    steps: Optional[List[dict]] = None,
    environments: Optional[List[str]] = None,
) -> str:
    """A server problem (``{field, detail, error?, session?}``) as one line
    that says what to change: ``steps[0].config.who: who is agent, you or
    both — who is one of: both (Agent, then me) | …``."""
    field = str(p.get("field") or "")
    detail = str(p.get("detail") or p.get("error") or "")
    line = "%s: %s" % (field or "route", _cut(detail, 300))
    extra = [choices_for(dto, field, detail, steps), cookbook.field_hint(field, detail)]
    if field.endswith(".where") and environments is not None and "it has" not in detail:
        extra.append(
            "where is branch or one of the repo's environments: %s"
            % (", ".join(environments) or "none configured")
        )
    if field == "start_at":
        extra.append("at is a stop id from lines (s1, s2…)")
    if p.get("error") in ERROR_HINTS and p.get("error") != "preflight":
        extra.append(ERROR_HINTS[str(p["error"])])
    more = [x for x in extra if x]
    if more:
        line += " — " + ". ".join(more)
    if p.get("session"):
        line += " [session %s]" % p["session"]
    return line


def _first_stop_text(title: str, sid: str) -> str:
    """Why a leading Agent stop is not sent on an existing session."""
    return (
        "not sent: on %s the work so far is the first Agent stop (still on "
        "its turn, the route waits for it to end; idle, it goes straight on "
        "to the next stop). To send its prompt, pass at=%r." % (title, sid)
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
class FlightTools:
    """The seven flight handlers; mixed into ``Toolbox``."""

    # -- helpers -------------------------------------------------------------- #
    def _fl_api(
        self, method: str, path: str, payload=None, what: str = "", steps=None, **kw
    ):
        try:
            if method == "GET":
                resp = self.api.get(path, **kw)
            elif method == "DELETE":
                resp = self.api.delete(path, **kw)
            elif method == "PATCH":
                resp = self.api.patch(path, payload or {}, **kw)
            else:
                resp = self.api.post(path, payload or {}, **kw)
        except client.ApiError as err:
            raise self._flight_error(err, what, steps) from None
        return resp if isinstance(resp, dict) else {}

    def _flight_error(
        self, err: client.ApiError, what: str, steps: Optional[List[dict]] = None
    ) -> ToolError:
        """The server's refusal as an agent can act on it: its word, its
        ``detail`` and ``field``, the allowed values when the field is a
        closed list, every other problem it named, and what to do next."""
        body = err.payload or {}
        if err.status in (404, 405) and (
            "Not Found" in err.message or err.status == 405
        ):
            return ToolError(
                "this MindFlock server has no routes; upgrade it to use %s" % what
            )
        msg = "%s failed: %s" % (what, err.message)
        if body.get("detail"):
            msg += " — %s" % _cut(body["detail"], 300)
        if body.get("field"):
            msg += " (field %s)" % body["field"]
            more = [
                x
                for x in (
                    self._choices(
                        str(body["field"]), str(body.get("detail") or ""), steps
                    ),
                    cookbook.field_hint(
                        str(body["field"]), str(body.get("detail") or "")
                    ),
                )
                if x
            ]
            if more:
                msg += ". " + ". ".join(more)
        if body.get("session"):
            msg += " [session %s]" % body["session"]
        rest = [p for p in body.get("problems") or [] if isinstance(p, dict)][1:]
        if rest:
            msg += ". Also: " + "; ".join(
                "%s: %s" % (p.get("field") or "route", _cut(p.get("detail"), 200))
                for p in rest
            )
        hint = ERROR_HINTS.get(err.message)
        if hint:
            msg += ". " + hint
        return ToolError(msg)

    def _choices(
        self, field: str, detail: str = "", steps: Optional[List[dict]] = None
    ) -> str:
        """The allowed values for a refused ``field`` when they are a closed
        list (stop kinds, a choice option's values, on_fail), from the served
        kinds; ``""`` otherwise or when the kinds can't be read."""
        if not _FIELD_RE.match(str(field or "")):
            return ""
        try:
            dto = self._kinds()
        except ToolError:
            return ""
        return choices_for(dto, field, detail, steps)

    def _check(self, payload: dict, what: str) -> Optional[dict]:
        """``POST /api/flights/check`` (the server's own check of a start,
        nothing created); None on a server that has no such route."""
        try:
            resp = self.api.post("/api/flights/check", payload, timeout=START_TIMEOUT_S)
        except client.ApiError as err:
            if err.status in (404, 405):
                return None
            raise self._flight_error(err, what) from None
        return resp if isinstance(resp, dict) else None

    def _kinds(self) -> dict:
        return self._fl_api("GET", "/api/flights/kinds", what="flight kinds")

    def _kinds_by_name(self) -> Dict[str, dict]:
        return {
            str(k.get("kind")): k
            for k in self._kinds().get("kinds") or []
            if isinstance(k, dict)
        }

    @staticmethod
    def _flight_path(fid: str, suffix: str = "") -> str:
        return "/api/flights/" + urllib.parse.quote(fid, safe="") + suffix

    def _get_flight(self, fid: str, what: str) -> dict:
        resp = self._fl_api("GET", self._flight_path(fid), what=what)
        f = resp.get("flight")
        if not isinstance(f, dict):
            raise ToolError("%s: the server returned no route" % what)
        return f

    def _flight_of(self, title: str, what: str) -> Optional[dict]:
        """``title``'s live flight, else its most recent one (None: none)."""
        resp = self._fl_api("GET", "/api/flights", what=what)
        mine = [
            f
            for f in resp.get("flights") or []
            if isinstance(f, dict) and f.get("session") == title
        ]
        if not mine:
            return None
        live = [f for f in mine if f.get("state") in UNFINISHED]
        pool = live or mine
        return max(pool, key=lambda f: float(f.get("updated") or 0))

    def _resolve_flight(self, flock: Flock, args: dict, what: str) -> dict:
        fid = str(args.get("id") or "").strip()
        if fid:
            return self._get_flight(fid, what)
        title = str(args.get("title") or "").strip() or (flock.self_title or "")
        if not title:
            raise ToolError(
                "%s needs id or title: you are not inside a MindFlock session" % what
            )
        f = self._flight_of(title, what)
        if f is None:
            raise ToolError(
                "%s: %r has no route (start_flight starts one)" % (what, title)
            )
        return f

    def _may_fly(
        self, flock: Flock, title: str, what: str, steps=(), confirm: bool = False
    ) -> None:
        """Any local session (steering: ``require_ship`` with ``anyone``); a
        route with a stop that can't be undone needs you or a descendant, as
        a merge does. A grounded flight (no session) needs only write scope."""
        if not title:
            self.policy.require_write(flock, what)
            if steps:
                self._require_confirm(list(steps), confirm, what)
            return
        bad = _irreversible(list(steps), self._kinds_by_name()) if steps else []
        try:
            self.policy.require_ship(
                flock, title, what, merge=bool(bad), confirm_merge=confirm, anyone=True
            )
        except ToolError as err:
            if bad and "merg" in str(err):
                raise ToolError(
                    "%s (stops that can't be undone: %s). %s"
                    % (err, ", ".join(bad), GROUNDED_WAY)
                ) from None
            raise

    def _require_confirm(self, steps: List[dict], confirm: bool, what: str) -> None:
        bad = _irreversible(steps, self._kinds_by_name())
        if bad and not confirm:
            raise ToolError(
                "%s: %s can't be undone — pass confirm_merge=true only when your "
                "user (or your task) clearly asked for it" % (what, ", ".join(bad))
            )

    def _by(self, flock: Flock) -> str:
        return ("agent:" + flock.self_title) if flock.self_title else "user"

    @staticmethod
    def _clean_steps(raw) -> List[dict]:
        return [dict(s) for s in raw or [] if isinstance(s, dict)]

    # -- describe_flight -------------------------------------------------------- #
    def describe_flight(self, args: dict, ctx: ToolContext) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError("describe_flight needs text")
        want = args.get("want") or "plan"
        out: Dict[str, Any] = {}
        try:
            resp = self.api.post(
                "/api/flights/describe",
                {"text": text, "want": want},
                timeout=DESCRIBE_TIMEOUT_S,
            )
            resp = resp if isinstance(resp, dict) else {}
            steps = self._clean_steps(resp.get("steps"))
            out["steps"] = steps
            out["lines"] = [step_line(s) for s in steps]
            if resp.get("model"):
                out["model"] = resp["model"]
        except client.ApiError as err:
            if err.message == "no_model":
                out["steps"] = []
                out["error"] = "no_model"
                out["hint"] = (
                    "no model CLI is installed for the server's one-shot describe: "
                    "write the steps yourself from the stops and recipes below, "
                    "then start_flight(dry_run=true)"
                )
            elif err.message == "could_not_describe":
                out["steps"] = []
                out["error"] = "could_not_describe"
                detail = (err.payload or {}).get("detail")
                out["hint"] = (
                    "the model's answer was not a route%s: write the steps "
                    "yourself from the stops and recipes below"
                    % (" (%s)" % _cut(detail, 200) if detail else "")
                )
            else:
                raise self._flight_error(err, "describe_flight") from None
        full = bool(args.get("kinds", True) or out.get("error"))
        # kinds=false: the agent has the cookbook already — no kinds fetch;
        # the read-back lints against the cookbook's own kinds.
        dto = self._kinds() if full else {}
        if out.get("steps"):
            out.update(read_back(out["steps"], dto))
        if full:
            out.update(cookbook_view(dto))
            if dto.get("on_fail"):
                out["on_fail"] = list(dto["on_fail"])
            try:
                plans = self._plans()
            except ToolError:
                plans = []
            if plans:
                out["saved"] = saved_lines(plans)
        return out

    # -- start_flight ----------------------------------------------------------- #
    def start_flight(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        what = "start_flight"
        steps = self._clean_steps(args.get("steps"))
        plan_id = str(args.get("plan_id") or "").strip()
        if not steps and not plan_id:
            raise ToolError(
                "start_flight needs steps (describe_flight writes them) or plan_id"
            )
        grounded = bool(args.get("grounded"))
        name = str(args.get("name") or "").strip()
        confirm = bool(args.get("confirm_merge"))
        title = ""
        if not grounded:
            title = str(args.get("title") or "").strip() or (flock.self_title or "")
            if not title:
                raise ToolError(
                    "start_flight needs title (the session that runs it): you are "
                    "not inside a MindFlock session — or pass grounded=true"
                )
        check = list(steps)
        base = 0
        if plan_id:
            plans = self._plans()
            plan = find_plan(plans, plan_id)
            if plan is None and not plan_id.startswith("builtin:"):
                names = ", ".join(
                    "%s (%s)" % (p.get("name"), p.get("id")) for p in plans[:SAVED_MAX]
                )
                raise ToolError(
                    "start_flight: no saved route %r — plan_id is a saved route's "
                    "id or name: %s" % (plan_id, names or "none saved yet")
                )
            plan = plan or {}
            plan_id = str(plan.get("id") or plan_id)
            check = list(plan.get("steps") or []) + check
            base = len(plan.get("steps") or [])
        dto = self._kinds()
        # The server gives a start's own stops the next sN (their ids are
        # dropped): the lint judges a Loop's back_to by those.
        view = (
            read_back(
                check, dto, on_session=not grounded, renumber=0 if grounded else base
            )
            if check
            else {}
        )
        payload: Dict[str, Any] = {"by": self._by(flock)}
        if plan_id:
            payload["plan_id"] = plan_id
        if steps:
            payload["steps"] = steps
        if grounded:
            payload["grounded"] = True
            if name:
                payload["name"] = name
        else:
            payload["title"] = title
            if args.get("at"):
                payload["start_at"] = str(args["at"])
        if args.get("start_on") in ("prompt", "run"):
            payload["start_on"] = args["start_on"]
        if args.get("dry_run"):
            return self._dry_run(flock, args, payload, check, view, dto, title)
        if view.get("problems"):
            raise _refuse(what, view)
        self._may_fly(flock, title, what, check, confirm)
        resp = self._fl_api(
            "POST",
            "/api/flights/start",
            payload,
            what,
            steps=steps,
            timeout=START_TIMEOUT_S,
        )
        f = resp.get("flight")
        if not isinstance(f, dict):
            raise ToolError("start_flight: the server returned no route")
        if name and not grounded and f.get("id"):
            try:
                r = self._fl_api(
                    "PATCH",
                    self._flight_path(str(f["id"])),
                    {"plan_name": name},
                    what + " (rename)",
                )
                f = r.get("flight") if isinstance(r.get("flight"), dict) else f
            except ToolError as err:
                _log.info("start_flight: rename failed: %s", err)
        compact = compact_flight(f, self.clock())
        out = {
            "id": f.get("id"),
            "state": f.get("state"),
            "session": f.get("session") or None,
            "name": f.get("plan_name"),
            "cursor": compact.get("cursor"),
            "steps": compact.get("steps"),
        }
        flown = [s for s in f.get("steps") or [] if isinstance(s, dict)]
        out["sentence"] = cookbook.sentence(flown or check, _labels(dto))
        if view.get("warnings"):
            out["warnings"] = view["warnings"]
        if resp.get("absorbed"):
            out["absorbed"] = resp["absorbed"]
            out["note"] = (
                "the session's own Goes as far as shipping became the route's leading "
                "ship stops"
            )
        if grounded:
            out["note"] = (
                "grounded: the stops sit on your user's Sky until they attach a "
                "session"
            )
        elif f.get("state") == "ready":
            out["note"] = (
                "ready: it takes off with the session's next prompt (or Run)"
                if f.get("start_on") == "prompt"
                else "ready: it takes off when your user presses Run "
                "(or control_flight op=run)"
            )
        return out

    def _dry_run(
        self,
        flock: Flock,
        args: dict,
        payload: dict,
        check: List[dict],
        view: Dict[str, Any],
        dto: dict,
        title: str,
    ) -> dict:
        """start_flight(dry_run=true): the server's own check of the very
        start (``POST /api/flights/check``) — the stops as it stores them,
        where it starts and what it skips, the session's current route and
        every refusal — plus the cookbook's lint. ``ok`` = the real start
        will be accepted. An older server without the check: the lint alone,
        and it says so."""
        what = "start_flight"
        chk = self._check(payload, what + "(dry_run)")
        out: Dict[str, Any] = dict(view, dry_run=True, session=title or None)
        problems = list(view.get("problems") or [])
        warnings = list(view.get("warnings") or [])
        flown = check
        if chk is None:
            out["checked"] = (
                "this server has no POST /api/flights/check: only the cookbook's "
                "lint ran, so the real start may still refuse an option"
            )
            out["lines"] = [step_line(s) for s in check]
            if (
                title
                and check
                and str(check[0].get("kind") or "") == "agent"
                and not args.get("at")
                and args.get("start_on") not in ("prompt", "run")
            ):
                out["first_stop"] = _first_stop_text(title, check[0].get("id") or "s1")
        else:
            flown = [s for s in chk.get("steps") or [] if isinstance(s, dict)]
            raw = self._clean_steps(args.get("steps"))
            envs = chk.get("environments")
            for p in chk.get("problems") or []:
                if isinstance(p, dict):
                    problems.append(problem_line(p, dto, raw, envs))
            for p in chk.get("warnings") or []:
                if isinstance(p, dict):
                    warnings.append(problem_line(p, dto, raw, envs))
            start = chk.get("start") if isinstance(chk.get("start"), dict) else {}
            skipped = {
                str(x.get("id")): str(x.get("reason") or "")
                for x in chk.get("skipped") or []
                if isinstance(x, dict)
            }
            lines = []
            for st in flown:
                line = step_line(st)
                if st.get("id") in skipped:
                    line = "skipped · %s — %s" % (line, skipped[st["id"]])
                elif st.get("id") == start.get("id"):
                    line = "▶ " + line
                lines.append(line)
            out["lines"] = lines
            if start:
                out["start"] = '%s "%s"' % (start.get("id"), start.get("title"))
            if chk.get("in_flight"):
                out["in_flight"] = chk["in_flight"]
            res = chk.get("resume") if isinstance(chk.get("resume"), dict) else None
            if res:
                why = res.get("reason") or res.get("state") or ""
                out["resume"] = '%s "%s" — route %s stopped there%s; %s' % (
                    res.get("stop"),
                    res.get("title"),
                    res.get("from_flight"),
                    " (%s)" % why if why else "",
                    (
                        "starting there"
                        if args.get("at") == "resume"
                        else "at='resume' carries on from there"
                    ),
                )
            if chk.get("absorbed"):
                out["absorbed"] = chk["absorbed"]
            if envs is not None and any(s.get("kind") == "verify" for s in flown):
                out["environments"] = envs
            if flown:
                out["sentence"] = cookbook.sentence(flown, _labels(dto))
            first = flown[0] if flown else {}
            if (
                title
                and first.get("kind") == "agent"
                and first.get("id") in skipped
                and not args.get("at")
            ):
                out["first_stop"] = _first_stop_text(title, first.get("id"))
        out.pop("problems", None)
        out.pop("warnings", None)
        if problems:
            out["problems"] = problems
        if warnings:
            out["warnings"] = warnings
        out["ok"] = not problems
        bad = _irreversible(flown or check, self._kinds_by_name())
        nxt = (
            "read the sentence back to your user; on their OK call "
            "start_flight with the same arguments and no dry_run"
        )
        if bad:
            out["needs_confirm_merge"] = bad
            if title:
                try:
                    self.policy.require_ship(
                        flock, title, what, merge=True, confirm_merge=True
                    )
                except ToolError as err:
                    out["merge_blocked"] = "%s. %s" % (err, GROUNDED_WAY)
                    nxt = (
                        "read the sentence back and say it will wait on their "
                        "Sky for them to attach this session; on their OK call "
                        "start_flight with grounded=true, the same steps, "
                        "confirm_merge=true and a name"
                    )
        out["next"] = (
            "fix the problems first (each says what to change), then "
            "start_flight(dry_run=true) again"
            if problems
            else nxt
        )
        return out

    def _plans(self) -> List[dict]:
        resp = self._fl_api("GET", "/api/flights/plans", what="saved routes")
        return [p for p in resp.get("plans") or [] if isinstance(p, dict)]

    # -- flight_status ---------------------------------------------------------- #
    def flight_status(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        f = self._resolve_flight(flock, args, "flight_status")
        return compact_flight(f, self.clock())

    # -- control_flight --------------------------------------------------------- #
    def control_flight(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        op = str(args.get("op") or "")
        if op not in CONTROL_OPS:
            raise ToolError("control_flight: op is one of %s" % ", ".join(CONTROL_OPS))
        what = "control_flight(%s)" % op
        f = self._resolve_flight(flock, args, what)
        fid = str(f.get("id") or "")
        if op == "again":
            return self._again(flock, args, f, what)
        self._may_fly(flock, str(f.get("session") or ""), what)
        if op in FLIGHT_OPS:
            path, payload = self._flight_path(fid, "/" + op), {}
        else:
            step = str(args.get("step") or "").strip()
            if not step and op != "jump":
                steps = f.get("steps") or []
                cur = int(f.get("cursor") or 0)
                if 0 <= cur < len(steps) and isinstance(steps[cur], dict):
                    step = str(steps[cur].get("id") or "")
            if not step:
                raise ToolError(
                    "%s needs step (a stop id from flight_status)%s"
                    % (what, "" if op == "jump" else ": the route has no current stop")
                )
            if op == "jump":
                path, payload = self._flight_path(fid, "/jump"), {"step": step}
            else:
                q = urllib.parse.quote(step, safe="")
                path = self._flight_path(fid, "/steps/%s/%s" % (q, op))
                payload = {}
                if op == "done" and args.get("note"):
                    payload["note"] = str(args["note"])
        resp = self._fl_api("POST", path, payload, what)
        nf = resp.get("flight")
        return compact_flight(nf if isinstance(nf, dict) else f, self.clock())

    def _again(self, flock: Flock, args: dict, f: dict, what: str) -> dict:
        """control_flight(op=again): ``POST /api/flights/{id}/again`` — the
        route flown again as a new route (``step`` = the stop to start at).
        Steering a session's route: its session when it is still here (a
        gone one: write scope), and confirm_merge for a stop that can't be
        undone, as start_flight asks."""
        steps = [s for s in f.get("steps") or [] if isinstance(s, dict)]
        title = str(f.get("session") or "")
        if title not in flock.local:
            title = ""
        self._may_fly(flock, title, what, steps, bool(args.get("confirm_merge")))
        payload: Dict[str, Any] = {"by": self._by(flock)}
        step = str(args.get("step") or "").strip()
        if step:
            payload["at"] = step
        resp = self._fl_api(
            "POST",
            self._flight_path(str(f.get("id") or ""), "/again"),
            payload,
            what,
            timeout=START_TIMEOUT_S,
        )
        nf = resp.get("flight")
        out = compact_flight(nf if isinstance(nf, dict) else f, self.clock())
        out["again_of"] = resp.get("from") or f.get("id")
        if resp.get("how"):
            out["how"] = resp["how"]
        return out

    # -- edit_flight ------------------------------------------------------------ #
    def edit_flight(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        what = "edit_flight"
        add = self._clean_steps(args.get("add"))
        remove = [str(s).strip() for s in args.get("remove") or [] if str(s).strip()]
        edits = [dict(e) for e in args.get("edit") or [] if isinstance(e, dict)]
        rename = str(args.get("rename") or "").strip()
        if not (add or remove or rename or edits):
            raise ToolError("edit_flight needs add, remove, edit or rename")
        f = self._resolve_flight(flock, args, what)
        fid = str(f.get("id") or "")
        have = [s for s in f.get("steps") or [] if isinstance(s, dict)]
        ids = [str(s.get("id") or "") for s in have]
        bad, patches = self._edit_plan(f, have, ids, remove, edits)
        after = [dict(s) for s in have if str(s.get("id") or "") not in set(remove)]
        for k, s in enumerate(after):
            if s.get("id") in patches:
                merged = dict(s)
                for key, val in patches[s["id"]].items():
                    merged[key] = val
                if merged.get("when") == "":
                    merged.pop("when", None)
                after[k] = merged
        first = self._first_open(f, have, remove)
        at = args.get("at")
        if add and at is not None and int(at) < first:
            bad.append(
                "at %d is at or before the current stop — a stop is added after "
                "it: at=%d or more (or leave at out to append)" % (int(at), first)
            )
        at = len(after) if at is None else max(0, min(int(at), len(after)))
        after = after[:at] + add + after[at:]
        dto = self._kinds()
        if add or edits:
            known = [str(k.get("kind")) for k in dto.get("kinds") or []]
            old = {
                x["text"]
                for x in cookbook.errors(
                    cookbook.lint(
                        have,
                        known=known or None,
                        fields=cookbook.kind_fields(dto) or None,
                        labels=_labels(dto),
                    )
                )
            }
            view = read_back(after, dto, on_session=bool(f.get("session")))
            bad += [
                p for p in view.get("problems") or [] if p.split(": ", 1)[-1] not in old
            ]
            bad += self._server_check(f, after, patches, add, dto)
        if f.get("state") in ("done", "cancelled"):
            bad.insert(0, "that route is %s: its stops can't change" % f["state"])
        if bad:
            raise ToolError(
                "edit_flight: nothing was changed — %s. (Route: %s.)"
                % ("; ".join(bad), cookbook.sentence(after, _labels(dto)))
            )
        touched = add + [s for s in after if s.get("id") in patches]
        self._may_fly(
            flock,
            str(f.get("session") or ""),
            what,
            touched,
            bool(args.get("confirm_merge")),
        )
        ops: List[Tuple[str, str, str, Optional[dict], Optional[List[dict]]]] = []
        for sid in remove:
            q = urllib.parse.quote(sid, safe="")
            ops.append(
                (
                    "removed " + sid,
                    "DELETE",
                    self._flight_path(fid, "/steps/" + q),
                    None,
                    None,
                )
            )
        for sid, patch in patches.items():
            q = urllib.parse.quote(sid, safe="")
            ops.append(
                (
                    "edited %s (%s)" % (sid, ", ".join(patch)),
                    "PATCH",
                    self._flight_path(fid, "/steps/" + q),
                    patch,
                    [next(s for s in have if s.get("id") == sid)],
                )
            )
        if add:
            payload: Dict[str, Any] = {"steps": add}
            if args.get("at") is not None:
                payload["at"] = int(args["at"])
            ops.append(
                (
                    "added %d" % len(add),
                    "POST",
                    self._flight_path(fid, "/steps"),
                    payload,
                    add,
                )
            )
        if rename:
            ops.append(
                (
                    "renamed",
                    "PATCH",
                    self._flight_path(fid),
                    {"plan_name": rename},
                    None,
                )
            )
        done: List[str] = []
        for k, (label, method, path, body, ctx_steps) in enumerate(ops):
            try:
                r = self._fl_api(method, path, body, what, steps=ctx_steps)
            except ToolError as err:
                left = [
                    op[0]
                    .replace("removed", "remove")
                    .replace("edited", "edit")
                    .replace("added", "add")
                    .replace("renamed", "rename")
                    for op in ops[k:]
                ]
                raise ToolError(
                    "%s. Applied: %s. Not applied: %s — flight_status shows the "
                    "route as it is now"
                    % (err, ", ".join(done) or "nothing", ", ".join(left))
                ) from None
            f = r.get("flight") if isinstance(r.get("flight"), dict) else f
            done.append(label)
        out = compact_flight(f, self.clock())
        out["changed"] = done
        return out

    @staticmethod
    def _first_open(f: dict, have: List[dict], remove: List[str]) -> int:
        """The first index a stop may be added at once ``remove`` is gone:
        any on a grounded / ready route, else after the current stop."""
        if f.get("state") in ("grounded", "ready"):
            return 0
        try:
            cursor = int(f.get("cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        gone = set(remove)
        before = sum(1 for s in have[:cursor] if s.get("id") in gone)
        here = 1 if cursor < len(have) and have[cursor].get("id") in gone else 0
        return cursor - before + 1 - here

    @staticmethod
    def _edit_plan(
        f: dict, have: List[dict], ids: List[str], remove: List[str], edits: List[dict]
    ) -> Tuple[List[str], Dict[str, dict]]:
        """``(problems, {sid: PATCH body})`` for edit_flight's ``remove`` and
        ``edit`` — everything the server would refuse that can be known
        before writing. A config is MERGED into the stop's own (the PATCH
        replaces the whole config)."""
        bad: List[str] = []
        states = f.get("step_states") if isinstance(f.get("step_states"), dict) else {}
        listed = ", ".join(ids) or "none"
        for sid in remove:
            if sid not in ids:
                bad.append("remove %s: no such stop (stops: %s)" % (sid, listed))
        if ids and set(ids) <= set(remove):
            bad.append("a route keeps its last stop: cancel the route instead")
        try:
            cursor = int(f.get("cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        ground = f.get("state") in ("grounded", "ready")
        patches: Dict[str, dict] = {}
        for e in edits:
            sid = str(e.get("step") or "").strip()
            keys = [k for k in ("title", "config", "on_fail", "when", "halt") if k in e]
            if sid not in ids:
                bad.append("edit %s: no such stop (stops: %s)" % (sid, listed))
                continue
            if sid in remove:
                bad.append("edit %s: it is also removed — do one or the other" % sid)
                continue
            if sid in patches:
                bad.append(
                    "edit %s: one edit per stop — put the changes together" % sid
                )
                continue
            if not keys:
                bad.append(
                    "edit %s: say what to change (title, config, on_fail, when or halt)"
                    % sid
                )
                continue
            i = ids.index(sid)
            st = (states.get(sid) or {}).get("state") or "pending"
            if keys != ["halt"] and not ground and (i <= cursor or st != "pending"):
                bad.append(
                    "edit %s: it has already started (%s) — only a stop still ahead "
                    "can change; halt alone may change on the current stop" % (sid, st)
                )
                continue
            patch = {k: e[k] for k in keys}
            if "config" in patch:
                if not isinstance(patch["config"], dict):
                    bad.append("edit %s: config is an object of options" % sid)
                    continue
                own = (
                    have[i].get("config")
                    if isinstance(have[i].get("config"), dict)
                    else {}
                )
                patch["config"] = dict(own, **patch["config"])
            patches[sid] = patch
        return bad, patches

    def _server_check(
        self,
        f: dict,
        after: List[dict],
        patches: Dict[str, dict],
        add: List[dict],
        dto: dict,
    ) -> List[str]:
        """The edited and added stops through the server's strict check
        (``POST /api/flights/check``, nothing written): each problem names the
        edit or add it is about. A grounded route may leave an option to fill
        in later; one in the air may not. [] on an older server."""
        edited = [s for s in after if s.get("id") in patches]
        rows = edited + add
        if not rows:
            return []
        body = {"grounded": True, "steps": [dict(s, id="") for s in rows]}
        chk = self._check(body, "edit_flight")
        if chk is None:
            return []
        found = [p for p in chk.get("problems") or [] if isinstance(p, dict)]
        if f.get("state") != "grounded":
            found += [
                dict(
                    p,
                    detail=str(p.get("detail") or "").replace(
                        "before it can fly: ", ""
                    ),
                )
                for p in chk.get("warnings") or []
                if isinstance(p, dict) and p.get("error") == "incomplete"
            ]
        out = []
        for p in found:
            field = str(p.get("field") or "")
            m = _FIELD_RE.match(field)
            k = int(m.group(1)) if m and m.group(1) is not None else -1
            if 0 <= k < len(edited):
                who, local = "edit %s" % edited[k].get("id"), field
            elif k >= len(edited):
                who = "add[%d]" % (k - len(edited))
                local = field.replace(
                    "steps[%d]" % k, "steps[%d]" % (k - len(edited)), 1
                )
            else:
                who, local = "route", field
            line = problem_line(p, dto, rows)
            if field and local != field:
                line = line.replace(field, local, 1)
            out.append("%s: %s" % (who, line))
        return out

    # -- save_flight_plan ------------------------------------------------------- #
    def save_flight_plan(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        what = "save_flight_plan"
        self.policy.require_write(flock, what)
        steps = self._clean_steps(args.get("steps"))
        if not steps:
            raise ToolError("save_flight_plan needs steps")
        old: Optional[dict] = None
        ref = str(args.get("id") or "").strip()
        if ref:
            plans = self._plans()
            old = find_plan(plans, ref)
            if old is None:
                names = ", ".join(
                    "%s (%s)" % (p.get("name"), p.get("id")) for p in plans[:SAVED_MAX]
                )
                raise ToolError(
                    "save_flight_plan: no saved route %r to replace — id is a saved "
                    "route's id or name: %s (leave id out to save a new one)"
                    % (ref, names or "none saved yet")
                )
        trig = args.get("trigger") if isinstance(args.get("trigger"), dict) else None
        if trig is None and old is not None and isinstance(old.get("trigger"), dict):
            trig = {
                k: v
                for k, v in old["trigger"].items()
                if k in ("kind", "schedule", "paused")
            }
        trig = trig or {}
        dto = self._kinds()
        view = read_back(steps, dto, on_session=False, has_pr=trig.get("kind") == "pr")
        if args.get("dry_run"):
            out = dict(
                view,
                dry_run=True,
                lines=[step_line(s) for s in steps],
                trigger=trig or {"kind": "manual"},
            )
            chk = self._check({"grounded": True, "steps": steps}, what + "(dry_run)")
            if chk is not None:
                probs = list(view.get("problems") or [])
                for p in list(chk.get("problems") or []) + [
                    w
                    for w in chk.get("warnings") or []
                    if isinstance(w, dict) and w.get("error") == "incomplete"
                ]:
                    if isinstance(p, dict):
                        probs.append(problem_line(p, dto, steps))
                if probs:
                    out["problems"] = probs
            out["ok"] = not out.get("problems")
            if old is not None:
                out["replaces"] = "%s (%s)" % (old.get("name"), old.get("id"))
            return out
        if view.get("problems"):
            raise _refuse(what, view)
        payload: Dict[str, Any] = {
            "name": str(args.get("name") or "").strip(),
            "steps": steps,
        }
        if old is not None:
            payload["id"] = old["id"]
        if args.get("description"):
            payload["description"] = str(args["description"])
        elif old is not None and old.get("description"):
            payload["description"] = str(old["description"])
        if trig:
            payload["trigger"] = dict(trig)
        resp = self._fl_api("POST", "/api/flights/plans", payload, what, steps=steps)
        plan = resp.get("plan") if isinstance(resp.get("plan"), dict) else {}
        out = {
            "plan_id": plan.get("id"),
            "name": plan.get("name"),
            "steps": [
                step_line(s) for s in plan.get("steps") or [] if isinstance(s, dict)
            ],
        }
        out["sentence"] = view["sentence"]
        if view.get("warnings"):
            out["warnings"] = view["warnings"]
        if old is not None:
            out["replaced"] = old["id"]
        saved = plan.get("trigger")
        if isinstance(saved, dict) and saved.get("kind") not in (None, "manual"):
            out["trigger"] = saved
        return out

    # -- run_pr_review ---------------------------------------------------------- #
    def _pr_review_setting(self) -> str:
        """``flights.pr_review_plan`` as the server has it, "" when unset or
        unreadable (the server then refuses with no_plan itself)."""
        try:
            resp = self._fl_api("GET", "/api/settings", what="settings")
        except ToolError:
            return ""
        group = (resp.get("settings") or {}).get("flights")
        return str((group or {}).get("pr_review_plan") or "").strip()

    def run_pr_review(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        what = "run_pr_review"
        url = str(args.get("pr_url") or "").strip()
        title = str(args.get("session") or "").strip()
        if not title and not url:
            title = flock.self_title or ""
        if not title and not url:
            raise ToolError(
                "run_pr_review needs session or pr_url: you are not inside a "
                "MindFlock session"
            )
        ref = str(args.get("plan") or "").strip()
        plan = None
        lookup = ref or self._pr_review_setting()
        if lookup:
            plan = find_plan(self._plans(), lookup)
            if plan is None and ref:
                raise ToolError(
                    "run_pr_review: no saved route %r — plan is a saved route's "
                    "id or name" % ref
                )
        steps = list((plan or {}).get("steps") or [])
        confirm = bool(args.get("confirm_merge"))
        if title:
            self._may_fly(flock, title, what, steps, confirm)
        else:
            # The server finds (or makes) the PR's session: a write.
            self.policy.require_write(flock, what)
            if steps:
                self._require_confirm(steps, confirm, what)
        payload: Dict[str, Any] = {"by": self._by(flock)}
        if title:
            payload["session"] = title
        if url:
            payload["pr_url"] = url
        if plan is not None:
            payload["plan"] = str(plan.get("id") or lookup)
        elif ref:
            payload["plan"] = ref
        resp = self._fl_api(
            "POST", "/api/pr-review", payload, what, timeout=START_TIMEOUT_S
        )
        out: Dict[str, Any] = {
            "mode": resp.get("mode"),
            "session": resp.get("session"),
            "flight": resp.get("flight") or None,
            "stops": [
                "%s %s %s" % (s.get("id"), s.get("kind"), _cut(s.get("title"), 60))
                for s in resp.get("stops") or []
                if isinstance(s, dict)
            ],
        }
        if resp.get("mode") == "inserted" and not out["stops"]:
            out["note"] = "the route already had every one of those stops"
        elif resp.get("mode") == "created":
            out["note"] = (
                "the PR's session is being made in the background; its route "
                "flies once it is up"
            )
        return out
