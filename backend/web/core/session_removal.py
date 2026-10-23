"""Session removal: the one teardown every exit path runs, and its bookkeeping.

An instance leaves ``ENGINE.instances`` through many doors — DELETE, /close,
/cleanup, a workspace deleted from Settings, a background Start that failed,
the engine's own tombstone convergence. Each route keeps its specific
teardown; what every one of them owes the rest of the flock goes through
:func:`_on_session_removed` (children orphaned, then every
:data:`_SESSION_REMOVED_HOOKS` entry). Also here: the failed-Start record
(:func:`_note_create_failure`, :func:`_drop_failed_start`) and forgetting a
removed session's per-title state (:func:`_forget_session_state`,
:func:`_prune_session_state`).

server.py registers :func:`_on_session_removed` with the engine (it owns
``ENGINE``) and re-imports every name defined here. Server-owned names, and
the names here tests rebind on ``server`` (``_CREATE_FAILURES``,
``_SESSION_REMOVED_HOOKS``, ``_orphan_children``), are reached through
:func:`_server`.
"""

from __future__ import annotations

import datetime as _datetime
import threading
import time
from typing import Dict

from backend import log
from backend.session import tmux
from backend.web.core import autopilot as _autopilot
from backend.web.core import events as _events
from backend.web.core import finished_children as _finished_children
from backend.web.core import hidden_sessions as _hidden_sessions
from backend.web.core import mailbox as _mailbox
from backend.web.core import ownership as _ownership
from backend.web.core import person_sessions as _person_sessions
from backend.web.core import shared_branch as _shared_branch
from backend.web.core import sleep as _sleep
from backend.web.core import spoken as _spoken
from backend.web.core import state_events as _state_events
from backend.web.core import task_headlines as _task_headlines
from backend.web.core import thread as _thread
from backend.web.core import voice as _voice


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


#: Recent background-Start failures: title -> {"error", "ts"}. A create answers
#: 202 before Start runs, so a caller polling the listing only ever sees the row
#: vanish; ``GET /api/create_failures`` hands it the reason (the same text the
#: ``session.create_failed`` event carries). Bounded and short-lived.
_CREATE_FAILURES: Dict[str, dict] = {}
_CREATE_FAILURES_LOCK = threading.Lock()
_CREATE_FAILURE_TTL_S = 600.0
_CREATE_FAILURES_MAX = 64


def _note_create_failure(title: str, error: str) -> None:
    srv = _server()
    now = time.time()
    with _CREATE_FAILURES_LOCK:
        for t in [
            t
            for t, rec in srv._CREATE_FAILURES.items()
            if now - rec.get("ts", 0.0) > _CREATE_FAILURE_TTL_S
        ]:
            srv._CREATE_FAILURES.pop(t, None)
        srv._CREATE_FAILURES[title] = {"error": str(error), "ts": now}
        while len(srv._CREATE_FAILURES) > _CREATE_FAILURES_MAX:
            oldest = min(
                srv._CREATE_FAILURES, key=lambda t: srv._CREATE_FAILURES[t]["ts"]
            )
            srv._CREATE_FAILURES.pop(oldest, None)


def _drop_failed_start(title: str, inst) -> bool:
    """Clear the registry entry a failed background ``Start`` left behind, and
    say whether this failure is ours to report.

    BY IDENTITY, NEVER BY NAME, and the difference is a running agent. Every
    creator here registers the instance as ``Loading`` and starts it on a
    background task, so by the time that task fails the title may belong to a
    DIFFERENT, live session — a fast delete-and-retry, a second force-start
    after the row was closed, two creates racing through one title. Popping by
    name then deleted the live session's record: its tmux session and its
    worktree carried on with nothing owning them (invisible in the rail, absent
    from ``ENGINE.save``), which is exactly how the orphan that blocks the next
    run of the same name is minted. Reporting by name is the same mistake one
    layer up — a "couldn't start" event, or a checklist stamped with a failure,
    about a session that is working.

    Returns True when nothing else claims the title (we popped our own entry, or
    it was already gone) — say so. False means a live session owns it: stay
    quiet.
    """
    srv = _server()
    with srv.ENGINE.lock:
        current = srv.ENGINE.instances.get(title)
        popped = current is inst
        if popped:
            srv.ENGINE.instances.pop(title, None)
    if popped:
        _on_session_removed(title)
        return True
    return current is None


