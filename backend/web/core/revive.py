"""Background revive: bring every session's agent back after a restart.

Agents otherwise revive lazily — only when a terminal attaches or a prompt is
sent (:func:`backend.web.core.agent_sessions._ensure_agent_session`) — so after
a reboot every session not on screen sat Offline until someone clicked it.
:func:`sweep` relaunches them in the background instead:

* **Who** — every session whose persisted status says its agent was running
  (``Running`` / ``Ready``). Skipped, each with a reason: paused sessions
  (their worktree is gone by design), sessions still provisioning, peer-shared
  sessions (they relaunch through the peer service), sessions whose worktree
  is missing, sessions put to sleep on purpose (``sleeping`` — core.sleep;
  they wake when someone opens them or sends them something), and sessions
  whose provider reports a usage limit (a relaunch would land on the limit
  screen; the prompt queue resumes them when the window reopens). A session whose agent tmux is still alive (a server
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

* **After a kill** — :func:`observe` / :func:`auto_tick`, driven by the
  server's 4s instances tick: an agent this server saw running that then dies
  to SIGKILL (exit marker 137, or no marker and its hooks last said
  ``working`` — a SIGKILL fires no Stop hook) is relaunched through
  :func:`relaunch` (so a mid-turn death gets its continue) once memory reads
  ok again — one at a time, :data:`GAP_SECONDS` apart, at most
  :data:`AUTO_MAX_PER_WINDOW` times per session per hour; after that it stays
  Offline with the reason on its row. The same skip rules apply, and
  ``general.revive_on_start`` switches it too. Its trip through Offline is not
  announced (:func:`holds_offline`), so a revived agent rings no bell.

One log line per revive, one summary line per sweep.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

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
#: Put to sleep on purpose (core.sleep): it wakes when someone needs it, not
#: because the server restarted.
SKIP_SLEEPING = "sleeping"
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
    # A sleeping agent was stopped on purpose, idle, to give its memory back:
    # a restart is no reason to wake it (all of them at once, after a reboot,
    # is the memory spike sleeping exists to avoid). Opening it, or sending it
    # anything, wakes it. Also for "Restart all", which never lists sleepers.
    try:
        from backend.web.core import sleep as _sleep

        if _sleep.is_sleeping(title):
            return SKIP_SLEEPING
    except Exception:  # noqa: BLE001
        pass
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

    * not asleep (:mod:`backend.web.core.sleep` stops an agent only once its
      turn has ENDED — the wake resumes it, with nothing to continue);
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

        from backend.web.core import sleep as _sleep

        if getattr(inst, "InPlace", False) or (getattr(inst, "PeerShare", "") or ""):
            return False
        if _sleep.is_sleeping(title):
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


# --------------------------------------------------------------------------- #
# Auto-revive: an agent the kernel killed comes back by itself
# --------------------------------------------------------------------------- #
# Agents raise their own oom_score_adj at launch
# (backend.providers.launch_script.OOM_SCORE_ADJ), so when the machine runs out
# of memory the kernel kills an agent's tree rather than this server. This is
# the other half: the agent it killed comes back once memory has recovered,
# without a click. No loop of its own — the instances tick reports every
# session's reading to observe() and calls auto_tick() once per pass.

#: Auto-revives one session gets within :data:`AUTO_WINDOW_SECONDS` before it
#: is left Offline: an agent whose own work runs the machine out of memory
#: every time must not be relaunched into the same squeeze forever.
AUTO_MAX_PER_WINDOW = 3
AUTO_WINDOW_SECONDS = 3600.0
#: How long a just-revived session's Offline reading is still not announced:
#: the activity probe memoizes ~2.5s, so the tick after a relaunch can still
#: serve the dead reading.
AUTO_SETTLE_SECONDS = 15.0
#: Force the auto-revive on/off (tests). ``None`` = automatic: on in a real
#: server unless ``MINDFLOCK_REVIVE_ON_START=0``, OFF under pytest (ENGINE is
#: the developer's real state there).
AUTO_ENABLED: Optional[bool] = None

#: ``auto_state()["state"]``: waiting for memory / its turn, or left Offline.
AUTO_PENDING = "pending"
AUTO_GAVE_UP = "gave_up"

_AUTO_LOCK = threading.Lock()
#: Titles this process has seen with a live agent — only their deaths count
#: (a session already Offline at boot is the startup sweep's business).
_AUTO_LIVE: Set[str] = set()
#: Killed agents waiting to come back: title -> (epoch seen dead, exit code).
_AUTO_PENDING: Dict[str, Tuple[float, Optional[int]]] = {}
#: title -> epochs of its auto-revives (the rate limit's window).
_AUTO_HISTORY: Dict[str, List[float]] = {}
#: title -> why it was left Offline (shown on its row).
_AUTO_GAVE_UP: Dict[str, str] = {}
#: title -> epoch of its last auto-revive (see AUTO_SETTLE_SECONDS).
_AUTO_REVIVED_AT: Dict[str, float] = {}
_AUTO_RUN: Dict[str, object] = {"busy": False, "last": 0.0, "held": False}


def _auto_on() -> bool:
    if AUTO_ENABLED is not None:
        return bool(AUTO_ENABLED)
    if os.environ.get("MINDFLOCK_REVIVE_ON_START", "1") == "0":
        return False
    from backend.web.core import restart

    return not restart._under_pytest()


def killed_code(srv, inst, title: str) -> Tuple[bool, Optional[int]]:
    """``(killed, exit_code)``: whether ``title``'s dead agent died to SIGKILL —
    the OOM killer's signature, never a person's quit. Exit marker 137 says so
    outright; no marker at all (a launch with no exit wrapper, the engine's
    first start) counts only when the agent died mid-turn
    (:func:`died_mid_turn`: a SIGKILL fires no Stop/SessionEnd hook, while a
    quit does). Blocking (file + tmux reads). Never raises."""
    from backend.providers import launch_script

    try:
        from backend.session import tmux

        code = srv._read_exit_marker(tmux.to_mindflock_tmux_name(title))
    except Exception:  # noqa: BLE001
        code = None
    if code == launch_script.KILLED_EXIT_CODE:
        return True, code
    if code is None and died_mid_turn(srv, inst, title):
        return True, None
    return False, code


def _should_run(inst) -> bool:
    """The session's own state says its agent belongs up (not paused, not
    still provisioning, started)."""
    if getattr(inst, "Status", None) in (session.Paused, session.Loading):
        return False
    try:
        return bool(inst.Started())
    except Exception:  # noqa: BLE001
        return True


def observe(srv, title: str, inst, activity: str, now: Optional[float] = None):
    """Report one session's activity reading (the instances tick, per row,
    BEFORE it emits state changes). A live reading marks the agent seen; the
    first Offline after one is a death, and a death by SIGKILL
    (:func:`killed_code`) queues the agent for :func:`auto_tick`. Blocking
    only on a death (marker + tmux reads). Never raises."""
    if now is None:
        now = time.time()
    try:
        if activity != "offline":
            with _AUTO_LOCK:
                _AUTO_LIVE.add(title)
                _AUTO_PENDING.pop(title, None)
                _AUTO_GAVE_UP.pop(title, None)
            return
        with _AUTO_LOCK:
            if title not in _AUTO_LIVE:
                return
            _AUTO_LIVE.discard(title)
        if not _auto_on() or not _should_run(inst):
            return
        killed, code = killed_code(srv, inst, title)
        if not killed or not enabled():
            return
        with _AUTO_LOCK:
            _AUTO_PENDING.setdefault(title, (now, code))
            _AUTO_GAVE_UP.pop(title, None)
    except Exception:  # noqa: BLE001 — a reading must never break the tick
        pass


def holds_offline(title: str, now: Optional[float] = None) -> bool:
    """Whether ``title``'s Offline reading should NOT be announced: it is
    queued to come back by itself, or just came back. The diff emitter keeps
    the previous activity meanwhile, so a revive rings no bell, toast or push;
    a session left Offline (:data:`AUTO_GAVE_UP`) is announced as usual."""
    if now is None:
        now = time.time()
    with _AUTO_LOCK:
        if title in _AUTO_PENDING:
            return True
        return now - _AUTO_REVIVED_AT.get(title, 0.0) < AUTO_SETTLE_SECONDS


def auto_state(title: str) -> Optional[dict]:
    """The row's ``auto_revive``: ``{"state": "pending"|"gave_up", "reason"}``
    or ``None`` — what the Offline chip says instead of its stock hint."""
    with _AUTO_LOCK:
        if title in _AUTO_PENDING:
            return {
                "state": AUTO_PENDING,
                "reason": "The agent was killed (likely out of memory) — it "
                "restarts by itself once memory has recovered",
            }
        reason = _AUTO_GAVE_UP.get(title)
    if reason:
        return {"state": AUTO_GAVE_UP, "reason": reason}
    return None


def _sweep_running() -> bool:
    lock = _LOCK
    return lock is not None and lock[1].locked()


def _start_thread(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True, name="auto-revive").start()


def auto_tick(
    srv=None,
    now: Optional[float] = None,
    start: Optional[Callable[..., None]] = None,
) -> Optional[str]:
    """Bring back at most one killed agent (once per instances tick). Returns
    the title handed to a launch, else ``None``. Waits — without spending the
    agent's budget — while a revive is in flight, a sweep runs, the last
    launch was under :data:`GAP_SECONDS` ago, or memory reads low; gives up on
    an agent revived :data:`AUTO_MAX_PER_WINDOW` times this hour. The launch
    itself runs on its own thread (``start``), off the tick. Never raises."""
    if srv is None:
        srv = _server()
    if now is None:
        now = time.time()
    start = start or _start_thread
    try:
        with _AUTO_LOCK:
            if not _AUTO_PENDING:
                return None
            if not _auto_on():
                _AUTO_PENDING.clear()
                return None
            if _AUTO_RUN["busy"] or now - float(_AUTO_RUN["last"]) < GAP_SECONDS:
                return None
            waiting = len(_AUTO_PENDING)
        if _sweep_running():
            return None
        if memory_pressure.is_low():
            if not _AUTO_RUN["held"]:
                _AUTO_RUN["held"] = True
                _info(
                    "auto-revive: holding %d killed agent(s) — %s",
                    waiting,
                    memory_pressure.describe(),
                )
            return None
        _AUTO_RUN["held"] = False
        with _AUTO_LOCK:
            title = min(_AUTO_PENDING, key=lambda t: _AUTO_PENDING[t][0])
            recent = [
                t for t in _AUTO_HISTORY.get(title, []) if now - t < AUTO_WINDOW_SECONDS
            ]
            if len(recent) >= AUTO_MAX_PER_WINDOW:
                _AUTO_PENDING.pop(title, None)
                _AUTO_HISTORY[title] = recent
                _AUTO_GAVE_UP[title] = (
                    "Killed %d times in the last hour (likely out of memory) — "
                    "left Offline so it can't take the machine down again; "
                    "Restart it when there is room" % (len(recent) + 1)
                )
                gave_up = True
            else:
                recent.append(now)
                _AUTO_HISTORY[title] = recent
                _AUTO_RUN["busy"] = True
                _AUTO_RUN["last"] = now
                gave_up = False
        if gave_up:
            _info(
                "auto-revive %s: not restarted — killed %d times in the last "
                "hour; left Offline",
                title,
                len(recent) + 1,
            )
            return None
        try:
            start(_auto_revive_one, srv, title, len(recent))
        except Exception:  # noqa: BLE001 — no thread: free the slot
            with _AUTO_LOCK:
                _AUTO_RUN["busy"] = False
            return None
        return title
    except Exception:  # noqa: BLE001 — housekeeping never breaks the tick
        return None


def _auto_revive_one(srv, title: str, nth: int = 1) -> None:
    """Relaunch one killed agent via :func:`relaunch` (re-checking the skip
    rules — paused, usage limit, quit, workspace — first). One log line.
    Blocking; runs on its own thread."""
    revived = attempted = False
    try:
        with _AUTO_LOCK:
            code = (_AUTO_PENDING.get(title) or (0.0, None))[1]
        inst = srv.ENGINE.instances.get(title)
        reason = SKIP_GONE if inst is None else None
        if reason is None:
            reason = skip_reason(srv, inst, title, time.time())
        if reason is None and _alive(srv, title):
            reason = "already running"
        if reason is not None:
            _info("auto-revive %s: not restarted (%s)", title, reason)
            return
        t0 = time.monotonic()
        attempted = True
        _name, err, continued = relaunch(inst, title, srv)
        if err is not None:
            with _AUTO_LOCK:
                _AUTO_GAVE_UP[title] = "Restart after it was killed failed: %s" % err
            _info("auto-revive %s: not started (%s)", title, err)
            return
        revived = True
        _info(
            "auto-revive %s: agent relaunched after it was killed (%s; %d/%d this "
            "hour, %.1fs)%s",
            title,
            "exit %d" % code if code is not None else "died mid-turn, no exit code",
            nth,
            AUTO_MAX_PER_WINDOW,
            time.monotonic() - t0,
            " — queued a continue" if continued else "",
        )
    except Exception as err:  # noqa: BLE001 — one bad session
        _info("auto-revive %s: failed (%s)", title, err)
    finally:
        with _AUTO_LOCK:
            _AUTO_PENDING.pop(title, None)
            if revived:
                _AUTO_REVIVED_AT[title] = time.time()
            if not attempted and _AUTO_HISTORY.get(title):
                # Nothing launched: give the reservation back.
                _AUTO_HISTORY[title].pop()
            _AUTO_RUN["busy"] = False
        try:
            srv._republish_session(title)
        except Exception:  # noqa: BLE001
            pass


def _auto_reset_for_tests() -> None:
    with _AUTO_LOCK:
        _AUTO_LIVE.clear()
        _AUTO_PENDING.clear()
        _AUTO_HISTORY.clear()
        _AUTO_GAVE_UP.clear()
        _AUTO_REVIVED_AT.clear()
        _AUTO_RUN.update({"busy": False, "last": 0.0, "held": False})


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
