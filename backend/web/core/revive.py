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
  flooded, and runs only while ``general.revive_on_start`` is on (opt-in;
  unset = OFF). ``POST /api/server/revive-all`` runs the same sweep on demand
  — a person's Start all, which releases the holds below.

* **Held offline** (``general.revive_on_start`` off, the default) — a crash
  must not refill the memory that just ran out, so nothing relaunches an agent
  that died: :func:`hold_on_boot` (run by the lifespan BEFORE the server
  answers anything) holds every session the sweep would have revived
  (:mod:`backend.web.core.held`), and :func:`observe` holds an agent that dies
  uncleanly while the server runs. A held session is refused by
  ``_ensure_agent_session`` and skipped by every sweep (``held offline``)
  until a person's Start (``/revive``, ``/revive-all``) releases it.
  :func:`announce_held` says once, after the boot quiet window, how many
  stayed offline.

* **Memory** — reviving a dozen agents straight after the OOM killer took
  them down is how it happens again. Before each launch the sweep checks
  :mod:`backend.memory_pressure`, counting every agent started in the last
  :data:`WARMUP_SECONDS` (by any path: :mod:`backend.web.core.launch_gate`
  reads tmux's own creation times) as still growing. While that reads low it
  waits (up to :data:`MEMORY_WAIT_SECONDS`) for headroom. If none comes, the
  rest of the sweep is handed to the launch gate's queue (``low memory``,
  and the summary's ``waiting``): the rows say "Waiting for memory", and the
  queue starts them one at a time as memory allows.
* **Continue** — a relaunch resumes the conversation, but not the turn the
  agent was in the middle of: it comes back parked at its prompt. When the
  agent died mid-turn (:func:`died_mid_turn`) a short "continue where you
  left off" is put at the FRONT of its prompt queue, which sends it once the
  CLI has drawn its prompt and settled idle (and never over a dialog or a
  person typing). It is an ordinary queue item: visible, and removable before
  it goes.

* **After a kill** — :func:`observe` / :func:`auto_tick`, driven by the
  server's 4s instances tick: an agent this server saw running that then dies
  to the OOM killer (exit marker 137; 143 right after an OOM kill — systemd
  stopping its scope, :func:`oom_seen_recently`; or no marker and its hooks
  last said ``working`` — a SIGKILL fires no Stop hook) is relaunched through
  :func:`relaunch` (so a mid-turn death gets its continue) once memory reads
  ok again — one at a time, :data:`GAP_SECONDS` apart, at most
  :data:`AUTO_MAX_PER_WINDOW` times per session per hour; after that it stays
  Offline with the reason on its row. The same skip rules apply, and
  ``general.revive_on_start`` switches it too (off: the agent is HELD
  instead). Its trip through Offline is not announced (:func:`holds_offline`),
  so a revived agent rings no bell.

One log line per revive, one summary line per sweep.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from backend import log, memory_pressure, providers, session
from backend.web.core import launch_gate

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
#: Held offline after a crash (core.held): only a person's Start brings it back.
SKIP_HELD = "held offline"
#: Its agent's CLI isn't installed (:func:`missing_agent`): a launch would
#: only fail again the same way.
SKIP_MISSING = "agent not installed"
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
    """``general.revive_on_start`` (unset = OFF: sessions whose agent died are
    held offline instead). Never raises."""
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().general.revive_on_start is True
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return False


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
    srv,
    inst,
    title: str,
    now: float,
    include_quit: bool = False,
    *,
    include_held: bool = False,
    check_limit: bool = True,
) -> Optional[str]:
    """Why ``inst`` must NOT be relaunched (``None`` = revive it). Cheap
    checks first; the usage meter (possibly a network read) last.
    ``include_quit``: an agent the user quit counts too (the rail's "Restart
    all", which restarts every row whose chip offers Restart).
    ``include_held``: a session held offline counts too (a person's Start
    all — the caller releases the hold right before the launch).
    ``check_limit``: False skips the usage-limit check (:func:`hold_on_boot`
    holds a limited session like any other)."""
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
    if not include_held:
        try:
            from backend.web.core import held as _held

            if _held.is_held(title):
                return SKIP_HELD
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
    if missing_agent(srv, inst, title) is not None:
        return SKIP_MISSING
    if check_limit and _limited(srv, inst, title, now):
        return SKIP_LIMIT
    return None


