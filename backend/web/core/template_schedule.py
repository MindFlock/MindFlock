"""Template schedules and Run now: start a FRESH session from a template.

A template (``addons/templates.py``) is already a session recipe — agent,
folder, workspace mode and a first prompt. Two ways to start one without the
New dialog live here:

* **Run now** (:func:`run_template`) — one click (Templates dialog, command
  palette) creates a new session from the template with its prompt already
  sent, through the very same ``core.session_create`` path the New dialog's
  ``POST /api/instances`` takes.
* **A schedule** — a template may carry ``schedule: {"at": "HH:MM", "days":
  "daily" | "weekdays"}`` or a full cron expression, ``schedule: {"cron":
  "*/30 * * * *"}`` (both local time; the cron math is
  :mod:`backend.web.core.cronspec`). :func:`run_loop` fires each due one.

Either way a template may carry ``autopilot`` (``commit`` / ``push`` /
``pr``): the new session is armed to carry its work that far once its turn
ends — the same autopilot the ⏩ button arms.

The rules a schedule keeps (all in the pure :func:`plan_tick`, tested with an
injected clock):

* A slot is identified by its LOCAL DATE (``{at, days}``) or local wall-clock
  minute (``{cron}``), not an epoch, so a DST shift can neither skip nor
  repeat one; the slot's epoch comes from ``mktime`` with ``isdst=-1`` (a
  02:30 that doesn't exist on a spring-forward day lands an hour later, never
  on another day).
* Never double-fires: ``last_slot`` is persisted BEFORE the session is
  created, so a crash mid-create loses that run rather than repeating it.
* After downtime only the most recent missed slot fires — never a backlog.
* A schedule fires only from when it was set (``armed_at``): saving "21:00" at
  22:00 waits for tomorrow; changing the time or days re-arms it.
* A scheduled run is SKIPPED (and says so in the history) while the
  template's previous run is still working, waiting on a question or stopped
  at a usage limit — a schedule never stacks sessions on top of one that
  hasn't finished. Run now always starts.
* Nothing fires more often than every
  :data:`~backend.web.core.cronspec.MIN_INTERVAL_SECONDS` — each run is a
  whole new worktree and agent.
* A schedule may be **paused** (``schedule.paused``: it keeps its cadence
  and fires nothing) or told to **skip its next run** (``schedule.skip_until``,
  an epoch: every slot at or before it passes without a session, noted in the
  history). Either way the slot is still spent, so resuming never replays
  one that fell inside the pause. Neither changes the signature, so neither
  re-arms the schedule.
* In a group of devices only the automation device (the one that runs PR
  review and issue handling, ``settings_hooks.automation_here``) fires. The
  others still advance ``last_slot`` silently, so a device that takes the
  role over later never replays a slot the previous one already ran.

Templates sync between devices (``store:templates``) and the schedule with
them; the run record (``template_runs.json``, with the last
:data:`HISTORY_MAX` runs per template) is per device and never synced.
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
from backend.web.core import cronspec

_FileName = "template_runs.json"
_LOCK = threading.Lock()

#: Seconds between loop ticks. A slot fires within this of its minute.
TICK_SECONDS = 30.0

DAYS = ("daily", "weekdays")
#: How far a template may carry its run on its own (autopilot depths; merge
#: stays a human's call).
AUTOPILOT_DEPTHS = ("commit", "push", "pr")
#: Runs kept per template in the run record.
HISTORY_MAX = 20
#: A previous run in one of these keeps a scheduled run from starting.
BUSY_ACTIVITIES = {
    "working": "the previous run is still working",
    "clarify": "the previous run is waiting on a question",
    "limit": "the previous run is stopped at a usage limit",
}
#: The history's words for a slot passed over by "Skip next".
SKIPPED_BY_HAND = "you skipped this run"
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
        raise ValueError("schedule must be an object: {at: 'HH:MM', days} or {cron}")
    if "cron" in raw:
        cron = cronspec.parse(str(raw.get("cron") or ""))
        if cronspec.too_frequent(cron):
            raise ValueError(
                "a schedule can't run more often than every %d minutes — each run "
                "is a new session" % (cronspec.MIN_INTERVAL_SECONDS // 60)
            )
        return {"cron": cron.expr, **_flags(raw)}
    at = str(raw.get("at", "") or "").strip()
    m = _HHMM_RE.match(at)
    if not m:
        raise ValueError("schedule.at must be a 24-hour time like 21:00")
    days = str(raw.get("days", "") or "daily").strip()
    if days not in DAYS:
        raise ValueError("schedule.days must be 'daily' or 'weekdays'")
    return {
        "at": "%02d:%02d" % (int(m.group(1)), int(m.group(2))),
        "days": days,
        **_flags(raw),
    }


def _flags(raw: dict) -> dict:
    """A schedule's optional switches, stored only when set (a schedule
    without them keeps the exact shape older devices wrote): ``paused`` and
    ``skip_until`` (epoch seconds; slots at or before it are skipped)."""
    out: dict = {}
    if raw.get("paused") is True:
        out["paused"] = True
    try:
        until = float(raw.get("skip_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if until > 0:
        out["skip_until"] = int(until)
    return out


def schedule_or_none(raw) -> Optional[dict]:
    """:func:`normalize_schedule` that drops a bad schedule instead of raising
    (what a synced-in template gets: never refuse the whole template)."""
    try:
        return normalize_schedule(raw)
    except ValueError:
        return None


def signature(sched: Optional[dict]) -> str:
    if not sched:
        return ""
    if "cron" in sched:
        return "cron:" + sched["cron"]
    return "%s/%s" % (sched["at"], sched["days"])


def normalize_autopilot(raw) -> str:
    """``raw`` as a stored autopilot depth ("" = leave the session as it
    ends). Raises ``ValueError`` for anything else."""
    v = str(raw or "").strip().lower()
    if v in ("", "off", "agent", "none", "leave"):
        return ""
    if v not in AUTOPILOT_DEPTHS:
        raise ValueError("autopilot must be one of: " + ", ".join(AUTOPILOT_DEPTHS))
    return v


def describe(sched: Optional[dict]) -> str:
    """The card's words for a schedule ("" for none)."""
    if not sched:
        return ""
    if "cron" in sched:
        return cronspec.describe(sched["cron"])
    return ("Weekdays at " if sched["days"] == "weekdays" else "Every day at ") + sched[
        "at"
    ]