# --------------------------------------------------------------------------- #
# Session removal: the one teardown every exit path runs.
#
# An instance leaves ``ENGINE.instances`` through many doors — DELETE, /close,
# /cleanup, a workspace deleted from Settings, a background Start that failed
# (_drop_failed_start), and the engine's own tombstone convergence. Each route
# keeps its own specific teardown (tmux, worktree, ports, …); what every one of
# them owes the REST of the flock goes through ``_on_session_removed``.
# --------------------------------------------------------------------------- #
#: Extra per-title cleanups run after a session is removed, in order, each
#: called as ``hook(title)`` and isolated (a failing hook is logged and the
#: rest still run). Features that keep per-title state elsewhere (a mailbox,
#: a per-session run file) append their forget function here.
_SESSION_REMOVED_HOOKS: list = []
# A removed session's inbox goes with it (a reused title must not read the
# old mail).
_SESSION_REMOVED_HOOKS.append(_mailbox.drop)


def _closed_just_now(title: str, within_s: float = 120.0) -> bool:
    """Whether ``title`` was just closed (the newest Recently-closed entry,
    stamped within ``within_s``), as opposed to deleted."""
    try:
        newest = (_server()._load_recently_closed() or [None])[0]
        if not newest or newest.get("title") != title:
            return False
        if newest.get("wiped"):
            # end_session records wiped sessions too; those were deleted.
            return False
        closed = _datetime.datetime.fromisoformat(str(newest.get("closed_at")))
        return (
            _datetime.datetime.now().astimezone() - closed
        ).total_seconds() < within_s
    except Exception:  # noqa: BLE001 — unknown reads as deleted
        return False


def _remember_finished_child(title: str) -> None:
    """Keep a removed sub-session on its orchestrator's Thread.

    Runs before the next tick replaces the snapshot, so ``title``'s last row
    (branch, stage, PR, diff size, final report) is still there. A removed
    PARENT takes its own finished list with it: a reused title must not show
    its namesake's workers."""
    rows = {r.get("title"): r for r in _events.sessions_snapshot()}
    row = rows.get(title)
    if row and row.get("parent"):
        parent_row = rows.get(row.get("parent")) or {}
        _finished_children.record(
            row,
            parent_created=parent_row.get("created_at"),
            how="closed" if _closed_just_now(title) else "deleted",
            seed=_thread.seed_for(title, row.get("created_at")),
        )
    _finished_children.forget_parent(title)


_SESSION_REMOVED_HOOKS.append(_remember_finished_child)


def _forget_mcp_run_file(title: str) -> None:
    """Delete the session's auto-attach MCP run file (Claude's per-launch
    ``--mcp-config``); a relaunch writes it afresh."""
    from backend.providers import mcp_attach as _mcp_attach

    _mcp_attach.forget(tmux.to_mindflock_tmux_name(title))


_SESSION_REMOVED_HOOKS.append(_forget_mcp_run_file)


def _forget_voice(title: str) -> None:
    """A removed session's voice flag, reply file and announced-reply memory
    go with it: a closed session leaves no flag behind (its hook would keep
    injecting the instruction into a namesake), and a re-opened or namesake
    session must not speak the dead one's last words."""
    name = tmux.to_mindflock_tmux_name(title)
    _voice.forget(name)
    _spoken.forget(name)
    _state_events._forget_spoken(title)


_SESSION_REMOVED_HOOKS.append(_forget_voice)


def _disarm_removed_session(title: str) -> None:
    """Forget a removed session's autopilot run (its lane).

    The store is keyed by TITLE and titles come straight back, so a record
    must leave with its session. ``autopilot.prune`` only drops a record once
    it is older than ``ARM_GRACE_S`` (the intake grace), which meant a session
    deleted within 30 minutes of being armed handed its target, attempt
    counters and halt reason to whichever session took the name next — a
    recreated ``untitled-2`` would commit and open a PR nobody asked it to.
    Intake still arms BEFORE its session exists; this only runs for a session
    that existed and is gone."""
    _autopilot.disarm(title)


_SESSION_REMOVED_HOOKS.append(_disarm_removed_session)


def _release_tracker_claim(title: str) -> None:
    """Take the ``mindflock:working`` label off the ticket a closed (or
    shipped) session had claimed on its tracker — on a background thread, the
    tracker is a network call. Never un-assigns; a no-op when the session
    mirrored nothing (backend.ticket_ingestion.tracker_claims)."""
    from backend.ticket_ingestion import tracker_claims as _tracker

    try:
        _tracker.release_soon(title)
    except Exception:  # noqa: BLE001 — bookkeeping, never the removal
        pass


_SESSION_REMOVED_HOOKS.append(_release_tracker_claim)