#: The exit codes of a command that never ran: not found, not executable.
_NEVER_RAN = (126, 127)


def missing_agent(srv, inst, title: str) -> Optional[str]:
    """The provider name of ``title``'s agent when its CLI isn't installed:
    its last launch exited 127/126 (the shell couldn't find or run it) AND
    its binary (honouring a saved binary path) is still not on PATH. Both,
    so neither a stale marker after an install nor a PATH quirk alone
    refuses a Start. ``None`` otherwise, and on any doubt. Blocking (a
    marker read). Never raises."""
    try:
        from backend.session import tmux as _tmux

        code = srv._read_exit_marker(_tmux.to_mindflock_tmux_name(title))
        if code not in _NEVER_RAN:
            return None
        if _alive(srv, title):
            return None
        import shutil

        from backend import doctor

        name = providers.resolve(getattr(inst, "Program", "") or "").name
        if not name or name == "generic":
            return None
        if shutil.which(doctor._resolve_agent_binary(name)):
            return None
        return name
    except Exception:  # noqa: BLE001 — doubt means "try the launch"
        return None


def missing_agent_error(name: str) -> str:
    """What a refused Start says about agent ``name``'s missing CLI."""
    try:
        from backend.web.addons.connections import agent_product_name

        who = agent_product_name(name)
    except Exception:  # noqa: BLE001
        who = name
    return (
        "%s isn't installed on this computer. Install it in Settings → "
        "Agents, then press Start." % who
    )


def _alive(srv, title: str) -> bool:
    """The agent tmux for ``title`` is already running (nothing to revive)."""
    try:
        from backend.session import tmux

        return srv._live_session_name(tmux.to_mindflock_tmux_name(title)) is not None
    except Exception:  # noqa: BLE001
        return False