def runs_many_per_day(sched: Optional[dict]) -> bool:
    """Whether a schedule can fire twice in one day (its run titles then
    carry the time, not just the date)."""
    if not sched or "cron" not in sched:
        return False
    c = cronspec.parse(sched["cron"])
    return len(c.hours) * len(c.minutes) > 1


def _hm(sched: dict) -> Tuple[int, int]:
    hh, mm = sched["at"].split(":")
    return int(hh), int(mm)


def _day_ok(day: _dt.date, sched: dict) -> bool:
    return sched["days"] == "daily" or day.weekday() < 5


def slot_epoch(day: _dt.date, sched: dict) -> float:
    """Local-time epoch of ``sched``'s slot on ``day`` (``isdst=-1``)."""
    hh, mm = _hm(sched)
    return time.mktime((day.year, day.month, day.day, hh, mm, 0, 0, 0, -1))


def latest_due(
    now: float, sched: dict, not_before: Optional[float] = None
) -> Optional[Tuple[str, float]]:
    """The most recent slot at or before ``now``: ``(slot, epoch)`` — the slot
    a local date for ``{at, days}``, a local ``YYYY-MM-DDTHH:MM`` for
    ``{cron}`` (both sort as strings). ``not_before`` only bounds the search."""
    if "cron" in sched:
        return cronspec.latest_due(cronspec.parse(sched["cron"]), now, not_before)
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
    if "cron" in sched:
        return cronspec.next_due(cronspec.parse(sched["cron"]), now)
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


