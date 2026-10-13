"""Template schedules and Run now: start a FRESH session from a template.

A template (``addons/templates.py``) is already a session recipe — agent,
folder, workspace mode and a first prompt. Two ways to start one without the
New dialog live here:

* **Run now** (:func:`run_template`) — one click (Templates dialog, command
  palette) creates a new session from the template with its prompt already
  sent, through the very same ``core.session_create`` path the New dialog's
  ``POST /api/instances`` takes.
* **A schedule** — a template may carry ``schedule: {"at": "HH:MM", "days":
  "daily" | "weekdays"}`` (local time). :func:`run_loop` fires each due one.

The rules a schedule keeps (all in the pure :func:`plan_tick`, tested with an
injected clock):

* A slot is identified by its LOCAL DATE, not an epoch, so a DST shift can
  neither skip nor repeat a day; the slot's epoch comes from ``mktime`` with
  ``isdst=-1`` (a 02:30 that doesn't exist on a spring-forward day lands an
  hour later, never on another day).
* Never double-fires: ``last_slot`` is persisted BEFORE the session is
  created, so a crash mid-create loses that run rather than repeating it.
* After downtime only the most recent missed slot fires — never a backlog.
* A schedule fires only from when it was set (``armed_at``): saving "21:00" at
  22:00 waits for tomorrow; changing the time or days re-arms it.
* In a group of devices only the automation device (the one that runs PR
  review and issue handling, ``settings_hooks.automation_here``) fires. The
  others still advance ``last_slot`` silently, so a device that takes the
  role over later never replays a slot the previous one already ran.

Templates sync between devices (``store:templates``) and the schedule with
them; the run record (``template_runs.json``) is per device and never synced.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from backend.config.config import GetConfigDir
from backend.fsutil import atomic_write_json, read_json

_FileName = "template_runs.json"
_LOCK = threading.Lock()

#: Seconds between loop ticks. A slot fires within this of its minute.
TICK_SECONDS = 30.0

DAYS = ("daily", "weekdays")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- #
# Schedule shape
# --------------------------------------------------------------------------- #
def normalize_schedule(raw) -> Optional[dict]:
    """``raw`` as the stored schedule, ``None`` for "no schedule" (absent,
    empty, ``{}``). Raises ``ValueError`` naming the problem otherwise."""
    if raw in (None, "", {}, False):
        return None
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object: {at: 'HH:MM', days}")
    at = str(raw.get("at", "") or "").strip()
    m = _HHMM_RE.match(at)
    if not m:
        raise ValueError("schedule.at must be a 24-hour time like 21:00")
    days = str(raw.get("days", "") or "daily").strip()
    if days not in DAYS:
        raise ValueError("schedule.days must be 'daily' or 'weekdays'")
    return {"at": "%02d:%02d" % (int(m.group(1)), int(m.group(2))), "days": days}


def schedule_or_none(raw) -> Optional[dict]:
    """:func:`normalize_schedule` that drops a bad schedule instead of raising
    (what a synced-in template gets: never refuse the whole template)."""
    try:
        return normalize_schedule(raw)
    except ValueError:
        return None


def signature(sched: Optional[dict]) -> str:
    return "%s/%s" % (sched["at"], sched["days"]) if sched else ""


def _hm(sched: dict) -> Tuple[int, int]:
    hh, mm = sched["at"].split(":")
    return int(hh), int(mm)


def _day_ok(day: _dt.date, sched: dict) -> bool:
    return sched["days"] == "daily" or day.weekday() < 5


def slot_epoch(day: _dt.date, sched: dict) -> float:
    """Local-time epoch of ``sched``'s slot on ``day`` (``isdst=-1``)."""
    hh, mm = _hm(sched)
    return time.mktime((day.year, day.month, day.day, hh, mm, 0, 0, 0, -1))


def latest_due(now: float, sched: dict) -> Optional[Tuple[str, float]]:
    """The most recent slot at or before ``now``: ``(local date, epoch)``."""
    today = _dt.date.fromtimestamp(now)
    for back in range(0, 8):
        day = today - _dt.timedelta(days=back)
        if not _day_ok(day, sched):
            continue
        at = slot_epoch(day, sched)
        if at <= now:
            return day.isoformat(), at
    return None


def next_due(now: float, sched: dict) -> Optional[float]:
    """Epoch of the first slot strictly after ``now``."""
    today = _dt.date.fromtimestamp(now)
    for ahead in range(0, 9):
        day = today + _dt.timedelta(days=ahead)
        if not _day_ok(day, sched):
            continue
        at = slot_epoch(day, sched)
        if at > now:
            return at
    return None