def plan(
    srv,
    now: Optional[float] = None,
    only: Optional[List[str]] = None,
    include_held: bool = False,
) -> Tuple[List[str], Dict[str, str], int]:
    """``(to_revive, skipped, alive)``: the titles to relaunch, oldest-first by
    last activity; ``{title: reason}`` for the ones left alone; and how many
    already have a live agent. ``only``: just these titles, and an agent the
    user quit counts too (the rail's "Restart all" names the rows whose chip
    offers Restart). ``include_held``: sessions held offline count too (a
    person's Start all). Blocking (tmux + meter reads) — run it off the event
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
        reason = skip_reason(
            srv, inst, title, now, include_quit, include_held=include_held
        )
        if reason is not None:
            skipped[title] = reason
            continue
        if _alive(srv, title):
            alive += 1
            continue
        todo.append((_last_seen(inst), title))
    todo.sort()
    return [t for _, t in todo], skipped, alive


def _revive_one(
    srv, title: str, include_quit: bool = False, release_held: bool = False
) -> Tuple[bool, str]:
    """Relaunch ``title``'s agent through ``_ensure_agent_session`` (re-checking
    the skip rules first: minutes may have passed since the plan). Returns
    ``(revived, skip_reason_or_error)`` — on success the second item is
    :data:`CONTINUED` when the agent was told to carry on, else ``""``.
    ``release_held``: a person's Start all — a held session's hold is
    released right before its launch. Blocking."""
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return False, SKIP_GONE
    reason = skip_reason(
        srv, inst, title, time.time(), include_quit, include_held=release_held
    )
    if reason is not None:
        return False, reason
    t0 = time.monotonic()
    # Counted as warming from now (core.launch_gate), and no longer waiting
    # in its queue: this is its start.
    launch_gate.cancel(title)
    launch_gate.note_start(title)
    _name, err, continued = relaunch(inst, title, srv, release=release_held)
    if err is not None:
        # Not an agent warming up: hand the launch slot back.
        launch_gate.unnote_start(title)
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


def relaunch(
    inst, title: str, srv=None, release: bool = False
) -> Tuple[str, Optional[str], bool]:
    """``_ensure_agent_session`` plus the continue: ``(name, error, continued)``.
    Whether the agent died mid-turn is read BEFORE the launch (the launch
    starts a new incarnation, after which the old marker no longer counts).
    The one path both the Restart chip and the sweep take. ``release``: a
    person's explicit Start — a hold (core.held) is released first, and put
    back when the launch fails, so a session that could not start stays
    held rather than up for grabs. Blocking."""
    if srv is None:
        srv = _server()
    from backend.web.core import held as _held

    # A CLI that isn't installed fails the same way on every launch: say so
    # rather than relaunch it (and, held, stay held).
    missing = missing_agent(srv, inst, title)
    if missing is not None:
        return "", missing_agent_error(missing), False
    was_held = _held.info(title) if release else None
    if was_held is not None:
        _held.release(title)
    mid_turn = died_mid_turn(srv, inst, title)
    name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        if was_held is not None:
            _held.hold(title, was_held["reason"])
        return name, err, False
    continued = mid_turn and queue_continue(srv, title)
    return name, None, continued


#: What one just-launched agent is still going to take as it warms up (the
#: CLI, its MCP servers, the first turn) — an agent measures ~350-800 MB a
#: minute in, but reads near zero the second it starts. A sweep counts this
#: for every agent it started in the last :data:`WARMUP_SECONDS`, so "Start
#: all" on 18 held sessions can't pass 18 memory checks before any of them
#: has grown (2026-11-04: that filled the machine within a minute of a boot).
AGENT_WARMUP_BYTES = 700 * 1024 * 1024
WARMUP_SECONDS = 90.0


def _headroom_ok(warming: int) -> bool:
    """Memory reads ok AND would still read ok once ``warming`` agents that
    were just started have grown. Unknown reads as ok."""
    if memory_pressure.is_low(fresh=True):
        return False
    if warming <= 0:
        return True
    snap = memory_pressure.snapshot()
    total = float(snap.get("total") or 0)
    if not total:
        return True
    low_share = float(memory_pressure.thresholds().get("low", (0.15,))[0] or 0.0)
    projected = float(snap.get("available") or 0) - warming * AGENT_WARMUP_BYTES
    return projected / total > low_share


def _wait_for_memory(srv, deadline: float, warming=None) -> bool:
    """Block until memory reads ok — counting the agents still warming up
    (``warming()`` → how many were started in the last
    :data:`WARMUP_SECONDS`) — (True) or ``deadline`` (monotonic) passes
    (False)."""
    while True:
        if _headroom_ok(warming() if warming else 0):
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
    release_held: bool = False,
) -> dict:
    """Revive every eligible session (see the module docstring) and return
    ``{"revived": [...], "skipped": {title: reason}, "already_running": n,
    "continued": [...], "summary": "revived N, skipped M: reasons"}``.
    ``only`` restricts the sweep to those titles (see :func:`plan`).
    ``release_held``: a person's Start all — sessions held offline are
    started too (each hold released right before its launch; one skipped for
    low memory stays held). One sweep at a time: a second caller waits for
    the first, then runs its own (cheap — whatever the first revived is alive
    by then)."""
    if srv is None:
        srv = _server()
    async with _sweep_lock():
        todo, skipped, alive = await asyncio.to_thread(
            plan, srv, None, only, release_held
        )
        include_quit = only is not None
        revived: List[str] = []
        continued: List[str] = []
        out_of_memory = False
        started_at: List[float] = []

        def _warming() -> int:
            # This sweep's own starts, or every agent the launch gate sees
            # warming (other paths, other processes), whichever is more.
            now = time.monotonic()
            mine = sum(1 for t in started_at if now - t < WARMUP_SECONDS)
            try:
                seen = int(launch_gate.assess().get("warming") or 0)
            except Exception:  # noqa: BLE001
                seen = 0
            return max(mine, seen)

        waiting: List[str] = []

        def _queue_rest(title: str) -> None:
            # Out of memory for now: the launch gate's queue starts it later
            # (the row says "Waiting for memory"). A full queue leaves it
            # Offline/held, as before.
            def _later() -> Optional[str]:
                ok, why = _revive_one(srv, title, include_quit, release_held)
                return None if ok else (why or "failed")

            try:
                launch_gate.enqueue(
                    title,
                    _later,
                    kind="revive",
                    source=trigger,
                    reason=memory_pressure.describe() or "waiting for memory",
                    args={"include_quit": include_quit, "release_held": release_held},
                )
                waiting.append(title)
            except launch_gate.QueueFull as full:
                _info("revive %s: left offline, not queued (%s)", title, full)

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
                    _wait_for_memory, srv, time.monotonic() + memory_wait, _warming
                ):
                    if not out_of_memory:
                        _info(
                            "revive sweep (%s): holding the rest — %s",
                            trigger,
                            memory_pressure.describe(),
                        )
                    out_of_memory = True
                    skipped[title] = SKIP_MEMORY
                    _queue_rest(title)
                    continue
                started_at.append(time.monotonic())
                try:
                    ok, why = await asyncio.to_thread(
                        _revive_one, srv, title, include_quit, release_held
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
        if waiting:
            out["waiting"] = waiting
            out["summary"] += "; %d waiting for memory" % len(waiting)
        _info("revive sweep (%s): %s", trigger, out["summary"])
        return out


def _restore_revive(rec: dict):
    """A saved revive after a restart (core.launch_gate): its launch, or None
    (dropped) when the session is gone."""
    title = str(rec.get("title") or "")
    srv = _server()
    if title not in srv.ENGINE.instances:
        return None
    args = rec.get("args") or {}
    include_quit = bool(args.get("include_quit"))
    release_held = bool(args.get("release_held"))

    def _later() -> Optional[str]:
        ok, why = _revive_one(srv, title, include_quit, release_held)
        return None if ok else (why or "failed")

    return _later


launch_gate.register_kind("revive", _restore_revive)


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


# --------------------------------------------------------------------------- #
# OOM evidence: was an exit 143 the OOM killer's doing?
# --------------------------------------------------------------------------- #
# An agent runs in its own systemd scope (providers.agent_limits). Before
# that scope carried ``OOMPolicy=continue``, the OOM killer taking ONE process
# in it (a pytest worker over MemoryMax) made systemd STOP the whole unit:
# the agent got SIGTERM and exited 143 — read as "the agent crashed", and
# never auto-revived (only 137 was). Scopes launched before the fix still
# behave that way, and a machine-wide OOM can end the same. So a 143 counts
# as killed when an OOM kill was seen shortly before: the fleet slice's own
# ``memory.events`` ``oom_kill`` counter rising (hierarchical: every agent
# scope under it, and it outlives a collected scope), or the kernel-wide
# counter :mod:`backend.memory_pressure` already tracks.

#: Exit code of a process ended by SIGTERM (128 + 15) — how systemd stops a
#: unit, OOMPolicy=stop included.
TERM_EXIT_CODE = 143
#: An OOM kill this recent explains a SIGTERM/SIGKILL death.
OOM_EVIDENCE_SECONDS = 300.0
_OOM_LOCK = threading.Lock()
#: The fleet slice's last ``oom_kill`` count and the epoch it last rose.
_OOM_SEEN: Dict[str, Optional[float]] = {"count": None, "at": None}


def _fleet_oom_events_path() -> Optional[str]:
    """``memory.events`` of the agents' slice under this user's systemd
    manager (cgroup v2), or None where there is no such thing."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    from backend.providers import agent_limits

    uid = getuid()
    stem = agent_limits.FLEET_SLICE
    if stem.endswith(".slice"):
        stem = stem[: -len(".slice")]
    parts = stem.split("-")
    # systemd nests "a-b.slice" inside "a.slice".
    chain = ["-".join(parts[: i + 1]) + ".slice" for i in range(len(parts))]
    return os.path.join(
        "/sys/fs/cgroup/user.slice",
        "user-%d.slice" % uid,
        "user@%d.service" % uid,
        *chain,
        "memory.events",
    )


