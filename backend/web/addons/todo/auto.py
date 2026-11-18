"""Auto sources: what a PERSON must do lands on the to-do list by itself.

Server-side and deterministic — read off the shared event bus, never left to
an agent remembering to add it:

1. **Verify** waiting on the person (``session.test_plan_checked`` whose run
   outcome is ``needs_you``): one ``manual_test`` item per step the person is
   asked to check (``test_plans.confirm_steps``), or one "Confirm verified"
   item when every step already has an answer.
2. **A route (flight) parked on the person** (``flight.needs_you``): one
   ``needs_you`` item — except at a Verify stop whose check is already (1).
3. **A team-run escalation** (``run.needs_you``): one ``needs_you`` item.

And they resolve themselves: each item carries a ``key`` naming the thing it
waits on (``verify:<plan>:<step>``, ``flight:<id>:<step>:<since>``,
``run:<run>:<task>:<reason>``), and when that thing has positively moved on
— the verify was confirmed or re-run, the flight left the stop, the run item
settled — the item is marked done. Events trigger that check for the one
plan / flight / run they name; a slow pass (:data:`RECONCILE_EVERY_S`)
rechecks every open auto item, so a missed event or a restart never leaves a
stale one. The same pass also ADDS: every flight parked on a person, every
verify waiting on one and every open team-run escalation gets its item if it
has none (a "you" stop with ``notify: false`` emits no event; a restart loses
any) — but never an item a person already closed for that same ask
(:func:`ensure`). The pass only ever closes items whose ``source`` is an auto
source, and only on a condition it actually READ: a store that reads empty
(missing or unreadable) closes nothing.

Ticking an auto item off by hand does NOT confirm the verify or move the
flight — the list is a reminder, the session is where the answer goes.

Plans, flights and runs are read through their modules' public read
functions only (``test_plans.get`` / ``list_plans``, ``flights.get`` /
``list_flights``, ``team_runs.load`` / ``list_runs``) — never written.

Threading: bus callbacks run on the EMITTER's thread, so :meth:`AutoSources.
on_event` does nothing but hand the envelope to a queue; one daemon thread
does the reading and writing.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Dict, List, Optional

from backend import log

from . import store

#: How often the slow pass rechecks every open auto item.
RECONCILE_EVERY_S = 60.0
#: The first pass after startup (lets the other stores settle).
FIRST_RECONCILE_S = 10.0

_VERIFY_EVENTS = frozenset(
    {
        "session.test_plan_checked",
        "session.test_plan_settled",
        "session.test_plan_step_failed",
        "session.test_plan_gave_up",
        "session.test_plan_ready",
        "session.test_plan_failed",
    }
)
_FLIGHT_EVENTS = frozenset({"flight.needs_you", "flight.changed", "flight.finished"})
_RUN_EVENTS = frozenset({"run.needs_you", "run.changed", "run.finished"})
WATCHED = _VERIFY_EVENTS | _FLIGHT_EVENTS | _RUN_EVENTS | {"session.deleted"}

TEXT_MAX = 300


def _log(fmt: str, *args) -> None:
    try:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(fmt, *args)
    except Exception:  # noqa: BLE001
        pass


def _one_line(text: str, limit: int = TEXT_MAX) -> str:
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Readers (public read functions only; any failure reads as "unknown")
# --------------------------------------------------------------------------- #
def _test_plans():
    from backend.web.core import test_plans

    return test_plans


def _flights():
    from backend.web.core import flights

    return flights


def _team_runs():
    from backend.web.core import team_runs

    return team_runs


def _plan_session(plan_id: str) -> str:
    """The session a plan belongs to: plans are keyed by session title; a
    checklist asked for by branch (``<repo>@<branch>``) has none."""
    return "" if "@" in plan_id else plan_id


# --------------------------------------------------------------------------- #
# 1. Verify
# --------------------------------------------------------------------------- #
def _newest_run(plan: dict) -> dict:
    runs = plan.get("runs") or []
    return runs[-1] if runs else {}


def verify_items(plan: dict) -> List[dict]:
    """The items a ``needs_you`` verify asks for (pure)."""
    tp = _test_plans()
    pid = str(plan.get("id") or "")
    title = str(plan.get("title") or pid)
    session = _plan_session(pid)
    where = ("on %s" % session) if session else "in Verify"
    finish = (
        "When it works, press Confirm verified %s (or Not verified). "
        "Ticking this item off does not confirm the check." % where
    )
    try:
        asks = {
            n["step"]: n.get("do") or ""
            for n in tp.run_progress(plan).get("needs_you") or []
        }
    except Exception:  # noqa: BLE001
        asks = {}
    steps = tp.confirm_steps(plan)
    if not steps:
        return [
            {
                "text": _one_line("Confirm verified: %s" % title),
                "details": "Every step of the check has an answer. " + finish,
                "kind": "manual_test",
                "source": "verify",
                "session": session,
                "key": "verify:%s:confirm" % pid,
            }
        ]
    out = []
    for step in steps:
        text = str(step.get("text") or "")
        lines = []
        if _one_line(text) != text.strip():
            lines.append(text.strip())
        if step.get("expect"):
            lines.append("Expect: %s" % step["expect"])
        if step.get("paste"):
            lines.append("Paste:\n%s" % step["paste"])
        if asks.get(step.get("id")):
            lines.append("The agent asks: %s" % asks[step["id"]])
        lines.append("Checklist: %s. %s" % (title, finish))
        out.append(
            {
                "text": _one_line(text) or "Check a step of %s" % title,
                "details": "\n\n".join(lines)[: store.MAX_DETAILS],
                "kind": "manual_test",
                "source": "verify",
                "session": session,
                "key": "verify:%s:%s" % (pid, step.get("id")),
            }
        )
    return out


def _verify_open(plan: Optional[dict], step_id: str, plans_readable: bool):
    """True = still waiting, False = ended, None = can't tell."""
    if plan is None:
        return False if plans_readable else None
    if str(_newest_run(plan).get("outcome") or "") != "needs_you":
        return False
    if step_id == "confirm":
        return True
    return step_id in {s.get("id") for s in _test_plans().confirm_steps(plan)}


