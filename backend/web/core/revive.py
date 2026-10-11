"""Background revive: bring every session's agent back after a restart.

Agents otherwise revive lazily — only when a terminal attaches or a prompt is
sent (:func:`backend.web.core.agent_sessions._ensure_agent_session`) — so after
a reboot every session not on screen sat Offline until someone clicked it.
:func:`sweep` relaunches them in the background instead:

* **Who** — every session whose persisted status says its agent was running
  (``Running`` / ``Ready``). Skipped, each with a reason: paused sessions
  (their worktree is gone by design), sessions still provisioning, peer-shared
  sessions (they relaunch through the peer service), sessions whose worktree
  is missing, and sessions whose provider reports a usage limit (a relaunch
  would land on the limit screen; the prompt queue resumes them when the
  window reopens). A session whose agent tmux is still alive (a server
  restart that did not take tmux down) is left alone and counted apart.
* **How** — the exact path the Offline chip's Restart action takes
  (``POST /api/instances/{title}/revive`` → ``_ensure_agent_session``), so one
  launch rule exists. At most :data:`CONCURRENCY` at a time with a
  :data:`GAP_SECONDS` breather, oldest-first by last activity. After each
  launch the session's row is republished, so ``session.status_changed`` /
  ``session.activity_changed`` fire and the rail turns from Offline to
  Working/Idle by itself.
* **When** — :func:`run_on_start` (registered by the server lifespan) waits
  out the boot quiet window first, so the notification settle logic isn't
  flooded, and runs only while ``general.revive_on_start`` is on (default).
  ``POST /api/server/revive-all`` runs the same sweep on demand.

One log line per revive, one summary line per sweep.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional, Tuple

from backend import log, providers, session

#: Launches in flight at once — each one starts an agent CLI, which is the
#: expensive part (node boot, MCP attach, auth refresh).
CONCURRENCY = 2
#: Pause after each launch before the same slot takes the next one.
GAP_SECONDS = 1.5

# Skip reasons (stable strings: the summary and the route report them).
SKIP_PAUSED = "paused"
SKIP_LOADING = "still provisioning"
SKIP_PEER = "peer-shared"
SKIP_WORKSPACE = "workspace missing"
SKIP_LIMIT = "usage limit"
SKIP_QUIT = "quit by user"
SKIP_GONE = "closed meanwhile"

#: Only one sweep at a time (the startup one and an on-demand one would
#: otherwise race to launch the same agents). Keyed to the loop that made it:
#: an asyncio lock can't be shared across event loops (tests run several).
_LOCK: Optional[Tuple[object, asyncio.Lock]] = None


def _sweep_lock() -> asyncio.Lock:
    global _LOCK
    loop = asyncio.get_running_loop()
    if _LOCK is None or _LOCK[0] is not loop:
        _LOCK = (loop, asyncio.Lock())
    return _LOCK[1]


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        logger.Printf(fmt, *args)


def enabled() -> bool:
    """``general.revive_on_start`` (unset = on). Never raises."""
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().general.revive_on_start is not False
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return True


def _last_seen(inst) -> float:
    from backend.web.core.engine import _inst_last_seen

    return _inst_last_seen(inst)


def _limited(srv, inst, title: str, now: float) -> bool:
    """Whether ``title``'s provider says it is out of usage right now: a hold
    the prompt queue already armed, or the provider's own live meter."""
    try:
        if srv._session_limited_until(title) > now:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        until = srv._live_limit_reset(provider, now, inst)
        return bool(until) and until > now
    except Exception:  # noqa: BLE001 — the meter is enrichment, never a gate
        return False


def skip_reason(srv, inst, title: str, now: float) -> Optional[str]:
    """Why ``inst`` must NOT be relaunched (``None`` = revive it). Cheap
    checks first; the usage meter (possibly a network read) last."""
    status = getattr(inst, "Status", None)
    if status == session.Paused:
        return SKIP_PAUSED
    if status == session.Loading:
        return SKIP_LOADING
    if getattr(inst, "PeerShare", "") or "":
        return SKIP_PEER
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return SKIP_WORKSPACE
    # An agent the user quit BEFORE the restart left an exit marker with a
    # natural code: it had already ended, so a fresh process is not a revive.
    try:
        from backend.session import tmux as _tmux

        code = srv._read_exit_marker(_tmux.to_mindflock_tmux_name(title))
        if code is not None and srv._is_natural_exit(code):
            return SKIP_QUIT
    except Exception:  # noqa: BLE001 — no marker reads as "revive it"
        pass
    if _limited(srv, inst, title, now):
        return SKIP_LIMIT
    return None