def _fleet_oom_kills() -> Optional[int]:
    """The fleet slice's cumulative ``oom_kill`` count, or None."""
    try:
        path = _fleet_oom_events_path()
        if not path:
            return None
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                if k == "oom_kill":
                    return int(v.strip())
    except (OSError, ValueError):
        return None
    return None


def _machine_oom_kills() -> int:
    """Recent kernel-wide OOM kills (memory_pressure's window), 0 if none."""
    try:
        return int(memory_pressure.snapshot().get("oom_kills") or 0)
    except Exception:  # noqa: BLE001
        return 0


def note_oom_counters(now: Optional[float] = None) -> None:
    """Read the fleet slice's OOM counter and stamp when it rises. Run each
    instances tick (:func:`auto_tick`) so a baseline exists before a death.
    Never raises."""
    if now is None:
        now = time.time()
    count = _fleet_oom_kills()
    if count is None:
        return
    with _OOM_LOCK:
        prev = _OOM_SEEN["count"]
        if prev is not None and count > prev:
            _OOM_SEEN["at"] = now
        _OOM_SEEN["count"] = count


def oom_seen_recently(now: Optional[float] = None) -> bool:
    """Whether an OOM kill — in the agents' slice or anywhere on the machine
    — happened within :data:`OOM_EVIDENCE_SECONDS`. Never raises."""
    if now is None:
        now = time.time()
    try:
        note_oom_counters(now)
        with _OOM_LOCK:
            at = _OOM_SEEN["at"]
        if at is not None and now - float(at) < OOM_EVIDENCE_SECONDS:
            return True
        return _machine_oom_kills() > 0
    except Exception:  # noqa: BLE001
        return False