def record_run(key: str, entry: dict, **fields) -> None:
    """Merge ``fields`` into one template's run record AND put ``entry`` at
    the head of its history (newest first, :data:`HISTORY_MAX` kept)."""
    with _LOCK:
        data = read_json(runs_path(), {}, expect=dict)
        runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
        rec = dict(runs.get(key) or {})
        rec.update(fields)
        hist = rec.get("history") if isinstance(rec.get("history"), list) else []
        rec["history"] = ([dict(entry)] + [h for h in hist if isinstance(h, dict)])[
            :HISTORY_MAX
        ]
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
        due = latest_due(now, sched, not_before=float(rec.get("armed_at") or 0) or None)
        if due is None:
            continue
        slot, at = due
        if at < float(rec.get("armed_at") or 0):
            continue
        if str(rec.get("last_slot") or "") >= slot:
            continue
        # Spent whatever happens next: a paused or skipped slot is never
        # replayed once the schedule resumes.
        rec["last_slot"] = slot
        if sched.get("paused"):
            continue
        if at <= float(sched.get("skip_until") or 0):
            if here:
                hist = (
                    rec.get("history") if isinstance(rec.get("history"), list) else []
                )
                rec["history"] = [
                    {"at": at, "trigger": "schedule", "skipped": SKIPPED_BY_HAND}
                ] + [h for h in hist if isinstance(h, dict)][: HISTORY_MAX - 1]
            continue
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


def session_title(name: str, when: float, with_time: bool = False) -> str:
    """``<template slug>-<mon><day>`` — "rivian-update-oct13"; with
    ``with_time`` (a schedule that fires more than once a day)
    ``<slug>-<mon><day>-<HHMM>`` — "deploy-watch-oct13-2130"."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40]
    slug = slug.strip("-") or "template"
    day = _dt.datetime.fromtimestamp(when)
    title = "%s-%s%d" % (slug, day.strftime("%b").lower(), day.day)
    return title + day.strftime("-%H%M") if with_time else title


def _payload(tpl: dict, title: str) -> dict:
    body = {"title": title}
    for f in ("program", "repo_path", "prompt", "workspace_strategy"):
        v = str(tpl.get(f) or "").strip() if f != "prompt" else str(tpl.get(f) or "")
        if v:
            body[f] = v
    for f in ("provisioned", "in_place", "init_repo"):
        if tpl.get(f):
            body[f] = True
    # Autopilot commits for THIS session, so it never drives a checkout other
    # sessions share — the New dialog's rule: with one, it gets a worktree.
    try:
        if normalize_autopilot(tpl.get("autopilot")):
            body.pop("in_place", None)
    except ValueError:
        pass
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
    create's own answer (202 + the new row), or 409 ``{error, detail,
    reason}`` for a template that can't launch (no folder, its agent not
    installed), recorded as a FAILED run before any session is made — a
    scheduled fire with nobody watching must never read as a success.
    ``trigger`` is ``"manual"`` or ``"schedule"``."""
    from backend.web.addons import templates as _t

    now = time.time() if now is None else now
    create = create or _default_create
    key = _key(tpl.get("name"))
    refusal = await asyncio.to_thread(_t.launch_refusal, tpl)
    if refusal:
        await asyncio.to_thread(_t.record_refused_run, tpl, refusal, trigger)
        if trigger == "schedule":
            _emit(
                "template.run_failed",
                tpl,
                trigger,
                error=str(refusal.get("error") or ""),
                reason=str(refusal.get("reason") or ""),
                detail=str(refusal.get("detail") or ""),
            )
        return 409, refusal
    sched = schedule_or_none(tpl.get("schedule"))
    base = session_title(
        tpl.get("name", ""),
        now,
        with_time=trigger == "schedule" and runs_many_per_day(sched),
    )
    if taken is None:

        def taken(title: str) -> bool:
            return _title_taken(title) or _branch_taken(tpl, title)

    status, body = 0, {}
    for n in range(1, 30):
        title = base if n == 1 else "%s-%d" % (base, n)
        if await asyncio.to_thread(taken, title):
            continue
        status, body = await create(_payload(tpl, title))
        if status != 409:
            break
    if 200 <= status < 300:
        title = str(body.get("title") or "")
        depth = ""
        try:
            depth = normalize_autopilot(tpl.get("autopilot"))
        except ValueError:
            pass
        armed = ""
        if depth and title:
            armed = await asyncio.to_thread(_arm_autopilot, title, depth)
        entry = {"at": now, "trigger": trigger, "session": title}
        if armed:
            entry["autopilot"] = armed
        record_run(
            key,
            entry,
            last_run_at=now,
            last_session=title,
            last_trigger=trigger,
            last_error="",
        )
        _emit("template.run_started", tpl, trigger, session=title)
    else:
        err = str(body.get("error") or "couldn't start (%s)" % status)
        if not status:
            err = "no free session name"
        _note_failure(tpl, trigger, now, err)
    return status or 409, body or {"error": "no free session name"}