# --------------------------------------------------------------------------- #
# 2. Flights
# --------------------------------------------------------------------------- #
def _covered_by_verify(step: dict, ss: dict) -> bool:
    """A Verify stop parked on its checklist is item (1)'s job; one parked
    because the check could not run (blocked / memory) or failed is not."""
    if step.get("kind") != "verify":
        return False
    detail = ss.get("detail") or {}
    return not detail.get("failed_reason") and detail.get("ask") not in (
        "blocked",
        "memory",
    )


def flight_item(flight: dict, text: str = "", items=None) -> Optional[dict]:
    """The item a flight parked on you asks for, or None (not parked, or a
    Verify stop the verify source covers)."""
    fl = _flights()
    if flight.get("state") != "waiting_you":
        return None
    cur = fl.waiting_step(flight)
    if cur is None:
        return None
    ss = (flight.get("step_states") or {}).get(cur["id"]) or {}
    if _covered_by_verify(cur, ss):
        return None
    if not text:
        text, items = fl.you_text(flight)
    since = int(float(ss.get("since") or 0.0))
    session = str(flight.get("session") or "")
    lines = ["- %s" % i for i in (items or []) if str(i).strip()]
    if _one_line(text) != str(text or "").strip():
        lines.insert(0, str(text).strip())
    route = str(flight.get("plan_name") or "A route")
    lines.append(
        "%s is waiting on you at %s. Answer it %s; ticking this item off "
        "does not move the route."
        % (
            route,
            cur.get("title") or "a stop",
            ("on " + session) if session else "in Routes",
        )
    )
    return {
        "text": _one_line(text)
        or "%s: %s" % (route, cur.get("title") or "waiting on you"),
        "details": "\n".join(lines)[: store.MAX_DETAILS],
        "kind": "needs_you",
        "source": "flight",
        "session": session,
        "key": "flight:%s:%s:%d" % (flight["id"], cur["id"], since),
    }


def _flight_open(flight: Optional[dict], step_id: str, since: int, readable: bool):
    if flight is None:
        return False if readable else None
    if flight.get("state") != "waiting_you":
        return False
    cur = _flights().waiting_step(flight)
    if cur is None or cur["id"] != step_id:
        return False
    if since:
        ss = (flight.get("step_states") or {}).get(step_id) or {}
        if int(float(ss.get("since") or 0.0)) != since:
            return False
    return True


