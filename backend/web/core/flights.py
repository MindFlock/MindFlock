"""Flights — one route from arrival to merge (docs/flights.md).

A **plan** is a row of blocks (steps); a **flight** is one run of a plan bound
to one session; a **step** is a robot step the server carries out by itself or
a *you* step that parks the flight until a person presses Done.

THIS MODULE IS THE PURE HALF, plus the two small stores. It never touches git,
tmux, GitHub or the bus: :mod:`backend.web.core.flight_driver` gathers an
observation, asks :func:`next_action` what to do, performs the side effect and
hands the verdict back to :func:`apply`. That split is what makes every rule
here unit-testable with plain dicts, the way ``team_runs.plan_actions`` is.

WHY THE POSITION IS STORED, unlike autopilot. The autopilot carries a session
to a TARGET, so its position is re-derived from git on every pass. A flight is a
SCRIPT the owner drew — "wait 5 minutes, then ask me, then merge" has no
git-visible position — so the cursor is persisted. What is re-derived on every
pass is whether the CURRENT step is done (the session row, the autopilot
record, the checklist store, the clock). A restart therefore resumes from the
stored cursor with fresh eyes.

TWO FILES under ``<config dir>/flights/`` (``$MINDFLOCK_FLIGHTS_DIR`` in
tests): ``plans.json`` (synced across devices like templates, through
``sync_stores``) and ``flights.json`` (local, like ``autopilot.json``). Every
accessor takes the module lock and re-reads the file under it; every write is
atomic. ``_normalize_*`` IS the migration mechanism — there is no version key:
a record written by an older build gains new fields at their defaults, and a
hand-mangled one cannot crash a pass.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import secrets
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard
from backend.fsutil import atomic_write_json, read_json
from backend.web.core import flight_kinds as _kinds
from backend.web.core import flight_when as _when

__all__ = [
    "STEP_KINDS",
    "install_kinds",
    "judge_for",
    "ON_FAIL",
    "TRIGGER_KINDS",
    "MAIN_TRIGGERS",
    "TRIGGER_QUIET_MINUTES",
    "mint_hook_token",
    "SUBJECT_KINDS",
    "BUILTIN_IDS",
    "PlanError",
    "FlightError",
    "flights_dir",
    "kinds_dto",
    "normalize_step",
    "normalize_plan",
    "list_plans",
    "get_plan",
    "save_plan",
    "delete_plan",
    "reset_plan",
    "plan_for_depth",
    "fill_prompt",
    "take_off",
    "arm_pending",
    "list_flights",
    "get",
    "for_session",
    "mutate",
    "delete",
    "claim",
    "prune",
    "row_dto",
    "event_data",
    "next_action",
    "fail_action",
    "apply",
    "op_done",
    "op_skip",
    "op_retry",
    "op_jump",
    "op_pause",
    "op_resume",
    "op_cancel",
    "adhoc_plan",
    "with_lead",
    "absorb_steps",
    "op_add_step",
    "op_edit_step",
    "op_remove_step",
    "op_remove_group",
    "op_add_steps",
    "op_add_plan",
    "op_group",
    "op_ungroup",
    "ground",
    "attach",
    "join_view",
    "child_state",
    "split_prompts",
    "spawned_titles",
    "branch_step",
    "join_dto",
]

_LOCK = threading.Lock()
_PLANS_FILE = "plans.json"
_FLIGHTS_FILE = "flights.json"

# --- Limits and clocks --------------------------------------------------------
MAX_STEPS = 40
MAX_HISTORY = 200
MAX_TEXT = 8000
#: Finished flights kept on disk (the Board shows the last 20).
KEEP_FINISHED = 200
#: The driver's pass interval (it also wakes on bus taps and route changes).
RUN_INTERVAL_S = 5.0
#: Consecutive idle before an agent step counts as settled — autopilot's rule.
IDLE_SETTLE_S = 30.0
#: How many times one step hands its failure to the agent (``on_fail: agent``)
#: before the flight halts instead.
MAX_FIX_ROUNDS = 2
#: An intake flight is armed BEFORE its session exists (autopilot's
#: ``ARM_GRACE_S`` idea): it is never pruned for a missing session this young.
ARM_GRACE_S = 1800.0
#: A flight whose session has been gone this long is cancelled by :func:`prune`.
GONE_GRACE_S = 1800.0
#: An unrefreshed driver lease is honoured this long (autopilot's value).
LEASE_STALE_S = 45.0
#: An agent step whose agent never showed any work gives up after this.
AGENT_START_DEADLINE_S = 7200.0
#: A verify step whose checklist never appears gives up after this.
VERIFY_WRITE_DEADLINE_S = 3600.0
#: A reviews step with no pull request to watch gives up after this.
NO_PR_DEADLINE_S = 1800.0
#: A step's side effect is recorded as ``detail.performing`` BEFORE it is
#: carried out; a mark older than this (a server that died mid-effect) no
#: longer stops the step from being tried again.
PERFORM_STALE_S = 600.0
#: Right after a push a PR can report no checks for a moment; "none" only
#: counts as "nothing to wait for" once this has passed.
CHECKS_NONE_GRACE_S = 120.0

#: Flight states. ``grounded`` is a row of stops on the board with no session
#: yet (nothing drives it until one is attached); ``paused`` is held by a
#: person (resume carries on where it was); ``halted`` is stopped on a failure
#: (retry / skip / resume); ``done``/``cancelled`` are final.
STATES = (
    "grounded",
    "running",
    "waiting_you",
    "paused",
    "halted",
    "done",
    "cancelled",
)
GROUNDED = "grounded"
#: States the driver moves.
ACTIVE = frozenset({"running", "waiting_you"})
#: States that hold their session (a new start is 409 ``in_flight``).
HOLDING = frozenset({"running", "waiting_you", "paused"})
UNFINISHED = frozenset({"grounded", "running", "waiting_you", "paused", "halted"})
FINAL = frozenset({"done", "cancelled"})
STEP_STATES = ("pending", "running", "waiting_you", "done", "skipped", "failed")
HISTORY_EVENTS = (
    "started",
    "done",
    "failed",
    "skipped",
    "you",
    "halted",
    "paused",
    "resumed",
    "jumped",
    "added",
    "edited",
    "removed",
    "moved",
    "halted_at",
    "attached",
)
ON_FAIL = ("halt", "agent", "skip", "you")
TRIGGER_KINDS = (
    "manual",
    "ticket",
    "pr",
    "issue",
    "schedule",
    "main_push",
    "main_red",
    "hook",
)
#: The triggers that watch a repository's default branch (``trigger.repo``
#: required): see backend.web.core.flight_triggers.
MAIN_TRIGGERS = ("main_push", "main_red")
#: ``trigger.quiet_minutes`` when a plan names none: no re-fire inside it.
TRIGGER_QUIET_MINUTES = 60
_HOOK_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
SUBJECT_KINDS = ("ticket", "pr", "issue", "schedule", "manual")
#: Who started a flight (``by``), besides ``agent:<title>``: the user, an
#: intake, a schedule, or one of the round-15 triggers by name.
BY_KINDS = ("user", "intake", "schedule", "main_push", "main_red", "hook")
SHIP_LANES = ("commit", "push", "pr", "merge")
VERIFY_WHO = ("agent", "you", "both")
#: A join waits for every live child of the session, or only the workers the
#: nearest split before it spawned.
JOIN_OF = ("all", "spawned")
JOIN_ON_FAILED = ("halt", "continue")
#: At most this many workers per split (``wait_for_session``'s target cap).
MAX_SPLIT = 20

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class PlanError(ValueError):
    """A plan (or step) that cannot be stored: ``field`` names the culprit
    (``steps[2].config.lane``) so the editor can point at it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class FlightError(Exception):
    """A flight operation refused: ``status`` + the machine word in
    ``error`` (``in_flight``, ``not_waiting``…) + a sentence in ``detail``."""

    def __init__(self, error: str, status: int = 400, detail: str = "") -> None:
        super().__init__(detail or error)
        self.error = error
        self.status = status
        self.detail = detail or error


# --------------------------------------------------------------------------- #
# The step registry (served by GET /api/flights/kinds)
# --------------------------------------------------------------------------- #
def _f(name, type_, label, default=None, options=None, placeholder=None) -> dict:
    out = {"name": name, "type": type_, "label": label}
    if options is not None:
        out["options"] = list(options)
    if default is not None:
        out["default"] = default
    if placeholder is not None:
        out["placeholder"] = placeholder
    return out


#: kind -> {label, actor, summary, fields, default_title}. The editor's palette
#: and forms are driven by this, so a field added here shows up in the UI.
STEP_KINDS: Dict[str, dict] = {
    "agent": {
        "label": "Agent",
        "actor": "robot",
        "summary": "Give the session's agent a prompt and wait until its turn has settled.",
        "default_title": "Agent works",
        "fields": [
            _f(
                "prompt",
                "textarea",
                "Prompt",
                default="",
                placeholder="What should the agent do? {ticket} {pr} {branch} {subject} are filled in.",
            ),
            _f("fresh", "bool", "Start a new session for this stop", default=False),
            _f("join", "bool", "Wait for its workers", default=True),
            _f(
                "program",
                "text",
                "CLI for a fresh session (empty = the same)",
                default="",
            ),
            _f(
                "model",
                "text",
                "Model for a fresh session (empty = the same)",
                default="",
            ),
        ],
    },
    "split": {
        "label": "Split",
        "actor": "robot",
        "summary": "Spawn workers in parallel, one per prompt — follow it with a Join.",
        "default_title": "Split",
        "fields": [
            _f(
                "prompts",
                "textarea",
                "Prompts (one worker per line)",
                default="",
                placeholder="Each line is one worker's task. {index} and {n} are filled in.",
            ),
            _f("count", "int", "Workers (when there is one prompt)", default=1),
            _f("model", "text", "Model (empty = the CLI's default)", default=""),
            _f(
                "fence_only",
                "text",
                "Each worker may only change (comma-separated paths)",
                default="",
                placeholder="src/api/, tests/api/",
            ),
            _f("report_back", "bool", "Workers report back when done", default=True),
        ],
    },
    "join": {
        "label": "Join",
        "actor": "robot",
        "summary": "Wait until every worker has finished.",
        "default_title": "Join",
        "fields": [
            _f("of", "select", "Wait for", default="all", options=JOIN_OF),
            _f(
                "on_failed",
                "select",
                "When a worker failed",
                default="halt",
                options=JOIN_ON_FAILED,
            ),
        ],
    },
    "ship": {
        "label": "Ship",
        "risk": ["remote"],
        "actor": "robot",
        "summary": "Commit, push, open the PR or merge it — Fast-track carries it out.",
        "default_title": "Push",
        "fields": [
            _f("lane", "select", "How far", default="push", options=SHIP_LANES),
            _f(
                "ask_first",
                "bool",
                "Ask me before it leaves the machine",
                default=False,
            ),
            _f("message", "text", "Commit message hint", default=""),
        ],
    },
    "verify": {
        "label": "Verify",
        "actor": "robot",
        "summary": "Write the branch's checklist and work through it; your items perch the flight on you.",
        "default_title": "Verify",
        "fields": [
            _f("who", "select", "Who checks", default="both", options=VERIFY_WHO),
            _f("wait_live", "bool", "Wait until it is live", default=False),
        ],
    },
    "you": {
        "label": "Perch",
        "actor": "you",
        "summary": "The flight perches until you are done: it asks you to do something and flies on when you press Done.",
        "default_title": "Check it",
        "fields": [
            _f(
                "text",
                "textarea",
                "What to do",
                default="",
                placeholder="Look over the PR",
            ),
            _f("items", "textarea", "Checklist (one per line)", default=""),
            _f("notify", "bool", "Notify me", default=True),
        ],
    },
    "wait": {
        "label": "Wait",
        "actor": "robot",
        "summary": "Wait a number of minutes, or until a time of day.",
        "default_title": "Wait",
        "fields": [
            _f("minutes", "int", "Minutes", default=10),
            _f("until", "text", "Or until (HH:MM)", default="", placeholder="18:00"),
        ],
    },
    "reviews": {
        "label": "Reviews settle",
        "actor": "robot",
        "summary": "Send each batch of new review comments to the agent and push, until the PR goes quiet.",
        "default_title": "Reviews settle",
        "fields": [
            _f("quiet_minutes", "int", "Quiet for (minutes)", default=10),
            _f("max_rounds", "int", "At most this many rounds", default=5),
            _f("include_bots", "bool", "Include bot reviewers", default=True),
        ],
    },
    "checks": {
        "label": "Checks",
        "actor": "robot",
        "summary": "Wait for the PR's checks to pass; a failing check fails the stop.",
        "default_title": "Checks",
        "fields": [_f("timeout_minutes", "int", "Give up after (minutes)", default=60)],
    },
    "command": {
        "label": "Command",
        "actor": "robot",
        "summary": "Run a shell command in the session's folder; a non-zero exit fails the stop.",
        "default_title": "Run a command",
        "fields": [
            _f("command", "textarea", "Command", default="", placeholder="make test"),
            _f("timeout_minutes", "int", "Give up after (minutes)", default=30),
        ],
    },
    "ticket": {
        "label": "Ticket",
        "risk": ["remote"],
        "actor": "robot",
        "summary": "Move the flight's ticket and/or comment on it (skipped without a ticket).",
        "default_title": "Ticket: update",
        "fields": [
            _f(
                "state",
                "text",
                "Move to state (its exact name or id; empty = comment only)",
                default="",
                placeholder="Done",
            ),
            _f("comment", "textarea", "Comment", default=""),
        ],
    },
    "notify": {
        "label": "Notify",
        "actor": "robot",
        "summary": "Send a notification to your phone and the bell; never waits.",
        "default_title": "Notify me",
        "fields": [_f("text", "textarea", "Message", default="")],
    },
}
#: Kinds that need the flight's session to exist (and not be paused).
SESSION_KINDS = frozenset(
    {"agent", "ship", "verify", "reviews", "checks", "command", "split", "join"}
)
#: Kinds that type into the session's agent (the driver wakes it first).
AGENT_KINDS = frozenset({"agent", "reviews"})


#: The palette's grouping (docs/flights.md "Round 15"): work (the agent and
#: what judges its work), ship (the way to main), outside (the world beyond
#: the worktree), people (perches). A plug-in names its own in ``spec``.
_FAMILY = {
    "agent": "work",
    "split": "work",
    "join": "work",
    "command": "work",
    "ship": "ship",
    "checks": "ship",
    "reviews": "ship",
    "wait": "outside",
    "notify": "outside",
    "ticket": "outside",
    "you": "people",
    "verify": "people",
}
FAMILIES = ("work", "ship", "outside", "people")


def kinds_dto() -> dict:
    """The ``GET /api/flights/kinds`` payload."""
    out = []
    for kind, spec in STEP_KINDS.items():
        out.append(
            {
                "kind": kind,
                "label": spec["label"],
                "actor": spec["actor"],
                "summary": spec["summary"],
                "fields": copy.deepcopy(spec["fields"]),
                "default_title": spec["default_title"],
                "risk": list(spec.get("risk") or []),
                "family": spec.get("family") or _FAMILY.get(kind, "outside"),
                "default_on_fail": spec.get("default_on_fail") or "halt",
            }
        )
    return {"kinds": out, "on_fail": list(ON_FAIL)}