def _alive(srv, title: str) -> bool:
    """The agent tmux for ``title`` is already running (nothing to revive)."""
    try:
        from backend.session import tmux

        return srv._live_session_name(tmux.to_mindflock_tmux_name(title)) is not None
    except Exception:  # noqa: BLE001
        return False


def plan(srv, now: Optional[float] = None) -> Tuple[List[str], Dict[str, str], int]:
    """``(to_revive, skipped, alive)``: the titles to relaunch, oldest-first by
    last activity; ``{title: reason}`` for the ones left alone; and how many
    already have a live agent. Blocking (tmux + meter reads) — run it off the
    event loop."""
    if now is None:
        now = time.time()
    with srv.ENGINE.lock:
        items = list(srv.ENGINE.instances.items())
    todo: List[Tuple[float, str]] = []
    skipped: Dict[str, str] = {}
    alive = 0
    for title, inst in items:
        reason = skip_reason(srv, inst, title, now)
        if reason is not None:
            skipped[title] = reason
            continue
        if _alive(srv, title):
            alive += 1
            continue
        todo.append((_last_seen(inst), title))
    todo.sort()
    return [t for _, t in todo], skipped, alive


def _revive_one(srv, title: str) -> Tuple[bool, str]:
    """Relaunch ``title``'s agent through ``_ensure_agent_session`` (re-checking
    the skip rules first: minutes may have passed since the plan). Returns
    ``(revived, skip_reason_or_error)``. Blocking."""
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return False, SKIP_GONE
    reason = skip_reason(srv, inst, title, time.time())
    if reason is not None:
        return False, reason
    t0 = time.monotonic()
    _name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        _info("revive %s: not started (%s)", title, err)
        return False, err
    _info(
        "revive %s: agent relaunched (%.1fs)",
        title,
        time.monotonic() - t0,
    )
    try:
        # Publish the fresh row + emit status/activity changes now, rather
        # than leaving the rail on Offline until the next 4s tick.
        srv._republish_session(title)
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def _summarize(revived: List[str], skipped: Dict[str, str], alive: int) -> dict:
    reasons: Dict[str, int] = {}
    for r in skipped.values():
        reasons[r] = reasons.get(r, 0) + 1
    detail = ", ".join("%d %s" % (n, r) for r, n in sorted(reasons.items()))
    line = "revived %d, skipped %d%s" % (
        len(revived),
        len(skipped),
        (": " + detail) if detail else "",
    )
    if alive:
        line += "; %d already running" % alive
    return {
        "revived": list(revived),
        "skipped": dict(skipped),
        "already_running": alive,
        "summary": line,
    }


async def sweep(
    srv=None,
    *,
    concurrency: int = CONCURRENCY,
    gap: float = GAP_SECONDS,
    trigger: str = "on demand",
) -> dict:
    """Revive every eligible session (see the module docstring) and return
    ``{"revived": [...], "skipped": {title: reason}, "already_running": n,
    "summary": "revived N, skipped M: reasons"}``. One sweep at a time: a
    second caller waits for the first, then runs its own (cheap — whatever
    the first revived is alive by then)."""
    if srv is None:
        srv = _server()
    async with _sweep_lock():
        todo, skipped, alive = await asyncio.to_thread(plan, srv)
        revived: List[str] = []
        queue: "asyncio.Queue[str]" = asyncio.Queue()
        for t in todo:
            queue.put_nowait(t)

        async def _worker() -> None:
            while True:
                try:
                    title = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    ok, why = await asyncio.to_thread(_revive_one, srv, title)
                except Exception as err:  # noqa: BLE001 — one bad session
                    ok, why = False, str(err)
                if ok:
                    revived.append(title)
                else:
                    skipped[title] = why or "failed"
                if not queue.empty() and gap > 0:
                    await asyncio.sleep(gap)

        workers = max(1, min(int(concurrency), len(todo) or 1))
        await asyncio.gather(*(_worker() for _ in range(workers)))
        out = _summarize(revived, skipped, alive)
        _info("revive sweep (%s): %s", trigger, out["summary"])
        return out


async def run_on_start(srv=None) -> Optional[dict]:
    """The startup sweep: wait out the boot quiet window, then :func:`sweep`
    if ``general.revive_on_start`` is on. ``None`` when it was off."""
    if srv is None:
        srv = _server()
    try:
        left = srv._BOOT_QUIET_SECONDS - (time.monotonic() - srv._BOOT_MONO)
    except Exception:  # noqa: BLE001
        left = 0.0
    if left > 0:
        # A beat past the window, so the first transitions it lets through
        # are the revives' own.
        await asyncio.sleep(left + 1.0)
    if not await asyncio.to_thread(enabled):
        _info("revive sweep (startup): off (general.revive_on_start)")
        return None
    return await sweep(srv, trigger="startup")