# --------------------------------------------------------------------------- #
# 3. Team runs
# --------------------------------------------------------------------------- #
def run_item(env: dict) -> Optional[dict]:
    data = env.get("data") or {}
    rid = str(data.get("run") or "")
    if not rid:
        return None
    task = str(data.get("task") or "")
    reason = str(data.get("reason") or "")
    name = str(data.get("name") or rid)
    who = str(data.get("ref") or data.get("title") or name)
    what = str(data.get("text") or reason or "needs you")
    lines = []
    if data.get("detail"):
        lines.append(str(data["detail"]))
    lines.append(
        "Team run %s is waiting on you. Open it to answer; ticking this item "
        "off does not answer the run." % name
    )
    return {
        "text": _one_line("%s: %s" % (who, what)),
        "details": "\n\n".join(lines)[: store.MAX_DETAILS],
        "kind": "needs_you",
        "source": "run",
        "session": str(env.get("session") or data.get("title") or ""),
        "key": "run:%s:%s:%s" % (rid, task, reason),
    }


def _run_open(run: Optional[dict], task: str, reason: str, readable: bool):
    tr = _team_runs()
    if run is None:
        return False if readable else None
    if run.get("state") in tr.RUN_FINISHED:
        return False
    if task:
        t = next((t for t in run.get("tasks") or [] if t.get("id") == task), None)
        return (
            bool(t)
            and t.get("state") == "needs_you"
            and (not reason or t.get("reason") == reason)
        )
    # A group-level ask: still open while the group still waits on it.
    state = run.get("state")
    if reason == "budget":
        return bool(run.get("paused")) and run.get("pause_reason") == "budget"
    if reason == "plan":
        return state == "plan_ready"
    if reason == "release":
        return state == "release_ready"
    if reason == "check_failed":
        return state == "checking" and (run.get("check") or {}).get("state") == "failed"
    if reason == "lead_gone":
        try:
            return bool(tr.lead_gone(run, time.time()))
        except Exception:  # noqa: BLE001
            return None
    return True  # unknown ask: until the group finishes


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _open_auto_items(prefix: str = "") -> List[dict]:
    return [
        i
        for i in store.list_items()
        if not i.get("done")
        and i.get("source") in store.AUTO_SOURCES
        and str(i.get("key") or "").startswith(prefix or "")
        and i.get("key")
    ]


def resolve(prefix: str = "") -> List[str]:
    """Close every open auto item (whose key starts with ``prefix``) whose
    condition has positively ended → the keys closed."""
    items = _open_auto_items(prefix)
    if not items:
        return []
    ended: List[str] = []
    cache: Dict[str, object] = {}

    def once(name: str, fn: Callable[[], object]):
        if name not in cache:
            try:
                cache[name] = fn()
            except Exception as err:  # noqa: BLE001 — unreadable = unknown
                _log("todo auto: reading %s failed: %v", name, err)
                cache[name] = _UNKNOWN
        return cache[name]

    for item in items:
        key = str(item["key"])
        try:
            verdict = _still_open(key, once)
        except Exception as err:  # noqa: BLE001
            _log("todo auto: checking %s failed: %v", key, err)
            verdict = None
        if verdict is False:
            ended.append(key)
    if ended:
        store.close_items(ended)
    return ended


_UNKNOWN = object()


def _still_open(key: str, once) -> Optional[bool]:
    parts = key.split(":")
    src = parts[0]
    if src == "verify" and len(parts) >= 3:
        pid, sid = ":".join(parts[1:-1]), parts[-1]
        plans = once("plans", lambda: _test_plans().list_plans())
        if plans is _UNKNOWN:
            return None
        plan = once("plan:" + pid, lambda: _test_plans().get(pid))
        if plan is _UNKNOWN:
            return None
        return _verify_open(plan, sid, readable_nonempty(plans))
    if src == "flight" and len(parts) >= 4:
        fid, sid = parts[1], parts[2]
        try:
            since = int(parts[3])
        except ValueError:
            since = 0
        flights = once("flights", lambda: _flights().list_flights())
        if flights is _UNKNOWN:
            return None
        flight = once("flight:" + fid, lambda: _flights().get(fid))
        if flight is _UNKNOWN:
            return None
        return _flight_open(flight, sid, since, readable_nonempty(flights))
    if src == "run" and len(parts) >= 4:
        rid, task, reason = parts[1], parts[2], ":".join(parts[3:])
        runs = once("runs", lambda: _team_runs().list_runs())
        if runs is _UNKNOWN:
            return None
        run = once("run:" + rid, lambda: _team_runs().load(rid))
        if run is _UNKNOWN:
            return None
        return _run_open(run, task, reason, readable_nonempty(runs))
    return None