def _oom_killed_code(code: Optional[int], now: Optional[float] = None) -> bool:
    """An exit code the OOM killer explains: 137 (SIGKILL) always; 143
    (SIGTERM — systemd stopping the agent's scope under OOMPolicy=stop) when
    an OOM kill was seen just before."""
    from backend.providers import launch_script

    if code == launch_script.KILLED_EXIT_CODE:
        return True
    return code == TERM_EXIT_CODE and oom_seen_recently(now)


def killed_code(srv, inst, title: str) -> Tuple[bool, Optional[int]]:
    """``(killed, exit_code)``: whether ``title``'s dead agent died to the OOM
    killer, never a person's quit. Exit marker 137 (SIGKILL) says so outright;
    143 (SIGTERM) does when an OOM kill was seen just before
    (:func:`oom_seen_recently` — systemd stopped the agent's scope over one
    child's OOM kill); no marker at all (a launch with no exit wrapper, the
    engine's first start) counts only when the agent died mid-turn
    (:func:`died_mid_turn`: a SIGKILL fires no Stop/SessionEnd hook, while a
    quit does). Blocking (file + tmux reads). Never raises."""
    try:
        from backend.session import tmux

        code = srv._read_exit_marker(tmux.to_mindflock_tmux_name(title))
    except Exception:  # noqa: BLE001
        code = None
    if _oom_killed_code(code):
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


#: Readings that mean the agent's CLI is up: a held session showing one was
#: started by something outside MindFlock, so its hold is stale.
_LIVE_READINGS = frozenset({"working", "idle", "clarify", "limit"})


def hold_reason_after_death(srv, inst, title: str) -> Optional[str]:
    """The :mod:`backend.web.core.held` reason for ``title``'s agent that just
    died, or ``None`` when the death is no reason to hold it: a clean quit
    (natural exit code), a sleep (stopped on purpose), a peer-shared session
    (the peer service owns its launches), an agent that is somehow up again,
    or no exit marker with no sign it died mid-turn (undecidable — the same
    line :func:`killed_code` draws). Killed by the OOM killer (137, or 143
    right after an OOM kill) → ``out of memory (killed)``; any other
    unnatural exit → ``the agent crashed``. Blocking
    (marker + tmux reads). Never raises."""
    from backend.web.core import held as _held
    from backend.web.core import sleep as _sleep

    try:
        if _sleep.is_sleeping(title) or (getattr(inst, "PeerShare", "") or ""):
            return None
        if _alive(srv, title):
            return None
        killed, code = killed_code(srv, inst, title)
        if killed:
            return _held.REASON_KILLED
        if code is None:
            return None
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        if provider.is_natural_exit(code):
            return None
        return _held.REASON_CRASH
    except Exception:  # noqa: BLE001
        return None


def _indeterminate(title: str) -> bool:
    """Whether ``title``'s last activity probe got NO answer from tmux
    (:func:`backend.web.core.agent_state.last_probe` ``"unknown"``) and its
    absence was never confirmed — an "offline" made of it is not a death."""
    try:
        from backend.web.core import agent_state as _agent_state

        return _agent_state.last_probe(
            title
        ) == "unknown" and not _agent_state.offline_confirmed(title)
    except Exception:  # noqa: BLE001
        return False