def _note_failure(tpl: dict, trigger: str, now: float, err: str) -> None:
    record_run(
        _key(tpl.get("name")),
        {"at": now, "trigger": trigger, "error": err},
        last_error=err,
        last_error_at=now,
    )
    # Run now's failure is the clicker's toast; a scheduled one happened with
    # nobody watching, so it is news.
    if trigger == "schedule":
        _emit(
            "template.run_failed",
            tpl,
            trigger,
            error=err,
            detail="Scheduled run of “%s” couldn't start: %s"
            % (tpl.get("name") or "", err),
        )


def _emit(event: str, tpl: dict, trigger: str, **data) -> None:
    """Tell the browser (the bell shows a scheduled run that failed or was
    skipped — nobody was watching when it happened). Never raises."""
    try:
        from backend.web.core import events

        events.BUS.emit(
            event,
            session=str(data.get("session") or ""),
            data={"template": str(tpl.get("name") or ""), "trigger": trigger, **data},
        )
    except Exception:  # noqa: BLE001
        pass


def _arm_autopilot(title: str, depth: str) -> str:
    """Arm the new session's autopilot to ``depth``; the depth armed, or ""
    when arming failed (the run still started — it just stops where the
    agent leaves it). Blocking."""
    try:
        from backend.web.core import lanes

        lane = lanes.lane_of_depth(depth)
        if not lane:
            return ""
        lanes.arm_session(title, lane, require_workspace=False, source="session")
        return depth
    except Exception as err:  # noqa: BLE001
        try:
            from backend import log

            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "template run %s: autopilot not armed: %v", title, err
                )
        except Exception:  # noqa: BLE001
            pass
        return ""


def _branch_taken(tpl: dict, title: str) -> bool:
    """Whether a NEW worktree session titled ``title`` would land on a branch
    that already exists in the template's repo — a closed run's branch left
    behind. The engine would quietly check that old branch out instead of
    starting fresh, so the title is skipped. Blocking (one git call)."""
    if tpl.get("provisioned") or tpl.get("in_place"):
        return False
    repo = str(tpl.get("repo_path") or "").strip()
    if not repo or not os.path.isdir(repo):
        return False
    try:
        from backend import gitutil
        from backend.web import server

        branch = server._session_branch_name(title)
        return bool(branch) and gitutil.ref_exists(
            repo, "refs/heads/" + branch, timeout=10
        )
    except Exception:  # noqa: BLE001
        return False


def busy_reason(title: str) -> str:
    """Why the session ``title`` keeps a scheduled run from starting ("" =
    it doesn't: gone, idle, offline)."""
    if not title:
        return ""
    return BUSY_ACTIVITIES.get(_session_activity(title), "")