def readable_nonempty(rows) -> bool:
    """A store that read back at least one record is readable; an empty one
    may be missing or unreadable, so "not there" proves nothing."""
    return bool(rows)


# --------------------------------------------------------------------------- #
# The reconcile pass: make sure every open ask HAS its item
# --------------------------------------------------------------------------- #
def _verify_wanted() -> List[tuple]:
    """``[(item, since)]`` for every plan whose newest run waits on a person;
    ``since`` = when that run started."""
    out = []
    for plan in _test_plans().list_plans():
        run = _newest_run(plan)
        if str(run.get("outcome") or "") != "needs_you":
            continue
        since = float(run.get("at") or 0.0)
        out += [(item, since) for item in verify_items(plan)]
    return out


def _flight_wanted() -> List[tuple]:
    """``[(item, since)]`` for every flight parked on a person — a "you" stop
    that rings no bell (``notify: false``) included; ``since`` = when the
    stop started (already part of the key)."""
    out = []
    for flight in _flights().list_flights(include_finished=False):
        if flight.get("state") != "waiting_you":
            continue
        item = flight_item(flight)
        if item is not None:
            out.append((item, float(item["key"].rsplit(":", 1)[-1] or 0)))
    return out


def _run_asks(run: dict, tr) -> List[tuple]:
    """``[(reason, text, session)]`` the GROUP itself waits on you for."""
    state = run.get("state")
    lead = str((run.get("lead") or {}).get("title") or "")
    out = []
    if run.get("paused") and run.get("pause_reason") == "budget":
        out.append(("budget", "paused: its budget is spent", ""))
    if state == "plan_ready" and run.get("plan"):
        n = len((run.get("plan") or {}).get("pieces") or [])
        out.append(
            (
                "plan",
                "%s proposed %d pieces — approve them" % (lead or "the lead", n),
                lead,
            )
        )
    rel = run.get("release") or {}
    if (
        state == "release_ready"
        and rel.get("state") == "ready"
        and (run.get("policy") or {}).get("release") == "ask"
    ):
        out.append(("release", "one PR is ready to open", lead))
    if state == "checking" and (run.get("check") or {}).get("state") == "failed":
        out.append(("check_failed", "the check failed on the merged branch", lead))
    try:
        gone = tr.lead_gone(run, time.time())
    except Exception:  # noqa: BLE001
        gone = ""
    if gone:
        out.append(("lead_gone", gone, lead))
    return out


def _run_wanted() -> List[tuple]:
    """``[(item, since)]`` for every open team-run escalation: a member
    waiting on you for a reason the run announces, and the group's own asks.
    ``since`` = the member's incarnation (0 for the group's)."""
    tr = _team_runs()
    announced = getattr(tr, "ANNOUNCED_REASONS", None)
    out = []
    for run in tr.list_runs(include_finished=False):
        if run.get("state") in tr.RUN_FINISHED:
            continue
        rid, name = str(run.get("id") or ""), str(run.get("name") or "")
        for t in run.get("tasks") or []:
            reason = str(t.get("reason") or "")
            if t.get("state") != "needs_you" or (
                announced is not None and reason not in announced
            ):
                continue
            title = str(t.get("title") or "")
            try:
                ref = tr.ref_of(t) if t.get("kind") == "ticket" else title
            except Exception:  # noqa: BLE001
                ref = title
            text = str(t.get("detail") or reason)
            env = {
                "session": title,
                "data": {
                    "run": rid,
                    "name": name,
                    "task": t.get("id") or "",
                    "title": title,
                    "ref": ref,
                    "reason": reason,
                    "text": text,
                    "detail": "%s: %s %s" % (name, ref, text),
                },
            }
            item = run_item(env)
            if item is not None:
                out.append((item, float(t.get("incarnation") or 0.0)))
        for reason, text, session in _run_asks(run, tr):
            env = {
                "session": session,
                "data": {
                    "run": rid,
                    "name": name,
                    "task": "",
                    "title": session,
                    "reason": reason,
                    "text": text,
                    "detail": "%s: %s" % (name, text),
                },
            }
            item = run_item(env)
            if item is not None:
                out.append((item, 0.0))
    return out


