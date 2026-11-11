"""Carries out :mod:`worker_order`: registers workers as they are created,
fences them, releases held tasks when their turn comes, and sweeps.

* :func:`on_create` — called by the create route right after it claims the
  title: decides whether the new worker is ordered (an ``after``, a fence,
  a step that names it, an orchestrator in ``serial`` / capped mode, or a
  fence that overlaps an unfinished sibling's) and, if so, registers it
  ``held``. The route then parks the task in the worker's prompt queue with
  the queue switched off, and creates the session WITHOUT its agent
  (:func:`defer_agent`): worktree, branch, fence and queued task exist, but
  no agent CLI runs (no tmux, no memory) until its turn.
* :func:`set_fence` — the ``POST /api/instances/{t}/fence`` route: a
  per-session fence (``red_zones.set_session_fence``, keyed by the worker's
  tmux name, owner ``orch:<parent>``) — "only here" and/or "keep out", for
  that session alone, never for the orchestrator or anyone else sharing its
  folder.
* :func:`tick` — every few seconds from the server's loop: lands pending
  fences, observes each worker (alive? reported?), applies
  :func:`worker_order.plan`, and on ``release`` fast-forwards the worker's
  fresh branch onto what it runs after (when that is safe — its agent has
  not started, so nothing uses the tree), starts its agent through the
  launch gate as an automatic start (:func:`start_on_turn`: it waits in the
  gate's queue while memory is short), prefixes the task with the fence and
  the hand-over, and switches its queue on — the ordinary drain types it in
  once the agent is idle.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from backend.web.core import launch_gate as _launch_gate
from backend.web.core import worker_order as _order

__all__ = [
    "on_create",
    "defer_agent",
    "agent_deferred",
    "mark_never_ran",
    "start_agent",
    "start_on_turn",
    "TURN_KIND",
    "set_fence",
    "clear_fence",
    "set_order",
    "tick",
    "observe",
    "order_view",
    "row_order",
    "wake",
    "run_loop",
    "FENCE_OWNER",
]

#: The owner tag on every fence an orchestrator set (bulk drop, the sweep).
FENCE_OWNER = "orch:"
TICK_S = 3.0
#: A held task with no order record is released once its session is this old.
ORPHAN_GRACE_S = 30.0
#: A running worker whose queue is still parked is healed (switched on) only
#: this long after its release was claimed: until then the claiming pass may
#: still be writing the preface (a fast-forward can take a minute).
RELEASE_GRACE_S = 120.0

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WAKE: Optional[asyncio.Event] = None
#: One tick at a time (the loop and a route-triggered pass).
_TICK_LOCK = threading.Lock()


def _server():
    from backend.web import server

    return server


def _created(inst) -> Optional[float]:
    try:
        return _server()._created_epoch(inst)
    except Exception:  # noqa: BLE001
        return None


def _tmux_of(title: str) -> str:
    from backend.session.tmux import tmux as _tmux

    return _tmux.to_mindflock_tmux_name(title)


def _wt_of(inst) -> str:
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    return wt if wt and os.path.isdir(wt) else ""


def clean_patterns(raw) -> Tuple[List[str], List[str]]:
    """``(patterns, problems)`` for a fence's path list — the zone matcher's
    own validation, so what is accepted here is what the guard enforces."""
    from backend.config import red_zones as _rz

    out: List[str] = []
    problems: List[str] = []
    if raw is None:
        return out, problems
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return out, ["paths must be a list of globs"]
    for p in raw[:50]:
        p = str(p or "").strip()
        if not p:
            continue
        try:
            norm, anchored = _rz.normalize_pattern(p)
            pat = ("/" + norm) if anchored else norm
            _rz.compile_pattern(pat)
        except (ValueError, TypeError) as err:
            problems.append("%s: %s" % (p, err))
            continue
        if pat not in out:
            out.append(pat)
    return out, problems


def fence_from(payload: Mapping) -> Tuple[Optional[dict], List[str]]:
    """``({only, keep_out, reason} | None, problems)`` from a request."""
    f = payload.get("fence") if isinstance(payload.get("fence"), dict) else payload
    only, p1 = clean_patterns(f.get("only"))
    out, p2 = clean_patterns(f.get("keep_out"))
    problems = p1 + p2
    both = sorted(set(only) & set(out))
    if both:
        problems.append("%s is both only-here and keep-out" % ", ".join(both))
    if not only and not out:
        return None, problems
    fence = {"only": only, "keep_out": out}
    reason = str(f.get("reason") or "").strip()
    if reason:
        fence["reason"] = reason[:300]
    return fence, problems


def _shares_folder(srv, title: str, wt: str) -> bool:
    """Whether another live session works in ``wt`` too (an in-place worker
    in its orchestrator's folder, a copy window)."""
    real = os.path.realpath(wt)
    for t, other in list(srv.ENGINE.instances.items()):
        if t == title:
            continue
        try:
            owt = other.GetWorktreePath()
        except Exception:  # noqa: BLE001
            continue
        if owt and os.path.realpath(owt) == real:
            return True
    return False


# --------------------------------------------------------------------------- #
# Fences
# --------------------------------------------------------------------------- #
def _apply_fence(
    srv, title: str, inst, fence: dict, by: str
) -> Tuple[bool, List[str], dict]:
    """Write ``title``'s fence and its folder's guard. → ``(applied,
    problems, where)``; not applied (no problems) while its folder does not
    exist yet."""
    wt = _wt_of(inst)
    if not wt:
        return False, [], {}
    tmux = _tmux_of(title)
    shared = _shares_folder(srv, title, wt)
    label = ("only here (set by %s)" % by) if by else "only here"
    added, problems = srv._red_zones.set_session_fence(
        wt,
        tmux,
        fence.get("only") or [],
        red=fence.get("keep_out") or [],
        name=(
            label
            if fence.get("only")
            else ("kept out by %s" % by if by else "kept out")
        ),
        owner=FENCE_OWNER + (by or ""),
        no_commit=False,
        by=by,
        reason=str(fence.get("reason") or ""),
        companions=not shared,
    )
    if not added:
        return False, problems or ["no path could be fenced"], {}
    try:
        srv._red_zones.sync_guard(os.path.realpath(wt), lroot=wt)
    except Exception as err:  # noqa: BLE001 — the monitor's tick re-syncs it
        problems.append("the guard could not be written yet (%s)" % err)
    return True, problems, {"wt": os.path.realpath(wt), "shared": shared}


def _drop_fence(srv, title: str, wt: str, **why: str) -> None:
    """Drop ``title``'s fence in ``wt``; ``why`` (``cause``, ``by``,
    ``reason``) goes in the fence log so the Map says why it went."""
    if not wt:
        return
    if srv._red_zones.drop_session_fence(wt, _tmux_of(title), **why):
        try:
            srv._red_zones.sync_guard(wt, lroot=wt)
        except Exception:  # noqa: BLE001
            pass


def set_fence(title: str, payload: Mapping) -> Tuple[int, dict]:
    """``POST /api/instances/{title}/fence``: ``{only, keep_out, reason, by,
    clear}`` → ``(status, body)``. Replaces the session's fence (``clear``:
    removes it). A held worker gets it in front of its task when it starts;
    a running one is told (queued mid-turn)."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return 404, {"error": "unknown session: %s" % title}
    try:
        in_run = bool(srv._row_run(title))
    except Exception:  # noqa: BLE001
        in_run = False
    if in_run:
        return 409, {
            "error": "%s belongs to a team run, which fences it (the plan's "
            "paths) — change the plan, not the session" % title
        }
    by = str(payload.get("by") or "").strip()
    why = str(payload.get("reason") or "").strip()[:300]
    if payload.get("clear"):
        fence, problems = None, []
    else:
        fence, problems = fence_from(payload)
        if problems:
            return 400, {"error": "; ".join(problems)}
        if fence is None:
            return 400, {
                "error": "give only=[globs] and/or keep_out=[globs] (or clear=true)"
            }
    parent = str(getattr(inst, "Parent", "") or "")
    created = _created(inst)
    applied, where = False, {}
    with _order.edit() as data:
        rec = _order.parent_rec(data, parent, None) if parent else None
        w = _order.worker_rec(rec, title)
        if w is None and parent:
            pinst = srv.ENGINE.instances.get(parent)
            rec = _order.parent_rec(data, parent, _created(pinst), make=True)
            w = _order.add_worker(rec, title, created=created)
            # Spawned before it was ordered: it is already running.
            w.update(state="running", released_at=time.time(), after=[], why={})
        old_wt = ((w or {}).get("fence") or {}).get("wt") or ""
        if w is not None:
            if fence is None:
                w.pop("fence", None)
            else:
                w["fence"] = dict(fence, by=by)
        held = bool(w is not None and w.get("state") == "held")
    if fence is None:
        cur = _wt_of(inst)
        for wt in {old_wt, os.path.realpath(cur) if cur else ""} - {""}:
            _drop_fence(srv, title, wt, cause="lifted", by=by, reason=why)
    problems: List[str] = []
    if fence is not None:
        applied, problems, where = _apply_fence(srv, title, inst, fence, by)
        if applied:
            with _order.edit() as data:
                rec = _order.parent_rec(data, parent, None) if parent else None
                w = _order.worker_rec(rec, title)
                if w is not None and w.get("fence") is not None:
                    w["fence"].update(applied=True, **where)
    told = None
    if not held:
        msg = (
            _order.fence_text(fence, by)
            if fence
            else "MindFlock: your fence was lifted%s%s — the folder's own zones "
            "still apply."
            % ((" by %s" % by) if by else "", (": %s" % why) if why else "")
        )
        try:
            told, _reason = srv._deliver_to_agent(inst, title, msg)
        except Exception:  # noqa: BLE001 — the fence holds either way
            told = False
    wake()
    body = {"ok": True, "fence": fence, "applied": applied, "held": held}
    if where:
        body["shared_folder"] = bool(where.get("shared"))
    if problems:
        body["problems"] = problems
    if told is not None:
        body["told"] = told
    if fence is not None and not applied and not problems:
        body["note"] = (
            "its folder is not ready yet: the fence lands before its task starts"
        )
    return 200, body


def clear_fence(title: str, by: str = "") -> Tuple[int, dict]:
    return set_fence(title, {"clear": True, "by": by})


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def on_create(
    parent: str,
    title: str,
    inst,
    payload: Mapping,
    files: Optional[List[str]] = None,
    prompt: str = "",
) -> Tuple[Optional[dict], Optional[str]]:
    """Register a new session in its orchestrator's order when it needs one.
    → ``(order | None, error | None)``; ``order`` = ``{"held": True, "after",
    "why", "fence"}`` when the route must park the task.

    An unordered spawn of an orchestrator that never ordered anything stays
    exactly as before (None): no record, no held task, no write. A team run's
    member (``run_member``) is never ordered here — its group runs it. A
    fenced session with no parent is kept under the parent key ``""``: held
    only until its fence lands."""
    if payload.get("run_member"):
        return None, None
    srv = _server()
    fence, problems = fence_from(payload)
    if problems:
        return None, "; ".join(problems)
    after_raw = payload.get("after")
    if isinstance(after_raw, str):
        after_raw = [after_raw]
    after = [
        str(a).strip()
        for a in (after_raw if isinstance(after_raw, list) else [])
        if str(a or "").strip()
    ]
    if not parent and after:
        return None, "after needs a parent session (an orchestrator's worker)"
    if parent and parent in after:
        return None, (
            "a worker can't run after its own orchestrator (it never reports) — "
            "name the workers it waits for"
        )
    pinst = srv.ENGINE.instances.get(parent) if parent else None
    pcreated = _created(pinst) if parent else None
    # Decide on a snapshot: most spawns are not ordered and must not write.
    rec = _order.parent_rec(_order.load(), parent, pcreated) if parent else None
    ordered = rec is not None and bool(
        rec.get("mode") == "serial"
        or _order.cap_of(rec)
        or title in (rec.get("planned") or {})
    )
    overlaps: Dict[str, str] = {}
    if (
        rec is not None
        and fence
        and fence.get("only")
        and str(payload.get("overlap") or "wait") != "parallel"
    ):
        for _seq, t, w in _order._unfinished(rec, but=title):
            other = (w.get("fence") or {}).get("only") or []
            shared = srv._team_runs.paths_overlap(fence["only"], other, files or [])
            if shared:
                overlaps[t] = shared
    if not (after or fence or ordered or overlaps):
        return None, None
    known = set(srv.ENGINE.instances)
    if rec is not None:
        known |= set(rec.get("workers") or {}) | set(rec.get("planned") or {})
    unknown = [a for a in after if a not in known]
    if unknown:
        return None, "after names no session of yours: %s" % ", ".join(unknown)
    if _order.would_cycle(
        dict(
            {
                t: x.get("after") or []
                for t, x in ((rec or {}).get("workers") or {}).items()
            },
            **{title: after},
        )
    ):
        return None, "that order has a cycle"
    # Park the task BEFORE the record exists: a pass that sees the worker
    # held always finds its task parked (never an empty queue it would
    # "release", leaving the task stuck behind a held flag).
    parked = False
    text = str(prompt or "").strip()
    if text:
        try:
            srv._prompt_queue.set_flags(title, enabled=False, held=True)
            srv._prompt_queue.enqueue(title, text)
            parked = True
        except Exception:  # noqa: BLE001 — the route seeds it instead
            try:
                srv._prompt_queue.set_flags(title, held=False)
            except Exception:  # noqa: BLE001
                pass
    with _order.edit() as data:
        live = _order.parent_rec(data, parent, pcreated, make=True)
        w = _order.add_worker(
            live,
            title,
            created=_created(inst),
            after=after,
            fence=dict(fence, by=parent) if fence else None,
            overlaps=overlaps,
        )
        if not parked:
            # Nothing to hold (no task, or the queue refused it): it starts
            # now; its fence still lands on the next pass.
            w.update(state="running", released_at=time.time())
        out = {
            "held": parked,
            "after": list(w["after"]),
            "why": dict(w["why"]),
            "fence": fence,
        }
        # The cap is the orchestrator's standing policy — often set for an
        # earlier batch and forgotten. Say so when it is what holds this one.
        cap = _order.cap_of(live)
        busy = sum(
            1
            for _s, t, x in _order._unfinished(live, but=title)
            if x.get("state") == "running"
        )
        if parked and cap and busy >= cap:
            out["waits_for_slot"] = {
                "cap": cap,
                "mode": str(live.get("mode") or "parallel"),
                "running": busy,
            }
    wake()
    return out, None


# --------------------------------------------------------------------------- #
# Observing + the pass
# --------------------------------------------------------------------------- #
def observe(rec: dict, instances: Mapping, parent: str = "") -> Dict[str, dict]:
    """``{title: {"exists", "report", "fenced"}}`` for every worker (and
    every live session an ``after`` names). A report is the newest one a
    worker left ``parent`` — read from its mailbox, so it still counts when
    the orchestrator's window has closed."""
    from backend.web.core import mailbox as _mailbox
    from backend.web.core import thread as _thread

    obs: Dict[str, dict] = {}
    disk: list = []  # read once, only when a worker is missing here
    names = set(rec.get("workers") or {})
    for w in (rec.get("workers") or {}).values():
        if isinstance(w, dict):
            names.update(w.get("after") or [])
    names.update(rec.get("planned") or {})
    for t in names:
        w = _order.worker_rec(rec, t)
        inst = instances.get(t)
        if inst is None:
            # A worker that was registered and is gone now is gone; a name
            # no session ever had is "missing" (not spawned yet). But "not in
            # THIS server's engine" is not "gone": a co-running server (the
            # dev launcher next to the service) shares this store, and it
            # created the worker a moment ago — it reaches our engine only
            # on the next state sync. Reading that as gone forgot a held
            # worker (its task parked for ever) and finished a running one.
            if w is not None:
                if not disk:
                    disk.append(_live_elsewhere())
                if _alive_elsewhere(t, w, disk[0]):
                    obs[t] = {"exists": None, "elsewhere": True}
                else:
                    obs[t] = {"exists": False}
            continue
        if w is not None and not _order._same(w.get("created"), _created(inst)):
            if _born(w) > float(_created(inst) or 0):
                # The RECORD is the newer one: the title was deleted and spawned
                # again on the other server, and this engine still holds the
                # old copy until its next sync. Judge the record as a missing
                # worker, never as gone.
                if not disk:
                    disk.append(_live_elsewhere())
                if _alive_elsewhere(t, w, disk[0], local_stale=True):
                    obs[t] = {"exists": None, "elsewhere": True}
                else:
                    obs[t] = {"exists": False}
            else:
                obs[t] = {"exists": False}  # its namesake, not it
            continue
        rep = None
        try:
            if parent:
                got = _thread.report_json(
                    _mailbox.last_result(parent, t, since=_created(inst))
                )
            else:
                got = _thread.last_report(instances, t)
        except Exception:  # noqa: BLE001
            got = None
        if got:
            since = float((w or {}).get("released_at") or (w or {}).get("held_at") or 0)
            if float(got.get("ts") or 0) >= since - 1:
                rep = str(got.get("status") or "") or None
        fence = (w or {}).get("fence")
        obs[t] = {
            "exists": True,
            "report": rep,
            "fenced": not fence or bool(fence.get("applied")),
        }
    return obs


#: A session reaches state.json only once it has STARTED, which for a
#: provisioned workspace can take minutes: until then a worker missing from
#: this server's engine, from the disk, and not tombstoned is the other
#: server's work in progress for this long. A deleted (or closed) session is
#: tombstoned, so it reads gone at once whatever its age.
ELSEWHERE_GRACE_S = 3600.0


def _live_elsewhere():
    """state.json's view of every server's sessions (``engine.DiskView``)."""
    from backend.web.core import engine as _engine

    return _engine.disk_view()


def _born(w: Mapping) -> float:
    try:
        return float(w.get("created") or w.get("held_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _young(w: Mapping) -> bool:
    return time.time() - _born(w) < ELSEWHERE_GRACE_S


def _deleted(t: str, w: Mapping, view) -> bool:
    """``t``'s tombstone is THIS worker's: newer than its creation. Titles are
    reused (review-1, review-2 every pass): an older tombstone belongs to a
    namesake and says nothing about a worker created since."""
    ts = view.tombstoned.get(t)
    return ts is not None and float(ts) > _born(w)


def _alive_elsewhere(
    t: str,
    w: Optional[Mapping],
    view,
    dead: Optional[set] = None,
    local_stale: bool = False,
) -> bool:
    """``t`` (missing from this engine, or held here only as an older copy:
    ``local_stale``) is still some server's session. ``dead``: the titles
    :func:`_looks_dead` already answered for (worked out before a lock)."""
    if w is not None and _deleted(t, w, view):
        return False
    if w is None and t in view.tombstoned:
        return False
    if t in view.live and not local_stale:
        # Saved — unless it is an entry no server will ever adopt again
        # (workspace and tmux both gone), which state.json keeps listing
        # until the next save.
        return not (t in dead if dead is not None else _looks_dead(t))
    if t in view.live:
        return True  # the disk entry may be the old copy or the new one
    return w is not None and _young(w)


def _looks_dead(t: str) -> bool:
    from backend.web.core import engine as _engine

    return _engine.looks_dead_on_disk(t)


def alive_anywhere(local: Iterable[str]) -> set:
    """``local`` (this engine's titles) plus every title another server
    still holds: saved and undeleted, or an unfinished worker of an order
    that is not tombstoned (one still being created is on no disk yet). For
    the loops that prune per-title stores (prompt queues, port blocks)."""
    view = _live_elsewhere()
    out = set(local) | set(view.live)
    for rec in (_order.load().get("parents") or {}).values():
        for t, w in ((rec or {}).get("workers") or {}).items():
            if (
                isinstance(w, dict)
                and w.get("state") in ("held", "running")
                and not _deleted(t, w, view)
            ):
                out.add(t)
    return out


def on_failed_start(title: str, created: Optional[float]) -> None:
    """``title``'s background start failed and its instance is gone: settle
    ITS order record now — a failed start never reaches the disk, so no
    tombstone says it is gone and it would read as the other server's work
    in progress for an hour, holding a cap slot. Only the record of THIS
    instance (same creation time): titles are reused, and a newer namesake's
    record must not be popped or finished. A held worker is forgotten, a
    running or stopped one is done ("gone")."""
    now = time.time()
    hit = False
    with _order.edit() as data:
        for rec in (data.get("parents") or {}).values():
            w = _order.worker_rec(rec, title)
            if (
                w is None
                or created is None
                or not _order._same(w.get("created"), created)
            ):
                continue
            hit = True
            if w.get("state") == "held":
                rec["workers"].pop(title, None)
            elif w.get("state") in ("running", "stopped"):
                w.update(state="done", ended_at=now, how="gone")
    if hit:
        wake()


# --------------------------------------------------------------------------- #
# Lazy start: a held worker's agent starts only when its turn comes
# --------------------------------------------------------------------------- #
#: The launch gate's kind for a held worker's agent start (core.launch_gate).
TURN_KIND = "worker turn"
#: Set on a deferred worker's tmux session object: the folder its first
#: launch was deferred in. Gone once its agent is launched.
_DEFERRED_DIR = "_mf_deferred_dir"
#: A deferred launch and the never-ran marker, one at a time (the create's
#: background Start and the order loop's release can meet).
_LAUNCH_LOCK = threading.Lock()


def defer_agent(inst) -> bool:
    """Make ``inst``'s coming ``Start(True)`` do everything but launch the
    agent: the worktree, the branch, the launch command and its env are all
    built, the session is started and saved, but its tmux session's
    ``start`` only notes the folder. :func:`start_agent` launches it later —
    the same first launch Start would have run. Once only: a later start
    (Resume) launches as usual. → whether it was deferred (an engine whose
    tmux object can't take it starts at once, as before). Before Start."""
    try:
        ts = getattr(inst, "_tmux_session", None)
        if ts is None:
            from backend.session import tmux as _tmux_pkg

            ts = _tmux_pkg.NewTmuxSession(inst.Title, inst.Program)
            inst._tmux_session = ts

        def _deferred_start(work_dir: str):
            ts.__dict__.pop("start", None)
            setattr(ts, _DEFERRED_DIR, work_dir)
            return None

        ts.start = _deferred_start
        return True
    except Exception:  # noqa: BLE001 — starting at once is the old behaviour
        return False


def agent_deferred(inst) -> bool:
    """``inst`` was started with its agent's first launch still to come
    (in this process: a restart forgets it, :func:`start_agent` copes)."""
    ts = getattr(inst, "_tmux_session", None)
    return bool(getattr(ts, "__dict__", {}).get(_DEFERRED_DIR))


def mark_never_ran(title: str, inst=None) -> None:
    """Record ``title``'s agent as one that ended cleanly — it never ran.
    Every path that brings a dead agent back reads that marker: core.revive
    skips a clean quit (neither the boot hold nor the sweep touch it, so it
    is never "offline after a crash"), and ``_ensure_agent_session`` starts
    a clean quit fresh, so a person's early Start opens a new conversation
    rather than ``--continue`` one it never had. The launch wrapper deletes
    the marker as the agent starts. ``inst``: only while its launch is
    still deferred (its turn may have come during the Start)."""
    with _LAUNCH_LOCK:
        if inst is not None and not agent_deferred(inst):
            return
        try:
            from backend.web.core import terminal as _terminal

            path = _terminal._exit_marker_path(_tmux_of(title))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("0\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 — a missing marker reads as a crash
            pass


def _agent_live(srv, title: str) -> bool:
    return srv._live_session_name(_tmux_of(title)) is not None


def _undefer(inst) -> None:
    ts = getattr(inst, "_tmux_session", None)
    try:
        ts.__dict__.pop(_DEFERRED_DIR, None)
    except Exception:  # noqa: BLE001
        pass


def start_agent(title: str, inst=None) -> Optional[str]:
    """Launch ``title``'s agent now → the error, or None (also when it is
    already up: a person pressed Start early). The deferred first launch
    when this process still holds it, else the relaunch path (after a
    restart; :func:`mark_never_ran` makes it a fresh start). Blocking."""
    srv = _server()
    if inst is None:
        inst = srv.ENGINE.instances.get(title)
        if inst is None:
            return "the session was closed"
    if _agent_live(srv, title):
        _undefer(inst)
        return None
    from backend.web.core import held as _held
    from backend.web.core import queue_driver as _queue_driver

    with _LAUNCH_LOCK:
        ts = getattr(inst, "_tmux_session", None)
        own = getattr(ts, "__dict__", {})
        wdir = own.get(_DEFERRED_DIR)
        if not wdir and "start" in own:
            # Its turn came while it is still being created: the Start under
            # way launches it after all.
            own.pop("start", None)
            return None
        _held.release(title)  # a worker that never ran is not held offline
        if wdir and os.path.isdir(wdir):
            err = ts.start(wdir)
            if err is not None and _agent_live(srv, title):
                err = None  # launched meanwhile by another path
            if err is not None:
                return str(err)
            _undefer(inst)
        else:
            _undefer(inst)
            _name, err = srv._ensure_agent_session(inst, title)
            if err is not None:
                return str(err)
    _queue_driver.note_launched(title)
    return None


def start_on_turn(srv, title: str, inst, parent: str = "") -> str:
    """Start ``title``'s agent for its turn, as an automatic start through
    the launch gate: launched now when the gate says go, else queued there
    ("Waiting for memory" on its row; the gate launches it). → a note for
    the record ("" when it started, or was already up)."""
    if _agent_live(srv, title):
        _undefer(inst)
        return ""
    try:
        queued = _launch_gate.gate_auto(
            title,
            lambda: start_agent(title),
            kind=TURN_KIND,
            source="worker order",
            parent=parent,
            args={},
        )
    except _launch_gate.QueueFull:
        # The queue drain relaunches it through the gate once its queue is on.
        return "its agent waits to start (the launch queue is full)"
    if queued is not None:
        return "its agent waits for memory to start"
    err = start_agent(title, inst)
    return ("its agent did not start: %s" % err)[:200] if err else ""


def _restore_turn(rec: dict):
    """A saved turn start after a restart (core.launch_gate): its launch, or
    None (dropped) when the session is gone."""
    title = str(rec.get("title") or "")
    if not title or title not in _server().ENGINE.instances:
        return None
    return lambda: start_agent(title)


_launch_gate.register_kind(TURN_KIND, _restore_turn)


def _branch_of(inst) -> str:
    try:
        return str(inst.Branch or "")
    except Exception:  # noqa: BLE001
        return ""


def _carry(srv, title: str, inst, rec: dict, w: dict) -> str:
    """Bring a released worker up to its orchestrator's CURRENT HEAD — what
    it would have forked from had it been spawned now (the orchestrator may
    have merged what it waited for meanwhile) — when that is a plain
    fast-forward of its untouched branch; its base commit moves with it, so
    its diff stays its own work. Predecessor branches the orchestrator has
    not merged are named, never pulled in (they would show up as this
    worker's own commits). → sentences for its task ("" when nothing
    applies). Never forces: a dirty or unreadable tree, own commits, a busy
    agent, an in-place or provisioned worker are left exactly as they are."""
    from backend.web.core import git_merge as _gm

    if not w.get("after"):
        return ""
    unmerged = []
    lines: List[str] = []
    wt = _wt_of(inst)
    parent = str(getattr(inst, "Parent", "") or "")
    pinst = srv.ENGINE.instances.get(parent) if parent else None
    pwt = _wt_of(pinst) if pinst is not None else ""
    psha = _gm.rev_parse(pwt) if pwt else ""
    movable = bool(
        wt
        and psha
        and not getattr(inst, "InPlace", False)
        and not getattr(inst, "Provisioned", False)
        and not _shares_folder(srv, title, wt)
    )
    head = _gm.rev_parse(wt) if movable else ""
    try:
        gw = inst.GetGitWorktree()
        base = str(gw.GetBaseCommitSHA() or "")
    except Exception:  # noqa: BLE001
        gw, base = None, ""
    if movable and head and base == head and psha != head:
        try:
            act = str(srv._agent_activity_cached(inst, title) or "")
        except Exception:  # noqa: BLE001
            act = ""
        changed = _gm.changed_paths(wt)
        # "offline": its agent has not started yet (a held worker's starts
        # after this), so nothing is using the tree.
        if (
            act in ("idle", "", "offline")
            and changed is not None
            and not changed
            and _gm.rev_parse(wt, psha)
            and _gm.is_ancestor(wt, head, psha)
        ):
            cp = _gm._git(wt, "merge", "--ff-only", "--quiet", psha, timeout=60)
            if cp is not None and cp.returncode == 0:
                try:
                    gw.baseCommitSHA = psha
                    srv.ENGINE.save()
                except Exception:  # noqa: BLE001
                    pass
                head = psha
                lines.append(
                    "Your branch was fast-forwarded to %s's current HEAD (%s)."
                    % (parent, psha[:10])
                )
    for p in w.get("after") or []:
        pi = srv.ENGINE.instances.get(p)
        br = _branch_of(pi) if pi is not None else ""
        br = br or str((_order.worker_rec(rec, p) or {}).get("branch") or "")
        if not br or not wt:
            continue
        sha = _gm.rev_parse(wt, br)
        if sha and _gm.is_ancestor(wt, sha, "HEAD") is False:
            unmerged.append("%s (branch %s)" % (p, br))
    if unmerged:
        lines.append(
            "Not in your tree yet: %s — `git merge` it first if your task builds "
            "on it." % "; ".join(unmerged)
        )
    return " ".join(lines)


def _release(srv, title: str, rec: dict, w: dict, detail: str, parent: str = "") -> str:
    """Hand a held worker its task: catch its branch up, start its agent
    (through the launch gate), then switch its queue on. → a short note for
    the record."""
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return ""
    preds = list(w.get("after") or [])
    lines: List[str] = []
    if preds and detail != "started by hand":
        carry = ""
        try:
            # Before its agent starts: nothing is using the tree yet.
            carry = _carry(srv, title, inst, rec, w)
        except Exception:  # noqa: BLE001 — a hand-over never blocks a release
            carry = ""
        lines.append(
            "MindFlock held this task until %s finished. %s"
            % (", ".join(preds[:6]), carry)
        )
    elif detail == "started by hand" and preds:
        lines.append(
            "MindFlock: started by hand before %s finished — it may still be "
            "changing related code." % ", ".join(preds[:6])
        )
    ftext = _order.fence_text(
        w.get("fence"), str((w.get("fence") or {}).get("by") or "")
    )
    if ftext:
        lines.append(ftext)
    # Its agent starts now — it was created without one. Before the queue
    # goes on, so the drain finds it launching (or waiting in the gate's
    # queue) rather than relaunching it itself.
    started = ""
    try:
        started = start_on_turn(srv, title, inst, parent)
    except Exception:  # noqa: BLE001 — the queue drain relaunches it anyway
        started = ""
    try:
        items = srv._prompt_queue.list_queue(title)
        if items and lines and srv._prompt_queue.get_state(title).get("held"):
            first = items[0]
            srv._prompt_queue.update_item(
                title,
                first["id"],
                "\n\n".join(x.strip() for x in lines if x.strip())
                + "\n\n"
                + str(first.get("text") or ""),
            )
        srv._prompt_queue.set_flags(title, enabled=True, held=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        srv._events.BUS.emit(
            "session.order", session=title, data={"state": "running", "after": preds}
        )
    except Exception:  # noqa: BLE001
        pass
    note = lines[0][:200] if lines and preds else ""
    if started:
        note = (started + (" — " + note if note else ""))[:200]
    return note


def _pass_sync() -> None:
    """One pass, in three phases so no git work runs under the store lock
    (every row build reads it): plan from a snapshot, do the side effects,
    then write back only what still applies."""
    srv = _server()
    now = time.time()
    # The store FIRST: a worker registered after this read is not in it, so
    # it can't read as gone in the session list taken next.
    data = _order.load()
    instances = dict(srv.ENGINE.instances)
    for parent, rec in list((data.get("parents") or {}).items()):
        if not isinstance(rec, dict):
            continue
        # 1. Land fences first: a held worker is released only once fenced.
        landed: Dict[str, Tuple[dict, dict]] = {}
        for t, w in list((rec.get("workers") or {}).items()):
            f = w.get("fence") if isinstance(w, dict) else None
            if not f or f.get("applied") or instances.get(t) is None:
                continue
            if not _order._same(w.get("created"), _created(instances[t])):
                continue  # this engine holds a namesake, not this worker
            ok, problems, where = _apply_fence(
                srv, t, instances[t], f, str(f.get("by") or parent)
            )
            if ok:
                f.update(applied=True, **where)
                landed[t] = (_fence_key(f), dict(applied=True, **where))
            elif problems:
                landed[t] = (_fence_key(f), {"problems": problems[:4]})
        # The user switched a held worker's queue on (its Queue tab): that is
        # "start it now".
        heal: List[str] = []
        for t, w in (rec.get("workers") or {}).items():
            if not isinstance(w, dict) or t not in instances:
                continue
            try:
                q = srv._prompt_queue.get_state(t)
            except Exception:  # noqa: BLE001
                continue
            if w.get("state") == "held" and q.get("held") and q.get("enabled"):
                w["start_now"] = True
            elif (
                w.get("state") == "running"
                and q.get("held")
                and now - float(w.get("released_at") or 0) > RELEASE_GRACE_S
            ):
                # Released, but its queue is still parked (a release that
                # died half-way). Not one under way: the claiming pass —
                # maybe the other server's — is still writing its preface.
                heal.append(t)
        for t in heal:
            try:
                srv._prompt_queue.set_flags(t, enabled=True, held=False)
            except Exception:  # noqa: BLE001
                pass
        # 2. Plan, and carry out releases (git + the queue) outside the lock.
        acts = _order.plan(rec, observe(rec, instances, parent))
        claimed = _claim_releases(
            parent,
            {
                t: (_order.worker_rec(rec, t) or {}).get("created")
                for kind, t, _d in acts
                if kind == "release"
            },
            now,
        )
        notes: Dict[str, Tuple[str, str]] = {}
        for kind, t, detail in acts:
            w = _order.worker_rec(rec, t)
            if kind == "release" and w is not None and t in claimed:
                note = ""
                try:
                    note = _release(srv, t, rec, w, detail, parent)
                except Exception:  # noqa: BLE001
                    note = ""
                inst = instances.get(t)
                notes[t] = (note, _branch_of(inst) if inst is not None else "")
        # 3. Write back. The disk view a drop needs is read BEFORE the store
        # lock (the other server waits on that lock): only when this record
        # looks gone from here.
        view = None
        dead: set = set()
        if (not parent or parent not in instances) and not any(
            t in instances for t in rec.get("workers") or {}
        ):
            view = _live_elsewhere()
            # Its tmux probes too: nothing slow runs under the store lock.
            dead = {
                t for t in rec.get("workers") or {} if t in view.live and _looks_dead(t)
            }
        with _order.edit() as d2:
            live = (d2.get("parents") or {}).get(parent)
            if not isinstance(live, dict):
                continue

            def _same_worker(t: str, w: Optional[dict]) -> bool:
                # The record still describes the worker this pass planned for
                # (a namesake may have registered meanwhile — a release's git
                # work for its siblings can take seconds).
                snap = _order.worker_rec(rec, t)
                return (
                    w is None
                    or snap is None
                    or _order._same(w.get("created"), snap.get("created"))
                )

            for t, (key, upd) in landed.items():
                w = _order.worker_rec(live, t)
                if not _same_worker(t, w):
                    continue
                lf = (w or {}).get("fence")
                if not isinstance(lf, dict):
                    # Cleared while this pass landed the old one: lift it.
                    if upd.get("wt"):
                        _drop_fence(srv, t, upd["wt"], cause="lifted")
                elif _fence_key(lf) == key:
                    lf.update(upd)
                # else: replaced meanwhile — left unapplied, next pass lands it
            for kind, t, detail in acts:
                w = _order.worker_rec(live, t)
                if w is None or not _same_worker(t, w):
                    continue
                inst = instances.get(t)
                if kind == "forget":
                    live["workers"].pop(t, None)
                elif kind == "release" and t in notes:
                    # Claimed (state running) before _release ran.
                    note, branch = notes[t]
                    w.update(note=note)
                    if branch:
                        w["branch"] = branch
                elif kind in ("done", "stopped"):
                    w.update(state=kind, ended_at=now, how=detail)
                    if inst is not None and _branch_of(inst):
                        w["branch"] = _branch_of(inst)
            fresh = srv.ENGINE.instances
            alive = [t for t in live.get("workers") or {} if t in fresh]
            if (not parent or parent not in fresh) and not alive and view is not None:
                # Gone here — but maybe only not adopted YET (another server's
                # orchestrator, or its brand-new workers): ask the disk.
                if (not parent or parent not in view.live) and not any(
                    _alive_elsewhere(t, w, view, dead=dead)
                    for t, w in (live.get("workers") or {}).items()
                    if isinstance(w, dict)
                ):
                    d2["parents"].pop(parent, None)
    _heal_orphans(srv, _order.load(), instances, now)
    _sweep_fences(srv, dict(srv.ENGINE.instances))


def _orphan(srv, data: Mapping, title: str, inst, now: float) -> bool:
    """``title``'s task is parked (queue held) but no order holds it: its
    record is gone, so no pass will ever release it. A just-created one is
    not an orphan yet — ``on_create`` parks the task a moment before it
    writes the record."""
    if _order.parent_of_worker(dict(data), title) is not None:
        return False
    born = _created(inst) or 0.0
    if born and now - born < ORPHAN_GRACE_S:
        return False
    try:
        return bool(srv._prompt_queue.get_state(title).get("held"))
    except Exception:  # noqa: BLE001
        return False


def _heal_orphans(srv, data: Mapping, instances: Mapping, now: float) -> None:
    """Hand every orphaned held task over (its queue switched on): a task
    parked for an order that no longer exists would otherwise wait for ever —
    a worker spawned with a fence sat idle until its orchestrator re-sent the
    task by message."""
    try:
        snap = srv._prompt_queue.snapshot()
    except Exception:  # noqa: BLE001
        return
    for title, entry in snap.items():
        inst = instances.get(title)
        if inst is None or not entry.get("held"):
            continue
        if not _orphan(srv, data, title, inst, now):
            continue
        try:
            srv._prompt_queue.set_flags(title, enabled=True, held=False)
        except Exception:  # noqa: BLE001
            pass


def _claim_releases(
    parent: str, titles: Mapping[str, Optional[float]], now: float
) -> set:
    """Mark ``titles`` (title → the creation time planned for) running under
    the store lock BEFORE releasing them → the ones this pass won. Both
    servers plan from their own snapshot: without the claim both released
    the same worker (two fast-forwards, the held / fence preface written in
    front of its task twice). A namesake registered since is not claimed."""
    if not titles:
        return set()
    won = set()
    with _order.edit() as data:
        live = (data.get("parents") or {}).get(parent)
        for t, created in titles.items():
            w = _order.worker_rec(live, t) if isinstance(live, dict) else None
            if (
                w is not None
                and w.get("state") == "held"
                and _order._same(w.get("created"), created)
            ):
                w.update(state="running", released_at=now)
                w.pop("start_now", None)
                won.add(t)
    return won


def _fence_key(f: Mapping) -> Tuple:
    return (
        tuple(f.get("only") or ()),
        tuple(f.get("keep_out") or ()),
        str(f.get("reason") or ""),
    )


def _sweep_fences(srv, instances: Mapping) -> None:
    """Drop every orchestrator-set fence whose session is gone (a new session
    that reuses the tmux name must not inherit it). Gone means gone from the
    disk too: a co-running server's new worker is not in this engine yet,
    and dropping its fence left it working unfenced (:func:`alive_anywhere`)."""
    try:
        live = {srv._red_zones._session_key(_tmux_of(t)) for t in instances}
        found = srv._red_zones.fences_by_owner_prefix(FENCE_OWNER)
        if any(key not in live for _wt, key in found):
            # Not in this engine: alive elsewhere (saved, or a worker still
            # being created — its fence lands before its first save)?
            live |= {
                srv._red_zones._session_key(_tmux_of(t)) for t in alive_anywhere(())
            }
        for wt, key in found:
            if key not in live:
                if srv._red_zones.drop_session_fence(wt, key, cause="ended"):
                    try:
                        srv._red_zones.sync_guard(wt, lroot=wt)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass


def tick() -> None:
    """One pass (thread-safe; a concurrent call is skipped, not queued)."""
    if not _TICK_LOCK.acquire(blocking=False):
        return
    try:
        _pass_sync()
    finally:
        _TICK_LOCK.release()


def wake() -> None:
    loop, ev = _LOOP, _WAKE
    if loop is None or ev is None:
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass


async def run_loop() -> None:
    """Keep every orchestrator's order (started by the lifespan)."""
    global _LOOP, _WAKE
    _LOOP = asyncio.get_running_loop()
    _WAKE = asyncio.Event()
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass
        _WAKE.clear()


# --------------------------------------------------------------------------- #
# Order changes + reads
# --------------------------------------------------------------------------- #
def set_order(parent: str, payload: Mapping) -> Tuple[int, dict]:
    """``POST /api/instances/{parent}/order``: ``{mode, max_parallel, steps,
    after: {title: [titles]}, start_now: [titles]}`` → ``(status, body)``."""
    srv = _server()
    pinst = srv.ENGINE.instances.get(parent)
    if pinst is None:
        return 404, {"error": "unknown session: %s" % parent}
    steps = payload.get("steps")
    if steps is not None and not isinstance(steps, list):
        return 400, {"error": "steps must be a list of lists of titles"}
    after = payload.get("after")
    if after is not None and not isinstance(after, dict):
        return 400, {
            "error": "after must map a worker's title to the titles it runs after"
        }
    start_now = payload.get("start_now") or []
    if isinstance(start_now, str):
        start_now = [start_now]
    # A worker of this orchestrator whose order record is gone but whose task
    # is still parked: "start it now" releases it directly (the order has
    # nothing left to say about it) — only once the rest of the change is
    # valid, so a 400 never leaves workers started behind it.
    orphans: List[str] = []
    data0 = _order.load()
    for t in [str(x or "").strip() for x in start_now]:
        inst = srv.ENGINE.instances.get(t)
        if (
            inst is not None
            and str(getattr(inst, "Parent", "") or "") == parent
            and _order.parent_of_worker(data0, t) is None
        ):
            try:
                if srv._prompt_queue.get_state(t).get("held"):
                    orphans.append(t)
            except Exception:  # noqa: BLE001
                pass
    start_now = [t for t in start_now if str(t or "").strip() not in orphans]
    with _order.edit() as data:
        rec = _order.parent_rec(data, parent, _created(pinst), make=True)
        problems = _order.set_policy(
            rec,
            mode=payload.get("mode"),
            max_parallel=payload.get("max_parallel"),
            steps=steps,
            after=after,
            start_now=start_now,
        )
    if problems:
        return 400, {"error": "; ".join(problems), "problems": problems}
    started: List[str] = []
    for t in orphans:
        try:
            srv._prompt_queue.set_flags(t, enabled=True, held=False)
            started.append(t)
        except Exception:  # noqa: BLE001
            pass
    tick()
    body = {"ok": True, "order": order_view(parent)}
    if started:
        body["started"] = started
    return 200, body


def order_view(parent: str) -> Optional[dict]:
    srv = _server()
    pinst = srv.ENGINE.instances.get(parent)
    data = _order.load()
    rec = _order.parent_rec(data, parent, _created(pinst) if pinst else None)
    if rec is None:
        return None
    return _order.view(rec, observe(rec, srv.ENGINE.instances, parent))


def row_order(title: str, parent: str) -> Optional[dict]:
    """The ``order`` field of a worker's row, or None. Cheap: one small
    file read, no git."""
    if not parent:
        return None
    data = _order.load()
    rec = (data.get("parents") or {}).get(parent)
    if not isinstance(rec, dict) or title not in (rec.get("workers") or {}):
        return None
    srv = _server()
    obs = (
        observe(rec, srv.ENGINE.instances, parent)
        if rec["workers"][title].get("state") == "held"
        else {}
    )
    return _order.row_order(rec, title, obs)