def observe(srv, title: str, inst, activity: str, now: Optional[float] = None):
    """Report one session's activity reading (the instances tick, per row,
    BEFORE it emits state changes). A live reading marks the agent seen (and
    drops a stale hold); the first Offline after one is a death. With
    ``general.revive_on_start`` off (the default) an unclean death HOLDS the
    session offline (:func:`hold_reason_after_death`); with it on, a death by
    SIGKILL (:func:`killed_code`) queues the agent for :func:`auto_tick`.
    Blocking only on a death (marker + tmux reads). Never raises."""
    if now is None:
        now = time.time()
    try:
        if activity != "offline":
            with _AUTO_LOCK:
                _AUTO_LIVE.add(title)
                _AUTO_PENDING.pop(title, None)
                _AUTO_GAVE_UP.pop(title, None)
            if activity in _LIVE_READINGS:
                from backend.web.core import held as _held

                if _held.release(title):
                    _info("held %s: released (its agent is running again)", title)
            return
        if _indeterminate(title):
            # The probe behind this "offline" got no answer (a has-session
            # timeout under load, a tmux error): no evidence of a death.
            return
        with _AUTO_LOCK:
            if title not in _AUTO_LIVE:
                return
            _AUTO_LIVE.discard(title)
        if not _should_run(inst):
            return
        if not enabled():
            reason = hold_reason_after_death(srv, inst, title)
            if reason is not None:
                from backend.web.core import held as _held

                if _held.hold(title, reason):
                    _info(
                        "held %s: offline since %s — it stays down until "
                        "someone presses Start",
                        title,
                        reason,
                    )
            return
        if not _auto_on():
            return
        killed, code = killed_code(srv, inst, title)
        if not killed:
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
    # The OOM counter's baseline, kept fresh so a death can be read against
    # it (:func:`oom_seen_recently`). One small file read per tick.
    note_oom_counters(now)
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
        # Memory, then the launch gate (core.launch_gate): agents still
        # warming up, the live-agent cap, the spacing between automatic
        # starts, and launches already waiting in its queue (they go first).
        why = ""
        if memory_pressure.is_low():
            why = memory_pressure.describe() or "memory is low"
        elif launch_gate.items():
            why = "other launches are waiting for memory"
        else:
            verdict = launch_gate.decide("auto", now=now)
            if verdict["decision"] != "go":
                why = verdict["reason"]
        if why:
            if not _AUTO_RUN["held"]:
                _AUTO_RUN["held"] = True
                _info("auto-revive: holding %d killed agent(s) — %s", waiting, why)
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
        launch_gate.note_start(title, auto=True)
        _name, err, continued = relaunch(inst, title, srv)
        if err is not None:
            launch_gate.unnote_start(title)
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
    with _OOM_LOCK:
        _OOM_SEEN.update({"count": None, "at": None})


#: SIGHUP's exit code: the agent's terminal went away (its tmux server died
#: with the machine/WSL) — the session was simply not running afterwards.
_HANGUP_EXIT_CODE = 129


def _not_running_reason(held_mod) -> str:
    """``held.REASON_NOT_RUNNING`` ("it was not running when the server
    started") once :mod:`backend.web.core.held` defines it — its REASONS set
    is closed (an unknown reason is stored as "crashed") — else the old
    ``the server restarted``."""
    return getattr(held_mod, "REASON_NOT_RUNNING", held_mod.REASON_RESTART)


def boot_reason(srv, inst, code: Optional[int], now: Optional[float] = None) -> str:
    """Why :func:`hold_on_boot` holds a session whose agent is not running,
    from its exit marker: the OOM killer's codes (137; 143 right after an OOM
    kill) → ``out of memory (killed)``; another unnatural exit → ``the agent
    crashed``; no marker, a hang-up (its terminal died with the machine) or
    anything undecidable → it was not running when the server started. The
    restart itself is never the reason: the agent may have died hours
    before it."""
    from backend.web.core import held as _held

    try:
        if code is None or code == _HANGUP_EXIT_CODE:
            return _not_running_reason(_held)
        if _oom_killed_code(code, now):
            return _held.REASON_KILLED
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        if not provider.is_natural_exit(code):
            return _held.REASON_CRASH
    except Exception:  # noqa: BLE001
        pass
    return _not_running_reason(_held)


def _boot_probe(srv, title: str) -> str:
    """``"live"`` / ``"gone"`` / ``"unknown"`` for ``title``'s agent tmux
    (:func:`backend.web.core.agent_sessions.session_probe`; ``srv`` may
    provide its own ``session_probe``). Never raises."""
    try:
        from backend.session import tmux as _tmux

        probe = getattr(srv, "session_probe", None)
        if probe is None:
            from backend.web.core.agent_sessions import session_probe as probe
        return str(probe(_tmux.to_mindflock_tmux_name(title)))
    except Exception:  # noqa: BLE001
        return "unknown"