# --------------------------------------------------------------------------- #
# Run record (per device, never synced)
# --------------------------------------------------------------------------- #
def runs_path() -> str:
    """``$MINDFLOCK_TEMPLATE_RUNS_FILE`` (tests), else
    ``<config dir>/template_runs.json``."""
    from backend.config.home_guard import guard

    env = os.environ.get("MINDFLOCK_TEMPLATE_RUNS_FILE")
    return guard(env or os.path.join(GetConfigDir(), _FileName), "template runs")


def load_runs() -> Dict[str, dict]:
    with _LOCK:
        data = read_json(runs_path(), {}, expect=dict)
    runs = data.get("runs")
    return dict(runs) if isinstance(runs, dict) else {}


def _save_runs(runs: Dict[str, dict]) -> None:
    atomic_write_json(runs_path(), {"runs": runs}, prefix=".tplrun.")


def save_runs(runs: Dict[str, dict]) -> None:
    with _LOCK:
        _save_runs(runs)


def update_run(key: str, **fields) -> None:
    """Merge ``fields`` into one template's run record."""
    with _LOCK:
        data = read_json(runs_path(), {}, expect=dict)
        runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
        rec = dict(runs.get(key) or {})
        rec.update(fields)
        runs[key] = rec
        _save_runs(runs)


def _key(name: str) -> str:
    return str(name or "").strip().lower()


# --------------------------------------------------------------------------- #
# The pure tick
# --------------------------------------------------------------------------- #
def plan_tick(
    now: float, templates: List[dict], runs: Dict[str, dict], here: bool
) -> Tuple[List[Tuple[dict, str]], Dict[str, dict]]:
    """What one tick does: ``(to_fire, runs')``.

    ``to_fire`` lists ``(template, slot date)`` to start now (always empty
    when not ``here``); ``runs'`` is the run record to persist — re-armed
    schedules, advanced ``last_slot``s, records of templates that lost their
    schedule dropped of their arming. Pure: no clock, no I/O."""
    out = {k: dict(v) for k, v in runs.items() if isinstance(v, dict)}
    fire: List[Tuple[dict, str]] = []
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        key = _key(tpl.get("name"))
        sched = schedule_or_none(tpl.get("schedule"))
        if not key:
            continue
        rec = out.get(key)
        if not sched:
            if rec and rec.get("sig"):
                rec.pop("sig", None)
                rec.pop("armed_at", None)
            continue
        sig = signature(sched)
        if rec is None or rec.get("sig") != sig:
            # First sight of this schedule (or a new time/days): arm it now,
            # so nothing before this moment counts as missed.
            rec = dict(rec or {})
            rec["sig"] = sig
            rec["armed_at"] = now
            out[key] = rec
        due = latest_due(now, sched)
        if due is None:
            continue
        slot, at = due
        if at < float(rec.get("armed_at") or 0):
            continue
        if str(rec.get("last_slot") or "") >= slot:
            continue
        rec["last_slot"] = slot
        if here:
            fire.append((tpl, slot))
    return fire, out


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #
def run_problem(tpl: dict) -> str:
    """Why ``tpl`` can't start a session on its own ("" = it can). A plain
    session with no folder would run in the server's own working directory —
    never what a template meant."""
    if not str(tpl.get("repo_path") or "").strip() and not tpl.get("provisioned"):
        return "this template has no folder — edit it and pick one"
    return ""


def session_title(name: str, when: float) -> str:
    """``<template slug>-<mon><day>`` — "rivian-update-oct13"."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40]
    slug = slug.strip("-") or "template"
    day = _dt.datetime.fromtimestamp(when)
    return "%s-%s%d" % (slug, day.strftime("%b").lower(), day.day)


def _payload(tpl: dict, title: str) -> dict:
    body = {"title": title}
    for f in ("program", "repo_path", "prompt", "workspace_strategy"):
        v = str(tpl.get(f) or "").strip() if f != "prompt" else str(tpl.get(f) or "")
        if v:
            body[f] = v
    for f in ("provisioned", "in_place", "init_repo"):
        if tpl.get(f):
            body[f] = True
    return body


async def _default_create(payload: dict) -> Tuple[int, dict]:
    from backend.web.core import session_create

    return await session_create.create_result(payload)


async def fire(
    tpl: dict,
    trigger: str,
    *,
    now: Optional[float] = None,
    create=None,
    taken: Optional[Callable[[str], bool]] = None,
) -> Tuple[int, dict]:
    """Start a session from ``tpl`` and record it. ``(status, body)`` — the
    create's own answer (202 + the new row), or 400 for a template that
    can't run. ``trigger`` is ``"manual"`` or ``"schedule"``."""
    now = time.time() if now is None else now
    create = create or _default_create
    key = _key(tpl.get("name"))
    problem = run_problem(tpl)
    if problem:
        if trigger == "schedule":
            update_run(key, last_error=problem, last_error_at=now)
        return 400, {"error": problem}
    base = session_title(tpl.get("name", ""), now)
    if taken is None:
        taken = _title_taken
    status, body = 0, {}
    for n in range(1, 10):
        title = base if n == 1 else "%s-%d" % (base, n)
        if taken(title):
            continue
        status, body = await create(_payload(tpl, title))
        if status != 409:
            break
    if 200 <= status < 300:
        update_run(
            key,
            last_run_at=now,
            last_session=str(body.get("title") or ""),
            last_trigger=trigger,
            last_error="",
        )
    else:
        update_run(
            key,
            last_error=str(body.get("error") or "couldn't start (%s)" % status),
            last_error_at=now,
        )
    return status or 409, body or {"error": "no free session name"}