def _session_activity(title: str) -> str:
    try:
        from backend.web import server

        if title not in server.ENGINE.instances:
            return ""
        return server._snapshot_activity(title)
    except Exception:  # noqa: BLE001
        return ""


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
            key = _key(tpl.get("name"))
            prev = str((new_runs.get(key) or {}).get("last_session") or "")
            busy = await asyncio.to_thread(busy_reason, prev)
            if busy:
                await asyncio.to_thread(
                    record_run,
                    key,
                    {
                        "at": now,
                        "trigger": "schedule",
                        "skipped": busy,
                        "session": prev,
                    },
                    last_skip_at=now,
                    last_skip=busy,
                )
                _emit(
                    "template.run_skipped",
                    tpl,
                    "schedule",
                    reason=busy,
                    session=prev,
                    detail="Skipped a scheduled run of “%s”: %s"
                    % (tpl.get("name") or "", busy),
                )
                continue
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
    last_error, paused?, skipping_at?}}, runs_here, runs_on}`` for ``GET
    /api/templates``. A paused schedule has no ``next_at``; one told to skip
    its next run names that slot (``skipping_at``) and ``next_at`` is the one
    after it."""
    now = time.time() if now is None else now
    runs = load_runs()
    out: Dict[str, dict] = {}
    for tpl in templates_list:
        key = _key(tpl.get("name"))
        if not key:
            continue
        rec = runs.get(key) or {}
        sched = schedule_or_none(tpl.get("schedule"))
        paused = bool(sched and sched.get("paused"))
        nxt = next_due(now, sched) if sched and not paused else None
        # "Skip next": the slot that will pass, and the one after it.
        skipping = None
        until = float((sched or {}).get("skip_until") or 0)
        if nxt and until and nxt <= until:
            skipping = nxt
            nxt = next_due(until, sched)
        hist = rec.get("history") if isinstance(rec.get("history"), list) else []
        row = {
            "next_at": nxt,
            "last_at": rec.get("last_run_at"),
            "last_session": rec.get("last_session") or "",
            "last_trigger": rec.get("last_trigger") or "",
            "last_error": rec.get("last_error") or "",
            "schedule_label": describe(sched),
            "history": [h for h in hist if isinstance(h, dict)],
        }
        # A skip newer than the last start/failure is what the card says.
        skip_at = float(rec.get("last_skip_at") or 0)
        if skip_at and skip_at >= max(
            float(rec.get("last_run_at") or 0), float(rec.get("last_error_at") or 0)
        ):
            row["last_skip"] = rec.get("last_skip") or ""
            row["last_skip_at"] = skip_at
        if paused:
            row["paused"] = True
        if skipping:
            row["skipping_at"] = skipping
        if any(v for v in row.values()):
            # The schedule's "at" is THIS device's local time: name the zone
            # on the row the card reads (EDT, CET…), as of the next run.
            if sched:
                row["tz"] = time_zone(nxt or now)
            out[key] = row
    return {
        "runs": out,
        "runs_here": automation_here(),
        "runs_on": automation_elsewhere(),
        "tz": time_zone(now),
    }


def time_zone(at: float) -> str:
    """This device's time-zone abbreviation at ``at`` ("EDT"), "" if unknown."""
    try:
        return time.strftime("%Z", time.localtime(at)) or ""
    except (OverflowError, OSError, ValueError):
        return ""


def preview(expr: str, now: Optional[float] = None, count: int = 3) -> dict:
    """What the editor shows under a cron field as you type: ``{ok, label,
    next: [epoch...]}`` or ``{ok: false, error}``. Applies the same rules a
    save does (:func:`normalize_schedule`)."""
    now = time.time() if now is None else now
    try:
        sched = normalize_schedule({"cron": expr})
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    assert sched is not None
    cron = cronspec.parse(sched["cron"])
    return {
        "ok": True,
        "cron": sched["cron"],
        "label": describe(sched),
        "next": cronspec.upcoming(cron, now, count),
    }
