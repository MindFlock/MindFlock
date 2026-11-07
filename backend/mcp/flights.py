"""The flight tools: ``describe_flight``, ``start_flight``, ``flight_status``,
``control_flight``, ``edit_flight`` and ``save_flight_plan``.

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
(the route read back as one sentence for the person to OK) → ``start_flight``.
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
FLIGHT_OPS = ("pause", "resume", "cancel", "run")
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
    "(what to fix, and how), and the COOKBOOK: stops (per kind: use / not / "
    "fields / waits; RARE kinds only when your user named them), recipes "
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
    "default yourself; else one of your descendants. On an existing session "
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
D_STATUS = (
    "A route, compact: state (grounded|ready|running|waiting_you|paused|"
    "halted|done|cancelled; ready = built on its session, not started), cursor (the current stop), reason, every stop as one "
    "line (id, state, kind, title, note, receipt, when) and the last 10 "
    "history lines. id, or title (that session's live route, else its "
    "latest); neither = your own session's route."
)
D_CONTROL = (
    "Steer a route on you or a descendant (or a grounded one). op: run (a "
    "ready route starts now) | pause | resume | cancel (Stop the route; the "
    "session is kept) | skip (a stop) | retry (a failed "
    "stop) | jump (move the cursor to step) | done (a Wait for me stop — one "
    "that needs a person — with an optional note). skip/retry/done default to "
    "the current stop; jump needs step. id, or title for that session's live "
    "route. Never press done on a stop your user should judge unless they "
    "said so."
)
D_EDIT = (
    "Fix a route's stops (the Sky does the rest): add [steps] (inserted at "
    "index at, else appended; only after the current stop), remove [step ids] "
    "(a stop ahead just goes; removing the running stop pauses the route "
    "right before the next; the last stop stays), rename = the route's name. "
    "Same scope and confirm_merge rule as start_flight. Returns the route as "
    "flight_status does."
)
D_SAVE_PLAN = (
    "Keep a row of stops as a saved route on your user's shelf (the store "
    "calls it a plan): name, steps (as start_flight takes them), description, "
    "trigger {kind: manual|ticket|pr|issue|schedule, schedule?: {at:'HH:MM', "
    "days:'daily'|'weekdays'} | {cron} | {once}, paused?} — a schedule "
    "trigger starts it by itself (ask your user for the time if they gave "
    "none). dry_run=true saves nothing and reads it back. Returns {plan_id, "
    "name, steps, sentence}; start_flight(plan_id=…) runs it."
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
        "at": dict(_STEP_ID, description="stop id to start at"),
        "confirm_merge": {"type": "boolean", "default": False},
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "start nothing: the read-back sentence and problems",
        },
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
    },
    ("op",),
)
S_EDIT = _obj(
    {
        "id": _FLIGHT_ID,
        "add": _STEPS,
        "at": {"type": "integer", "minimum": 0, "maximum": MAX_STEPS},
        "remove": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": _STEP_ID,
        },
        "rename": {"type": "string", "minLength": 1, "maxLength": 80},
        "confirm_merge": {"type": "boolean", "default": False},
    },
    ("id",),
)
S_SAVE_PLAN = _obj(
    {
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
            ", ".join(fields) or "none",
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
    per kind — use / not / fields / waits — or, for a kind newer than the
    cookbook, the server's own summary line), ``recipes`` (common routes in
    the terse ``kind(cfg)!on_fail`` notation), ``rules``. Lines shrink until
    the whole view fits ``budget`` JSON characters."""
    kinds = [k for k in dto.get("kinds") or [] if isinstance(k, dict) and k.get("kind")]
    served = {str(k["kind"]) for k in kinds}

    def build(width: int) -> dict:
        stops: List[str] = []
        for k in kinds:
            kind = str(k["kind"])
            line = cookbook.stop_line(kind, str(k.get("label") or ""))
            if not line:
                line = compact_kinds({"kinds": [k]})[0]
            elif "irreversible" in [str(r) for r in k.get("risk") or []]:
                line += " · [irreversible]"
            stops.append(_cut(line, width))
        recipes = [
            _cut(cookbook.recipe_line(r), width + 120)
            for r in cookbook.RECIPES
            if all(s["kind"] in served for s in r["steps"])
        ]
        return {"stops": stops, "recipes": recipes, "rules": list(cookbook.RULES)}

    width = 400
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
) -> Dict[str, Any]:
    """``{sentence, problems?, warnings?}`` for ``steps``: the route in one
    line to read back to the person, and the cookbook's lint (errors =
    problems, the tools refuse those; warnings = odd, worth saying)."""
    known = [str(k.get("kind")) for k in dto.get("kinds") or [] if isinstance(k, dict)]
    issues = cookbook.lint(
        steps, known=known or None, on_session=on_session, has_pr=has_pr
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


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
class FlightTools:
    """The six flight handlers; mixed into ``Toolbox``."""

    # -- helpers -------------------------------------------------------------- #
    def _fl_api(self, method: str, path: str, payload=None, what: str = "", **kw):
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
            raise self._flight_error(err, what) from None
        return resp if isinstance(resp, dict) else {}

    @staticmethod
    def _flight_error(err: client.ApiError, what: str) -> ToolError:
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
            taught = cookbook.field_hint(
                str(body["field"]), str(body.get("detail") or "")
            )
            if taught:
                msg += ". " + taught
        hint = {
            "in_flight": "that session already has a route running: "
            "flight_status(title=…) shows it; cancel it first or edit_flight it",
            "in_progress": "only stops after the current one can change",
            "needs_title": "name the session (title) or start it grounded=true",
            "unknown_flight": "flight_status(title=…) finds a session's route",
            "last_step": "a route keeps its last stop: cancel the route instead",
            "finished": "that route is over",
            "grounded": "a grounded route does nothing until your user attaches "
            "a session to it (only cancel works)",
        }.get(err.message)
        if hint:
            msg += ". " + hint
        return ToolError(msg)

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
        """You or a descendant (``require_ship``'s rule, merge included); a
        grounded flight (no session) needs only write scope."""
        if not title:
            self.policy.require_write(flock, what)
            if steps:
                self._require_confirm(list(steps), confirm, what)
            return
        bad = _irreversible(list(steps), self._kinds_by_name()) if steps else []
        try:
            self.policy.require_ship(
                flock, title, what, merge=bool(bad), confirm_merge=confirm
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
        dto = self._kinds()
        view = read_back(check, dto, on_session=not grounded) if check else {}
        if args.get("dry_run"):
            out = dict(view, dry_run=True, session=title or None)
            out["lines"] = [step_line(s) for s in check]
            if (
                title
                and check
                and str(check[0].get("kind") or "") == "agent"
                and not args.get("at")
                and args.get("start_on") not in ("prompt", "run")
            ):
                # The read-back lists the agent stop as if it will be sent; on
                # an existing session it is not — say what happens instead.
                out["first_stop"] = (
                    "not sent: on %s the work so far is the first Agent stop "
                    "(still on its turn, the route waits for it to end; idle, "
                    "it goes straight on to the next stop). To send its "
                    "prompt, pass at=%r." % (title, check[0].get("id") or "s1")
                )
            bad = _irreversible(check, self._kinds_by_name())
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
            out["next"] = "fix the problems first" if view.get("problems") else nxt
            return out
        if view.get("problems"):
            raise _refuse(what, view)
        self._may_fly(flock, title, what, check, confirm)
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
        resp = self._fl_api(
            "POST", "/api/flights/start", payload, what, timeout=START_TIMEOUT_S
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
        self._may_fly(flock, str(f.get("session") or ""), what)
        fid = str(f.get("id") or "")
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

    # -- edit_flight ------------------------------------------------------------ #
    def edit_flight(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        what = "edit_flight"
        add = self._clean_steps(args.get("add"))
        remove = [str(s).strip() for s in args.get("remove") or [] if str(s).strip()]
        rename = str(args.get("rename") or "").strip()
        if not (add or remove or rename):
            raise ToolError("edit_flight needs add, remove or rename")
        f = self._get_flight(str(args["id"]), what)
        if add:
            have = [s for s in f.get("steps") or [] if isinstance(s, dict)]
            gone = set(remove)
            after = [s for s in have if str(s.get("id") or "") not in gone]
            at = args.get("at")
            at = len(after) if at is None else max(0, min(int(at), len(after)))
            after = after[:at] + add + after[at:]
            dto = self._kinds()
            known = [str(k.get("kind")) for k in dto.get("kinds") or []]
            old = {
                x["text"]
                for x in cookbook.errors(cookbook.lint(have, known=known or None))
            }
            view = read_back(after, dto, on_session=bool(f.get("session")))
            new = [
                p for p in view.get("problems") or [] if p.split(": ", 1)[-1] not in old
            ]
            if new:
                raise _refuse(what, dict(view, problems=new))
        self._may_fly(
            flock,
            str(f.get("session") or ""),
            what,
            add,
            bool(args.get("confirm_merge")),
        )
        fid = str(f.get("id") or args["id"])
        done: List[str] = []
        for sid in remove:
            q = urllib.parse.quote(sid, safe="")
            r = self._fl_api(
                "DELETE",
                self._flight_path(fid, "/steps/" + q),
                what="%s(remove %s)" % (what, sid),
            )
            f = r.get("flight") if isinstance(r.get("flight"), dict) else f
            done.append("removed " + sid)
        if add:
            payload: Dict[str, Any] = {"steps": add}
            if args.get("at") is not None:
                payload["at"] = int(args["at"])
            r = self._fl_api("POST", self._flight_path(fid, "/steps"), payload, what)
            f = r.get("flight") if isinstance(r.get("flight"), dict) else f
            done.append("added %d" % len(add))
        if rename:
            r = self._fl_api(
                "PATCH", self._flight_path(fid), {"plan_name": rename}, what
            )
            f = r.get("flight") if isinstance(r.get("flight"), dict) else f
            done.append("renamed")
        out = compact_flight(f, self.clock())
        out["changed"] = done
        return out

    # -- save_flight_plan ------------------------------------------------------- #
    def save_flight_plan(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        self.policy.require_write(flock, "save_flight_plan")
        steps = self._clean_steps(args.get("steps"))
        if not steps:
            raise ToolError("save_flight_plan needs steps")
        trig = args.get("trigger") if isinstance(args.get("trigger"), dict) else {}
        dto = self._kinds()
        view = read_back(steps, dto, on_session=False, has_pr=trig.get("kind") == "pr")
        if args.get("dry_run"):
            return dict(
                view,
                dry_run=True,
                lines=[step_line(s) for s in steps],
                trigger=trig or {"kind": "manual"},
            )
        if view.get("problems"):
            raise _refuse("save_flight_plan", view)
        payload: Dict[str, Any] = {
            "name": str(args.get("name") or "").strip(),
            "steps": steps,
        }
        if args.get("description"):
            payload["description"] = str(args["description"])
        if isinstance(args.get("trigger"), dict):
            payload["trigger"] = dict(args["trigger"])
        resp = self._fl_api("POST", "/api/flights/plans", payload, "save_flight_plan")
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
        saved = plan.get("trigger")
        if isinstance(saved, dict) and saved.get("kind") not in (None, "manual"):
            out["trigger"] = saved
        return out
