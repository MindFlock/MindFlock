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

* **Memory** — reviving a dozen agents straight after the OOM killer took
  them down is how it happens again. Before each launch the sweep checks
  :mod:`backend.memory_pressure`; while it reads low it waits (up to
  :data:`MEMORY_WAIT_SECONDS`) for headroom, and if none comes the rest of
  the sweep is skipped (``low memory``) — the Offline chips stay, for a
  Restart when the machine has room.
* **Continue** — a relaunch resumes the conversation, but not the turn the
  agent was in the middle of: it comes back parked at its prompt. When the
  agent died mid-turn (:func:`died_mid_turn`) a short "continue where you
  left off" is put at the FRONT of its prompt queue, which sends it once the
  CLI has drawn its prompt and settled idle (and never over a dialog or a
  person typing). It is an ordinary queue item: visible, and removable before
  it goes.

One log line per revive, one summary line per sweep.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional, Tuple

from backend import log, memory_pressure, providers, session

#: Launches in flight at once — each one starts an agent CLI, which is the
#: expensive part (node boot, MCP attach, auth refresh).
CONCURRENCY = 2
#: Pause after each launch before the same slot takes the next one.
GAP_SECONDS = 1.5
#: How long one launch waits for memory to recover before the sweep gives up
#: on the rest, and how often it re-reads while waiting.
MEMORY_WAIT_SECONDS = 120.0
MEMORY_POLL_SECONDS = 5.0

#: What an agent that died mid-turn is told once it is back: the relaunch
#: brought its conversation back, not the turn it was in.
CONTINUE_PROMPT = (
    "Your previous run was cut off mid-task: the agent process died "
    "(MindFlock restarted it and resumed this conversation). Continue where "
    "you left off — re-check anything that was in flight (a command, a test "
    "run, a half-written file) before carrying on."
)

# Skip reasons (stable strings: the summary and the route report them).
SKIP_PAUSED = "paused"
SKIP_LOADING = "still provisioning"
SKIP_PEER = "peer-shared"
SKIP_WORKSPACE = "workspace missing"
SKIP_LIMIT = "usage limit"
SKIP_QUIT = "quit by user"
SKIP_GONE = "closed meanwhile"
SKIP_MEMORY = "low memory"
#: :func:`_revive_one`'s second item on a revive that queued a continue.
CONTINUED = "continued"

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


def skip_reason(
    srv, inst, title: str, now: float, include_quit: bool = False
) -> Optional[str]:
    """Why ``inst`` must NOT be relaunched (``None`` = revive it). Cheap
    checks first; the usage meter (possibly a network read) last.
    ``include_quit``: an agent the user quit counts too (the rail's "Restart
    all", which restarts every row whose chip offers Restart)."""
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
        if not include_quit and code is not None and srv._is_natural_exit(code):
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


def plan(
    srv, now: Optional[float] = None, only: Optional[List[str]] = None
) -> Tuple[List[str], Dict[str, str], int]:
    """``(to_revive, skipped, alive)``: the titles to relaunch, oldest-first by
    last activity; ``{title: reason}`` for the ones left alone; and how many
    already have a live agent. ``only``: just these titles, and an agent the
    user quit counts too (the rail's "Restart all" names the rows whose chip
    offers Restart). Blocking (tmux + meter reads) — run it off the event
    loop."""
    if now is None:
        now = time.time()
    include_quit = only is not None
    with srv.ENGINE.lock:
        items = list(srv.ENGINE.instances.items())
    if only is not None:
        wanted = set(only)
        items = [(t, i) for t, i in items if t in wanted]
    todo: List[Tuple[float, str]] = []
    skipped: Dict[str, str] = {}
    alive = 0
    for title, inst in items:
        reason = skip_reason(srv, inst, title, now, include_quit)
        if reason is not None:
            skipped[title] = reason
            continue
        if _alive(srv, title):
            alive += 1
            continue
        todo.append((_last_seen(inst), title))
    todo.sort()
    return [t for _, t in todo], skipped, alive


def _revive_one(srv, title: str, include_quit: bool = False) -> Tuple[bool, str]:
    """Relaunch ``title``'s agent through ``_ensure_agent_session`` (re-checking
    the skip rules first: minutes may have passed since the plan). Returns
    ``(revived, skip_reason_or_error)`` — on success the second item is
    :data:`CONTINUED` when the agent was told to carry on, else ``""``.
    Blocking."""
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return False, SKIP_GONE
    reason = skip_reason(srv, inst, title, time.time(), include_quit)
    if reason is not None:
        return False, reason
    t0 = time.monotonic()
    _name, err, continued = relaunch(inst, title, srv)
    if err is not None:
        _info("revive %s: not started (%s)", title, err)
        return False, err
    _info(
        "revive %s: agent relaunched (%.1fs)%s",
        title,
        time.monotonic() - t0,
        " — died mid-turn, queued a continue" if continued else "",
    )
    try:
        # Publish the fresh row + emit status/activity changes now, rather
        # than leaving the rail on Offline until the next 4s tick.
        srv._republish_session(title)
    except Exception:  # noqa: BLE001
        pass
    return True, (CONTINUED if continued else "")