def ensure() -> List[str]:
    """Add the item for every open ask that has none → the keys added.

    Covers what the event path cannot: a stop that rings no bell, a missed
    event, a restart. Same keys as the event path, and an ask whose key is
    already OPEN is left alone. Never brings back what a PERSON closed: a key
    somebody ticked off or deleted during this same ask (closed at or after
    its ``since``) stays closed — only a close the server made itself
    (``auto_closed``) or one from an earlier round does not count."""
    wanted: List[tuple] = []
    for name, fn in (
        ("verify", _verify_wanted),
        ("flights", _flight_wanted),
        ("runs", _run_wanted),
    ):
        try:
            wanted += fn()
        except Exception as err:  # noqa: BLE001 — one unreadable store skips its asks
            _log("todo auto: reading %s for the pass failed: %v", name, err)
    if not wanted:
        return []
    open_keys = {
        i.get("key") for i in store.list_items() if not i.get("done") and i.get("key")
    }
    closed = store.person_closed()
    add: List[dict] = []
    seen = set()
    for item, since in wanted:
        key = item["key"]
        if key in open_keys or key in seen:
            continue
        if key in closed and closed[key] >= since:
            continue
        seen.add(key)
        add.append(_clean(item))
    if not add:
        return []
    for start in range(0, len(add), store.MAX_BATCH):
        store.add_items(add[start : start + store.MAX_BATCH])
    return [a["key"] for a in add]


def reconcile() -> None:
    """The slow pass: close what ended, then add what is missing."""
    resolve()
    ensure()


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def handle(env: dict) -> None:
    """React to one bus envelope (on the worker thread; tests call it)."""
    name = str(env.get("event") or "")
    data = env.get("data") or {}
    if name in _VERIFY_EVENTS:
        pid = str(data.get("plan") or env.get("session") or "")
        if not pid:
            return
        resolve("verify:%s:" % pid)
        if name not in ("session.test_plan_checked", "session.test_plan_settled"):
            return
        plan = _test_plans().get(pid)
        if plan is None or str(_newest_run(plan).get("outcome") or "") != "needs_you":
            return
        wanted = verify_items(plan)
        if name == "session.test_plan_settled" and wanted[0]["key"] != (
            "verify:%s:confirm" % pid
        ):
            return  # steps still open: their items already say so
        store.add_items([_clean(i) for i in wanted])
        return
    if name in _FLIGHT_EVENTS:
        fid = str(data.get("id") or "")
        if not fid:
            return
        resolve("flight:%s:" % fid)
        if name != "flight.needs_you":
            return
        flight = _flights().get(fid)
        if flight is None:
            return
        item = flight_item(flight, str(data.get("text") or ""), data.get("items"))
        if item is not None:
            store.add_items([_clean(item)])
        return
    if name in _RUN_EVENTS:
        rid = str(data.get("run") or "")
        if not rid:
            return
        resolve("run:%s:" % rid)
        if name != "run.needs_you":
            return
        item = run_item(env)
        if item is not None:
            store.add_items([_clean(item)])
        return
    if name == "session.deleted":
        resolve()


def _clean(item: dict) -> dict:
    """Through the same validation as any other writer (drops empties)."""
    return store.clean_new({k: v for k, v in item.items() if v not in ("", None)})


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
_STOP = object()


class AutoSources:
    """Bus subscription + one daemon thread (events, then the slow pass)."""

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue(maxsize=2000)
        self._thread: Optional[threading.Thread] = None
        self._unsub: Optional[Callable[[], None]] = None

    def on_event(self, env: dict) -> None:
        """The bus callback — runs on the emitter's thread: queue and return."""
        try:
            if isinstance(env, dict) and env.get("event") in WATCHED:
                self._q.put_nowait(env)
        except Exception:  # noqa: BLE001 — a full queue: the slow pass catches up
            pass

    def start(self, subscribe: Callable) -> None:
        if self._thread is not None:
            return
        self._unsub = subscribe("*", self.on_event)
        self._thread = threading.Thread(
            target=self._run, name="mf-todo-auto", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                pass
            self._unsub = None
        try:
            self._q.put_nowait(_STOP)
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        due = time.time() + FIRST_RECONCILE_S
        while True:
            try:
                env = self._q.get(timeout=max(0.1, due - time.time()))
            except queue.Empty:
                env = None
            if env is _STOP:
                return
            if env is not None:
                try:
                    handle(env)
                except Exception as err:  # noqa: BLE001 — never stop the worker
                    _log("todo auto: %s failed: %v", env.get("event"), err)
            if time.time() >= due:
                try:
                    reconcile()
                    store.archive_now()
                except Exception as err:  # noqa: BLE001
                    _log("todo auto: reconcile failed: %v", err)
                due = time.time() + RECONCILE_EVERY_S
