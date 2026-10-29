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

__all__ = [
    "STEP_KINDS",
    "ON_FAIL",
    "TRIGGER_KINDS",
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
#: Right after a push a PR can report no checks for a moment; "none" only
#: counts as "nothing to wait for" once this has passed.
CHECKS_NONE_GRACE_S = 120.0

#: Flight states. ``paused`` is held by a person (resume carries on where it
#: was); ``halted`` is stopped on a failure (retry / skip / resume);
#: ``done``/``cancelled`` are final.
STATES = ("running", "waiting_you", "paused", "halted", "done", "cancelled")
#: States the driver moves.
ACTIVE = frozenset({"running", "waiting_you"})
#: States that hold their session (a new start is 409 ``in_flight``).
HOLDING = frozenset({"running", "waiting_you", "paused"})
UNFINISHED = frozenset({"running", "waiting_you", "paused", "halted"})
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
)
ON_FAIL = ("halt", "agent", "skip", "you")
TRIGGER_KINDS = ("manual", "ticket", "pr", "issue", "schedule")
SUBJECT_KINDS = ("ticket", "pr", "issue", "schedule", "manual")
SHIP_LANES = ("commit", "push", "pr", "merge")
VERIFY_WHO = ("agent", "you", "both")

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
            _f("fresh", "bool", "Start a new session for this step", default=False),
        ],
    },
    "ship": {
        "label": "Ship",
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
        "summary": "Write the branch's checklist and work through it; your items park the flight.",
        "default_title": "Verify",
        "fields": [
            _f("who", "select", "Who checks", default="both", options=VERIFY_WHO),
            _f("wait_live", "bool", "Wait until it is live", default=False),
        ],
    },
    "you": {
        "label": "You",
        "actor": "you",
        "summary": "Park the flight and ask you to do something; it goes on when you press Done.",
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
        "summary": "Wait for the PR's checks to pass; a failing check fails the step.",
        "default_title": "Checks",
        "fields": [_f("timeout_minutes", "int", "Give up after (minutes)", default=60)],
    },
    "command": {
        "label": "Command",
        "actor": "robot",
        "summary": "Run a shell command in the session's folder; a non-zero exit fails the step.",
        "default_title": "Run a command",
        "fields": [
            _f("command", "textarea", "Command", default="", placeholder="make test"),
            _f("timeout_minutes", "int", "Give up after (minutes)", default=30),
        ],
    },
    "ticket": {
        "label": "Ticket",
        "actor": "robot",
        "summary": "Move the flight's ticket and/or comment on it (skipped without a ticket).",
        "default_title": "Ticket: update",
        "fields": [
            _f("state", "text", "Move to state", default="", placeholder="Done"),
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
SESSION_KINDS = frozenset({"agent", "ship", "verify", "reviews", "checks", "command"})
#: Kinds that type into the session's agent (the driver wakes it first).
AGENT_KINDS = frozenset({"agent", "reviews"})


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
        return {"prompt": _text(c.get("prompt")), "fresh": _bool(c.get("fresh"), False)}
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
    return {}


def normalize_step(
    raw, index: int = 0, strict: bool = True, taken=None
) -> Optional[dict]:
    """One step coerced to ``{id, kind, title, config, on_fail}``.

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
    config = _step_config(kind, raw.get("config"), strict, where + ".config")
    on_fail = _text(raw.get("on_fail"), 8).lower() or "halt"
    if on_fail not in ON_FAIL:
        if strict:
            raise PlanError("on_fail is halt, agent, skip or you", where + ".on_fail")
        on_fail = "halt"
    if kind == "you":
        on_fail = "halt"  # a you step cannot fail
    taken = taken if taken is not None else set()
    sid = _text(raw.get("id"), 32)
    if not _ID_RE.match(sid) or sid in taken:
        n = index + 1
        while "s%d" % n in taken:
            n += 1
        sid = "s%d" % n
    taken.add(sid)
    title = _text(raw.get("title"), 120) or _default_title(kind, config)
    return {
        "id": sid,
        "kind": kind,
        "title": title,
        "config": config,
        "on_fail": on_fail,
    }


def _default_title(kind: str, config: dict) -> str:
    if kind == "ship":
        return {"commit": "Commit", "push": "Push", "pr": "Open PR", "merge": "Merge"}[
            config.get("lane") or "push"
        ]
    if kind == "wait":
        if config.get("until"):
            return "Wait until %s" % config["until"]
        return "Wait %d min" % int(config.get("minutes") or 0)
    return STEP_KINDS[kind]["default_title"]


def normalize_steps(raw, strict: bool = True) -> List[dict]:
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
        step = normalize_step(s, i, strict=strict, taken=taken)
        if step is not None:
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
            raise PlanError("a plan needs a name", "name")
        name = "Untitled plan"
    trig = raw.get("trigger") if isinstance(raw.get("trigger"), dict) else {}
    tkind = _text(trig.get("kind"), 16).lower() or "manual"
    if tkind not in TRIGGER_KINDS:
        if strict:
            raise PlanError(
                "trigger is manual, ticket, pr, issue or schedule", "trigger.kind"
            )
        tkind = "manual"
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
                _s("s10", "ticket", "Ticket: Done", comment="Merged by MindFlock"),
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
    """A plan by id. ``builtin:<kind>@<lane>`` is a VIRTUAL plan: that
    built-in (as edited) cut after its matching ship step — what a per-source
    depth means (see :func:`plan_for_depth`)."""
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
    cut = _cut_index(base["steps"], m.group(2))
    plan = copy.deepcopy(base)
    if cut is not None:
        plan["steps"] = plan["steps"][: cut + 1]
        label = {"commit": "Commit", "push": "Push", "pr": "Open PR", "merge": "Merge"}
        plan["name"] = "%s (to %s)" % (base["name"], label[m.group(2)])
    plan["id"] = pid
    return plan


def _cut_index(steps: List[dict], lane: str) -> Optional[int]:
    for i, s in enumerate(steps):
        if s["kind"] == "ship" and s["config"].get("lane") == lane:
            return i
    return None


def plan_for_depth(kind: str, depth: str) -> str:
    """The plan a per-source fast-track ``depth`` means for an intake ``kind``
    (``ticket``/``pr``/``issue``/``schedule``): the built-in cut after the
    matching ship step, ``""`` for ``off``/``agent``/unknown. ``merge`` — and
    a depth the built-in has no ship step for, or whose step is its last — is
    the whole built-in (a merged ticket still gets its closing comment)."""
    base = _BUILTIN_FOR_KIND.get(str(kind or "").strip().lower())
    depth = str(depth or "").strip().lower()
    if base is None or depth not in SHIP_LANES:
        return ""
    if depth == "merge":
        return base
    plan = get_plan(base)
    if plan is None:
        return ""
    cut = _cut_index(plan["steps"], depth)
    if cut is None or cut == len(plan["steps"]) - 1:
        return base
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
def _load_flights_locked() -> Dict[str, dict]:
    data = read_json(flights_path(), {}, expect=dict)
    items = data.get("flights") if isinstance(data, dict) else None
    out: Dict[str, dict] = {}
    if isinstance(items, dict):
        for fid, raw in items.items():
            f = _normalize_flight(fid, raw)
            if f is not None:
                out[f["id"]] = f
    return out


def _save_flights_locked(flights: Dict[str, dict]) -> None:
    atomic_write_json(flights_path(), {"flights": flights}, prefix=".flf.")


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
    if by not in ("user", "intake", "schedule") and not by.startswith("agent:"):
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
        flights = _load_flights_locked()
    out = [f for f in flights.values() if include_finished or f["state"] not in FINAL]
    return sorted(out, key=lambda f: -(f["started"] or 0.0))


def get(flight_id: str) -> Optional[dict]:
    with _LOCK:
        return _load_flights_locked().get(str(flight_id or ""))


def for_session(title: str, unfinished_only: bool = True) -> Optional[dict]:
    """The flight bound to ``title`` (the newest unfinished one), or None."""
    if not title:
        return None
    best = None
    for f in list_flights():
        if f["session"] != title:
            continue
        if unfinished_only and f["state"] not in UNFINISHED:
            continue
        if best is None or f["started"] > best["started"]:
            best = f
    return best


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


def _put_new(flight: dict) -> dict:
    """Store a new flight (caller-built), enforcing one unfinished flight per
    session: a running/waiting one is a 409 ``in_flight``; a halted one is
    cancelled ("replaced") so a parked flight never blocks a fresh start."""
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
    """``{ticket} {pr} {branch} {subject}`` filled from the flight. An unknown
    brace is left as typed (a prompt may legitimately contain braces)."""
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
    }
    return re.sub(
        r"\{(ticket|pr|branch|subject)\}", lambda m: values[m.group(1)], str(text or "")
    )


def _first_agent_index(steps: List[dict]) -> Optional[int]:
    for i, s in enumerate(steps):
        if s["kind"] == "agent":
            return i
    return None


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
) -> dict:
    """Create a ``running`` flight of ``plan`` bound to ``session``.

    * ``attach`` (an EXISTING session): the cursor starts at ``start_at`` or at
      the first non-``agent`` step — the work the agent did is the agent step.
    * ``seeded`` (the session was created — or is being created by intake —
      with the plan's first agent step as its INITIAL prompt): that step is
      already running, so the driver never sends it a second time.
    * otherwise the cursor is at 0, pending.

    Steps before the cursor are recorded ``skipped`` (history ``jumped``).
    Raises :class:`FlightError` (``in_flight`` 409, ``unknown_step`` 400).
    """
    ts = float(now if now is not None else time.time())
    steps = copy.deepcopy(plan["steps"])
    cursor = 0
    if start_at:
        ids = [s["id"] for s in steps]
        if start_at not in ids:
            raise FlightError("unknown_step", 400, "no step %s in this plan" % start_at)
        cursor = ids.index(start_at)
    elif attach:
        cursor = next(
            (i for i, s in enumerate(steps) if s["kind"] != "agent"), len(steps)
        )
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
    return _put_new(flight)


def arm_pending(title: str, plan_id: str, subject, by: str = "intake") -> dict:
    """The intake hook: a ``running`` flight for ``title`` BEFORE its session
    exists (keyed like autopilot — an intake title is deterministic). The plan's
    first agent step is the session's initial prompt (the intake-built one), so
    it is recorded as already running. Raises :class:`FlightError`
    (``unknown_plan`` 404, ``in_flight`` 409)."""
    plan = get_plan(plan_id)
    if plan is None:
        raise FlightError("unknown_plan", 404, "no such plan: %s" % plan_id)
    who = (
        by
        if by in ("user", "intake", "schedule") or str(by).startswith("agent:")
        else "intake"
    )
    return take_off(
        plan,
        session=str(title or ""),
        subject=_normalize_subject(subject),
        by=who,
        seeded=True,
    )


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


def row_dto(title: str) -> Optional[dict]:
    """The ``flight`` field of a session's ``/api/instances`` row, or None."""
    f = for_session(title)
    if f is None:
        return None
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
    }


def you_text(flight: dict) -> Tuple[str, List[str]]:
    """What a parked step asks of you: ``(text, items)``."""
    cur = _current(flight)
    if cur is None:
        return "", []
    d = flight["step_states"][cur["id"]]["detail"]
    if d.get("failed_reason"):
        return "%s failed: %s" % (cur["title"], d["failed_reason"]), []
    if cur["kind"] == "you":
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


def _judge_agent(flight, step, ss, obs, now) -> dict:
    d = ss["detail"]
    need = not d.get("no_prompt")
    held = _settle(d, obs.get("session") or {}, ss["since"], now, need_work=need)
    if held is not None:
        return held
    return {"action": "done", "note": "the agent's turn settled"}


def _judge_ship(flight, step, ss, obs, now) -> dict:
    rec = obs.get("autopilot")
    lane = step["config"]["lane"]
    if not rec:
        return {
            "action": "fail",
            "reason": "Fast-track was turned off before %s finished" % step["title"],
        }
    state = rec.get("state")
    if state == "done" and _awaiting_go(rec):
        return _wait("waiting for your go — Fast-track asks before it ships")
    if state == "done":
        return {"action": "done", "note": rec.get("url") or ""}
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
            return {
                "action": "done",
                "note": "the agent's checklist items are answered",
            }
        return _wait("the agent's checklist items are still open", **up)
    if who == "you":
        open_human = open_human + open_agent
    elif open_agent and d.get("run_started"):
        # The agent's run ended with items it never answered: a person's now.
        open_human = open_human + open_agent
    if v.get("settled") or not open_human:
        return {"action": "done", "note": "every checklist item has an answer"}
    return {
        "action": "you",
        "note": "%d checklist item%s for you"
        % (len(open_human), "" if len(open_human) == 1 else "s"),
        "detail": dict(up, items=open_human[:40]),
    }


def _judge_wait(flight, step, ss, obs, now) -> dict:
    until = float(ss["detail"].get("until") or 0.0)
    if not until:
        return {"action": "run", "note": "setting the timer"}
    if now >= until:
        return {"action": "done", "note": ""}
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
        return {
            "action": "done",
            "note": "no new review comments for %d min" % cfg["quiet_minutes"],
        }
    return _wait(
        "quiet for %s of %d min" % (_mins(now - quiet_since), cfg["quiet_minutes"])
    )


def _judge_checks(flight, step, ss, obs, now) -> dict:
    c = obs.get("checks") or {}
    verdict = c.get("verdict") or ""
    waited = now - ss["since"]
    if verdict == "ok":
        return {"action": "done", "note": "every check passed"}
    if verdict == "none" and waited >= CHECKS_NONE_GRACE_S:
        return {"action": "done", "note": "no checks report on this commit"}
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
        return _wait("running: %s" % step["config"]["command"].splitlines()[0][:80])
    out = str(c.get("output") or "")
    if c.get("exit") == 0:
        return {"action": "done", "note": "exit 0", "detail": {"output": out}}
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
}


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


def next_action(flight: dict, obs: dict, now: float) -> dict:
    """What the driver should do for ``flight`` now — one of ``wait | run |
    done | fail | you | advance | finish`` plus a ``note`` (the visible wait
    sentence) and per-action keys (``detail`` updates, ``phase`` for a
    sub-step effect, ``then`` + ``reason`` on a fail).

    ``obs`` is a plain dict snapshot: ``session`` (row facts: present, pending,
    status, sleeping, activity, limited, queue_pending, background_waiting,
    worked_at, turn_ended_at, branch, pr_url), ``autopilot`` (the session's
    record or None), ``verify`` (checklist facts), ``reviews`` (``ids`` of the
    actionable comments, None = unknown), ``checks`` (``verdict``,
    ``failing``), ``command`` (the running job or None). Pure: no I/O.
    """
    if flight["state"] not in ACTIVE:
        return _wait("")
    step = _current(flight)
    if step is None:
        return {"action": "finish", "note": "every step is done"}
    ss = flight["step_states"][step["id"]]
    sess = obs.get("session") or {}
    st = ss["state"]
    if st in ("done", "skipped"):
        return {"action": "advance", "note": "", "keep": True}
    if st == "failed":
        return _wait(flight["reason"] or "the step failed")
    hold = _session_hold(flight, step, sess)
    if st == "pending":
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
    out = _JUDGES[step["kind"]](flight, step, ss, obs, now)
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


def apply(flight: dict, action: dict, now: float) -> dict:
    """Mutate ``flight`` by ``action`` (from :func:`next_action`, possibly
    amended by the driver with an effect's outcome) and return it."""
    kind = action.get("action")
    flight["updated"] = now
    for key in ("session", "repo_root", "branch"):
        if action.get(key):
            flight[key] = str(action[key])
    step = _current(flight)
    if kind == "finish" or step is None:
        if flight["state"] not in FINAL:
            _finish(flight, "done", "", now)
        return flight
    sid = step["id"]
    ss = flight["step_states"][sid]
    note = str(action.get("note") or "")
    upd = action.get("detail") if isinstance(action.get("detail"), dict) else {}
    if kind == "wait":
        ss["detail"].update(upd)
        for k in [k for k, v in ss["detail"].items() if v is None]:
            ss["detail"].pop(k, None)
        ss["note"] = note
    elif kind == "run":
        if action.get("phase"):
            ss["detail"].update(upd)
            ss["note"] = note
        else:
            ss["state"] = "running"
            ss["since"] = now
            ss["detail"] = dict(upd)
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


def op_done(
    flight: dict, sid: str, note: str = "", now: Optional[float] = None
) -> dict:
    """Done on a parked step (a *you* step, a verify step waiting on you, or
    a step whose failure was handed to you)."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
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
    _not_final(flight)
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
    _not_final(flight)
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
    flight["state"] = "running"
    flight["reason"] = ""
    flight["updated"] = ts
    _hist(flight, ts, sid, "resumed", "retry")
    return flight


def op_jump(flight: dict, sid: str, now: Optional[float] = None) -> dict:
    """Move the cursor to ``sid``: forward marks the steps in between skipped,
    backward resets the target and everything after it to pending."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
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
    flight["state"] = "running"
    flight["reason"] = ""
    flight["updated"] = ts
    return flight


def op_pause(flight: dict, now: Optional[float] = None) -> dict:
    """Hold a running / waiting flight where it is (``paused``). A halted one
    is already stopped and stays halted; pausing twice is a no-op."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    if flight["state"] in ACTIVE:
        cur = _current(flight)
        flight["state"] = "paused"
        flight["reason"] = ""
        _hist(flight, ts, cur["id"] if cur else "", "paused", "paused by you")
    flight["updated"] = ts
    return flight


def op_resume(flight: dict, now: Optional[float] = None) -> dict:
    """Carry on: a paused flight where it was (back to waiting on you if the
    step is parked), a failure-halted one by running its step again."""
    ts = float(now if now is not None else time.time())
    _not_final(flight)
    cur = _current(flight)
    if cur is None:
        _finish(flight, "done", "", ts)
        return flight
    ss = flight["step_states"][cur["id"]]
    if ss["state"] == "failed":
        flight["step_states"][cur["id"]] = dict(_blank_step_state(), since=ts)
        ss = flight["step_states"][cur["id"]]
    flight["state"] = "waiting_you" if ss["state"] == "waiting_you" else "running"
    flight["reason"] = ""
    flight["updated"] = ts
    ss["detail"].pop("idle_since", None)
    _hist(flight, ts, cur["id"], "resumed", "")
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