def _title_taken(title: str) -> bool:
    try:
        from backend.web import server

        return title in server.ENGINE.instances
    except Exception:  # noqa: BLE001
        return False


async def run_template(name: str) -> Tuple[int, dict]:
    """Run now: start ``name`` (404 when there is no such template)."""
    from backend.web.addons import templates

    key = _key(name)
    tpl = next(
        (t for t in templates.list_templates() if _key(t.get("name")) == key), None
    )
    if tpl is None:
        return 404, {"error": "no template named %s" % name}
    async with _fire_lock():
        return await fire(tpl, "manual")


# One fire at a time: a Run now racing the loop must not pick the same title.
# Created lazily: the lock binds to the running loop on first use.
_FIRE_LOCK: Optional[asyncio.Lock] = None


def _fire_lock() -> asyncio.Lock:
    global _FIRE_LOCK
    if _FIRE_LOCK is None:
        _FIRE_LOCK = asyncio.Lock()
    return _FIRE_LOCK


def automation_here() -> bool:
    """Whether schedules fire on THIS device (the group's automation device;
    a lone device always). Never raises."""
    try:
        from backend.web.core import settings_hooks

        return bool(settings_hooks.automation_here())
    except Exception:  # noqa: BLE001
        return False


def automation_elsewhere() -> str:
    """The OTHER device's name that fires schedules, "" when it is this one."""
    try:
        from backend.web.core import settings_hooks

        return str(settings_hooks.automation_device() or "")
    except Exception:  # noqa: BLE001
        return ""


async def tick(now: Optional[float] = None, here: Optional[bool] = None) -> List[str]:
    """One loop pass: persist the plan, then fire what's due. Returns the
    names fired (tests)."""
    from backend.web.addons import templates

    now = time.time() if now is None else now
    if here is None:
        here = await asyncio.to_thread(automation_here)
    tpls = await asyncio.to_thread(templates.list_templates)
    async with _fire_lock():
        runs = await asyncio.to_thread(load_runs)
        to_fire, new_runs = plan_tick(now, tpls, runs, here)
        if new_runs != runs:
            # Persisted BEFORE anything starts: the slot is spent even if the
            # create below crashes the process.
            await asyncio.to_thread(save_runs, new_runs)
        fired = []
        for tpl, _slot in to_fire:
            await fire(tpl, "schedule", now=now)
            fired.append(str(tpl.get("name") or ""))
    return fired


async def run_loop() -> None:
    """Forever: :func:`tick` every :data:`TICK_SECONDS` (the addon's startup
    registers it; off under pytest)."""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — one bad tick never stops it
            try:
                from backend import log

                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("template schedule tick failed: %v", err)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(TICK_SECONDS)


# --------------------------------------------------------------------------- #
# What the UI shows
# --------------------------------------------------------------------------- #
def status_for(templates_list: List[dict], now: Optional[float] = None) -> dict:
    """``{runs: {<name lower>: {next_at, last_at, last_session, last_trigger,
    last_error}}, runs_here, runs_on}`` for ``GET /api/templates``."""
    now = time.time() if now is None else now
    runs = load_runs()
    out: Dict[str, dict] = {}
    for tpl in templates_list:
        key = _key(tpl.get("name"))
        if not key:
            continue
        rec = runs.get(key) or {}
        sched = schedule_or_none(tpl.get("schedule"))
        row = {
            "next_at": next_due(now, sched) if sched else None,
            "last_at": rec.get("last_run_at"),
            "last_session": rec.get("last_session") or "",
            "last_trigger": rec.get("last_trigger") or "",
            "last_error": rec.get("last_error") or "",
        }
        if any(v for v in row.values()):
            out[key] = row
    return {
        "runs": out,
        "runs_here": automation_here(),
        "runs_on": automation_elsewhere(),
    }
