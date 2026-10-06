"""Worker order: how an orchestrator's workers run relative to each other.

An orchestrator agent (any session that spawns workers through the MindFlock
MCP) can say *in what order* its workers run, and MindFlock enforces it — the
agent never has to remember to wait:

* **after** — a worker is created at once (its worktree, its fence, its agent)
  but its task is **held** until every session it runs after has finished.
* **one at a time / N at a time** — the orchestrator's ``mode``: ``serial``
  runs its workers one at a time, each after the one spawned before it;
  ``parallel`` runs them together, at most ``max_parallel`` at a time when
  that is set (the rest wait for a free slot, oldest first).
* **steps** — a plan declared up front: ``[["w1", "w2"], ["w3"]]`` runs w1 and
  w2 together, then w3 once both are done. Titles may name workers that are
  not spawned yet; they pick the order up when they are.
* **overlap** — a worker fenced to paths another unfinished worker of the
  same orchestrator may also change runs *after* it, automatically (unless
  the spawn says ``overlap: "parallel"``).

A held worker's task waits in its own prompt queue with the queue switched
off, so it is visible (and the user can start it by hand from the Queue tab);
release switches the queue on and the ordinary drain types it in once the
agent is idle. That is the whole delivery mechanism — nothing new types into
a terminal.

What "finished" means, per worker:

* ``done`` — it reported ``done`` (``report_result``) since it was created, or
  it is gone (closed / deleted: the orchestrator took its work or dropped it);
* ``stopped`` — it reported ``blocked`` or ``failed``: it frees its slot, but
  the workers after it stay held until it reports ``done`` or the
  orchestrator starts them anyway (``start_now``).

This module is the store plus the PURE planner (:func:`plan`) and the diagram
view (:func:`view`); ``worker_order_driver`` observes the live sessions and
carries the planner's actions out. Persisted at ``<config dir>/worker_order.json``
(or ``$MINDFLOCK_WORKER_ORDER_FILE``), keyed by the orchestrator's title, with
its creation time so a reused title never inherits a namesake's order.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard

__all__ = [
    "MODES",
    "MAX_PARALLEL",
    "store_path",
    "load",
    "edit",
    "parent_rec",
    "worker_rec",
    "parent_of_worker",
    "add_worker",
    "set_policy",
    "plan",
    "steps_of",
    "would_cycle",
    "view",
    "row_order",
    "forget_parent",
    "fence_text",
]

_FileName = "worker_order.json"
MODES = ("parallel", "serial")
#: The highest "N at a time" (the spawn cap bounds it in practice anyway).
MAX_PARALLEL = 16
#: Workers remembered per orchestrator (finished ones dropped first).
PER_PARENT = 60
#: Two creation times within this many seconds are the same session.
_SAME_S = 1.0

STATES = ("held", "running", "done", "stopped")

_LOCK = threading.RLock()


def store_path() -> str:
    """``$MINDFLOCK_WORKER_ORDER_FILE`` (tests point it at a tmp file), else
    ``<config dir>/worker_order.json``. Refused under pytest when it resolves
    into the real home (see ``backend.config.home_guard``)."""
    env = os.environ.get("MINDFLOCK_WORKER_ORDER_FILE")
    if env:
        return guard(env, "worker order")
    return guard(os.path.join(GetConfigDir(), _FileName), "worker order")


def _blank_doc() -> dict:
    return {"version": 1, "parents": {}}


def _load_raw() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _blank_doc()
    if not isinstance(data, dict) or not isinstance(data.get("parents"), dict):
        return _blank_doc()
    return data


def _save(data: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".order.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: (path, mtime_ns, size) → the parsed store: every row build reads it.
_CACHE: Dict[str, object] = {"key": None, "data": None}


def load() -> dict:
    """A fresh copy of the whole store (parsed once per change on disk)."""
    with _LOCK:
        path = store_path()
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            return _blank_doc()
        if _CACHE["key"] != key:
            _CACHE["data"] = _load_raw()
            _CACHE["key"] = key
        return copy.deepcopy(_CACHE["data"])


@contextmanager
def edit() -> Iterator[dict]:
    """Read-modify-write the store under the lock (saved on a clean exit)."""
    with _LOCK:
        data = _load_raw()
        yield data
        _save(data)
        _CACHE["key"] = None


def _same(a, b) -> bool:
    if a is None or b is None:
        return True
    try:
        return abs(float(a) - float(b)) < _SAME_S
    except (TypeError, ValueError):
        return True


def _blank_parent(created: Optional[float]) -> dict:
    return {
        "created": created,
        "mode": "parallel",
        "max_parallel": 0,
        "planned": {},
        "workers": {},
        "seq": 0,
    }


def parent_rec(
    data: dict, parent: str, created: Optional[float] = None, *, make: bool = False
) -> Optional[dict]:
    """``parent``'s record in ``data`` (created when ``make``), or None. A
    record left by an earlier session of the same title (a different creation
    time) is replaced, never inherited."""
    parents = data.setdefault("parents", {})
    rec = parents.get(parent)
    if isinstance(rec, dict) and not _same(rec.get("created"), created):
        rec = None
        if make:
            parents.pop(parent, None)
    if not isinstance(rec, dict):
        if not make:
            return None
        rec = parents[parent] = _blank_parent(created)
    for k, v in _blank_parent(created).items():
        rec.setdefault(k, v)
    if rec.get("created") is None and created is not None:
        rec["created"] = created
    return rec


def worker_rec(rec: Optional[dict], title: str) -> Optional[dict]:
    w = (rec or {}).get("workers", {}).get(title)
    return w if isinstance(w, dict) else None


def parent_of_worker(data: dict, title: str) -> Optional[str]:
    """The orchestrator whose order ``title`` is in, or None."""
    for parent, rec in (data.get("parents") or {}).items():
        if isinstance(rec, dict) and title in (rec.get("workers") or {}):
            return parent
    return None


def _clean_titles(titles: Iterable, *, exclude: str = "") -> List[str]:
    out: List[str] = []
    for t in titles or ():
        t = str(t or "").strip()
        if t and t != exclude and t not in out:
            out.append(t)
    return out


def _unfinished(rec: dict, *, but: str = "") -> List[Tuple[int, str, dict]]:
    out = []
    for t, w in (rec.get("workers") or {}).items():
        if t != but and isinstance(w, dict) and w.get("state") in ("held", "running"):
            out.append((int(w.get("seq") or 0), t, w))
    out.sort()
    return out


def add_worker(
    rec: dict,
    title: str,
    *,
    created: Optional[float],
    after: Iterable[str] = (),
    why: Optional[Mapping[str, str]] = None,
    fence: Optional[dict] = None,
    overlaps: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> dict:
    """Register a new worker of ``rec``'s orchestrator and decide what it runs
    after: what the spawn asked (``after``), what a declared step says
    (``rec["planned"]``), the one spawned just before it in ``serial`` mode,
    and every unfinished worker its fence overlaps (``overlaps``: title →
    the shared path, already computed by the caller). Always registered
    ``held`` — the next :func:`plan` pass releases it at once when nothing
    holds it (and its fence, if any, is in place)."""
    now = time.time() if now is None else now
    reasons: Dict[str, str] = {}
    for t in _clean_titles(after, exclude=title):
        reasons[t] = (why or {}).get(t) or "asked"
    for t in _clean_titles((rec.get("planned") or {}).get(title) or (), exclude=title):
        reasons.setdefault(t, "step")
    if rec.get("mode") == "serial":
        prev = _unfinished(rec, but=title)
        if prev:
            reasons.setdefault(prev[-1][1], "one at a time")
    for t, path in (overlaps or {}).items():
        if t != title:
            reasons.setdefault(t, "overlap: " + str(path)[:120])
    rec["seq"] = int(rec.get("seq") or 0) + 1
    w = {
        "seq": rec["seq"],
        "created": created,
        "after": list(reasons),
        "why": reasons,
        "state": "held",
        "held_at": now,
        "released_at": None,
        "ended_at": None,
        "how": "",
        "note": "",
    }
    if fence:
        w["fence"] = dict(fence)
    rec.setdefault("workers", {})[title] = w
    _trim(rec)
    return w


def _trim(rec: dict) -> None:
    workers = rec.get("workers") or {}
    if len(workers) <= PER_PARENT:
        return
    done = sorted(
        (int(w.get("seq") or 0), t)
        for t, w in workers.items()
        if isinstance(w, dict) and w.get("state") in ("done", "stopped")
    )
    for _seq, t in done[: len(workers) - PER_PARENT]:
        workers.pop(t, None)


def set_policy(
    rec: dict,
    *,
    mode: Optional[str] = None,
    max_parallel: Optional[int] = None,
    steps: Optional[List[List[str]]] = None,
    after: Optional[Mapping[str, Iterable[str]]] = None,
    start_now: Iterable[str] = (),
) -> List[str]:
    """Apply an orchestrator's order change. → problems (empty = applied).

    ``steps`` replaces the declared plan: every title in step k runs after
    every title in step k-1 — for workers already held as well as ones spawned
    later. ``after`` sets one worker's dependencies outright (a held worker
    only: a running one has started). ``start_now`` releases held workers at
    the next pass whatever they wait on. Nothing is half-applied: a cycle or
    an unknown mode refuses the whole change."""
    problems: List[str] = []
    if mode is not None and mode not in MODES:
        problems.append("mode must be 'parallel' or 'serial'")
    if max_parallel is not None:
        try:
            mp = int(max_parallel)
        except (TypeError, ValueError):
            mp = -1
        if mp < 0 or mp > MAX_PARALLEL:
            problems.append("max_parallel must be 0 (no limit) to %d" % MAX_PARALLEL)
    workers = rec.get("workers") or {}
    deps: Dict[str, List[str]] = {
        t: list(w.get("after") or []) for t, w in workers.items() if isinstance(w, dict)
    }
    planned: Dict[str, List[str]] = dict(rec.get("planned") or {})
    if steps is not None:
        planned = {}
        prev: List[str] = []
        seen: set = set()
        for i, step in enumerate(steps):
            names = _clean_titles(step if isinstance(step, list) else [step])
            if not names:
                problems.append("step %d is empty" % (i + 1))
                continue
            for n in names:
                if n in seen:
                    problems.append("%s is in more than one step" % n)
                seen.add(n)
                planned[n] = list(prev)
            prev = names
        for t, pre in planned.items():
            w = workers.get(t)
            if isinstance(w, dict) and w.get("state") == "held":
                deps[t] = _clean_titles(list(deps.get(t) or []) + pre, exclude=t)
    for t, pre in (after or {}).items():
        w = workers.get(t)
        if not isinstance(w, dict):
            problems.append("%s is not one of your workers" % t)
            continue
        if w.get("state") != "held":
            problems.append("%s has already started" % t)
            continue
        deps[t] = _clean_titles(pre, exclude=t)
    graph = dict(planned)
    graph.update(deps)
    cyc = would_cycle(graph)
    if cyc:
        problems.append("that order has a cycle: %s" % " → ".join(cyc))
    for t in _clean_titles(start_now):
        w = workers.get(t)
        if not isinstance(w, dict):
            problems.append("%s is not one of your workers" % t)
        elif w.get("state") != "held":
            problems.append("%s has already started" % t)
    if problems:
        return problems
    if mode is not None:
        rec["mode"] = mode
    if max_parallel is not None:
        rec["max_parallel"] = int(max_parallel)
    rec["planned"] = planned
    for t, w in workers.items():
        if not isinstance(w, dict) or t not in deps or w.get("state") != "held":
            continue
        new = deps[t]
        why = dict(w.get("why") or {})
        for p in new:
            if p not in why:
                why[p] = "step" if p in (planned.get(t) or ()) else "asked"
        w["why"] = {p: why[p] for p in new}
        w["after"] = new
    for t in _clean_titles(start_now):
        workers[t]["start_now"] = True
    return []


def would_cycle(graph: Mapping[str, Iterable[str]]) -> List[str]:
    """A dependency cycle in ``{node: [nodes it runs after]}`` as a path
    (``[a, b, a]``), or ``[]``."""
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n: str) -> List[str]:
        state[n] = 1
        stack.append(n)
        for m in graph.get(n) or ():
            if state.get(m) == 1:
                return stack[stack.index(m) :] + [m]
            if state.get(m) is None:
                got = visit(m)
                if got:
                    return got
        stack.pop()
        state[n] = 2
        return []

    for n in list(graph):
        if state.get(n) is None:
            got = visit(n)
            if got:
                return got
    return []


# --------------------------------------------------------------------------- #
# The planner (PURE)
# --------------------------------------------------------------------------- #
def _pred_state(rec: dict, pred: str, obs: Mapping[str, dict]) -> str:
    """``done`` / ``stopped`` / ``running`` / ``held`` / ``missing`` (never
    spawned yet) for one predecessor."""
    w = worker_rec(rec, pred)
    if w is not None:
        return str(w.get("state") or "held")
    o = obs.get(pred)
    if o is None:
        return "missing"
    if o.get("exists") is False:
        return "done"
    rep = o.get("report")
    if rep == "done":
        return "done"
    if rep in ("blocked", "failed"):
        return "stopped"
    return "running"


def waiting_on(rec: dict, title: str, obs: Mapping[str, dict]) -> List[Tuple[str, str]]:
    """``[(pred, its state)]`` for the predecessors ``title`` still waits on."""
    w = worker_rec(rec, title) or {}
    out = []
    for p in w.get("after") or []:
        st = _pred_state(rec, p, obs)
        if st != "done":
            out.append((p, st))
    return out


def cap_of(rec: dict) -> int:
    """How many may run at once (0 = no limit)."""
    if rec.get("mode") == "serial":
        return 1
    try:
        return max(0, int(rec.get("max_parallel") or 0))
    except (TypeError, ValueError):
        return 0


def plan(rec: dict, obs: Mapping[str, dict]) -> List[Tuple[str, str, str]]:
    """The actions one pass takes for an orchestrator: ``[(kind, title,
    detail)]`` with kind ``release`` / ``done`` / ``stopped`` / ``resumed`` /
    ``forget``. PURE: ``obs`` maps worker title → ``{"exists": bool,
    "report": "done"|"blocked"|"failed"|None (since it was created/released),
    "fenced": bool (its fence is in place, or it has none)}``.

    Order of a pass: settle running workers first (a report or a gone
    session frees its slot), then release held ones oldest first while slots
    last — a ``start_now`` one ignores both its predecessors and the cap."""
    acts: List[Tuple[str, str, str]] = []
    workers = rec.get("workers") or {}
    states = {t: w.get("state") for t, w in workers.items() if isinstance(w, dict)}
    for t, w in sorted(workers.items(), key=lambda kv: int(kv[1].get("seq") or 0)):
        if not isinstance(w, dict):
            continue
        o = obs.get(t) or {}
        st = states[t]
        if o.get("exists") is False:
            if st == "held":
                acts.append(("forget", t, "deleted before it started"))
                states[t] = "gone"
            elif st in ("running", "stopped"):
                acts.append(("done", t, "gone"))
                states[t] = "done"
            continue
        rep = o.get("report")
        if st == "running" and rep == "done":
            acts.append(("done", t, "reported done"))
            states[t] = "done"
        elif st == "running" and rep in ("blocked", "failed"):
            acts.append(("stopped", t, "reported " + rep))
            states[t] = "stopped"
        elif st == "stopped" and rep == "done":
            acts.append(("done", t, "reported done"))
            states[t] = "done"
    cap = cap_of(rec)
    running = sum(1 for s in states.values() if s == "running")
    view_rec = dict(
        rec,
        workers={
            t: dict(w, state=states.get(t, w.get("state")))
            for t, w in workers.items()
            if isinstance(w, dict) and states.get(t) != "gone"
        },
    )
    for t, w in sorted(workers.items(), key=lambda kv: int(kv[1].get("seq") or 0)):
        if not isinstance(w, dict) or states.get(t) != "held":
            continue
        o = obs.get(t) or {}
        if not o.get("fenced", True):
            continue  # its fence lands first — never a minute unfenced
        if w.get("start_now"):
            acts.append(("release", t, "started by hand"))
            states[t] = "running"
            running += 1
            continue
        if waiting_on(view_rec, t, obs):
            continue
        if cap and running >= cap:
            continue
        acts.append(("release", t, ""))
        states[t] = "running"
        running += 1
    return acts


# --------------------------------------------------------------------------- #
# The diagram view
# --------------------------------------------------------------------------- #
def steps_of(rec: dict) -> Dict[str, int]:
    """Each worker's (and each declared, not-yet-spawned title's) step: 1 for
    one that runs after nothing, else one more than its latest predecessor.
    Cycles (refused on input, but a hand-edited store) count as step 1."""
    graph: Dict[str, List[str]] = {
        t: list(p) for t, p in (rec.get("planned") or {}).items()
    }
    for t, w in (rec.get("workers") or {}).items():
        if isinstance(w, dict):
            graph[t] = list(w.get("after") or [])
    memo: Dict[str, int] = {}

    def step(n: str, path: Tuple[str, ...]) -> int:
        if n in memo:
            return memo[n]
        if n in path:
            return 1
        pre = [p for p in graph.get(n) or () if p in graph]
        got = 1 + max((step(p, path + (n,)) for p in pre), default=0)
        memo[n] = got
        return got

    for n in graph:
        step(n, ())
    return memo


def _word(
    w: dict, waits: List[Tuple[str, str]], cap: int, rec: dict
) -> Tuple[str, str]:
    """``(state word, one-line detail)`` for the diagram and the row."""
    st = w.get("state")
    if st == "held":
        if w.get("start_now"):
            return "starting", "started by hand"
        if waits:
            parts = []
            for p, s in waits[:3]:
                parts.append(
                    p
                    + {
                        "stopped": " (stopped — it reported blocked/failed)",
                        "missing": " (not started yet)",
                        "held": " (waiting itself)",
                    }.get(s, "")
                )
            more = " +%d" % (len(waits) - 3) if len(waits) > 3 else ""
            return "waiting", "after " + ", ".join(parts) + more
        if cap:
            return "waiting", "for a free slot (%d at a time)" % cap
        return "starting", ""
    if st == "running":
        return "running", w.get("note") or ""
    if st == "stopped":
        return "stopped", w.get("how") or ""
    return "done", w.get("how") or ""


def view(rec: Optional[dict], obs: Mapping[str, dict]) -> Optional[dict]:
    """The orchestrator's order as the Thread's diagram reads it, or None when
    it has never ordered or held anything:
    ``{mode, max_parallel, cap, steps: [{n, workers: [{title, state, word,
    detail, after, why, fence, released_at, ended_at, planned}]}]}``. A title
    declared in a step but not spawned yet is listed with ``planned: true``."""
    if not rec:
        return None
    workers = {
        t: w for t, w in (rec.get("workers") or {}).items() if isinstance(w, dict)
    }
    planned = rec.get("planned") or {}
    if not workers and not planned:
        return None
    cap = cap_of(rec)
    steps = steps_of(rec)
    cards: Dict[int, List[dict]] = {}
    for t, w in sorted(workers.items(), key=lambda kv: int(kv[1].get("seq") or 0)):
        waits = waiting_on(rec, t, obs) if w.get("state") == "held" else []
        word, detail = _word(w, waits, cap, rec)
        card = {
            "title": t,
            "state": w.get("state"),
            "word": word,
            "detail": detail,
            "after": list(w.get("after") or []),
            "why": dict(w.get("why") or {}),
            "fence": w.get("fence") or None,
            "released_at": w.get("released_at"),
            "ended_at": w.get("ended_at"),
            "planned": False,
        }
        cards.setdefault(steps.get(t, 1), []).append(card)
    for t, pre in planned.items():
        if t in workers:
            continue
        cards.setdefault(steps.get(t, 1), []).append(
            {
                "title": t,
                "state": "planned",
                "word": "not started",
                "detail": ("after " + ", ".join(pre)) if pre else "",
                "after": list(pre),
                "why": {p: "step" for p in pre},
                "fence": None,
                "released_at": None,
                "ended_at": None,
                "planned": True,
            }
        )
    return {
        "mode": rec.get("mode") or "parallel",
        "max_parallel": int(rec.get("max_parallel") or 0),
        "cap": cap,
        "steps": [{"n": n, "workers": cards[n]} for n in sorted(cards)],
    }


def row_order(
    rec: Optional[dict], title: str, obs: Mapping[str, dict]
) -> Optional[dict]:
    """``{state, word, detail, after, parent}`` for one worker's row (what
    ``wait_for_session`` and the rail read), or None when it has no order."""
    w = worker_rec(rec, title)
    if w is None:
        return None
    waits = waiting_on(rec, title, obs) if w.get("state") == "held" else []
    word, detail = _word(w, waits, cap_of(rec or {}), rec or {})
    return {
        "state": w.get("state"),
        "word": word,
        "detail": detail,
        "after": list(w.get("after") or []),
        "fence": w.get("fence") or None,
    }


def forget_parent(data: dict, parent: str) -> None:
    (data.get("parents") or {}).pop(parent, None)


def fence_text(fence: Optional[Mapping], by: str = "") -> str:
    """The lines a worker's task starts with when it is fenced."""
    if not fence:
        return ""
    only = list(fence.get("only") or [])
    out = list(fence.get("keep_out") or [])
    if not only and not out:
        return ""
    who = ("set by %s" % by) if by else "set by your orchestrator"
    lines = ["MindFlock fence (%s) — enforced on every edit:" % who]
    if only:
        lines.append("- you may change ONLY: " + ", ".join(only))
    if out:
        lines.append("- keep OUT of (read-only for you): " + ", ".join(out))
    if fence.get("reason"):
        lines.append("- why: " + str(fence["reason"])[:300])
    lines.append(
        "If the task needs a change outside the fence, finish what is inside it "
        "and say what else is needed in your report instead of working around it."
    )
    return "\n".join(lines)