# --------------------------------------------------------------------------- #
# Normalisers (one for a POSTed plan, a describe answer and a stored file)
# --------------------------------------------------------------------------- #
def _text(value, limit: int = MAX_TEXT) -> str:
    return str(value if value is not None else "").replace("\x00", "").strip()[:limit]


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _items(value) -> List[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    out = [_text(v, 500) for v in value]
    return [v for v in out if v][:40]


def _step_config(kind: str, raw: dict, strict: bool, where: str) -> dict:
    """``raw`` coerced to ``kind``'s config, raising :class:`PlanError` (in
    strict mode) for the few things that make a step meaningless."""
    c = raw if isinstance(raw, dict) else {}
    if kind == "agent":
        return {
            "prompt": _text(c.get("prompt")),
            "fresh": _bool(c.get("fresh"), False),
            "join": _bool(c.get("join"), True),
            "program": _text(c.get("program"), 100),
            "model": _text(c.get("model"), 200),
        }
    if kind == "split":
        prompts = c.get("prompts")
        if isinstance(prompts, list):
            prompts = "\n".join(str(p) for p in prompts)
        prompts = _text(prompts)
        if not prompts.strip() and strict:
            raise PlanError(
                "a split step needs at least one prompt", where + ".prompts"
            )
        return {
            "prompts": prompts,
            "count": _int(c.get("count"), 1, 1, MAX_SPLIT),
            "model": _text(c.get("model"), 200),
            "fence_only": _text(c.get("fence_only"), 2000),
            "report_back": _bool(c.get("report_back"), True),
        }
    if kind == "join":
        of = _text(c.get("of"), 16).lower() or "all"
        if of not in JOIN_OF:
            if strict:
                raise PlanError("of is all or spawned", where + ".of")
            of = "all"
        on_failed = _text(c.get("on_failed"), 16).lower() or "halt"
        if on_failed not in JOIN_ON_FAILED:
            if strict:
                raise PlanError("on_failed is halt or continue", where + ".on_failed")
            on_failed = "halt"
        return {"of": of, "on_failed": on_failed}
    if kind == "ship":
        lane = _text(c.get("lane"), 16).lower() or "push"
        if lane not in SHIP_LANES:
            if strict:
                raise PlanError(
                    "a ship step goes to commit, push, pr or merge", where + ".lane"
                )
            lane = "push"
        return {
            "lane": lane,
            "ask_first": _bool(c.get("ask_first"), False),
            "message": _text(c.get("message"), 500),
        }
    if kind == "verify":
        who = _text(c.get("who"), 8).lower() or "both"
        if who not in VERIFY_WHO:
            if strict:
                raise PlanError("who is agent, you or both", where + ".who")
            who = "both"
        return {"who": who, "wait_live": _bool(c.get("wait_live"), False)}
    if kind == "you":
        return {
            "text": _text(c.get("text")),
            "items": _items(c.get("items")),
            "notify": _bool(c.get("notify"), True),
        }
    if kind == "wait":
        until = _text(c.get("until"), 5)
        if until and not _HHMM_RE.match(until):
            if strict:
                raise PlanError("until is a time like 18:00", where + ".until")
            until = ""
        minutes = _int(c.get("minutes"), 0, 0, 60 * 24 * 14)
        if not until and minutes <= 0:
            if strict and c.get("minutes") not in (None, ""):
                raise PlanError("wait at least one minute", where + ".minutes")
            minutes = 10
        return {"minutes": 0 if until else minutes, "until": until}
    if kind == "reviews":
        return {
            "quiet_minutes": _int(c.get("quiet_minutes"), 10, 1, 24 * 60),
            "max_rounds": _int(c.get("max_rounds"), 5, 1, 50),
            "include_bots": _bool(c.get("include_bots"), True),
        }
    if kind == "checks":
        return {"timeout_minutes": _int(c.get("timeout_minutes"), 60, 1, 24 * 60)}
    if kind == "command":
        cmd = _text(c.get("command"), 4000)
        if not cmd and strict:
            raise PlanError("a command step needs a command", where + ".command")
        return {
            "command": cmd,
            "timeout_minutes": _int(c.get("timeout_minutes"), 30, 1, 24 * 60),
        }
    if kind == "ticket":
        return {"state": _text(c.get("state"), 200), "comment": _text(c.get("comment"))}
    if kind == "notify":
        return {"text": _text(c.get("text"), 1000)}
    plug = _kinds.get(kind)
    if plug is not None:
        return plug.config(c, strict, where)
    return {}


def _normalize_group(raw) -> Optional[dict]:
    """A step's ``group`` — the stop with sub-steps it belongs to (docs/flights.md
    "Groups"): ``{id, name, plan_id}``, or None when its id or name is empty.
    Never refuses, strict or not: a group is a label on the board."""
    if not isinstance(raw, dict):
        return None
    gid = _text(raw.get("id"), 40)
    name = _text(raw.get("name"), 80)
    if not gid or not name:
        return None
    return {"id": gid, "name": name, "plan_id": _text(raw.get("plan_id"), 64)}


def normalize_step(
    raw,
    index: int = 0,
    strict: bool = True,
    taken=None,
    config_strict: Optional[bool] = None,
) -> Optional[dict]:
    """One step coerced to ``{id, kind, title, config, on_fail}`` plus
    ``halt: True`` on a halt point (omitted when false) and ``group`` on a
    sub-step of a compound stop (omitted when it has none).

    Strict (a POSTed plan, a describe answer): raises :class:`PlanError`. Lenient
    (a stored file): returns None for a step that cannot be a step. ``taken``
    is the set of ids already used in this plan — a duplicate gets a new id.
    """
    where = "steps[%d]" % index
    if not isinstance(raw, dict):
        if strict:
            raise PlanError("a step is an object", where)
        return None
    kind = _text(raw.get("kind"), 16).lower()
    if kind not in STEP_KINDS:
        if strict:
            raise PlanError(
                "unknown step kind %r — one of %s" % (kind, ", ".join(STEP_KINDS)),
                where + ".kind",
            )
        return None
    # The config may be checked more gently than the shape (a block on the
    # ground: its kind must be real, its prompts may still be empty).
    cstrict = strict if config_strict is None else config_strict
    config = _step_config(kind, raw.get("config"), cstrict, where + ".config")
    on_fail = _text(raw.get("on_fail"), 8).lower() or str(
        STEP_KINDS[kind].get("default_on_fail") or "halt"
    )
    if on_fail not in ON_FAIL:
        if strict:
            raise PlanError("on_fail is halt, agent, skip or you", where + ".on_fail")
        on_fail = "halt"
    if kind == "you":
        on_fail = "halt"  # a you step cannot fail
    halt = raw.get("halt")
    if halt is not None and not isinstance(halt, bool):
        if strict:
            raise PlanError("halt is true or false", where + ".halt")
        halt = _bool(halt, False)
    taken = taken if taken is not None else set()
    sid = _text(raw.get("id"), 32)
    if not _ID_RE.match(sid) or sid in taken:
        n = index + 1
        while "s%d" % n in taken:
            n += 1
        sid = "s%d" % n
    taken.add(sid)
    title = _text(raw.get("title"), 120) or _default_title(kind, config)
    step = {
        "id": sid,
        "kind": kind,
        "title": title,
        "config": config,
        "on_fail": on_fail,
    }
    if halt:
        step["halt"] = True
    group = _normalize_group(raw.get("group"))
    if group is not None:
        step["group"] = group
    when = _normalize_when(raw.get("when"), strict, where)
    if when:
        step["when"] = when
    return step


def _normalize_when(raw, strict: bool, where: str) -> str:
    """A step's ``when`` (docs/flights.md "Round 15") in its canonical
    spelling, "" when none. Strict: a bad one is ``PlanError(message,
    where + ".when")``; lenient: it is dropped."""
    text = str(raw if raw is not None else "").replace("\x00", "").strip()
    if not text:
        return ""
    try:
        if len(text) > _when.MAX_LEN:
            raise _when.WhenError(
                "when is at most %d characters" % _when.MAX_LEN, _when._HINT
            )
        return _when.canonical(_when.parse(text))
    except _when.WhenError as err:
        if strict:
            raise PlanError(err.message, where + ".when")
        return ""


def _default_title(kind: str, config: dict) -> str:
    if kind == "ship":
        return {"commit": "Commit", "push": "Push", "pr": "Open PR", "merge": "Merge"}[
            config.get("lane") or "push"
        ]
    if kind == "wait":
        if config.get("until"):
            return "Wait until %s" % config["until"]
        return "Wait %d min" % int(config.get("minutes") or 0)
    plug = _kinds.get(kind)
    if plug is not None and plug.default_title is not None:
        return (
            str(plug.default_title(config) or "") or STEP_KINDS[kind]["default_title"]
        )
    return STEP_KINDS[kind]["default_title"]


def normalize_steps(
    raw, strict: bool = True, config_strict: Optional[bool] = None
) -> List[dict]:
    """A step list, 1..:data:`MAX_STEPS` long in strict mode."""
    if not isinstance(raw, list):
        if strict:
            raise PlanError("steps is a list", "steps")
        return []
    if strict and not raw:
        raise PlanError("a plan needs at least one step", "steps")
    if strict and len(raw) > MAX_STEPS:
        raise PlanError("a plan has at most %d steps" % MAX_STEPS, "steps")
    taken: set = set()
    out = []
    for i, s in enumerate(raw[:MAX_STEPS]):
        step = normalize_step(
            s, i, strict=strict, taken=taken, config_strict=config_strict
        )
        if step is not None:
            if strict and step["kind"] == "land" and i != len(raw) - 1:
                # It closes the session: nothing can fly after it.
                raise PlanError("a Land stop is the last stop", "steps[%d].kind" % i)
            out.append(step)
    return out


def _normalize_schedule(raw, strict: bool):
    if raw in (None, "", {}, False):
        return None
    try:
        from backend.web.core import template_schedule as _ts

        return _ts.normalize_schedule(raw)
    except ValueError as err:
        if strict:
            raise PlanError(str(err), "trigger.schedule")
        return None
    except Exception:  # noqa: BLE001 — the schedule module failing is not a plan error
        return raw if isinstance(raw, dict) else None


def _is_repo(path: str) -> bool:
    """Whether ``path`` is a git work tree (a ``.git`` dir, or a worktree's
    ``.git`` file) — cheap, no subprocess: the strict check of
    ``trigger.repo``."""
    try:
        return os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def mint_hook_token(plan: dict, previous: Optional[dict] = None) -> None:
    """A ``hook`` trigger's token, in place: the one the client sent, else
    the stored plan's (``previous``: a save that left it out keeps it), else
    a fresh ``token_hex(16)``. Any other kind keeps what it has (switching
    back to hook keeps the URL)."""
    trig = plan.get("trigger") or {}
    if trig.get("token"):
        return
    prev = (previous or {}).get("trigger") or {}
    if prev.get("token"):
        trig["token"] = prev["token"]
    elif trig.get("kind") == "hook":
        trig["token"] = secrets.token_hex(16)


def normalize_plan(raw, strict: bool = True) -> dict:
    """A plan coerced to its stored shape (see docs/flights.md). Strict raises
    :class:`PlanError`; lenient never raises. ``id`` / ``builtin`` /
    ``updated`` are the STORE's to assign — the caller sets them after."""
    if not isinstance(raw, dict):
        if strict:
            raise PlanError("a plan is an object", "")
        raw = {}
    name = _text(raw.get("name"), 120)
    if not name:
        if strict:
            raise PlanError("a flyway needs a name", "name")
        name = "Untitled flyway"
    trig = raw.get("trigger") if isinstance(raw.get("trigger"), dict) else {}
    tkind = _text(trig.get("kind"), 16).lower() or "manual"
    if tkind not in TRIGGER_KINDS:
        if strict:
            raise PlanError(
                "trigger is manual, ticket, pr, issue, schedule, main_push, "
                "main_red or hook",
                "trigger.kind",
            )
        tkind = "manual"
    trepo = _text(trig.get("repo"), 1000)
    if tkind in MAIN_TRIGGERS and strict:
        if not trepo:
            raise PlanError("a repository to watch is needed", "trigger.repo")
        if not os.path.isabs(trepo):
            raise PlanError("the repository is an absolute path", "trigger.repo")
        if not _is_repo(trepo):
            raise PlanError("not a git repository: %s" % trepo, "trigger.repo")
    ttoken = _text(trig.get("token"), 64).lower()
    if not _HOOK_TOKEN_RE.match(ttoken):
        ttoken = ""
    sess = raw.get("session") if isinstance(raw.get("session"), dict) else {}
    return {
        "id": _text(raw.get("id"), 64),
        "name": name,
        "description": _text(raw.get("description"), 2000),
        "builtin": bool(raw.get("builtin")),
        "trigger": {
            "kind": tkind,
            "schedule": _normalize_schedule(trig.get("schedule"), strict),
            "paused": _bool(trig.get("paused"), False),
            "repo": trepo,
            "quiet_minutes": _int(
                trig.get("quiet_minutes"), TRIGGER_QUIET_MINUTES, 0, 7 * 24 * 60
            ),
            "token": ttoken,
        },
        "session": {
            "repo_path": _text(sess.get("repo_path"), 1000),
            "program": _text(sess.get("program"), 200),
            "model": _text(sess.get("model"), 200),
            "in_place": _bool(sess.get("in_place"), False),
        },
        "steps": normalize_steps(raw.get("steps"), strict=strict),
        "updated": _float(raw.get("updated")),
    }


# --------------------------------------------------------------------------- #
# Built-in plans
# --------------------------------------------------------------------------- #
def _s(sid: str, kind: str, title: str, on_fail: str = "halt", **config) -> dict:
    return {
        "id": sid,
        "kind": kind,
        "title": title,
        "config": config,
        "on_fail": on_fail,
    }


_BOT_PR_PROMPT = (
    "This pull request was opened by a bot (a dependency or tooling update). "
    "Read what it changes and the release notes or changelog of every package "
    "it bumps, looking for breaking changes, deprecations and new defaults "
    "that touch this codebase. Search the code for the APIs those notes name. "
    "If something needs adapting, make the smallest change that does it and "
    "commit it on this branch; if nothing does, change nothing. End with one "
    "line saying what the update changes for this project."
)
_RED_MAIN_PROMPT = (
    "The default branch is red: a check on its latest commit is failing. Find "
    "out which check fails and why: read its log, then reproduce the failure "
    "locally. Look at the commits that landed since main was last green and "
    "find the one that broke it. Fix the cause, not the symptom: never skip, "
    "delete or loosen a test to make it pass. Keep the change as small as the "
    "fix allows, and end with one line naming the cause and the commit that "
    "introduced it."
)
_NIGHTLY_PROMPT = (
    "Nightly chores: update this project's dependencies to their latest "
    "versions within the ranges its manifests allow (pip/uv, npm, go, cargo: "
    "whichever it uses), and refresh the lock files. Read the changelog of "
    "anything that moved a minor version or more and adapt the code where an "
    "API changed. Leave major-version bumps out and list them at the end for a "
    "person to decide. Do not change behaviour beyond what an update needs."
)
_NOTES_PROMPT = (
    "Prepare a release. Find the latest release tag and read every commit and "
    "merged pull request since it. Pick the next version by semver: a breaking "
    "change bumps major, a feature minor, fixes only patch. Write the release "
    "notes in the project's own style (its CHANGELOG, else a new section at the "
    "top of CHANGELOG.md) grouped as Breaking, Added, Fixed and Changed, each "
    "line saying what a user notices, not how it was done. Bump the version "
    "wherever the project records it. Do not tag yet."
)
_PUBLISH_PROMPT = (
    "The release pull request is merged. Check out the default branch, pull, "
    "and tag the merge commit with the version from the release notes "
    "(annotated, in the format the earlier tags use), then push the tag. "
    "Publish the release with the notes you wrote (gh release create, or the "
    "project's own release script if it has one) and attach the build "
    "artifacts if the project builds any. End with the release's URL."
)
_REPRODUCE_PROMPT = (
    "Before fixing anything, reproduce the report with a test. Write the "
    "smallest automated test, next to the existing tests for that code, that "
    "fails today because of exactly the bug described, and that will pass "
    "once it is fixed. Run it and confirm it fails for the right reason (the "
    "bug, not a typo or an import error). Do not change any code under test "
    "yet."
)
_MAKE_IT_PASS_PROMPT = (
    "Now fix the bug. Make the failing test you wrote pass by changing the "
    "code under test, not the test. Find the root cause and fix it there, "
    "and check whether the same mistake exists elsewhere. Run the whole test "
    "suite, not just the new test, and keep it green."
)
#: The presets: saved stops of one agent step each (trigger manual), dragged
#: onto a line from the shelf. ``{id: (name, description, prompt)}``.
_PRESETS = {
    "builtin:preset-docs": (
        "Update the docs",
        "Bring the docs in line with what this branch changed.",
        "Read the diff of {branch} against the default branch and find every "
        "piece of documentation it makes wrong or incomplete: the README, the "
        "docs folder, docstrings, CLI help, config references, examples. "
        "Update them to describe the code as it now is, in the style already "
        "there. Add docs for anything new a user can see. Change no code.",
    ),
    "builtin:preset-tests": (
        "Write tests",
        "Cover what this branch changed with tests.",
        "Read the diff of {branch} against the default branch and write tests "
        "for the behaviour it adds or changes, next to the existing tests and "
        "in their style. Cover the edge cases and the failure paths, not just "
        "the happy path. Run the suite and make sure every new test passes; "
        "if one exposes a bug, report it rather than weakening the test.",
    ),
    "builtin:preset-security": (
        "Security pass",
        "Look over this branch's changes for security problems and fix them.",
        "Review the diff of {branch} against the default branch for security "
        "problems: injection (SQL, shell, path), unchecked input, secrets in "
        "code or logs, missing auth or permission checks, unsafe "
        "deserialisation, SSRF, and new dependencies with known advisories. "
        "Fix what you find with the smallest change that closes the hole. "
        "List each finding with its file and line, including the ones you "
        "chose not to fix and why.",
    ),
    "builtin:preset-selfreview": (
        "Self-review",
        "Review your own work like a strict reviewer would, then fix it.",
        "Review the diff of {branch} against the default branch as a strict "
        "reviewer would: bugs, missed edge cases, error handling, naming, "
        "dead code, leftover debugging, and changes the task did not ask "
        "for. Fix every real problem you find and run the tests. End with a "
        "short list of what you changed and anything you left for a person "
        "to decide.",
    ),
    "builtin:preset-explain": (
        "Explain the diff",
        "A plain-language explanation of what this branch changes.",
        "Explain the diff of {branch} against the default branch to someone "
        "who has not read it. Say what it changes and why, file by file in "
        "order of importance, and call out anything risky, surprising or "
        "hard to undo. Keep it short enough to read in two minutes. Change "
        "no files.",
    ),
}


def _builtin_defaults() -> Dict[str, dict]:
    raw = {
        "builtin:ticket": {
            "name": "Ticket to merge",
            "description": "Work the ticket, ship it as a PR, let reviews and checks settle, ask you, merge.",
            "trigger": {"kind": "ticket"},
            "steps": [
                _s("s1", "agent", "Agent works the ticket", prompt=""),
                _s("s2", "ship", "Commit", lane="commit"),
                _s("s3", "ship", "Push", lane="push"),
                _s("s4", "verify", "Verify", who="agent"),
                _s("s5", "ship", "Open PR", lane="pr"),
                _s("s6", "reviews", "Reviews settle", quiet_minutes=10),
                _s("s7", "checks", "Checks", on_fail="agent"),
                _s(
                    "s8",
                    "you",
                    "Approve the merge",
                    text="Look over the pull request and press Done to merge it.",
                ),
                _s("s9", "ship", "Merge", lane="merge"),
                _s("s10", "ticket", "Ticket: comment", comment="Merged by MindFlock"),
            ],
        },
        "builtin:pr": {
            "name": "Pull request",
            "description": "Address the review comments, push, let reviews and checks settle.",
            "trigger": {"kind": "pr"},
            "steps": [
                _s("s1", "agent", "Agent addresses the review comments", prompt=""),
                _s("s2", "ship", "Commit", lane="commit"),
                _s("s3", "ship", "Push", lane="push"),
                _s("s4", "reviews", "Reviews settle", quiet_minutes=10),
                _s("s5", "checks", "Checks", on_fail="agent"),
            ],
        },
        "builtin:schedule": {
            "name": "Scheduled task",
            "description": "Run the agent, then commit, push and open a PR.",
            "trigger": {"kind": "schedule"},
            "steps": [
                _s("s1", "agent", "Agent works", prompt=""),
                _s("s2", "ship", "Commit", lane="commit"),
                _s("s3", "ship", "Push", lane="push"),
                _s("s4", "ship", "Open PR", lane="pr"),
            ],
        },
        "builtin:verify": {
            "name": "Verify on live",
            "description": "Once the branch is live, the agent and you work through its checklist.",
            "trigger": {"kind": "manual"},
            "steps": [_s("s1", "verify", "Verify on live", who="both", wait_live=True)],
        },
        # Round 15: more built-ins, after the four above (whose order the depth
        # cut and the shelf rely on).
        "builtin:botpr": {
            "name": "Bot PR",
            "description": "A dependency bot's PR: read it, test it, bring it up to date, merge once checks pass.",
            "trigger": {"kind": "pr"},
            "steps": [
                _s("s1", "agent", "Agent reads it", prompt=_BOT_PR_PROMPT),
                _s("s2", "test", "Test", on_fail="agent"),
                _s("s3", "sync", "Sync with main", on_fail="agent"),
                _s("s4", "checks", "Checks", on_fail="agent"),
                _s("s5", "ship", "Merge", lane="merge"),
            ],
        },
        "builtin:redmain": {
            "name": "Red main",
            "description": "The default branch went red: find out why, fix it, open a PR and ask you.",
            # The repository to watch is the owner's to pick in the inspector:
            # an empty repo loads (lenient) and the trigger loop skips it.
            "trigger": {"kind": "main_red", "repo": ""},
            "steps": [
                _s("s1", "agent", "Agent investigates", prompt=_RED_MAIN_PROMPT),
                _s("s2", "test", "Test", on_fail="agent"),
                _s("s3", "ship", "Commit", lane="commit"),
                _s("s4", "ship", "Push", lane="push"),
                _s("s5", "ship", "Open PR", lane="pr"),
                _s(
                    "s6",
                    "you",
                    "Look at the fix",
                    text="Main was red. Look over the fix's pull request and press Done.",
                ),
            ],
        },
        "builtin:nightly": {
            "name": "Nightly chores",
            "description": "Every night at 03:00: update the dependencies, run the tests, open a PR.",
            # Seeded paused: it fires only once the owner set its folder and
            # switched it on (an armed seed would start a session on every
            # install, every night).
            "trigger": {
                "kind": "schedule",
                "schedule": {"at": "03:00", "days": "daily"},
                "paused": True,
            },
            "steps": [
                _s("s1", "agent", "Agent updates deps", prompt=_NIGHTLY_PROMPT),
                _s("s2", "test", "Test", on_fail="agent"),
                _s("s3", "ship", "Commit", lane="commit"),
                _s("s4", "ship", "Push", lane="push"),
                _s("s5", "ship", "Open PR", lane="pr"),
            ],
        },
        "builtin:release": {
            "name": "Release",
            "description": "Write the release notes, ship them as a PR, merge on your go, tag and publish, land.",
            "trigger": {"kind": "manual"},
            "steps": [
                _s("s1", "agent", "Agent writes the notes", prompt=_NOTES_PROMPT),
                _s("s2", "ship", "Commit", lane="commit"),
                _s("s3", "ship", "Push", lane="push"),
                _s("s4", "ship", "Open PR", lane="pr"),
                _s("s5", "checks", "Checks", on_fail="agent"),
                _s(
                    "s6",
                    "you",
                    "Approve the release",
                    text="Read the release notes in the pull request and press Done to merge and publish.",
                ),
                _s("s7", "ship", "Merge", lane="merge"),
                _s(
                    "s8",
                    "agent",
                    "Agent tags and publishes",
                    prompt=_PUBLISH_PROMPT,
                ),
                _s("s9", "land", "Land"),
            ],
        },
        "builtin:bugreport": {
            "name": "Bug report",
            "description": "Reproduce the report with a failing test first, make it pass, ship it, merge on your go.",
            "trigger": {"kind": "ticket"},
            "steps": [
                _s(
                    "s1",
                    "agent",
                    "Write a failing test that reproduces the report",
                    prompt=_REPRODUCE_PROMPT,
                ),
                _s(
                    "s2",
                    "test",
                    "Write the failing test first",
                    on_fail="agent",
                    expect="fail",
                ),
                _s("s3", "agent", "Make it pass", prompt=_MAKE_IT_PASS_PROMPT),
                _s("s4", "test", "Test", on_fail="agent"),
                _s("s5", "ship", "Commit", lane="commit"),
                _s("s6", "ship", "Push", lane="push"),
                _s("s7", "verify", "Verify", who="agent"),
                _s("s8", "ship", "Open PR", lane="pr"),
                _s("s9", "reviews", "Reviews settle", quiet_minutes=10),
                _s("s10", "checks", "Checks", on_fail="agent"),
                _s(
                    "s11",
                    "you",
                    "Approve the merge",
                    text="Look over the fix and its test and press Done to merge it.",
                ),
                _s("s12", "ship", "Merge", lane="merge"),
                _s("s13", "ticket", "Ticket: comment", comment="Fixed by MindFlock"),
            ],
        },
    }
    for pid, (name, description, prompt) in _PRESETS.items():
        raw[pid] = {
            "name": name,
            "description": description,
            "trigger": {"kind": "manual"},
            "steps": [_s("s1", "agent", name, prompt=prompt)],
        }
    out = {}
    for pid, p in raw.items():
        plan = normalize_plan(p, strict=False)
        plan["id"] = pid
        plan["builtin"] = True
        plan["updated"] = 0.0
        out[pid] = plan
    return out


BUILTIN_IDS = tuple(_builtin_defaults().keys())
#: Which built-in a trigger kind's depth cut starts from. An issue is worked
#: like a ticket (there is no separate built-in for it).
_BUILTIN_FOR_KIND = {
    "ticket": "builtin:ticket",
    "issue": "builtin:ticket",
    "pr": "builtin:pr",
    "schedule": "builtin:schedule",
}
_CUT_RE = re.compile(r"^(builtin:[a-z]+)@(commit|push|pr|merge)$")


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def flights_dir() -> str:
    """``$MINDFLOCK_FLIGHTS_DIR`` (tests), else ``<config dir>/flights``.
    Raises (an ``OSError``) under pytest when neither is redirected."""
    env = os.environ.get("MINDFLOCK_FLIGHTS_DIR")
    if env:
        return guard(env, "flights store")
    return os.path.join(GetConfigDir(), "flights")


def plans_path() -> str:
    return os.path.join(flights_dir(), _PLANS_FILE)


def flights_path() -> str:
    return os.path.join(flights_dir(), _FLIGHTS_FILE)


def _load_plans_raw() -> List[dict]:
    data = read_json(plans_path(), {}, expect=dict)
    items = data.get("plans") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []


def read_plans_strict() -> Dict[str, dict]:
    """``{id: plan}`` as stored, raising when the file exists but cannot be
    read — what the sync store needs (an empty answer would tombstone every
    plan on every device). A missing file is the built-ins."""
    with _LOCK:
        try:
            with open(plans_path(), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
    items = data.get("plans") if isinstance(data, dict) else None
    if items is not None and not isinstance(items, list):
        raise ValueError("plans.json: plans is not a list")
    out = _builtin_defaults()
    for p in items or []:
        if isinstance(p, dict) and str(p.get("id") or ""):
            out[str(p["id"])] = _stored_plan(p)
    return out


def _stored_plan(raw: dict) -> dict:
    plan = normalize_plan(raw, strict=False)
    pid = str(raw.get("id") or "")
    plan["id"] = pid
    plan["builtin"] = pid in BUILTIN_IDS
    if not plan["steps"] and plan["builtin"]:
        plan = _builtin_defaults()[pid]
    return plan


def _plans_locked() -> Dict[str, dict]:
    """Every plan, built-ins seeded (caller holds the lock)."""
    plans = _builtin_defaults()
    for raw in _load_plans_raw():
        pid = str(raw.get("id") or "")
        if pid:
            plans[pid] = _stored_plan(raw)
    return plans


def _save_plans_locked(plans: Dict[str, dict]) -> None:
    atomic_write_json(plans_path(), {"plans": list(plans.values())}, prefix=".flp.")


def _plan_order(plan: dict) -> tuple:
    if plan["builtin"]:
        return (
            0,
            BUILTIN_IDS.index(plan["id"]) if plan["id"] in BUILTIN_IDS else 99,
            "",
        )
    return (1, 0, plan["name"].lower())


def list_plans() -> List[dict]:
    """Every plan, built-ins first (in their fixed order), then by name."""
    with _LOCK:
        plans = _plans_locked()
    return sorted(plans.values(), key=_plan_order)


def get_plan(plan_id: str) -> Optional[dict]:
    """A plan by id. ``builtin:<kind>@<lane>`` is a VIRTUAL plan: the ship
    ladder a per-source fast-track depth means (see :func:`plan_for_depth`)."""
    pid = str(plan_id or "")
    m = _CUT_RE.match(pid)
    with _LOCK:
        plans = _plans_locked()
    if m is None:
        plan = plans.get(pid)
        return copy.deepcopy(plan) if plan else None
    base = plans.get(m.group(1))
    if base is None:
        return None
    return _ladder_plan(pid, base, m.group(2))


_LANE_LABEL = {"commit": "Commit", "push": "Push", "pr": "Open PR", "merge": "Merge"}


def _ladder_plan(pid: str, base: dict, depth: str) -> dict:
    """The SHIP LADDER for ``depth``: ``base``'s first agent step (as edited),
    then commit → push → pr → merge cut at ``depth``, nothing else. This is
    what a per-source depth did before Flights existed (a fast-track), so an
    existing setup gains no verify / reviews / checks / you / ticket step it
    never asked for; the full built-in is opt-in (pick it as the plan)."""
    steps: List[dict] = []
    agent = next((s for s in base["steps"] if s["kind"] == "agent"), None)
    if agent is not None:
        steps.append(dict(copy.deepcopy(agent), id="s1", on_fail="halt"))
    for lane in SHIP_LANES[: SHIP_LANES.index(depth) + 1]:
        steps.append(
            {
                "id": "s%d" % (len(steps) + 1),
                "kind": "ship",
                "title": _LANE_LABEL[lane],
                "config": {"lane": lane, "ask_first": False, "message": ""},
                "on_fail": "halt",
            }
        )
    return {
        "id": pid,
        "name": "Fast-track to %s" % _LANE_LABEL[depth],
        "description": "The plain fast-track ladder: the agent, then %s."
        % " → ".join(_LANE_LABEL[l] for l in SHIP_LANES[: SHIP_LANES.index(depth) + 1]),
        "builtin": True,
        "trigger": copy.deepcopy(base["trigger"]),
        "session": copy.deepcopy(base["session"]),
        "steps": steps,
        "updated": base["updated"],
    }


def plan_for_depth(kind: str, depth: str) -> str:
    """The plan a per-source fast-track ``depth`` means for an intake ``kind``
    (``ticket``/``pr``/``issue``/``schedule``): ``builtin:<kind>@<depth>``,
    the ship ladder only (:func:`_ladder_plan`) — every existing setup behaves
    exactly as before. ``""`` for ``off``/``agent``/unknown. An issue uses
    the ticket built-in's agent step."""
    base = _BUILTIN_FOR_KIND.get(str(kind or "").strip().lower())
    depth = str(depth or "").strip().lower()
    if base is None or depth not in SHIP_LANES:
        return ""
    return "%s@%s" % (base, depth)


def _new_id(prefix: str, taken) -> str:
    while True:
        pid = prefix + secrets.token_hex(3)
        if pid not in taken:
            return pid


def save_plan(raw, now: Optional[float] = None) -> dict:
    """Create (no ``id``) or replace (``id``) a plan. Raises :class:`PlanError`
    for a bad plan, :class:`FlightError` 404 for an id nobody has."""
    plan = normalize_plan(raw, strict=True)
    ts = float(now if now is not None else time.time())
    with _LOCK:
        plans = _plans_locked()
        pid = plan["id"]
        if pid:
            if _CUT_RE.match(pid):
                raise PlanError("a depth cut of a built-in can't be edited", "id")
            if pid not in plans:
                raise FlightError("unknown_plan", 404, "no such plan: %s" % pid)
        else:
            pid = _new_id("p_", plans)
        plan["id"] = pid
        plan["builtin"] = pid in BUILTIN_IDS
        plan["updated"] = ts
        mint_hook_token(plan, plans.get(pid))
        plans[pid] = plan
        _save_plans_locked(plans)
    return copy.deepcopy(plan)


def put_plan(pid: str, raw, now: Optional[float] = None) -> dict:
    """Write a plan under ``pid`` whether or not it exists (the sync store's
    write: an id minted on another device). Lenient — a synced plan is never
    refused for a step this build does not know; that step is dropped."""
    plan = normalize_plan(raw, strict=False)
    if not plan["steps"]:
        raise ValueError("a synced plan with no usable steps")
    with _LOCK:
        plans = _plans_locked()
        plan["id"] = pid
        plan["builtin"] = pid in BUILTIN_IDS
        plan["updated"] = (
            _float((raw or {}).get("updated"), 0.0) if isinstance(raw, dict) else 0.0
        )
        if not plan["updated"]:
            plan["updated"] = float(now if now is not None else time.time())
        mint_hook_token(plan, plans.get(pid))
        plans[pid] = plan
        _save_plans_locked(plans)
    return plan


def delete_plan(plan_id: str) -> str:
    """Remove a plan: ``"deleted"``, ``"reset"`` (a built-in goes back to its
    default instead) or ``""`` (no such plan)."""
    pid = str(plan_id or "")
    with _LOCK:
        plans = _plans_locked()
        if pid not in plans:
            return ""
        if pid in BUILTIN_IDS:
            plans[pid] = _builtin_defaults()[pid]
            _save_plans_locked(plans)
            return "reset"
        plans.pop(pid, None)
        _save_plans_locked(plans)
    return "deleted"


def reset_plan(plan_id: str) -> Optional[dict]:
    """A built-in back to its default (None for anything else)."""
    pid = str(plan_id or "")
    if pid not in BUILTIN_IDS:
        return None
    delete_plan(pid)
    return get_plan(pid)


# --- flights.json -------------------------------------------------------------
#: The parsed flights.json, keyed on the file's identity: every session row
#: of every instances snapshot asks :func:`for_session`, and N rows must cost
#: one read, not N. ``(path, ino, mtime_ns, size)`` -> {id: flight}. Never
#: handed out: readers copy what they return. Guarded by :data:`_LOCK`.
_CACHE: Dict[str, object] = {"key": None, "flights": {}}


def _file_key(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return (path, None)
    return (path, st.st_ino, st.st_mtime_ns, st.st_size)


def _cached_flights_locked() -> Dict[str, dict]:
    """The parsed store (shared — callers must not mutate it). Re-read only
    when the file changed (another process, an atomic replace)."""
    path = flights_path()
    key = _file_key(path)
    if _CACHE["key"] == key and key[1] is not None:
        return _CACHE["flights"]  # type: ignore[return-value]
    data = read_json(path, {}, expect=dict)
    items = data.get("flights") if isinstance(data, dict) else None
    out: Dict[str, dict] = {}
    if isinstance(items, dict):
        for fid, raw in items.items():
            f = _normalize_flight(fid, raw)
            if f is not None:
                out[f["id"]] = f
    _CACHE["key"] = key
    _CACHE["flights"] = out
    return out


def _load_flights_locked() -> Dict[str, dict]:
    """A private, mutable copy of the store (for read-modify-write)."""
    return copy.deepcopy(_cached_flights_locked())


def _save_flights_locked(flights: Dict[str, dict]) -> None:
    path = flights_path()
    atomic_write_json(path, {"flights": flights}, prefix=".flf.")
    _CACHE["key"] = _file_key(path)
    _CACHE["flights"] = copy.deepcopy(flights)


def _blank_step_state() -> dict:
    return {"state": "pending", "since": 0.0, "note": "", "detail": {}, "rounds": 0}


def _normalize_subject(raw) -> dict:
    s = raw if isinstance(raw, dict) else {}
    kind = _text(s.get("kind"), 16).lower() or "manual"
    return {
        "kind": kind if kind in SUBJECT_KINDS else "manual",
        "source": _text(s.get("source"), 500),
        "id": _text(s.get("id"), 200),
        "title": _text(s.get("title"), 500),
        "url": _text(s.get("url"), 1000),
    }


def _normalize_flight(fid, raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    fid = _text(raw.get("id") or fid, 64)
    if not fid:
        return None
    steps = normalize_steps(raw.get("steps"), strict=False)
    if not steps:
        return None
    states = raw.get("step_states") if isinstance(raw.get("step_states"), dict) else {}
    step_states = {}
    for s in steps:
        e = states.get(s["id"]) if isinstance(states.get(s["id"]), dict) else {}
        st = _blank_step_state()
        state = str(e.get("state") or "pending")
        st["state"] = state if state in STEP_STATES else "pending"
        st["since"] = _float(e.get("since"))
        st["note"] = _text(e.get("note"), 1000)
        st["detail"] = dict(e["detail"]) if isinstance(e.get("detail"), dict) else {}
        st["rounds"] = _int(e.get("rounds"), 0, 0, 1000)
        step_states[s["id"]] = st
    state = str(raw.get("state") or "running")
    hist = []
    for h in raw.get("history") if isinstance(raw.get("history"), list) else []:
        if isinstance(h, dict) and h.get("event") in HISTORY_EVENTS:
            hist.append(
                {
                    "at": _float(h.get("at")),
                    "step": _text(h.get("step"), 32),
                    "event": h["event"],
                    "note": _text(h.get("note"), 1000),
                }
            )
    by = _text(raw.get("by"), 200) or "user"
    if by not in BY_KINDS and not by.startswith("agent:"):
        by = "user"
    return {
        "id": fid,
        "plan_id": _text(raw.get("plan_id"), 64),
        "plan_name": _text(raw.get("plan_name"), 120),
        "steps": steps,
        "subject": _normalize_subject(raw.get("subject")),
        "session": _text(raw.get("session"), 300),
        "repo_root": _text(raw.get("repo_root"), 1000),
        "branch": _text(raw.get("branch"), 300),
        "state": state if state in STATES else "running",
        "cursor": _int(raw.get("cursor"), 0, 0, len(steps)),
        "step_states": step_states,
        "reason": _text(raw.get("reason"), 1000),
        "history": hist[-MAX_HISTORY:],
        "started": _float(raw.get("started")),
        "updated": _float(raw.get("updated")),
        "finished": _float(raw.get("finished")),
        "by": by,
        # When this flight's session was first seen missing (prune's clock).
        "gone_since": _float(raw.get("gone_since")),
        # What has been announced already, so a restart never repeats a
        # needs_you / finished push: "<step id>:<rounds>:you", "finished".
        "announced": (
            [str(a)[:80] for a in raw.get("announced") or [] if a][-20:]
            if isinstance(raw.get("announced"), list)
            else []
        ),
        "boot": _text(raw.get("boot"), 64),
        "owner": _text(raw.get("owner"), 64),
        "owner_at": _float(raw.get("owner_at")),
    }


def list_flights(include_finished: bool = True) -> List[dict]:
    """Every flight, newest first."""
    with _LOCK:
        flights = _cached_flights_locked()
        out = [
            copy.deepcopy(f)
            for f in flights.values()
            if include_finished or f["state"] not in FINAL
        ]
    return sorted(out, key=lambda f: -(f["started"] or 0.0))


def get(flight_id: str) -> Optional[dict]:
    with _LOCK:
        f = _cached_flights_locked().get(str(flight_id or ""))
        return copy.deepcopy(f) if f is not None else None


def for_session(title: str, unfinished_only: bool = True) -> Optional[dict]:
    """The flight bound to ``title`` (the newest unfinished one), or None."""
    if not title:
        return None
    best = None
    with _LOCK:
        for f in _cached_flights_locked().values():
            if f["session"] != title:
                continue
            if unfinished_only and f["state"] not in UNFINISHED:
                continue
            if best is None or f["started"] > best["started"]:
                best = f
        return copy.deepcopy(best) if best is not None else None


def mutate(flight_id: str, fn: Callable[[dict], None]) -> Optional[dict]:
    """Read-modify-write one flight under the lock; ``fn`` may raise
    :class:`FlightError` (nothing is written then)."""
    with _LOCK:
        flights = _load_flights_locked()
        f = flights.get(str(flight_id or ""))
        if f is None:
            return None
        fn(f)
        f = _normalize_flight(f["id"], f) or f
        flights[f["id"]] = f
        _save_flights_locked(flights)
    return copy.deepcopy(f)


def delete(flight_id: str) -> bool:
    with _LOCK:
        flights = _load_flights_locked()
        if flights.pop(str(flight_id or ""), None) is None:
            return False
        _save_flights_locked(flights)
    return True


def _put_new(flight: dict, replaced: Optional[list] = None) -> dict:
    """Store a new flight (caller-built), enforcing one unfinished flight per
    session: a running/waiting/paused one is a 409 ``in_flight``; a halted one
    is cancelled ("replaced") so a parked flight never blocks a fresh start.
    Each replaced flight is appended to ``replaced`` (the caller announces
    it; the driver's pass also sweeps any it missed)."""
    with _LOCK:
        flights = _load_flights_locked()
        title = flight["session"]
        for f in flights.values():
            if not title or f["session"] != title:
                continue
            if f["state"] in HOLDING:
                raise FlightError(
                    "in_flight",
                    409,
                    "%s is already on a flight (%s)" % (title, f["plan_name"]),
                )
            if f["state"] == "halted":
                _finish(f, "cancelled", "replaced by a new flight", flight["started"])
                if replaced is not None:
                    replaced.append(copy.deepcopy(f))
        flight["id"] = _new_id("f_", flights)
        flights[flight["id"]] = flight
        _keep_finished(flights)
        _save_flights_locked(flights)
    return copy.deepcopy(flight)


def _keep_finished(flights: Dict[str, dict]) -> None:
    done = sorted(
        (f for f in flights.values() if f["state"] in FINAL),
        key=lambda f: f["finished"] or f["updated"],
    )
    for f in done[: max(0, len(done) - KEEP_FINISHED)]:
        flights.pop(f["id"], None)


# --------------------------------------------------------------------------- #
# Taking off
# --------------------------------------------------------------------------- #
def fill_prompt(text: str, flight: dict) -> str:
    """``{ticket} {pr} {branch} {subject} {preview}`` filled from the flight
    (``{preview}``: the latest done Preview stop's ``detail.url``, "" when
    none). An unknown brace is left as typed (a prompt may legitimately
    contain braces)."""
    subj = flight.get("subject") or {}
    kind = subj.get("kind") or "manual"
    ident = subj.get("id") or ""
    values = {
        "ticket": ident if kind in ("ticket", "issue") else "",
        "pr": (
            (subj.get("url") or ("#%s" % ident if ident else ""))
            if kind == "pr"
            else ""
        ),
        "branch": flight.get("branch") or "",
        "subject": subj.get("title") or ident or flight.get("plan_name") or "",
        "preview": preview_url(flight),
    }
    return re.sub(
        r"\{(ticket|pr|branch|subject|preview)\}",
        lambda m: values[m.group(1)],
        str(text or ""),
    )


def preview_url(flight: dict) -> str:
    """The URL of the latest done ``preview`` stop of ``flight``, "" when
    none has flown."""
    url = ""
    states = flight.get("step_states") or {}
    for s in flight.get("steps") or []:
        if s.get("kind") != "preview":
            continue
        ss = states.get(s.get("id")) or {}
        if ss.get("state") == "done":
            url = str((ss.get("detail") or {}).get("url") or "") or url
    return url


def _first_agent_index(steps: List[dict]) -> Optional[int]:
    for i, s in enumerate(steps):
        if s["kind"] == "agent":
            return i
    return None


def start_cursor(plan: dict, start_at: str = "", attach: bool = False) -> int:
    """Where a flight of ``plan`` starts: ``start_at``'s index, the first
    non-agent step when attaching, else 0. Raises :class:`FlightError`
    ``unknown_step`` — call it BEFORE creating anything for the flight."""
    steps = plan["steps"]
    if start_at:
        ids = [s["id"] for s in steps]
        if start_at not in ids:
            raise FlightError("unknown_step", 400, "no step %s in this plan" % start_at)
        return ids.index(start_at)
    if attach:
        return next(
            (i for i, s in enumerate(steps) if s["kind"] != "agent"), len(steps)
        )
    return 0


def take_off(
    plan: dict,
    *,
    session: str,
    subject: Optional[dict] = None,
    by: str = "user",
    attach: bool = False,
    start_at: str = "",
    seeded: bool = False,
    repo_root: str = "",
    branch: str = "",
    now: Optional[float] = None,
    replaced: Optional[list] = None,
) -> dict:
    """Create a ``running`` flight of ``plan`` bound to ``session``.

    * ``attach`` (an EXISTING session): the cursor starts at ``start_at`` or at
      the first non-``agent`` step — the work the agent did is the agent step.
    * ``seeded`` (the session was created — or is being created by intake —
      with the plan's first agent step as its INITIAL prompt): that step is
      already running, so the driver never sends it a second time.
    * otherwise the cursor is at 0, pending.

    Steps before the cursor are recorded ``skipped`` (history ``jumped``).
    A halted flight it replaces on ``session`` is appended to ``replaced``.
    Raises :class:`FlightError` (``in_flight`` 409, ``unknown_step`` 400).
    """
    ts = float(now if now is not None else time.time())
    steps = copy.deepcopy(plan["steps"])
    cursor = start_cursor(plan, start_at, attach)
    flight = _normalize_flight(
        "pending",
        {
            "id": "pending",
            "plan_id": plan.get("id") or "",
            "plan_name": plan.get("name") or "",
            "steps": steps,
            "subject": subject or {"kind": "manual"},
            "session": session,
            "repo_root": repo_root,
            "branch": branch,
            "state": "running",
            "cursor": cursor,
            "started": ts,
            "updated": ts,
            "by": by,
        },
    )
    if flight is None:
        raise FlightError("bad_plan", 400, "the plan has no steps")
    for i, s in enumerate(flight["steps"][:cursor]):
        flight["step_states"][s["id"]]["state"] = "skipped"
        flight["step_states"][s["id"]]["since"] = ts
    if cursor:
        _hist(
            flight,
            ts,
            steps[cursor - 1]["id"],
            "jumped",
            "started at %s"
            % (steps[cursor]["title"] if cursor < len(steps) else "the end"),
        )
    first_agent = _first_agent_index(flight["steps"])
    if seeded and not attach and first_agent == cursor and cursor < len(steps):
        ss = flight["step_states"][steps[cursor]["id"]]
        ss["state"] = "running"
        ss["since"] = ts
        ss["detail"] = {"seeded": True}
        _hist(flight, ts, steps[cursor]["id"], "started", "the session's first prompt")
    if cursor >= len(steps):
        _finish(flight, "done", "", ts)
    return _put_new(flight, replaced)


def arm_pending(
    title: str,
    plan_id: str,
    subject,
    by: str = "intake",
    replaced: Optional[list] = None,
    branch: str = "",
) -> dict:
    """The intake hook: a ``running`` flight for ``title`` BEFORE its session
    exists (keyed like autopilot — an intake title is deterministic). The plan's
    first agent step is the session's initial prompt (the intake-built one), so
    it is recorded as already running. ``branch``: the branch the start will
    cut (fills ``{branch}``). Raises :class:`FlightError` (``unknown_plan``
    404, ``in_flight`` 409)."""
    plan = get_plan(plan_id)
    if plan is None:
        raise FlightError("unknown_plan", 404, "no such plan: %s" % plan_id)
    who = by if by in BY_KINDS or str(by).startswith("agent:") else "intake"
    return take_off(
        plan,
        session=str(title or ""),
        subject=_normalize_subject(subject),
        by=who,
        seeded=True,
        replaced=replaced,
        branch=str(branch or ""),
    )


#: The ``plan_id`` of a flight drawn at take-off rather than from a saved plan.
ADHOC_PLAN_ID = "adhoc"


def adhoc_plan(raw_steps, base: Optional[dict] = None, name: str = "") -> dict:
    """The plan an ad-hoc ``POST /api/flights/start`` flies: ``raw_steps``
    validated like a plan's (strict: :class:`PlanError` with ``steps[i]…``),
    after ``base``'s steps when a ``plan_id`` came too — those keep their ids,
    the given ones take the next free ``sN``. Alone, the given steps are
    numbered ``s1..sN`` and the plan is :data:`ADHOC_PLAN_ID`."""
    if not isinstance(raw_steps, list):
        raise PlanError("steps is a list", "steps")
    stripped = [dict(s, id="") if isinstance(s, dict) else s for s in raw_steps]
    given = normalize_steps(stripped, strict=True)
    if base is None:
        return {
            "id": ADHOC_PLAN_ID,
            "name": _text(name, 120) or "Ad-hoc flight",
            "session": _blank_session(),
            "steps": given,
        }
    steps = copy.deepcopy(base["steps"])
    if len(steps) + len(given) > MAX_STEPS:
        raise PlanError("a flight has at most %d steps" % MAX_STEPS, "steps")
    _renumber_after(steps, given)
    return dict(copy.deepcopy(base), steps=steps + given)


def _blank_session() -> dict:
    return {"repo_path": "", "program": "", "model": "", "in_place": False}


def _sid_number(sid: str) -> int:
    m = re.match(r"^s(\d+)$", str(sid or ""))
    return int(m.group(1)) if m else 0


def _renumber_after(existing: List[dict], new: List[dict], used=()) -> None:
    """Give each of ``new`` the next free ``sN`` after every id in
    ``existing`` (and ``used``: ids history still names), in place."""
    n = max(
        [_sid_number(s["id"]) for s in existing] + [_sid_number(u) for u in used] + [0]
    )
    for s in new:
        n += 1
        s["id"] = "s%d" % n


def with_lead(
    plan: dict, lead: List[dict], start_at: str = "", attach: bool = False
) -> Tuple[dict, str]:
    """``plan`` with ``lead`` (absorbed ship steps) placed where its flight
    starts — before the plan's own steps from that point — and the
    ``start_at`` that makes the flight begin on the first of them. Steps
    before the start are recorded skipped either way. Raises
    :class:`FlightError` ``unknown_step`` / :class:`PlanError` (over 40)."""
    if not lead:
        return plan, start_at
    cursor = start_cursor(plan, start_at, attach)
    steps = copy.deepcopy(plan["steps"])
    if len(steps) + len(lead) > MAX_STEPS:
        raise PlanError("a flight has at most %d steps" % MAX_STEPS, "steps")
    lead = copy.deepcopy(lead)
    _renumber_after(steps, lead)
    return (
        dict(plan, steps=steps[:cursor] + lead + steps[cursor:]),
        lead[0]["id"],
    )


#: Which ship rung a session stage proves done (``autopilot._STAGE_ACHIEVED``).
_STAGE_RUNG = {"committed": "commit", "pushed": "push", "pr": "pr"}


def absorb_steps(rec: Optional[dict], stage: str) -> List[dict]:
    """The ship steps that carry on an armed fast-track run ``rec`` (the
    autopilot record) from the observed session ``stage``: every rung after
    the one the stage proves, up to the run's target (its ``lane``, else its
    ``depth``), in :data:`_DEPTH_ORDER` order. ``merge`` is never proven by a
    stage. ``ask_first`` lands on the one step the run would have asked
    before — the commit for a commit lane, else the push — and only while
    that ask is still ahead (the record is held short of its lane). The run's
    own commit message rides along unless it was a placeholder. Pure."""
    if not rec:
        return []
    lane = str(rec.get("lane") or "")
    depth = str(rec.get("depth") or "")
    target = lane if lane in SHIP_LANES else depth
    if target not in SHIP_LANES:
        return []
    got = _STAGE_RUNG.get(str(stage or ""), "")
    first = _DEPTH_ORDER.index(got) + 1 if got else _DEPTH_ORDER.index("commit")
    rungs = [
        r
        for r in _DEPTH_ORDER[first : _DEPTH_ORDER.index(target) + 1]
        if r in SHIP_LANES
    ]
    held = (
        bool(rec.get("ask_first"))
        and depth in _DEPTH_ORDER
        and _DEPTH_ORDER.index(depth) < _DEPTH_ORDER.index(target)
    )
    ask_at = "commit" if target == "commit" else "push"
    message = (
        ""
        if rec.get("message_auto") or rec.get("message_written")
        else _text(rec.get("message"), 500)
    )
    return [
        normalize_step(
            {
                "kind": "ship",
                "config": {
                    "lane": r,
                    "ask_first": held and r == ask_at,
                    "message": message,
                },
            },
            i,
        )
        for i, r in enumerate(rungs)
    ]


def note_absorbed(
    flight: dict, lead_ids: List[str], halted: str = "", now: Optional[float] = None
) -> dict:
    """History for the steps a start took over from a fast-track run (an
    ``added`` event on the first of them; a run that had stopped keeps its
    reason there)."""
    ts = float(now if now is not None else time.time())
    titles = [s["title"] for s in flight["steps"] if s["id"] in lead_ids]
    if not titles:
        return flight
    note = "taken over from Fast-track: %s" % " → ".join(titles)
    if halted:
        note += " (it had stopped: %s)" % halted
    _hist(flight, ts, lead_ids[0], "added", note)
    return flight


# --------------------------------------------------------------------------- #
# Lease, prune
# --------------------------------------------------------------------------- #
def claim(
    flight_id: str, owner: str, now: Optional[float] = None
) -> Tuple[Optional[dict], bool]:
    """Take or refresh the driver lease (autopilot's :func:`claim`, per
    flight). ``(flight, took_over)`` or ``(None, False)`` while another live
    server holds it. A new owner re-earns the current step's idle dwell."""
    if not flight_id or not owner:
        return None, False
    ts = float(now if now is not None else time.time())
    with _LOCK:
        flights = _load_flights_locked()
        f = flights.get(flight_id)
        if f is None:
            return None, False
        held = f["owner"]
        if held and held != owner and ts - f["owner_at"] <= LEASE_STALE_S:
            return None, False
        took = held != owner
        f["owner"] = owner
        f["owner_at"] = ts
        if took:
            f["boot"] = owner
            cur = _current(f)
            if cur is not None:
                f["step_states"][cur["id"]]["detail"].pop("idle_since", None)
        _save_flights_locked(flights)
    return copy.deepcopy(f), took


def prune(live_titles, now: Optional[float] = None) -> List[dict]:
    """Cancel unfinished flights whose session has been gone longer than
    :data:`GONE_GRACE_S` (a flight armed before its session exists keeps
    :data:`ARM_GRACE_S` from take-off). Returns the cancelled flights (the
    driver announces them)."""
    live = set(live_titles or [])
    ts = float(now if now is not None else time.time())
    out = []
    with _LOCK:
        flights = _load_flights_locked()
        changed = False
        for f in flights.values():
            if f["state"] not in UNFINISHED or not f["session"]:
                continue
            if f["session"] in live:
                if f["gone_since"]:
                    f["gone_since"] = 0.0
                    changed = True
                continue
            if not f["gone_since"]:
                f["gone_since"] = ts
                changed = True
            gone_for = ts - max(
                f["gone_since"], f["started"] + ARM_GRACE_S - GONE_GRACE_S
            )
            if gone_for > GONE_GRACE_S:
                _finish(f, "cancelled", "its session is gone", ts)
                out.append(copy.deepcopy(f))
                changed = True
        if changed:
            _save_flights_locked(flights)
    return out


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def _current(flight: dict) -> Optional[dict]:
    c = flight["cursor"]
    return flight["steps"][c] if 0 <= c < len(flight["steps"]) else None


def _actor(step: dict, ss: dict) -> str:
    if step["kind"] == "you" or ss["state"] == "waiting_you":
        return "you"
    return STEP_KINDS[step["kind"]]["actor"]


def step_view(flight: dict) -> dict:
    """``{id, kind, title, state, actor}`` of the current step (``{}`` past the
    end)."""
    cur = _current(flight)
    if cur is None:
        return {}
    ss = flight["step_states"][cur["id"]]
    return {
        "id": cur["id"],
        "kind": cur["kind"],
        "title": cur["title"],
        "state": ss["state"],
        "actor": _actor(cur, ss),
        "note": ss["note"],
        "since": ss["since"],
    }


def join_dto(flight: dict) -> Optional[dict]:
    """``{waiting, total}`` while the current step (an agent step that waits
    for its workers, or a join) has workers outstanding, else None."""
    cur = _current(flight)
    if cur is None or flight["state"] not in UNFINISHED:
        return None
    if cur["kind"] != "join" and not (
        cur["kind"] == "agent" and cur["config"].get("join", True)
    ):
        return None
    d = flight["step_states"][cur["id"]]["detail"]
    waiting = _int(d.get("waiting"), 0, 0, 10000)
    if not waiting:
        return None
    return {"waiting": waiting, "total": _int(d.get("total"), waiting, waiting, 10000)}


def branch_step(flight: dict, child: str, created: Optional[float] = None) -> str:
    """The step of ``flight`` during which worker ``child`` was spawned: the
    split that names it, else the step whose ``since`` ≤ ``created`` < the
    next started step's ``since``, else the current step."""
    for s in flight["steps"]:
        if s["kind"] == "split" and child in (
            flight["step_states"][s["id"]]["detail"].get("children") or []
        ):
            return s["id"]
    cur = _current(flight) or flight["steps"][-1]
    if not created:
        return cur["id"]
    found = ""
    for s in flight["steps"]:
        since = flight["step_states"][s["id"]]["since"]
        if since <= 0:
            continue
        if since <= created:
            found = s["id"]
        else:
            break
    return found or cur["id"]


def row_dto(
    title: str, parent: str = "", created: Optional[float] = None
) -> Optional[dict]:
    """The ``flight`` field of a session's ``/api/instances`` row, or None.

    ``parent`` (the row's live parent) adds ``branch: {parent, step}`` while
    the parent is in a flight — a worker drawn as a branch of that flight. A
    worker with no flight of its own then gets a dto with only that: ``id``
    ``""``, ``state`` ``""`` (never live, so nothing reads it as a flight)."""
    f = for_session(title)
    pf = for_session(parent) if parent and parent != title else None
    branch = (
        {"parent": parent, "step": branch_step(pf, title, created)}
        if pf is not None
        else None
    )
    if f is None:
        if branch is None:
            return None
        return {
            "id": "",
            "plan_name": pf["plan_name"],
            "cursor": 0,
            "total": 0,
            "state": "",
            "reason": "",
            "step": None,
            "join": None,
            "branch": branch,
        }
    step = step_view(f)
    return {
        "id": f["id"],
        "plan_name": f["plan_name"],
        "cursor": f["cursor"],
        "total": len(f["steps"]),
        "state": f["state"],
        "reason": f["reason"],
        "step": (
            {k: step[k] for k in ("id", "kind", "title", "state", "actor")}
            if step
            else None
        ),
        "join": join_dto(f),
        "branch": branch,
    }


def you_text(flight: dict) -> Tuple[str, List[str]]:
    """What a parked step asks of you: ``(text, items)``."""
    cur = _current(flight)
    if cur is None:
        return "", []
    d = flight["step_states"][cur["id"]]["detail"]
    if d.get("failed_reason"):
        return "%s failed: %s" % (cur["title"], d["failed_reason"]), []
    if cur["kind"] in ("you", "someone"):
        # A perch, yours or someone else's (docs/flights.md "Round 15"):
        # the bell says what was asked, not the stop's name.
        return cur["config"].get("text") or cur["title"], list(
            cur["config"].get("items") or []
        )
    if cur["kind"] == "verify":
        items = [str(i) for i in d.get("items") or []]
        return (
            "Check %d item%s on the checklist"
            % (len(items), "" if len(items) == 1 else "s"),
            items,
        )
    return cur["title"], []


def event_data(flight: dict) -> dict:
    """The ``flight.*`` event payload (docs/flights.md "Events")."""
    step = step_view(flight)
    detail = "%s · %s" % (flight["plan_name"], step.get("title") or flight["state"])
    if flight["state"] == "waiting_you":
        text, _items_ = you_text(flight)
        detail = "%s: %s" % (flight["session"] or flight["plan_name"], text)
    elif flight["state"] == "paused" and flight["reason"] == HALT_POINT:
        detail = "%s: halted before %s" % (
            flight["session"] or flight["plan_name"],
            step.get("title") or "the next stop",
        )
    elif flight["state"] == "paused":
        detail = "%s: paused" % (flight["session"] or flight["plan_name"])
    elif flight["state"] in ("halted", "cancelled") and flight["reason"]:
        detail = "%s: %s" % (flight["plan_name"], flight["reason"])
    elif flight["state"] == "done":
        detail = "%s landed" % flight["plan_name"]
    return {
        "id": flight["id"],
        "plan_name": flight["plan_name"],
        "session": flight["session"],
        "state": flight["state"],
        "cursor": flight["cursor"],
        "step": (
            {k: step.get(k) for k in ("id", "kind", "title", "state")} if step else {}
        ),
        "reason": flight["reason"],
        "detail": detail[:300],
    }


# --------------------------------------------------------------------------- #
# The pure planner
# --------------------------------------------------------------------------- #
def _wait(note: str, **detail) -> dict:
    out = {"action": "wait", "note": note}
    if detail:
        out["detail"] = detail
    return out


def _mins(seconds: float) -> str:
    m = int(max(0.0, seconds) // 60)
    return "%dm" % m if m < 60 else "%dh%02dm" % (m // 60, m % 60)


_ACTIVITY_WORDS = {
    "working": "the agent is working",
    "clarify": "the agent is asking you something",
    "limit": "usage limit — waiting for the window to reopen",
    "offline": "the agent is offline",
    "": "the agent's state is unknown",
}


def _session_hold(flight: dict, step: dict, sess: dict) -> Optional[dict]:
    """A visible wait while the session cannot take this step, else None."""
    if step["kind"] not in SESSION_KINDS:
        return None
    if not flight["session"]:
        return _wait("no session yet")
    if not sess.get("present"):
        if sess.get("pending"):
            return _wait("the session is starting")
        return _wait("waiting for %s to start" % flight["session"])
    if sess.get("status") == "loading":
        return _wait("the session is starting")
    if sess.get("status") == "paused":
        return _wait("the session is paused")
    # A SLEEPING session is not a hold: the sleep sweep only ever stops an idle
    # agent, so its turn is over (see :func:`_settle`), and a step that types
    # into it wakes it first (the driver's ``sleep.ensure_awake``). Waiting on
    # it instead would park a flight forever behind a sweep that put it there
    # BECAUSE it was waiting.
    return None


def _settle(
    detail: dict,
    sess: dict,
    since: float,
    now: float,
    need_work: bool = True,
    deadline: float = AGENT_START_DEADLINE_S,
) -> Optional[dict]:
    """The agent-settled gate (autopilot's ``_agent_settled`` + ``worked_at``):
    a wait/fail action while the agent's turn is not over, else None."""
    upd = {}
    worked = float(detail.get("worked_at") or 0.0)
    activity = "idle" if sess.get("sleeping") else str(sess.get("activity") or "")
    if not worked and (
        activity == "working"
        or float(sess.get("turn_ended_at") or 0.0) > since
        or float(sess.get("worked_at") or 0.0) > since
    ):
        worked = now
        upd["worked_at"] = now
    if sess.get("limited") or activity == "limit":
        return _wait(_ACTIVITY_WORDS["limit"], idle_since=None, **upd)
    if activity != "idle":
        return _wait(
            _ACTIVITY_WORDS.get(activity, "the agent is %s" % activity),
            idle_since=None,
            **upd,
        )
    if sess.get("queue_pending"):
        return _wait("the prompt queue still has work", idle_since=None, **upd)
    if sess.get("background_waiting"):
        return _wait(
            "a background job the agent started is still waiting",
            idle_since=None,
            **upd,
        )
    idle_since = detail.get("idle_since")
    if idle_since is None:
        return _wait("the agent just went idle", idle_since=now, **upd)
    if now - float(idle_since) < IDLE_SETTLE_S:
        return _wait("confirming the agent is done", **upd)
    if need_work and not worked:
        if now - since > deadline:
            return {"action": "fail", "reason": "the agent never started working"}
        return _wait("waiting for the agent to start", **upd)
    return None


def _merge_detail(action: dict, upd: dict) -> dict:
    if upd:
        action.setdefault("detail", {}).update(upd)
    return action


# --- Receipts -------------------------------------------------------------------
#: A receipt (``detail.receipt`` on ``done``) is what the stop did, in a few
#: words: the box shows it once the stop has flown.
MAX_RECEIPT = 80
_PR_NUMBER_RE = re.compile(r"/(?:pull|pulls|merge_requests)/(\d+)")


def plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _span(seconds: float) -> str:
    """A duration a person reads: ``"12 min"``, ``"2 h 5 min"``."""
    m = max(1, int(round(max(0.0, seconds) / 60.0)))
    if m < 60:
        return "%d min" % m
    return "%d h" % (m // 60) + (" %d min" % (m % 60) if m % 60 else "")


def _receipt(action: dict, text: str) -> dict:
    """``action`` (a ``done``) carrying ``text`` as its receipt."""
    action.setdefault("detail", {})["receipt"] = str(text)[:MAX_RECEIPT]
    return action


def ship_receipt(lane: str, url: str = "") -> str:
    """A ship stop's receipt: ``"PR #291"`` for the pr lane (from the PR's
    url), else what the lane did."""
    if lane == "pr":
        m = _PR_NUMBER_RE.search(str(url or ""))
        return "PR #%s" % m.group(1) if m else "PR opened"
    return {"commit": "committed", "push": "pushed", "merge": "merged"}.get(
        lane, "shipped"
    )


def ticket_receipt(config: dict) -> str:
    """A ticket stop's receipt: ``"→ Done"`` when it moved the ticket, else
    ``"commented"``."""
    state = str((config or {}).get("state") or "").strip()
    if state:
        return "→ %s" % state
    return "commented" if (config or {}).get("comment") else "nothing to do"


def _judge_agent(flight, step, ss, obs, now) -> dict:
    d = ss["detail"]
    need = not d.get("no_prompt")
    sess = obs.get("session") or {}
    held = _settle(d, sess, ss["since"], now, need_work=need)
    kids = obs.get("children")
    if not step["config"].get("join", True) or not isinstance(kids, list):
        if held is not None:
            return held
        return _receipt(
            {"action": "done", "note": "the agent's turn settled"},
            "settled after %s" % _span(now - ss["since"]),
        )
    view = join_view(kids, d, ss["since"], now)
    upd = view["detail"] if (view["total"] or "children" in d) else {}
    if held is not None:
        return _merge_detail(held, upd)
    if view["waiting"]:
        return _wait(view["note"], **upd)
    if sess.get("messages_pending"):
        # A worker's report is typed into the agent once it is idle: the agent
        # has not read it yet, so its turn is not over.
        return _wait(
            "a worker's report is on its way to the agent", idle_since=None, **upd
        )
    if view["last_finish"] and float(d.get("idle_since") or 0.0) < view["last_finish"]:
        # The last worker finished after the agent went idle: give the agent
        # one settle window to pick its report up before calling it done.
        return _wait("the agent is reading its workers' reports", idle_since=now, **upd)
    return _receipt(
        _merge_detail(
            {
                "action": "done",
                "note": (
                    "the agent and its %d worker%s are done"
                    % (view["total"], "" if view["total"] == 1 else "s")
                    if view["total"]
                    else "the agent's turn settled"
                ),
            },
            upd,
        ),
        "settled after %s" % _span(now - ss["since"]),
    )


# --- Fan-out and join ---------------------------------------------------------
#: A worker's idle-settle gates — ``wait_for_session``'s (``backend/mcp/
#: tools.py`` SETTLE_CLAUDE_S / SETTLE_OTHER_S / OFFLINE_DONE_S /
#: DISPATCH_QUIET_S), so a join and an orchestrator's wait agree on "finished".
CHILD_SETTLE_CLAUDE_S = 8.0
CHILD_SETTLE_OTHER_S = 30.0
CHILD_OFFLINE_DONE_S = 30.0
#: A worker this young that idles has not visibly started its task yet.
CHILD_QUIET_S = 60.0
#: Child states: still going (``working``, ``asking``) or finished.
CHILD_OPEN = ("working", "asking")


def child_settle_s(provider: str) -> float:
    return CHILD_SETTLE_CLAUDE_S if provider == "claude" else CHILD_SETTLE_OTHER_S


def child_state(child: dict, prev: Optional[dict], since: float, now: float) -> dict:
    """One worker's join entry ``{title, state, report?}`` from its observed
    facts (``present``, ``status``, ``activity``, ``activity_since``,
    ``sleeping``, ``held``, ``queue_pending``, ``background_waiting``,
    ``messages_pending``, ``created``, ``settle_s``, ``report``). ``state`` is
    ``working | asking | done | failed | gone``.

    Finished = a report newer than ``since`` (``failed`` when it says so), or
    idle-settled by ``wait_for_session``'s gates, or offline long enough, or
    closed / deleted. A ``blocked`` report is a question: ``asking`` until the
    worker works again (``prev`` remembers that it did)."""
    out: dict = {"title": str(child.get("title") or "")}
    if not child.get("present") or child.get("status") == "paused":
        out["state"] = "gone"
        return out
    activity = str(child.get("activity") or "")
    if child.get("sleeping") and activity == "offline":
        activity = "idle"  # asleep: stopped only once its turn was over
    act_since = _float(child.get("activity_since"))
    rep = child.get("report") if isinstance(child.get("report"), dict) else None
    if rep is not None and _float(rep.get("ts")) <= since:
        rep = None  # an earlier step's report
    if rep is not None:
        rts = _float(rep.get("ts"))
        out["report"] = {
            "id": str(rep.get("id") or ""),
            "status": str(rep.get("status") or ""),
            "summary": str(rep.get("summary") or ""),
            "ts": rts,
        }
        busy_again = activity not in ("idle", "offline") and act_since > rts
        status = out["report"]["status"]
        if status == "blocked":
            resumed = busy_again or (
                isinstance(prev, dict)
                and prev.get("state") in ("working", "done")
                and ((prev.get("report") or {}).get("id") == out["report"]["id"])
            )
            if not resumed:
                out["state"] = "asking"
                return out
        elif not busy_again:
            out["state"] = "failed" if status == "failed" else "done"
            return out
    out["state"] = _activity_state(child, activity, act_since, now)
    return out


def _activity_state(child: dict, activity: str, act_since: float, now: float) -> str:
    """``wait_for_session``'s observed verdict for a worker with no report:
    ``done`` once idle-settled (or offline long enough), else open."""
    if activity == "clarify":
        return "asking"
    if child.get("status") == "loading" or child.get("held"):
        return "working"
    if activity == "offline":
        if act_since > 0 and now - act_since >= CHILD_OFFLINE_DONE_S:
            return "done"
        return "working"
    if activity != "idle":
        return "working"
    if (
        child.get("queue_pending")
        or child.get("background_waiting")
        or child.get("messages_pending")
    ):
        return "working"
    if act_since <= 0:
        return "working"  # a placeholder idle the tick has not probed yet
    settle = _float(child.get("settle_s"), CHILD_SETTLE_OTHER_S)
    if now - act_since < settle:
        return "working"
    if now - _float(child.get("created")) < CHILD_QUIET_S:
        return "working"
    return "done"


def join_view(children: List[dict], detail: dict, since: float, now: float) -> dict:
    """The join over ``children`` (observed facts, see :func:`child_state`):
    ``{detail: {waiting, total, children}, waiting, total, note, failed,
    last_finish}``. ``detail`` is the step's own (its last entries are the
    ``prev`` each worker is judged against)."""
    prev = {
        str(e.get("title") or ""): e
        for e in detail.get("children") or []
        if isinstance(e, dict)
    }
    kids = [c for c in children if isinstance(c, dict) and c.get("title")]
    entries = [child_state(c, prev.get(str(c["title"])), since, now) for c in kids]
    open_ = [e for e in entries if e["state"] in CHILD_OPEN]
    asking = [e["title"] for e in entries if e["state"] == "asking"]
    failed = [e for e in entries if e["state"] == "failed"]
    # When the last worker finished (its report, else when it went idle): the
    # agent step gives its agent one settle window after it.
    last = 0.0
    for e, c in zip(entries, kids):
        if e["state"] in ("done", "failed"):
            at = _float((e.get("report") or {}).get("ts")) or _float(
                c.get("activity_since")
            )
            last = max(last, at)
    if asking:
        note = "worker%s %s %s asking" % (
            "" if len(asking) == 1 else "s",
            ", ".join(asking[:4]) + (" …" if len(asking) > 4 else ""),
            "is" if len(asking) == 1 else "are",
        )
    elif open_:
        note = "waiting for %d of %d worker%s" % (
            len(open_),
            len(entries),
            "" if len(entries) == 1 else "s",
        )
    else:
        note = ""
    return {
        "detail": {"waiting": len(open_), "total": len(entries), "children": entries},
        "waiting": len(open_),
        "total": len(entries),
        "note": note,
        "failed": failed,
        "last_finish": last,
    }


def split_step_before(flight: dict, index: Optional[int] = None) -> Optional[dict]:
    """The nearest ``split`` step before ``index`` (default: the cursor)."""
    i = flight["cursor"] if index is None else index
    for s in reversed(flight["steps"][: max(0, i)]):
        if s["kind"] == "split":
            return s
    return None


def spawned_titles(flight: dict, index: Optional[int] = None) -> List[str]:
    """The workers the nearest split before ``index`` spawned (``of: spawned``)."""
    s = split_step_before(flight, index)
    if s is None:
        return []
    kids = flight["step_states"][s["id"]]["detail"].get("children") or []
    return [str(t) for t in kids if isinstance(t, str) and t]


def join_since(flight: dict) -> float:
    """What a join step counts reports from: the step before it began (the
    agent or split that put the workers to work), else the join itself."""
    i = flight["cursor"]
    own = flight["step_states"][flight["steps"][i]["id"]]["since"]
    if i > 0:
        before = flight["step_states"][flight["steps"][i - 1]["id"]]["since"]
        if before > 0:
            return min(before, own) if own else before
    return own


def split_prompts(config: dict, flight: dict) -> List[str]:
    """A split step's worker prompts: one per non-empty line, or the one line
    ``count`` times; ``{index}`` (1-based) and ``{n}`` filled, plus the
    flight's own placeholders."""
    lines = [
        ln.strip() for ln in str(config.get("prompts") or "").splitlines() if ln.strip()
    ][:MAX_SPLIT]
    if len(lines) == 1:
        lines = lines * _int(config.get("count"), 1, 1, MAX_SPLIT)
    n = len(lines)
    return [
        fill_prompt(ln, flight).replace("{index}", str(i + 1)).replace("{n}", str(n))
        for i, ln in enumerate(lines)
    ]


def _judge_join(flight, step, ss, obs, now) -> dict:
    kids = obs.get("children")
    if not isinstance(kids, list):
        return _wait("looking for the workers")
    view = join_view(kids, ss["detail"], join_since(flight), now)
    upd = view["detail"]
    if step["config"].get("on_failed", "halt") == "halt" and view["failed"]:
        bad = view["failed"][0]
        return {
            "action": "fail",
            "reason": "worker %s failed: %s"
            % (
                bad["title"],
                (bad.get("report") or {}).get("summary") or "no summary",
            ),
            "detail": upd,
        }
    if view["waiting"]:
        return _wait(view["note"], **upd)
    done = sum(1 for e in upd.get("children") or [] if e.get("state") == "done")
    return _receipt(
        {
            "action": "done",
            "note": (
                "all %d worker%s finished"
                % (view["total"], "" if view["total"] == 1 else "s")
                if view["total"]
                else "no workers to wait for"
            ),
            "detail": upd,
        },
        "%d of %d done" % (done, view["total"]) if view["total"] else "no workers",
    )


def _ours(flight: dict, rec: dict, lane: str) -> bool:
    """Whether an autopilot record is the one THIS flight armed for ``lane`` —
    a person's own ⏩ run on the same session is not this step's outcome."""
    return (
        rec.get("source") == "flight"
        and rec.get("item") == flight["id"]
        and (rec.get("lane") or rec.get("depth")) == lane
    )


_BUSY_NOTE = "Fast-track is busy with your own run"


def _judge_ship(flight, step, ss, obs, now) -> dict:
    rec = obs.get("autopilot")
    lane = step["config"]["lane"]
    if not rec:
        return {
            "action": "fail",
            "reason": "Fast-track was turned off before %s finished" % step["title"],
        }
    if not _ours(flight, rec, lane):
        return _wait(_BUSY_NOTE)
    state = rec.get("state")
    if state == "done" and _awaiting_go(rec):
        return _wait("waiting for your go — Fast-track asks before it ships")
    if state == "done":
        return _receipt(
            {"action": "done", "note": rec.get("url") or ""},
            ship_receipt(lane, rec.get("url") or ""),
        )
    if state == "halted":
        return {"action": "fail", "reason": rec.get("reason") or "Fast-track stopped"}
    return _wait(rec.get("note") or "shipping: %s" % (rec.get("step") or lane))


#: The autopilot ladder (``autopilot.DEPTH_ORDER``); the ship lanes share its
#: rung names.
_DEPTH_ORDER = ("off", "agent", "commit", "push", "pr", "merge")


def _awaiting_go(rec: dict) -> bool:
    """``lanes.awaiting_approval``: an ask-first record parked done at its held
    rung, short of its lane."""
    if not rec.get("ask_first"):
        return False
    lane, held = str(rec.get("lane") or ""), str(rec.get("depth") or "")
    if lane not in _DEPTH_ORDER or held not in _DEPTH_ORDER:
        return False
    return _DEPTH_ORDER.index(held) < _DEPTH_ORDER.index(lane)


def _judge_verify(flight, step, ss, obs, now) -> dict:
    v = obs.get("verify") or {}
    d = ss["detail"]
    who = step["config"]["who"]
    if not v.get("plan"):
        if now - ss["since"] > VERIFY_WRITE_DEADLINE_S:
            return {
                "action": "fail",
                "reason": "no checklist was written for this branch",
            }
        return _wait("writing the checklist")
    up = {"plan": v["plan"], "branch": v.get("branch") or flight.get("branch") or ""}
    state = v.get("state") or ""
    if state == "generating":
        return _wait("writing the checklist", **up)
    if state == "failed" and not v.get("steps"):
        return {
            "action": "fail",
            "reason": "the checklist could not be written: %s"
            % (v.get("error") or "no reason given"),
        }
    if step["config"]["wait_live"] and not v.get("live"):
        return _wait(
            "waiting for the work to reach %s"
            % (v.get("live_branch") or "the live branch"),
            **up,
        )
    failed = list(v.get("failed") or [])
    if failed:
        return {"action": "fail", "reason": "checklist item failed: %s" % failed[0]}
    open_agent = list(v.get("open_agent") or [])
    open_human = list(v.get("open_human") or [])
    if state == "running":
        return _wait("the agent is working through the checklist", **up)
    if who in ("agent", "both") and open_agent and not d.get("run_started"):
        return {
            "action": "run",
            "phase": "agent_run",
            "note": "starting the agent on the checklist",
            "detail": up,
        }
    if who == "agent":
        if v.get("settled") or not open_agent:
            return _receipt(
                {"action": "done", "note": "the agent's checklist items are answered"},
                _verify_receipt(v),
            )
        return _wait("the agent's checklist items are still open", **up)
    if who == "you":
        open_human = open_human + open_agent
    elif open_agent and d.get("run_started"):
        # The agent's run ended with items it never answered: a person's now.
        open_human = open_human + open_agent
    if v.get("settled") or not open_human:
        return _receipt(
            {"action": "done", "note": "every checklist item has an answer"},
            _verify_receipt(v),
        )
    return {
        "action": "you",
        "note": "%d checklist item%s for you"
        % (len(open_human), "" if len(open_human) == 1 else "s"),
        "detail": dict(up, items=open_human[:40]),
    }


def _verify_receipt(v: dict) -> str:
    """``"k of n"``: the checklist's answered items of all of them."""
    n = len(v.get("steps") or [])
    k = v.get("answered")
    if not isinstance(k, int):
        k = n - len(v.get("open_agent") or []) - len(v.get("open_human") or [])
    return "%d of %d" % (max(0, min(k, n)), n)


def _judge_wait(flight, step, ss, obs, now) -> dict:
    until = float(ss["detail"].get("until") or 0.0)
    if not until:
        return {"action": "run", "note": "setting the timer"}
    if now >= until:
        cfg = step["config"]
        return _receipt(
            {"action": "done", "note": ""},
            (
                "until %s" % cfg["until"]
                if cfg.get("until")
                else "%d min" % int(cfg.get("minutes") or 0)
            ),
        )
    return _wait("%s left" % _mins(until - now))


def _judge_reviews(flight, step, ss, obs, now) -> dict:
    cfg = step["config"]
    d = ss["detail"]
    sess = obs.get("session") or {}
    if not sess.get("pr_url"):
        if now - ss["since"] > NO_PR_DEADLINE_S:
            return {"action": "fail", "reason": "there is no pull request to watch"}
        return _wait("waiting for the pull request")
    phase = d.get("phase") or "watch"
    if phase == "agent":
        held = _settle(d, sess, float(d.get("phase_since") or ss["since"]), now)
        if held is not None:
            return held
        return {"action": "run", "phase": "push", "note": "pushing the review fixes"}
    if phase == "push":
        rec = obs.get("autopilot")
        if not rec:
            return {
                "action": "fail",
                "reason": "Fast-track was turned off before the review fixes were pushed",
            }
        if not _ours(flight, rec, "push"):
            return _wait(_BUSY_NOTE)
        if rec.get("state") == "halted":
            return {"action": "fail", "reason": rec.get("reason") or "the push stopped"}
        if rec.get("state") == "done":
            return _wait(
                "pushed — watching for new comments",
                phase="watch",
                quiet_since=now,
                idle_since=None,
            )
        return _wait(rec.get("note") or "pushing the review fixes")
    ids = (obs.get("reviews") or {}).get("ids")
    if isinstance(ids, list) and not d.get("seeded"):
        # What was already on the PR when this step began was the previous
        # step's business (a PR flight's first prompt carries it): only what
        # arrives from here on is "new".
        return _wait(
            "watching for new review comments", seen=[str(i) for i in ids], seeded=True
        )
    seen = set(str(i) for i in d.get("seen") or [])
    new = [str(i) for i in ids if str(i) not in seen] if isinstance(ids, list) else []
    if new:
        if int(d.get("review_rounds") or 0) >= cfg["max_rounds"]:
            return {
                "action": "fail",
                "reason": "still getting review comments after %d rounds"
                % cfg["max_rounds"],
            }
        return {
            "action": "run",
            "phase": "send",
            "note": "%d new review comment%s for the agent"
            % (len(new), "" if len(new) == 1 else "s"),
            "comments": new,
        }
    quiet_since = float(d.get("quiet_since") or ss["since"])
    quiet = cfg["quiet_minutes"] * 60.0
    if now - quiet_since >= quiet:
        rounds = int(d.get("review_rounds") or 0)
        return _receipt(
            {
                "action": "done",
                "note": "no new review comments for %d min" % cfg["quiet_minutes"],
            },
            "%s, quiet" % (plural(rounds, "round") if rounds else "no rounds"),
        )
    return _wait(
        "quiet for %s of %d min" % (_mins(now - quiet_since), cfg["quiet_minutes"])
    )


def _judge_checks(flight, step, ss, obs, now) -> dict:
    c = obs.get("checks") or {}
    verdict = c.get("verdict") or ""
    waited = now - ss["since"]
    if verdict == "ok":
        return _receipt({"action": "done", "note": "every check passed"}, "all green")
    if verdict == "none" and waited >= CHECKS_NONE_GRACE_S:
        return _receipt(
            {"action": "done", "note": "no checks report on this commit"}, "no checks"
        )
    if verdict == "failed":
        names = [str(n) for n in c.get("failing") or []]
        return {
            "action": "fail",
            "reason": "failing checks: %s"
            % (", ".join(names[:8]) or "a check is failing"),
        }
    if waited > step["config"]["timeout_minutes"] * 60.0:
        return {
            "action": "fail",
            "reason": "checks still not finished after %d min"
            % step["config"]["timeout_minutes"],
        }
    if verdict in ("", "unknown"):
        return _wait("could not read the checks yet")
    return _wait("checks are running")


def _judge_command(flight, step, ss, obs, now) -> dict:
    c = obs.get("command")
    if not c:
        return {
            "action": "run",
            "note": "running the command again (it was interrupted)",
        }
    if c.get("running"):
        lines = str(step["config"].get("command") or "").splitlines() or ["the command"]
        return _wait("running: %s" % lines[0][:80])
    out = str(c.get("output") or "")
    if c.get("exit") == 0:
        return {
            "action": "done",
            "note": "exit 0",
            "detail": {"output": out, "receipt": "exit 0"},
        }
    return {
        "action": "fail",
        "reason": "exited %s: %s" % (c.get("exit"), out[-2000:].strip() or "no output"),
        "detail": {"output": out},
    }


def _judge_rerun(flight, step, ss, obs, now) -> dict:
    """ticket / notify / you after a restart mid-effect: do it again."""
    return {"action": "run", "note": ""}


_JUDGES = {
    "agent": _judge_agent,
    "ship": _judge_ship,
    "verify": _judge_verify,
    "wait": _judge_wait,
    "reviews": _judge_reviews,
    "checks": _judge_checks,
    "command": _judge_command,
    "ticket": _judge_rerun,
    "notify": _judge_rerun,
    "you": _judge_rerun,
    # A split interrupted mid-spawn runs again; the executor adopts the
    # workers it already made instead of spawning them twice.
    "split": _judge_rerun,
    "join": _judge_join,
}


def judge_for(kind: str) -> Callable:
    """The judge of a running step of ``kind``: a built-in's, else the
    registered plug-in's (:mod:`backend.web.core.flight_kinds`)."""
    judge = _JUDGES.get(kind)
    if judge is not None:
        return judge
    plug = _kinds.get(kind)
    if plug is None:
        raise KeyError(kind)
    return plug.judge


def _resolve_fail(flight: dict, step: dict, ss: dict, action: dict, sess: dict) -> dict:
    """Attach ``then`` (what ``on_fail`` means right now) to a fail action."""
    then = step["on_fail"]
    reason = action.get("reason") or "failed"
    if then == "agent":
        if not flight["session"] or not sess.get("present"):
            then = "halt"
        elif ss["rounds"] >= MAX_FIX_ROUNDS:
            then = "halt"
            reason = "%s (after %d fix rounds)" % (reason, ss["rounds"])
    action = dict(action, then=then, reason=reason)
    return action


def _guard_trip(flight: dict, ss: dict, obs: dict, now: float, sess: dict):
    """The first guard that trips among the done steps before the cursor
    (``Kind.guard``: a budget stop watching the whole flight), as an action
    for the CURRENT step (``ss``) with ``guard`` = the guard step's id and a
    fail resolved by the GUARD step's ``on_fail``; None when none trips."""
    for gstep in flight["steps"][: flight["cursor"]]:
        plug = _kinds.get(gstep["kind"])
        if plug is None or plug.guard is None:
            continue
        if flight["step_states"][gstep["id"]]["state"] != "done":
            continue
        out = plug.guard(flight, gstep, obs, now)
        if out is None:
            continue
        if out.get("action") == "fail":
            out = _resolve_fail(flight, gstep, ss, out, sess)
        return dict(out, guard=gstep["id"])
    return None


def fail_action(flight: dict, reason: str, obs: Optional[dict] = None) -> dict:
    """A ``fail`` action for the current step with its ``on_fail`` resolved —
    what the driver turns a side effect that raised into."""
    step = _current(flight)
    if step is None:
        return {"action": "finish", "note": ""}
    ss = flight["step_states"][step["id"]]
    return _resolve_fail(
        flight,
        step,
        ss,
        {"action": "fail", "reason": str(reason or "failed")},
        (obs or {}).get("session") or {},
    )


def _when_verdict(step: dict, obs: dict) -> Optional[dict]:
    """A pending step's ``when`` (docs/flights.md "Round 15"): None to carry
    on; ``skip_when`` when the condition is false against ``obs["facts"]``;
    a visible wait while the facts are missing (the driver gathers them for
    every pending step with a ``when``, so that is a safety net)."""
    when = str(step.get("when") or "")
    if not when:
        return None
    try:
        cond = _when.parse(when)
    except _when.WhenError:
        return None  # a stored condition nobody can read never blocks a flight
    facts = obs.get("facts")
    if not isinstance(facts, dict):
        return _wait("checking %s" % when)
    ok, why = _when.evaluate(cond, facts)
    if ok:
        return None
    return {"action": "skip_when", "note": "skipped: %s — %s" % (when, why)}


def next_action(flight: dict, obs: dict, now: float) -> dict:
    """What the driver should do for ``flight`` now — one of ``wait | run |
    done | fail | you | advance | finish | hold`` plus a ``note`` (the visible wait
    sentence) and per-action keys (``detail`` updates, ``phase`` for a
    sub-step effect, ``then`` + ``reason`` on a fail).

    ``obs`` is a plain dict snapshot: ``session`` (row facts: present, pending,
    status, sleeping, activity, limited, queue_pending, background_waiting,
    worked_at, turn_ended_at, branch, pr_url), ``autopilot`` (the session's
    record or None), ``verify`` (checklist facts), ``reviews`` (``ids`` of the
    actionable comments, None = unknown), ``checks`` (``verdict``,
    ``failing``), ``command`` (the running job or None), ``guard`` (guard
    step id -> what its kind observed; see :func:`_guard_trip`). Pure: no I/O.
    """
    if flight["state"] not in ACTIVE:
        return _wait("")
    step = _current(flight)
    if step is None:
        return {"action": "finish", "note": "every step is done"}
    ss = flight["step_states"][step["id"]]
    sess = obs.get("session") or {}
    st = ss["state"]
    performing = ss["detail"].get("performing")
    if (
        isinstance(performing, dict)
        and now - float(performing.get("at") or 0.0) < PERFORM_STALE_S
    ):
        # A side effect for this step is being carried out (by this pass or
        # another server's): never start it a second time.
        return _wait("carrying out %s" % step["title"])
    if st in ("done", "skipped"):
        return {"action": "advance", "note": "", "keep": True}
    if st == "failed":
        return _wait(flight["reason"] or "the step failed")
    hold = _session_hold(flight, step, sess)
    if st == "pending":
        if _armed_halt(step, ss):
            return {"action": "hold", "note": HALT_POINT}
        skip = _when_verdict(step, obs)
        if skip is not None:
            return skip
        if step["kind"] == "ticket" and (
            flight["subject"].get("kind") not in ("ticket", "issue")
            or not flight["subject"].get("id")
        ):
            return {"action": "advance", "note": "no ticket on this flight"}
        if step["kind"] == "you":
            text = step["config"].get("text") or step["title"]
            return {
                "action": "you",
                "note": text,
                "detail": {"items": list(step["config"].get("items") or [])},
            }
        if hold is not None:
            return hold
        if (
            step["kind"] == "command"
            and not str(step["config"].get("command") or "").strip()
        ):
            # A synced plan's lenient normalisation lets an empty one through.
            return _resolve_fail(
                flight, step, ss, {"action": "fail", "reason": "no command"}, sess
            )
        tripped = _guard_trip(flight, ss, obs, now, sess)
        if tripped is not None:
            return tripped
        return {"action": "run", "note": "starting %s" % step["title"]}
    if hold is not None:
        return hold
    if st == "waiting_you":
        if step["kind"] == "verify" and not ss["detail"].get("failed_reason"):
            out = _judge_verify(flight, step, ss, obs, now)
            if out["action"] in ("done", "fail"):
                return (
                    _resolve_fail(flight, step, ss, out, sess)
                    if out["action"] == "fail"
                    else out
                )
        return _wait("waiting on you")
    # running
    tripped = _guard_trip(flight, ss, obs, now, sess)
    if tripped is not None:
        return tripped
    if ss["detail"].get("phase") == "fix":
        held = _settle(ss["detail"], sess, ss["since"], now)
        if held is not None:
            if held["action"] == "fail":
                return _resolve_fail(flight, step, ss, held, sess)
            held["note"] = "waiting for the agent's fix: " + held["note"]
            return held
        return {
            "action": "run",
            "retry": True,
            "note": "retrying %s after the agent's fix" % step["title"],
        }
    out = judge_for(step["kind"])(flight, step, ss, obs, now)
    if out["action"] == "fail":
        out = _resolve_fail(flight, step, ss, out, sess)
    if out["action"] == "you" and st == "waiting_you":
        return _wait(out["note"])
    return out


# --------------------------------------------------------------------------- #
# Applying a verdict
# --------------------------------------------------------------------------- #
def _hist(flight: dict, now: float, sid: str, event: str, note: str = "") -> None:
    flight["history"].append(
        {"at": now, "step": sid, "event": event, "note": str(note or "")[:1000]}
    )
    if len(flight["history"]) > MAX_HISTORY:
        del flight["history"][: len(flight["history"]) - MAX_HISTORY]


def _finish(flight: dict, state: str, reason: str, now: float) -> None:
    flight["state"] = state
    flight["reason"] = reason
    flight["finished"] = now
    flight["updated"] = now


def _advance(flight: dict, now: float) -> None:
    """Move the cursor past every step already done/skipped; finish at the end."""
    steps = flight["steps"]
    while flight["cursor"] < len(steps) and flight["step_states"][
        steps[flight["cursor"]]["id"]
    ]["state"] in ("done", "skipped"):
        flight["cursor"] += 1
    if flight["cursor"] >= len(steps):
        _finish(flight, "done", "", now)
    else:
        flight["state"] = "running"
        _hit_halt_point(flight, now)


#: ``reason`` of a flight paused at a halt point.
HALT_POINT = "halt point"


def _armed_halt(step: dict, ss: dict) -> bool:
    """A halt point the flight has not stopped at yet: the step is marked
    ``halt``, has not started, and was never let through (``halt_hit``)."""
    return (
        bool(step.get("halt"))
        and ss["state"] == "pending"
        and not ss["detail"].get("halt_hit")
        and not ss["detail"].get("performing")
    )


def _hit_halt_point(flight: dict, now: float) -> bool:
    """Pause the flight before its current step when that step is an armed
    halt point (``paused``, reason ``halt point``, history ``halted_at``).
    The step keeps ``detail.halt_hit``, so Resume carries on into it."""
    step = _current(flight)
    if step is None or flight["state"] in FINAL:
        return False
    ss = flight["step_states"][step["id"]]
    if not _armed_halt(step, ss):
        return False
    ss["detail"]["halt_hit"] = True
    flight["state"] = "paused"
    flight["reason"] = HALT_POINT
    flight["updated"] = now
    _hist(flight, now, step["id"], "halted_at", "halted before %s" % step["title"])
    return True


def _let_through(flight: dict) -> None:
    """A "go" on the current step (resume, retry, a jump onto it) carries on
    into it: its halt point, if any, counts as passed."""
    step = _current(flight)
    if step is not None and step.get("halt"):
        flight["step_states"][step["id"]]["detail"]["halt_hit"] = True


def apply(flight: dict, action: dict, now: float) -> dict:
    """Mutate ``flight`` by ``action`` (from :func:`next_action`, possibly
    amended by the driver with an effect's outcome) and return it."""
    kind = action.get("action")
    flight["updated"] = now
    for key in ("session", "repo_root", "branch"):
        if action.get(key):
            flight[key] = str(action[key])
    step = _current(flight)
    if kind == "finish" and step is not None:
        # A step was added after the pass read the flight: never land over it.
        _advance(flight, now)
        return flight
    if kind == "finish" or step is None:
        if flight["state"] not in FINAL:
            _finish(flight, "done", "", now)
        return flight
    sid = step["id"]
    ss = flight["step_states"][sid]
    note = str(action.get("note") or "")
    upd = action.get("detail") if isinstance(action.get("detail"), dict) else {}
    gss = flight["step_states"].get(str(action.get("guard") or ""))
    if gss is not None and isinstance(action.get("guard_detail"), dict):
        gss["detail"].update(action["guard_detail"])  # a guard's high-water mark
    if kind == "hold":
        _hit_halt_point(flight, now)
    elif kind == "wait":
        ss["detail"].update(upd)
        for k in [k for k, v in ss["detail"].items() if v is None]:
            ss["detail"].pop(k, None)
        ss["note"] = note
    elif kind == "run":
        if action.get("phase"):
            ss["detail"].update(upd)
            ss["note"] = note
        else:
            hit = {"halt_hit": True} if ss["detail"].get("halt_hit") else {}
            ss["state"] = "running"
            ss["since"] = now
            ss["detail"] = dict(hit, **upd)
            ss["note"] = note
            _hist(
                flight,
                now,
                sid,
                "started",
                "retry after the agent's fix" if action.get("retry") else "",
            )
        flight["state"] = "running"
    elif kind == "done":
        ss["state"] = "done"
        ss["note"] = note
        ss["detail"].update(upd)
        ss["detail"].pop("idle_since", None)
        _hist(flight, now, sid, "done", note)
        _advance(flight, now)
    elif kind == "advance":
        if not action.get("keep"):
            ss["state"] = "skipped"
            ss["note"] = note
            _hist(flight, now, sid, "skipped", note)
        _advance(flight, now)
    elif kind == "skip_when":
        # The step's ``when`` is false: skipped by the planner, never run.
        ss["state"] = "skipped"
        ss["note"] = note
        ss["detail"].update(upd)
        _hist(flight, now, sid, "skipped", note)
        _advance(flight, now)
    elif kind == "you":
        ss["state"] = "waiting_you"
        ss["since"] = (
            now if ss["since"] == 0.0 or step["kind"] == "you" else ss["since"]
        )
        ss["note"] = note
        ss["detail"].update(upd)
        flight["state"] = "waiting_you"
        _hist(flight, now, sid, "you", note)
    elif kind == "fail":
        reason = str(action.get("reason") or "failed")
        then = action.get("then") or "halt"
        ss["detail"].update(upd)
        if then == "skip":
            ss["state"] = "failed"
            ss["note"] = reason
            _hist(flight, now, sid, "failed", reason + " — moving on")
            flight["cursor"] += 1
            _advance(flight, now)
        elif then == "agent":
            ss["rounds"] += 1
            ss["state"] = "running"
            ss["since"] = now
            ss["note"] = "the agent is fixing: " + reason
            ss["detail"] = {"phase": "fix", "reason": reason}
            _hist(flight, now, sid, "failed", reason + " — handed to the agent")
            flight["state"] = "running"
        elif then == "you":
            ss["state"] = "waiting_you"
            ss["since"] = now
            ss["note"] = reason
            ss["detail"]["failed_reason"] = reason
            flight["state"] = "waiting_you"
            _hist(flight, now, sid, "failed", reason)
            _hist(flight, now, sid, "you", reason)
        else:
            ss["state"] = "failed"
            ss["note"] = reason
            flight["state"] = "halted"
            flight["reason"] = reason
            _hist(flight, now, sid, "failed", reason)
            _hist(flight, now, sid, "halted", reason)
    return flight


# --------------------------------------------------------------------------- #
# Operations behind the routes (pure on a flight dict; ``mutate`` stores them)
# --------------------------------------------------------------------------- #
def _step_index(flight: dict, sid: str) -> int:
    for i, s in enumerate(flight["steps"]):
        if s["id"] == sid:
            return i
    raise FlightError("unknown_step", 404, "no step %s on this flight" % sid)


def _not_final(flight: dict) -> None:
    if flight["state"] in FINAL:
        raise FlightError(
            "finished",
            409,
            "this flight has already %s"
            % ("landed" if flight["state"] == "done" else "been cancelled"),
        )


def _airborne(flight: dict) -> None:
    """Done / skip / retry / jump / pause / resume need a flight with a
    session: 409 ``grounded`` on one still on the board (attach it first)."""
    _not_final(flight)
    if flight["state"] == GROUNDED:
        raise FlightError(
            "grounded", 409, "this flight has no session yet — fly it on one first"
        )


def op_done(
    flight: dict, sid: str, note: str = "", now: Optional[float] = None
) -> dict:
    """Done on a parked step (a *you* step, a verify step waiting on you, or
    a step whose failure was handed to you)."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    i = _step_index(flight, sid)
    ss = flight["step_states"][sid]
    if i != flight["cursor"] or ss["state"] != "waiting_you":
        raise FlightError(
            "not_waiting", 409, "%s is not waiting on you" % flight["steps"][i]["title"]
        )
    ss["state"] = "done"
    ss["note"] = str(note or "")[:1000]
    ss["detail"].pop("failed_reason", None)
    _hist(flight, ts, sid, "done", note or "you pressed Done")
    flight["reason"] = ""
    flight["updated"] = ts
    _advance_keeping_pause(flight, ts)
    return flight


def _advance_keeping_pause(flight: dict, now: float) -> None:
    """:func:`_advance`, but a paused flight stays paused: ticking a step off
    is not the same as pressing Resume."""
    paused = flight["state"] == "paused"
    _advance(flight, now)
    if paused and flight["state"] not in FINAL:
        flight["state"] = "paused"


def op_skip(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """Skip a step: the current one moves the flight on; a later one is marked
    so the cursor passes over it when it gets there."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    i = _step_index(flight, sid)
    if i < flight["cursor"]:
        raise FlightError("behind", 409, "that step is already behind the flight")
    ss = flight["step_states"][sid]
    ss["state"] = "skipped"
    ss["note"] = "skipped by you"
    _hist(flight, ts, sid, "skipped", "skipped by you")
    flight["updated"] = ts
    if i == flight["cursor"]:
        flight["reason"] = ""
        _advance_keeping_pause(flight, ts)
    return flight


def op_retry(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """Run a failed (or parked-on-failure) current step again."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    i = _step_index(flight, sid)
    ss = flight["step_states"][sid]
    retryable = (
        ss["state"] in ("failed", "running")
        or ss["detail"].get("failed_reason")
        or flight["state"] == "halted"
    )
    if i != flight["cursor"] or not retryable:
        raise FlightError(
            "not_failed", 409, "%s has nothing to retry" % flight["steps"][i]["title"]
        )
    flight["step_states"][sid] = dict(_blank_step_state(), since=ts)
    _let_through(flight)
    flight["state"] = "running"
    flight["reason"] = ""
    flight["updated"] = ts
    _hist(flight, ts, sid, "resumed", "retry")
    return flight


def op_jump(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """Move the cursor to ``sid``: forward marks the steps in between skipped,
    backward resets the target and everything after it to pending."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    i = _step_index(flight, sid)
    steps = flight["steps"]
    if i >= flight["cursor"]:
        for s in steps[flight["cursor"] : i]:
            if flight["step_states"][s["id"]]["state"] not in (
                "done",
                "skipped",
                "failed",
            ):
                flight["step_states"][s["id"]]["state"] = "skipped"
    for s in steps[i:]:
        flight["step_states"][s["id"]] = _blank_step_state()
    _hist(flight, ts, sid, "jumped", "moved to %s" % steps[i]["title"])
    flight["cursor"] = i
    _let_through(flight)
    flight["state"] = "running"
    flight["reason"] = ""
    flight["updated"] = ts
    return flight


def op_pause(flight: dict, now: Optional[float] = None) -> dict:
    """Hold a running / waiting flight where it is (``paused``). A halted one
    is already stopped and stays halted; pausing twice is a no-op."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    if flight["state"] in ACTIVE:
        cur = _current(flight)
        flight["state"] = "paused"
        flight["reason"] = ""
        _hist(flight, ts, cur["id"] if cur else "", "paused", "paused by you")
    flight["updated"] = ts
    return flight


def op_resume(flight: dict, now: Optional[float] = None) -> dict:
    """Carry on: a paused flight where it was (back to waiting on you if the
    step is parked), a failure-halted one by running its step again. A halt
    point the flight stopped at (or was about to) is let through."""
    ts = float(now if now is not None else time.time())
    _airborne(flight)
    cur = _current(flight)
    if cur is None:
        _finish(flight, "done", "", ts)
        return flight
    ss = flight["step_states"][cur["id"]]
    if ss["state"] == "failed":
        flight["step_states"][cur["id"]] = dict(_blank_step_state(), since=ts)
        ss = flight["step_states"][cur["id"]]
    _let_through(flight)
    flight["state"] = "waiting_you" if ss["state"] == "waiting_you" else "running"
    flight["reason"] = ""
    flight["updated"] = ts
    ss["detail"].pop("idle_since", None)
    _hist(flight, ts, cur["id"], "resumed", "")
    return flight


def validate_for_takeoff(flight: dict) -> None:
    """Every step as strict as a plan's (``steps[i]…`` in the error), before a
    grounded flight binds to a session: a Split with no prompts, a Wait with
    no minutes… is refused here, with the field, not when it was placed."""
    normalize_steps(copy.deepcopy(flight["steps"]), strict=True)


def op_rename(flight: dict, name: str, now: Optional[float] = None) -> dict:
    """``PATCH /api/flights/{id} {plan_name}``: the flight's own name — its
    line's title (a grounded one's especially). Any unfinished state; empty
    is 400 ``bad_name``."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    text = _text(name, 120).strip()
    if not text:
        raise FlightError("bad_name", 400, "a name needs some letters")
    flight["plan_name"] = text
    flight["updated"] = ts
    cur = _current(flight)
    _hist(flight, ts, cur["id"] if cur else "", "edited", "renamed to %s" % text)
    return flight


def op_cancel(
    flight: dict, reason: str = "cancelled by you", now: Optional[float] = None
) -> dict:
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    cur = _current(flight)
    _hist(flight, ts, cur["id"] if cur else "", "halted", reason)
    _finish(flight, "cancelled", reason, ts)
    return flight


def _editable(flight: dict) -> None:
    """Steps change on any flight not yet over: in the air (running, waiting,
    paused), halted on a failure (the owner tacks on or takes out stops to
    get it going again) or still on the ground. A landed or cancelled one is
    history."""
    if flight["state"] not in UNFINISHED:
        raise FlightError(
            "finished",
            409,
            "this flight has %s — its steps can't change"
            % {"done": "landed", "cancelled": "been cancelled"}.get(
                flight["state"], "ended"
            ),
        )


def _first_open(flight: dict) -> int:
    """The first index a step may be edited, moved or placed at: every one on
    a grounded flight (it has no current step), else the one after the
    cursor."""
    return 0 if flight["state"] == GROUNDED else flight["cursor"] + 1


def _pending_index(flight: dict, sid: str) -> int:
    """The index of ``sid`` if it can still change — ahead of the flight and
    untouched, or any step of a grounded one — else 409 ``in_progress``."""
    i = _step_index(flight, sid)
    ss = flight["step_states"][sid]
    if i < _first_open(flight) or ss["state"] != "pending" or ss["detail"]:
        raise FlightError(
            "in_progress",
            409,
            "%s has already started — only a step still ahead can change"
            % flight["steps"][i]["title"],
        )
    return i


def _lenient(flight: dict) -> bool:
    """A block on the ground may be incomplete (a Split with no prompts yet):
    the owner fills it in before it flies, and :func:`validate_for_takeoff`
    checks it then. In the air every step is validated as a plan's."""
    return flight["state"] == GROUNDED


def _one_step(raw, index: int, taken, strict: bool = True) -> dict:
    """A single body step validated like a plan's (``field`` names ``step…``)."""
    try:
        return normalize_step(
            raw, index, strict=True, config_strict=strict, taken=taken
        )
    except PlanError as err:
        raise PlanError(err.message, re.sub(r"^steps\[\d+\]", "step", err.field))


def _insert_index(flight: dict, at, n: int = 1) -> int:
    """Where ``n`` new steps go: the end, or index ``at``, which must be after
    the current step (409 ``in_progress``) — or AT it on a paused flight whose
    current step has not started (it sits right before that step, so the new
    one flies first: what Undo of a removed flying stop needs). 400
    ``bad_at``; 409 ``too_many_steps`` past :data:`MAX_STEPS`."""
    steps = flight["steps"]
    if len(steps) + n > MAX_STEPS:
        raise FlightError(
            "too_many_steps", 409, "a flight has at most %d steps" % MAX_STEPS
        )
    if at is None:
        return len(steps)
    try:
        if isinstance(at, bool):
            raise TypeError(at)
        i = int(at)
    except (TypeError, ValueError):
        raise FlightError("bad_at", 400, "at is a step index")
    if i < 0 or i > len(steps):
        raise FlightError("bad_at", 400, "at is 0..%d" % len(steps))
    first = _first_open(flight)
    cur = _current(flight)
    if (
        flight["state"] == "paused"
        and i == flight["cursor"]
        and (
            cur is None
            or (
                flight["step_states"][cur["id"]]["state"] == "pending"
                and not flight["step_states"][cur["id"]]["detail"].get("performing")
            )
        )
    ):
        first = flight["cursor"]
    if i < first:
        raise FlightError(
            "in_progress", 409, "a step can only be added after the current one"
        )
    return i


def _insert_steps(flight: dict, new: List[dict], i: int, now: float) -> dict:
    """Place validated ``new`` steps at index ``i``: each takes the next free
    ``sN`` (never one history still names), starts pending, and gets an
    ``added`` history event."""
    steps = flight["steps"]
    _renumber_after(steps, new, [h["step"] for h in flight["history"]])
    steps[i:i] = new
    nxt = i + len(new)
    where = "at the end" if nxt >= len(steps) else "before %s" % steps[nxt]["title"]
    for step in new:
        flight["step_states"][step["id"]] = _blank_step_state()
        _hist(flight, now, step["id"], "added", "%s, %s" % (step["title"], where))
    flight["updated"] = now
    return flight


def op_add_step(
    flight: dict, raw, at: Optional[int] = None, now: Optional[float] = None
) -> dict:
    """Add a step to a flight in the air (or on the ground): appended, or
    inserted at index ``at`` (see :func:`_insert_index`). It takes the next
    free ``sN``. A flight whose last step is finishing gains a step to go on
    to (the planner only lands with nothing left ahead)."""
    ts = float(now if now is not None else time.time())
    _editable(flight)
    i = _insert_index(flight, at)
    step = _one_step(
        (
            dict(
                raw,
                id="",
                strict=not _lenient(flight),
            )
            if isinstance(raw, dict)
            else raw
        ),
        i,
        {s["id"] for s in flight["steps"]},
    )
    return _insert_steps(flight, [step], i, ts)


def _group_ids(flight: dict) -> set:
    return {s["group"]["id"] for s in flight["steps"] if s.get("group")}


def _fresh_group(flight: dict, name: str, plan_id: str = "") -> dict:
    """A new ``group`` (``g_<6hex>``) no step of ``flight`` uses yet."""
    name = _text(name, 80)
    if not name:
        raise FlightError("bad_group", 400, "a stop needs a name")
    return {
        "id": _new_id("g_", _group_ids(flight)),
        "name": name,
        "plan_id": _text(plan_id, 64),
    }


def op_add_steps(
    flight: dict,
    raws,
    at: Optional[int] = None,
    group=None,
    now: Optional[float] = None,
) -> dict:
    """Several steps at once (``POST /steps {steps, at?, group?}``), validated
    like a plan's (``steps[i]…``) and placed as :func:`op_add_step` places
    one. ``group`` (``{name}``) makes them ONE new stop; without it each keeps
    the ``group`` it came with."""
    ts = float(now if now is not None else time.time())
    _editable(flight)
    if not isinstance(raws, list):
        raise PlanError("steps is a list", "steps")
    stripped = [dict(r, id="") if isinstance(r, dict) else r for r in raws]
    new = normalize_steps(stripped, strict=True, config_strict=not _lenient(flight))
    i = _insert_index(flight, at, len(new))
    if group is not None:
        g = _fresh_group(flight, group.get("name") if isinstance(group, dict) else "")
        for s in new:
            s["group"] = dict(g)
    return _insert_steps(flight, new, i, ts)


def op_add_plan(
    flight: dict, plan: dict, at: Optional[int] = None, now: Optional[float] = None
) -> dict:
    """A saved stop's steps (``POST /steps {plan_id, at?}``) as ONE new group:
    fresh step ids, ``group.name`` = the plan's name, ``group.plan_id`` = its
    id."""
    ts = float(now if now is not None else time.time())
    _editable(flight)
    new = copy.deepcopy(plan["steps"])
    i = _insert_index(flight, at, len(new))
    g = _fresh_group(flight, plan.get("name") or "Stop", plan.get("id") or "")
    for s in new:
        s["group"] = dict(g)
    return _insert_steps(flight, new, i, ts)


def op_edit_step(flight: dict, sid: str, patch, now: Optional[float] = None) -> dict:
    """Change a step still ahead of the flight: ``title`` / ``config`` (the
    whole config, validated like a plan step's) / ``on_fail`` / ``halt`` /
    ``group``; its kind and id stay. ``halt`` alone may also change on the
    current step (any flight not yet over, a halted one too): clearing it
    there un-marks a halt point without resuming. ``group`` is
    :func:`_edit_group`'s."""
    ts = float(now if now is not None else time.time())
    patch = dict(patch) if isinstance(patch, dict) else {}
    if "group" in patch:
        _edit_group(flight, sid, patch.pop("group"), ts)
        if not patch:
            return flight
    if set(patch) & {"title", "config", "on_fail", "when"} or "halt" not in patch:
        _editable(flight)
        i = _pending_index(flight, sid)
    else:
        _not_final(flight)
        i = _step_index(flight, sid)
        st = flight["step_states"][sid]["state"]
        if flight["state"] != GROUNDED and (
            i < flight["cursor"]
            or st in ("done", "skipped")
            or (i > flight["cursor"] and st != "pending")
        ):
            raise FlightError(
                "in_progress",
                409,
                "%s has already flown — only a stop not yet done can halt"
                % flight["steps"][i]["title"],
            )
    raw = copy.deepcopy(flight["steps"][i])
    for key in ("title", "config", "on_fail", "halt", "when"):
        if key in patch:
            raw[key] = patch[key]
    if "when" in patch and not str(patch["when"] or "").strip():
        raw.pop("when", None)  # "" clears the condition
    step = _one_step(
        raw, i, {s["id"] for s in flight["steps"]} - {sid}, strict=not _lenient(flight)
    )
    if step == flight["steps"][i]:
        return flight
    was = bool(flight["steps"][i].get("halt"))
    flight["steps"][i] = step
    note = step["title"]
    if bool(step.get("halt")) != was:
        note += ": halt point %s" % ("set" if step.get("halt") else "cleared")
    _hist(flight, ts, sid, "edited", note)
    flight["updated"] = ts
    return flight


def _edit_group(flight: dict, sid: str, value, now: float) -> None:
    """PATCH's ``group``. ``{name}`` renames the step's group — every step of
    it, flown ones too (a name is only a label), on any flight not yet over;
    on a step with no group, one that can change becomes a stop of its own
    under that name. ``null`` takes this one step (one that can change) out
    of its group."""
    if value is not None and not isinstance(value, dict):
        raise PlanError("group is {name} or null", "step.group")
    _not_final(flight)
    i = _step_index(flight, sid)
    step = flight["steps"][i]
    if value is None:
        _editable(flight)
        _pending_index(flight, sid)
        if step.get("group"):
            name = step.pop("group")["name"]
            _hist(flight, now, sid, "edited", "%s: out of %s" % (step["title"], name))
            flight["updated"] = now
        return
    name = _text(value.get("name"), 80)
    if not name:
        raise FlightError("bad_group", 400, "a stop needs a name")
    if not step.get("group"):
        _editable(flight)
        _pending_index(flight, sid)
        step["group"] = _fresh_group(flight, name)
        _hist(flight, now, sid, "edited", "%s: a stop of its own" % step["title"])
        flight["updated"] = now
        return
    gid = step["group"]["id"]
    if step["group"]["name"] == name:
        return
    for s in flight["steps"]:
        if s.get("group") and s["group"]["id"] == gid:
            s["group"]["name"] = name
    _hist(flight, now, sid, "edited", "the stop is now %s" % name)
    flight["updated"] = now


def op_group(flight: dict, sids, name, now: Optional[float] = None) -> dict:
    """Make the steps ``sids`` (side by side, each able to change) ONE new
    stop named ``name``; returns the new ``group``. 409 ``in_progress`` for a
    step that can't change, 400 ``bad_group`` when they are not consecutive
    (or no name / no steps)."""
    ts = float(now if now is not None else time.time())
    _editable(flight)
    if not isinstance(sids, list) or not sids:
        raise FlightError("bad_group", 400, "steps is a list of step ids")
    g = _fresh_group(flight, name)
    idx = sorted({_pending_index(flight, str(sid)) for sid in sids})
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise FlightError("bad_group", 400, "only stops side by side can make one stop")
    for i in idx:
        flight["steps"][i]["group"] = dict(g)
    _hist(
        flight,
        ts,
        flight["steps"][idx[0]]["id"],
        "edited",
        "%d stops grouped as %s" % (len(idx), g["name"]),
    )
    flight["updated"] = ts
    return g


def op_ungroup(flight: dict, gid: str, now: Optional[float] = None) -> dict:
    """Every step of group ``gid`` loses it (404 ``unknown_group``) — on any
    flight not yet over, flown sub-steps too (a group is only a label)."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    mine = [s for s in flight["steps"] if s.get("group") and s["group"]["id"] == gid]
    if not mine:
        raise FlightError("unknown_group", 404, "no stop %s on this flight" % gid)
    for s in mine:
        name = s.pop("group")["name"]
    _hist(flight, ts, mine[0]["id"], "edited", "%s taken apart" % name)
    flight["updated"] = ts
    return flight


#: ``reason`` of a flight paused because its current stop was removed.
STOP_REMOVED = "stop removed"


def _remove_at(flight: dict, i: int, now: float) -> dict:
    """Pop step ``i``: one ahead (or any on a grounded flight) just goes; a
    flown one behind the cursor shifts the cursor down with it; the CURRENT
    one leaves the flight ``paused`` (``stop removed``) with its cursor on the
    step that followed — it sits right before it. Returns the removed step."""
    step = flight["steps"].pop(i)
    flight["step_states"].pop(step["id"], None)
    note = step["title"]
    if flight["state"] != GROUNDED and i < flight["cursor"]:
        flight["cursor"] -= 1
    elif flight["state"] != GROUNDED and i == flight["cursor"]:
        flight["state"] = "paused"
        flight["reason"] = STOP_REMOVED
        note = "removed %s while it flew" % step["title"]
    _hist(flight, now, step["id"], "removed", note)
    flight["updated"] = now
    return step


LAST_STOP_REMOVED = "its last stop was removed"


def _last_ones(flight: dict, n: int, ts: float) -> bool:
    """Removing ``n`` steps would leave none: the flight has nothing left to
    fly, so it is cancelled instead (the owner's "get rid of it" on a one-stop
    line — round 12). True when that happened; the caller returns."""
    if n < len(flight["steps"]):
        return False
    op_cancel(flight, LAST_STOP_REMOVED, ts)
    return True


def op_remove_step(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """Take a step out of a flight not yet over — any step (docs/flights.md
    "Removing the stop that is flying", :func:`_remove_at`). Taking the last
    one out cancels the flight (:func:`_last_ones`). The step's side effects
    (an armed lane, a command) are the driver's to stop."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    i = _step_index(flight, sid)
    if _last_ones(flight, 1, ts):
        return flight
    _remove_at(flight, i, ts)
    return flight


def op_remove_group(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """``DELETE …/steps/{sid}?group=1``: every step of ``sid``'s group (just
    ``sid`` when it has none), by :func:`op_remove_step`'s rules, last first
    so the cursor lands right before what followed the group."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    i = _step_index(flight, sid)
    gid = (flight["steps"][i].get("group") or {}).get("id")
    idx = [
        k
        for k, s in enumerate(flight["steps"])
        if k == i or (gid and (s.get("group") or {}).get("id") == gid)
    ]
    if _last_ones(flight, len(idx), ts):
        return flight
    for k in reversed(idx):
        _remove_at(flight, k, ts)
    return flight


def op_move_step(flight: dict, sid: str, at, now: Optional[float] = None) -> dict:
    """Move a step still ahead of the flight to index ``at`` of the final
    list (pop then insert, like ``list.insert``). ``at`` past the end is the
    last place; at/before the current step is 409 ``in_progress`` (a grounded
    flight has none). Its ``step_states`` record moves with it; the same
    index changes nothing. Groups are left alone."""
    ts = float(now if now is not None else time.time())
    _editable(flight)
    if isinstance(at, bool) or not isinstance(at, int):
        raise PlanError("at is a step index", "at")
    i = _pending_index(flight, sid)
    steps = flight["steps"]
    if at < _first_open(flight):
        raise FlightError(
            "in_progress", 409, "a step can only move after the current one"
        )
    j = min(at, len(steps) - 1)
    if j == i:
        return flight
    step = steps.pop(i)
    steps.insert(j, step)
    _hist(flight, ts, sid, "moved", "%s, %d → %d" % (step["title"], i, j))
    flight["updated"] = ts
    return flight


# --------------------------------------------------------------------------- #
# Grounded flights: stops with no session yet
# --------------------------------------------------------------------------- #
def ground(
    plan: Optional[dict] = None,
    raw_steps=None,
    name: str = "",
    by: str = "user",
    now: Optional[float] = None,
) -> dict:
    """Store a ``grounded`` flight (``POST /start {grounded: true}``): no
    session, cursor 0, every step pending; the driver never touches it until
    :func:`attach`. ``plan``'s steps come as ONE group (as the add route
    makes them), then ``raw_steps`` validated like a plan's — grouped under
    ``name`` when it is set. Steps are ``s1..sN``. ``plan_id`` is the plan's
    (or ``adhoc``); ``plan_name`` is ``name``, else the plan's, else "Stops".
    Raises :class:`PlanError` (``steps…``) and :class:`FlightError`
    (``needs_steps`` 400)."""
    ts = float(now if now is not None else time.time())
    name = _text(name, 120)
    steps: List[dict] = []
    taken: set = set()
    if plan is not None:
        g = {
            "id": _new_id("g_", taken),
            "name": _text(plan.get("name") or "Stop", 80),
            "plan_id": _text(plan.get("id"), 64),
        }
        taken.add(g["id"])
        for s in copy.deepcopy(plan["steps"]):
            steps.append(dict(s, group=dict(g)))
    if raw_steps is not None:
        if not isinstance(raw_steps, list):
            raise PlanError("steps is a list", "steps")
        stripped = [dict(s, id="") if isinstance(s, dict) else s for s in raw_steps]
        # On the ground a block may be incomplete (its config); take-off
        # validates. Its kind and shape are checked as always.
        given = normalize_steps(stripped, strict=True, config_strict=False)
        if name:
            g = {"id": _new_id("g_", taken), "name": name[:80], "plan_id": ""}
            for s in given:
                s["group"] = dict(g)
        steps += given
    if not steps:
        raise FlightError(
            "needs_steps", 400, "a grounded flight needs plan_id or steps"
        )
    if len(steps) > MAX_STEPS:
        raise PlanError("a flight has at most %d steps" % MAX_STEPS, "steps")
    _renumber_after([], steps)
    flight = _normalize_flight(
        "pending",
        {
            "id": "pending",
            "plan_id": (plan or {}).get("id") or ADHOC_PLAN_ID,
            "plan_name": name or (plan or {}).get("name") or "Stops",
            "steps": steps,
            "subject": {"kind": "manual"},
            "session": "",
            "state": GROUNDED,
            "cursor": 0,
            "started": ts,
            "updated": ts,
            "by": by,
        },
    )
    return _put_new(flight)


def op_attach(
    flight: dict,
    title: str,
    repo_root: str = "",
    branch: str = "",
    seeded: bool = False,
    now: Optional[float] = None,
) -> dict:
    """Bind a grounded ``flight`` to session ``title`` and take off from its
    cursor: ``running`` (history ``attached``), or ``paused`` at a halt point
    on that step. ``seeded``: the session was just created with the first
    (agent) step as its initial prompt, so that step is already running —
    :func:`take_off`'s rule. 409 ``not_grounded`` on any other flight."""
    ts = float(now if now is not None else time.time())
    if flight["state"] != GROUNDED:
        raise FlightError("not_grounded", 409, "this flight already has a session")
    flight["session"] = str(title)
    flight["repo_root"] = str(repo_root or "")
    flight["branch"] = str(branch or "")
    flight["state"] = "running"
    flight["reason"] = ""
    flight["started"] = ts
    flight["updated"] = ts
    flight["gone_since"] = 0.0
    cur = _current(flight)
    _hist(flight, ts, cur["id"] if cur else "", "attached", "attached to %s" % title)
    if cur is None:
        _finish(flight, "done", "", ts)
    elif seeded and cur["kind"] == "agent":
        ss = flight["step_states"][cur["id"]]
        ss["state"] = "running"
        ss["since"] = ts
        ss["detail"] = {"seeded": True}
        _hist(flight, ts, cur["id"], "started", "the session's first prompt")
    else:
        _hit_halt_point(flight, ts)
    return flight


def attach(
    flight_id: str,
    title: str,
    repo_root: str = "",
    branch: str = "",
    seeded: bool = False,
    now: Optional[float] = None,
) -> dict:
    """:func:`op_attach` under the store lock, refusing (404 ``unknown_flight``,
    409 ``not_grounded``, 409 ``in_flight`` when ``title`` already has an
    unfinished flight) against the store as it is."""
    with _LOCK:
        flights = _load_flights_locked()
        f = flights.get(str(flight_id or ""))
        if f is None:
            raise FlightError("unknown_flight", 404, "no such flight")
        if f["state"] != GROUNDED:
            raise FlightError("not_grounded", 409, "this flight already has a session")
        for g in flights.values():
            if title and g["session"] == title and g["state"] in UNFINISHED:
                raise FlightError(
                    "in_flight",
                    409,
                    "%s is already on a flight (%s)" % (title, g["plan_name"]),
                )
        op_attach(f, title, repo_root, branch, seeded, now)
        f = _normalize_flight(f["id"], f) or f
        flights[f["id"]] = f
        _save_flights_locked(flights)
    return copy.deepcopy(f)


def next_until(spec: str, now: float) -> float:
    """Epoch of the next local ``HH:MM`` after ``now`` (today or tomorrow)."""
    m = _HHMM_RE.match(str(spec or ""))
    if m is None:
        return now
    here = _dt.datetime.fromtimestamp(now)
    at = here.replace(
        hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
    )
    if at.timestamp() <= now:
        at = at + _dt.timedelta(days=1)
    return at.timestamp()


# --------------------------------------------------------------------------- #
# Plug-in kinds (docs/flights.md "Round 15"): every module of
# ``core/flight_kinds/`` registers one kind; installed here, after everything
# a plug-in may use at import time is defined.
# --------------------------------------------------------------------------- #
def install_kinds() -> None:
    """Merge the registered plug-in kinds into :data:`STEP_KINDS`,
    :data:`SESSION_KINDS` and :data:`AGENT_KINDS` (idempotent; a test that
    registers a fake kind calls it again)."""
    global SESSION_KINDS, AGENT_KINDS
    _kinds.load()
    for plug in _kinds.all_kinds():
        STEP_KINDS[plug.kind] = plug.spec
        if plug.needs_session:
            SESSION_KINDS = SESSION_KINDS | {plug.kind}
        else:
            SESSION_KINDS = SESSION_KINDS - {plug.kind}
        if plug.types_to_agent:
            AGENT_KINDS = AGENT_KINDS | {plug.kind}
        else:
            AGENT_KINDS = AGENT_KINDS - {plug.kind}


install_kinds()