def died_mid_turn(srv, inst, title: str) -> bool:
    """Whether ``title``'s agent is dead AND died in the middle of a turn, so a
    relaunch should be told to carry on. All of:

    * no live agent tmux (a live one is reused, nothing was interrupted);
    * the death was unnatural — the provider's exit-code policy, the same one
      that makes the relaunch RESUME rather than start fresh (a clean quit
      starts a new conversation, and "continue" would mean nothing to it);
    * the CLI can resume at all (``resume_flag``), and the session owns its
      workspace (an in-place session always starts fresh);
    * the CLI's own hook marker last said ``working``: the turn began
      (UserPromptSubmit / tool hooks) and never ended (no Stop / SessionEnd —
      a SIGKILL fires neither). A marker over 6 hours old reads as no signal,
      so an agent that died long ago is not prodded.

    Blocking (tmux + file reads). Never raises — any doubt answers False."""
    try:
        from backend.providers import activity_markers
        from backend.session import tmux

        if getattr(inst, "InPlace", False) or (getattr(inst, "PeerShare", "") or ""):
            return False
        name = tmux.to_mindflock_tmux_name(title)
        if srv._live_session_name(name) is not None:
            return False
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        if not provider.launcher_spec().resume_flag:
            return False
        if provider.is_natural_exit(srv._read_exit_marker(name)):
            return False
        return activity_markers.read_activity_marker(name) == "working"
    except Exception:  # noqa: BLE001
        return False


def queue_continue(srv, title: str) -> bool:
    """Put :data:`CONTINUE_PROMPT` at the front of ``title``'s prompt queue.
    False (nothing queued) when the user paused a queue that holds prompts —
    enqueueing switches a queue on, which would release their prompts too —
    or a continue is already waiting there. Never raises."""
    try:
        from backend.web.core import prompt_queue

        st = prompt_queue.get_state(title)
        if st["items"] and not st["enabled"]:
            return False
        if any(it.get("text") == CONTINUE_PROMPT for it in st["items"]):
            return False
        prompt_queue.enqueue(title, CONTINUE_PROMPT, index=0)
    except Exception:  # noqa: BLE001
        return False
    try:
        srv._emit_queue_changed(title)
    except Exception:  # noqa: BLE001
        pass
    return True


def relaunch(inst, title: str, srv=None) -> Tuple[str, Optional[str], bool]:
    """``_ensure_agent_session`` plus the continue: ``(name, error, continued)``.
    Whether the agent died mid-turn is read BEFORE the launch (the launch
    starts a new incarnation, after which the old marker no longer counts).
    The one path both the Restart chip and the sweep take. Blocking."""
    if srv is None:
        srv = _server()
    mid_turn = died_mid_turn(srv, inst, title)
    name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        return name, err, False
    continued = mid_turn and queue_continue(srv, title)
    return name, None, continued


def _wait_for_memory(srv, deadline: float) -> bool:
    """Block until memory reads ok (True) or ``deadline`` (monotonic) passes
    (False). Unknown reads as ok."""
    while True:
        if not memory_pressure.is_low():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(MEMORY_POLL_SECONDS)


def _summarize(
    revived: List[str],
    skipped: Dict[str, str],
    alive: int,
    continued: Optional[List[str]] = None,
) -> dict:
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
    if continued:
        line += "; told %d to continue" % len(continued)
    return {
        "revived": list(revived),
        "continued": list(continued or []),
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
    memory_wait: float = MEMORY_WAIT_SECONDS,
    only: Optional[List[str]] = None,
) -> dict:
    """Revive every eligible session (see the module docstring) and return
    ``{"revived": [...], "skipped": {title: reason}, "already_running": n,
    "continued": [...], "summary": "revived N, skipped M: reasons"}``.
    ``only`` restricts the sweep to those titles (see :func:`plan`). One
    sweep at a time: a second caller waits for the first, then runs its own
    (cheap — whatever the first revived is alive by then)."""
    if srv is None:
        srv = _server()
    async with _sweep_lock():
        todo, skipped, alive = await asyncio.to_thread(plan, srv, None, only)
        include_quit = only is not None
        revived: List[str] = []
        continued: List[str] = []
        out_of_memory = False
        queue: "asyncio.Queue[str]" = asyncio.Queue()
        for t in todo:
            queue.put_nowait(t)

        async def _worker() -> None:
            nonlocal out_of_memory
            while True:
                try:
                    title = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if out_of_memory or not await asyncio.to_thread(
                    _wait_for_memory, srv, time.monotonic() + memory_wait
                ):
                    if not out_of_memory:
                        _info(
                            "revive sweep (%s): holding the rest — %s",
                            trigger,
                            memory_pressure.describe(),
                        )
                    out_of_memory = True
                    skipped[title] = SKIP_MEMORY
                    continue
                try:
                    ok, why = await asyncio.to_thread(
                        _revive_one, srv, title, include_quit
                    )
                except Exception as err:  # noqa: BLE001 — one bad session
                    ok, why = False, str(err)
                if ok:
                    revived.append(title)
                    if why == CONTINUED:
                        continued.append(title)
                else:
                    skipped[title] = why or "failed"
                if not queue.empty() and gap > 0:
                    await asyncio.sleep(gap)

        workers = max(1, min(int(concurrency), len(todo) or 1))
        await asyncio.gather(*(_worker() for _ in range(workers)))
        out = _summarize(revived, skipped, alive, continued)
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