def hold_on_boot(srv=None, now: Optional[float] = None) -> dict:
    """The server-start half of "held offline" (:mod:`backend.web.core.held`),
    run by the lifespan BEFORE the server answers anything (a pane attaching
    a millisecond later must already be refused). With
    ``general.revive_on_start`` off (the default) every session whose
    persisted state says its agent was running — exactly the ones the
    startup sweep would have revived, a usage-limited one included — and
    whose agent tmux is CONFIRMED gone (tmux answered; a probe that timed
    out holds nothing) is held, with the reason its exit marker gives
    (:func:`boot_reason`). Holds already on disk stay; those of sessions no
    longer in the engine go. With the setting on, every hold is dropped
    instead (old behaviour: the sweep revives them). → ``{"held": [new
    titles], "total": n, "released": n}``. Blocking (tmux reads) — run it off
    the event loop. Never raises."""
    from backend.session import tmux as _tmux
    from backend.web.core import held as _held

    out: dict = {"held": [], "total": 0, "released": 0}
    if srv is None:
        srv = _server()
    if now is None:
        now = time.time()
    try:
        if enabled():
            out["released"] = _held.release_all()
            if out["released"]:
                _info(
                    "held: released %d (general.revive_on_start is on)",
                    out["released"],
                )
            return out
        with srv.ENGINE.lock:
            items = list(srv.ENGINE.instances.items())
        _held.prune([t for t, _ in items])
        for title, inst in items:
            try:
                if _held.is_held(title) or not _should_run(inst):
                    continue
                if (
                    skip_reason(
                        srv, inst, title, now, include_held=True, check_limit=False
                    )
                    is not None
                ):
                    continue  # paused, peer, no workspace, asleep, quit…
                if _alive(srv, title):
                    continue
                probe = _boot_probe(srv, title)
                if probe != "gone":
                    # No answer from tmux (a has-session timeout under the
                    # boot's load, a tmux error): the agent may well be alive.
                    # Holding it would refuse its pane; the instances tick
                    # decides once tmux answers.
                    _info("held %s: not checked at boot (tmux probe: %s)", title, probe)
                    continue
                try:
                    code = srv._read_exit_marker(_tmux.to_mindflock_tmux_name(title))
                except Exception:  # noqa: BLE001
                    code = None
                reason = boot_reason(srv, inst, code, now)
                if _held.hold(title, reason):
                    out["held"].append(title)
            except Exception:  # noqa: BLE001 — one bad session
                continue
        live = {t for t, _ in items}
        out["total"] = sum(1 for t in _held.held() if t in live)
        if out["total"]:
            _info(
                "held: %d session(s) stay offline until someone presses Start "
                "(%d new this boot)",
                out["total"],
                len(out["held"]),
            )
    except Exception as err:  # noqa: BLE001 — a boot must never fail over this
        _info("held: boot check failed (%s)", err)
    return out


async def announce_held(srv=None) -> Optional[dict]:
    """After the boot quiet window: when sessions are held offline, ONE
    ``system.sessions_held`` event (the notify addon's ``sessions_held``
    rule) — "N sessions stayed offline after the restart — press Start on the
    ones you need." — rather than an Offline alarm per session. → the
    envelope, or ``None`` when nothing is held."""
    from backend.web.core import events as _events
    from backend.web.core import held as _held

    if srv is None:
        srv = _server()
    try:
        left = srv._BOOT_QUIET_SECONDS - (time.monotonic() - srv._BOOT_MONO)
    except Exception:  # noqa: BLE001
        left = 0.0
    if left > 0:
        await asyncio.sleep(left + 1.0)
    try:
        with srv.ENGINE.lock:
            live = set(srv.ENGINE.instances)
        titles = sorted(t for t in _held.held() if t in live)
    except Exception:  # noqa: BLE001
        return None
    if not titles:
        return None
    return _events.BUS.emit(
        "system.sessions_held",
        new=str(len(titles)),
        data={
            "count": len(titles),
            "titles": titles,
            "detail": _held.notice_text(len(titles)),
        },
    )


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
        _info(
            "revive sweep (startup): off (general.revive_on_start) — agents "
            "that died stay offline until someone presses Start"
        )
        return None
    return await sweep(srv, trigger="startup")