def _orphan_children(title: str) -> list:
    """Clear ``Parent`` on every instance whose parent is ``title``; persist
    when any changed. Returns the orphaned titles.

    Titles are reused (``untitled-2`` comes straight back), and a parent link
    is authority — an orchestrator manages its children — so a dangling link
    must never be inherited by whichever session takes the name next.
    """
    srv = _server()
    if not title:
        return []
    with srv.ENGINE.lock:
        orphans = [
            t
            for t, i in srv.ENGINE.instances.items()
            if t != title and (getattr(i, "Parent", "") or "") == title
        ]
        for t in orphans:
            srv.ENGINE.instances[t].Parent = ""
    if orphans:
        srv.ENGINE.save()
    return orphans


def _orphan_dangling_parents() -> list:
    """The backstop sweep behind :func:`_orphan_children`: clear any ``Parent``
    that does not name a live session (a removal that slipped past every hook,
    or a parent that never came back after a restart). Persists only when
    something changed; returns the titles it detached."""
    srv = _server()
    with srv.ENGINE.lock:
        live = set(srv.ENGINE.instances)
        stale = [
            t
            for t, i in srv.ENGINE.instances.items()
            if (getattr(i, "Parent", "") or "")
            and (i.Parent not in live or i.Parent == t)
        ]
        for t in stale:
            srv.ENGINE.instances[t].Parent = ""
    if stale:
        srv.ENGINE.save()
    return stale


def _on_session_removed(title: str) -> None:
    """Run the flock-wide teardown for a session that just left the registry.

    Call AFTER the instance is popped (and outside ``ENGINE.lock``): orphans its
    children, then runs every :data:`_SESSION_REMOVED_HOOKS` entry. Never
    raises — a removal has already happened and must not be reported as failed
    because some cleanup did.
    """
    srv = _server()
    try:
        srv._orphan_children(title)
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("orphaning children of %s failed: %v", title, err)
    for hook in list(srv._SESSION_REMOVED_HOOKS):
        try:
            hook(title)
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "session-removed hook %s failed for %s: %v",
                    getattr(hook, "__name__", hook),
                    title,
                    err,
                )


def _forget_session_state(title: str) -> None:
    """Forget a session ENTIRELY — memoized results and rolling per-title state.

    For the delete path only. Titles are reused (``untitled-2`` comes straight
    back), so a namesake must not inherit the dead session's pane hash, CPU
    baseline, limit verdict or work history; and without this the per-title
    dicts accumulate dead entries for the life of the server under churn.
    """
    srv = _server()
    srv._forget_probes(title)
    for _d in (
        srv._ACTIVITY_CACHE,
        srv._LIMIT_PROBE,
        srv._TRUST_DISMISS_AT,
        srv._HUMAN_INPUT_AT,
    ):
        _d.pop(title, None)
    # A namesake must not come back hidden from the rail either.
    _hidden_sessions.drop(title)
    # …nor credited to the dead session's person.
    _person_sessions.drop(title)
    # …nor working on somebody's shared branch.
    _shared_branch.drop(title)
    # …nor its owner, driver or presence stamps.
    _ownership.drop(title)
    # …nor asleep.
    _sleep.drop(title)
    # …nor described by the dead session's last task.
    _task_headlines.drop(title)
    # Keyed by TMUX SESSION NAME, not by title (see agent_state._maybe_record_
    # thread), so it needs the translation the others do not.
    srv._THREAD_RECORD_AT.pop(tmux.to_mindflock_tmux_name(title), None)


def _prune_session_state(live_titles) -> None:
    """Drop per-title probe state for sessions that are no longer in the engine.

    The backstop behind :func:`_forget_session_state`, for the removals that
    never reach a route: a workspace deleted from Settings, and the tombstone
    convergence that pops instances another MindFlock deleted. Without it those
    dicts only ever grow, for the life of the process.
    """
    srv = _server()
    live = set(live_titles or ())
    live_names = {tmux.to_mindflock_tmux_name(t) for t in live}
    for _d in (
        srv._ACTIVITY_CACHE,
        srv._LIMIT_PROBE,
        srv._TRUST_DISMISS_AT,
        srv._HUMAN_INPUT_AT,
    ):
        for dead in [t for t in list(_d) if t not in live]:
            _d.pop(dead, None)
    for dead in [n for n in list(srv._THREAD_RECORD_AT) if n not in live_names]:
        srv._THREAD_RECORD_AT.pop(dead, None)
    _sleep.prune(live)
    _task_headlines.prune(live)
