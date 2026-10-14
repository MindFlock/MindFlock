"""Sleeping agents: give an idle agent's memory back, wake it on demand.

An open agent window holds its CLI and one copy of every MCP server the user
configured (measured 2026-10-14: ~250 MB of Claude plus ~500 MB of MCP
servers, per window) for as long as the window exists — idle for hours
included. Nothing used to offload it. Sleeping does:

* **Sleep** — kill the session's *agent* tmux session (the CLI, its MCP
  servers, everything in that pane; the shell pane is left alone), then
  reap whatever of that process tree outlived the hangup. The session row,
  worktree, branch and conversation all stay. A sleep record
  (``<config dir>/sleeping.json``, ``title -> {since, reason, freed_mb}``)
  says the agent is ASLEEP, not dead, which every reader of a dead agent
  checks:

  - :func:`backend.web.core.agent_sessions._ensure_agent_session` relaunches
    it RESUMING the conversation (the per-window id from thread markers),
    whatever exit code the hangup left — and clears the record;
  - the revive sweep skips it (``sleeping``), so a reboot doesn't wake
    every sleeper, and :func:`backend.web.core.revive.died_mid_turn` never
    reads it as a crash (no "continue" prompt — it was not mid-turn);
  - the state emitter swallows its ``offline`` (no "agent offline" bell,
    toast or push), and the rail paints it Sleeping, outside the Offline
    count and "Restart all".

  Killing the whole tmux session rather than the pane's process is what
  makes the wake path the ordinary one: a dead agent session is exactly what
  ``_ensure_agent_session`` already knows how to bring back.

* **When** — :func:`tick`, every :data:`TICK_SECONDS`, puts to sleep (at most
  :data:`MAX_PER_PASS` per pass, longest-idle first) every session for which
  :func:`veto` finds nothing: an agent that can resume, in a workspace it
  owns, whose turn ENDED and has stayed ended for ``general.sleep_idle_
  minutes`` (default :data:`DEFAULT_IDLE_MINUTES`; 0 = off), with no
  background job, nothing queued, no message waiting, no team run or
  fast-track chain or working children depending on it, nobody's terminal
  attached and nobody typing. While memory reads low (``memory_pressure``
  low/critical) the wait drops to :data:`PRESSURE_IDLE_MINUTES`.

* **Wake** — :func:`ensure_awake`, the one helper every path that types into
  a session goes through when the session is asleep (a terminal attaching,
  ``/send``, the queue's Send now, the mailbox lane and ``delivery: "now"``,
  MindFlock's own composed messages, ``POST /api/instances/{title}/wake``).
  It relaunches through ``_ensure_agent_session`` and opens the prompt
  queue's post-boot grace, so whatever triggered it is typed only once the
  CLI has drawn its prompt (a prompt is put on the queue rather than typed
  into a booting CLI). The prompt queue's own drain wakes a sleeper simply
  by relaunching it (its ``offline`` path).

Keyed by session title throughout (``foo`` and ``foo-copy`` share a folder
and a branch, never a sleep record); remote rows (``device::title``) are
never here — only this server's ENGINE sessions are swept.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from backend import config, log, memory_pressure, providers, session
from backend.session import tmux
from backend.web.core import title_store

#: ``general.sleep_idle_minutes`` when unset.
DEFAULT_IDLE_MINUTES = 45
#: The wait while memory reads low/critical (never longer than the setting).
PRESSURE_IDLE_MINUTES = 5
#: How often the sweep looks.
TICK_SECONDS = 60.0
#: At most this many sessions put to sleep per pass (each kill + reap takes a
#: few seconds, and the next pass is a minute away).
MAX_PER_PASS = 3
#: After the hangup, how long the pane's process tree gets to exit on its
#: own before survivors are sent SIGTERM, then SIGKILL as long again later.
REAP_WAIT_SECONDS = 3.0
#: A sleep record whose agent tmux is alive again this long after it was
#: written is healed away by the sweep (something relaunched the agent
#: without going through the wake path).
HEAL_AFTER_S = 60.0

# Why a session went to sleep (the record's ``reason``).
REASON_IDLE = "idle"
REASON_MEMORY = "memory"
REASON_MANUAL = "manual"

# Vetoes (stable strings: the route's 409 and the tests name them).
V_ASLEEP = "already asleep"
V_STATUS = "not running"  # not started, paused, provisioning
V_PEER = "peer-shared"
V_IN_PLACE = "in place"
V_NO_RESUME = "its CLI can't resume"
V_WORKSPACE = "workspace missing"
V_NOT_IDLE = "not idle"
V_BACKGROUND = "background jobs running"
V_QUEUE = "prompts queued"
V_MAIL = "messages waiting"
V_SETUP = "setup running"
V_TEAM = "in a team run"
V_AUTOPILOT = "fast-track running"
V_WORKERS = "its workers are running"
V_RECENT = "idle too briefly"
V_TYPED = "typed in recently"
V_WOKE = "woken recently"
V_NO_AGENT = "agent not running"
V_VIEWED = "open in a terminal"
V_TURN = "turn not ended"
V_DIALOG = "dialog on screen"
V_THREAD = "its conversation id is unknown and its folder is shared"

#: Vetoes an explicit sleep (route / CLI) skips: they are about WHEN the
#: sweep may decide on its own, not about whether sleeping is safe.
_TIME_VETOES = frozenset({V_RECENT, V_TYPED, V_WOKE})

#: A child in one of these makes its parent an active orchestrator.
_BUSY_ACTIVITIES = ("working", "clarify", "limit")

_LOCK = threading.Lock()
#: ``(path, {title: record})`` — keyed by path so a per-test store reloads.
_CACHE: Optional[Tuple[str, Dict[str, dict]]] = None
#: Epoch of each title's last wake (the "woken recently" veto). In memory:
#: a restart has no recent wakes worth honouring.
_WOKE_AT: Dict[str, float] = {}
#: Why the pending wake of a title happened, for the log line (set by
#: :func:`ensure_awake` just before it relaunches).
_WAKE_WHY: Dict[str, str] = {}


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        logger.Printf(fmt, *args)


# --------------------------------------------------------------------------- #
# the sleep records
# --------------------------------------------------------------------------- #
def _path() -> str:
    env = os.environ.get("MINDFLOCK_SLEEP_FILE", "")
    if env:
        return env
    return os.path.join(config.GetConfigDir(), "sleeping.json")


def _clean(d: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            since = float(v.get("since") or 0.0)
        except (TypeError, ValueError):
            continue
        out[str(k)] = {
            "since": since,
            "reason": str(v.get("reason") or REASON_IDLE),
            "freed_mb": max(0, int(v.get("freed_mb") or 0)),
        }
    return out


def _load() -> Dict[str, dict]:
    global _CACHE
    try:
        p = _path()
    except OSError:
        p = ""
    if _CACHE is None or _CACHE[0] != p:
        _CACHE = (p, title_store.load(lambda: p, _clean) if p else {})
    return _CACHE[1]


def _persist() -> None:
    if _CACHE is not None and _CACHE[0]:
        path = _CACHE[0]
        title_store.persist(lambda: path, _CACHE[1], "sleeping sessions")


def info(title: str) -> Optional[dict]:
    """``{"since", "reason", "freed_mb"}`` while ``title`` is asleep, else
    None. Never raises — a row must never fail to build over this."""
    try:
        with _LOCK:
            rec = _load().get(str(title or ""))
            return dict(rec) if rec else None
    except Exception:  # noqa: BLE001
        return None


def is_sleeping(title: str) -> bool:
    """Whether ``title``'s agent is asleep (not dead). A dict read."""
    return info(title) is not None


def mark(title: str, reason: str, freed_mb: int = 0) -> None:
    """Record ``title`` as asleep (before its agent is stopped, so no reader
    ever sees the dead agent without the record)."""
    with _LOCK:
        _load()[str(title)] = {
            "since": time.time(),
            "reason": reason,
            "freed_mb": max(0, int(freed_mb or 0)),
        }
        _persist()


def woke(title: str) -> None:
    """``title``'s agent was just relaunched: forget the record and log the
    wake (with what woke it), stamp the "woken recently" clock, and open the
    prompt queue's post-boot grace so the queue drain and the mailbox lane
    type nothing into the CLI before it has drawn its prompt. Called by
    ``_ensure_agent_session`` on every launch of a sleeping session, whoever
    asked for it. Never raises."""
    try:
        with _LOCK:
            rec = _load().pop(str(title), None)
            if rec is not None:
                _persist()
        why = _WAKE_WHY.pop(title, "") or "relaunched"
        if rec is None:
            return
        now = time.time()
        _WOKE_AT[title] = now
        _info(
            "wake %s: %s (asleep %s)",
            title,
            why,
            _span(now - float(rec.get("since") or now)),
        )
        try:
            q = _server()._QUEUE_STATE.setdefault(
                title,
                {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None},
            )
            q["rebooted_at"] = now
            q["armed"] = True
            q["idle_since"] = None
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — a wake must never fail over bookkeeping
        pass


def drop(title: str) -> None:
    """Forget a removed session (missing is fine): titles are reused, and a
    namesake must not come back asleep."""
    _WOKE_AT.pop(title, None)
    _WAKE_WHY.pop(title, None)
    try:
        with _LOCK:
            if _load().pop(str(title), None) is not None:
                _persist()
    except Exception:  # noqa: BLE001
        pass


def prune(live_titles: Iterable[str]) -> None:
    """Drop the records of sessions no longer in the engine."""
    live = set(live_titles or ())
    try:
        with _LOCK:
            recs = _load()
            gone = [t for t in recs if t not in live]
            for t in gone:
                recs.pop(t, None)
            if gone:
                _persist()
    except Exception:  # noqa: BLE001
        pass
    for d in (_WOKE_AT, _WAKE_WHY):
        for t in [t for t in d if t not in live]:
            d.pop(t, None)


def _span(secs: float) -> str:
    secs = max(0.0, float(secs))
    if secs < 90:
        return "%ds" % int(secs)
    if secs < 5400:
        return "%dm" % int(round(secs / 60))
    return "%.1fh" % (secs / 3600)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def idle_minutes() -> int:
    """``general.sleep_idle_minutes`` (unset = :data:`DEFAULT_IDLE_MINUTES`,
    0 = sleeping off). Never raises. A LOCAL import, read per call: the
    store caches by mtime, and a module-level name would NameError into the
    fallback forever."""
    try:
        from backend.config import settings as _settings

        val = _settings.load_settings().general.sleep_idle_minutes
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return DEFAULT_IDLE_MINUTES
    if val is None:
        return DEFAULT_IDLE_MINUTES
    return max(0, int(val))


def threshold_seconds(level: Optional[str] = None) -> Optional[float]:
    """How long an agent must have sat idle before the sweep may sleep it,
    or None when sleeping is off. Memory low/critical caps it at
    :data:`PRESSURE_IDLE_MINUTES`."""
    mins = idle_minutes()
    if mins <= 0:
        return None
    if level is None:
        level = memory_pressure.level()
    if level in ("low", "critical"):
        mins = min(mins, PRESSURE_IDLE_MINUTES)
    return mins * 60.0


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #
def _ppid(pid: int) -> Optional[int]:
    """``pid``'s parent pid (``/proc``, else ``ps``), or None."""
    st = _proc_stat(pid)
    if st is not None:
        return st[0]
    try:
        from backend.web.core import proc as _proc

        out = _proc.run_capped(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return int((out.stdout or b"").strip()) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _outside_clients(name: str) -> int:
    """How many tmux clients are attached to session ``name`` from OUTSIDE
    this server — ``mindflock attach``, a raw ``tmux attach``, SSH, a Windows
    Terminal tab: people the web layer cannot see. This server's own clients
    (its children) are left out: the web terminals are counted by
    ``ownership.terminals`` already, and the engine keeps a PTY attached to
    every session it STARTED in this process (``TmuxSession.restore``), which
    is nobody looking. Unknown reads as 1: when in doubt, someone is."""
    try:
        from backend.web.core import proc as _proc

        out = _proc.run_capped(
            ["tmux", "list-clients", "-t", "=" + name, "-F", "#{client_pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if out.returncode != 0:
            return 1
        pids = [
            int(x)
            for x in (out.stdout or b"").decode("ascii", "replace").split()
            if x.isdigit()
        ]
    except Exception:  # noqa: BLE001
        return 1
    me = os.getpid()
    return sum(1 for pid in pids if _ppid(pid) != me)


def _busy_parents(rows: Iterable[dict]) -> set:
    """Titles with at least one child whose agent is busy (working, on a
    prompt, or on a limit screen): orchestrators whose workers are at it."""
    out = set()
    for r in rows or ():
        if not isinstance(r, dict) or r.get("device"):
            continue
        parent = str(r.get("parent") or "")
        if parent and r.get("activity") in _BUSY_ACTIVITIES and not r.get("sleeping"):
            out.add(parent)
    return out


def _shares_folder(srv, inst, title: str) -> bool:
    """Whether another session works in ``title``'s folder (a copy window)."""
    try:
        wt = os.path.realpath(inst.GetWorktreePath())
    except Exception:  # noqa: BLE001
        return True
    with srv.ENGINE.lock:
        others = [i for t, i in srv.ENGINE.instances.items() if t != title]
    for other in others:
        try:
            if os.path.realpath(other.GetWorktreePath()) == wt:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _thread_known(inst, title: str) -> bool:
    """Whether the wake can name THIS window's conversation (the id its hooks
    recorded), rather than resuming the folder's newest one. A provisioned
    launcher resumes with the CLI's bulk flag (``--continue``) whatever id is
    recorded, so it never can."""
    try:
        from backend.session import provisioned as _provisioned

        wt = inst.GetWorktreePath()
        if os.path.isfile(os.path.join(wt, _provisioned.LAUNCHER_BASENAME)):
            return False
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        return bool(provider.resume_thread_id(tmux.to_mindflock_tmux_name(title)))
    except Exception:  # noqa: BLE001
        return False


def _quiet_since(srv, title: str, row: dict) -> Tuple[float, str]:
    """The newest of the clocks that say something happened in ``title``,
    with the veto each one stands for."""
    clocks = [
        (float(row.get("activity_since") or 0.0), V_RECENT),
        (float(srv._HUMAN_INPUT_AT.get(title, 0.0) or 0.0), V_TYPED),
        (float(_WOKE_AT.get(title, 0.0) or 0.0), V_WOKE),
        (float((srv._QUEUE_STATE.get(title) or {}).get("sent_at") or 0.0), V_RECENT),
        (float((srv._MAIL_STATE.get(title) or {}).get("sent_at") or 0.0), V_RECENT),
    ]
    return max(clocks, key=lambda c: c[0])


def veto(
    srv,
    inst,
    title: str,
    row: Optional[dict],
    now: float,
    idle_s: float,
    *,
    rows: Iterable[dict] = (),
    manual: bool = False,
) -> Optional[str]:
    """Why ``title``'s agent must NOT be put to sleep now (None = sleep it).

    ``row`` is the session's published ``/api/instances`` row (activity,
    background jobs, queue, setup — read, never re-probed); ``rows`` the
    whole listing (for working children); ``idle_s`` the idle threshold.
    Cheap checks first; the probes that shell out (tmux, the screen) last,
    and they re-read live what the row could have gone stale on.
    ``manual`` (the route / CLI) skips the timing vetoes only."""
    if is_sleeping(title):
        return V_ASLEEP
    try:
        if not inst.Started() or inst.Status in (session.Paused, session.Loading):
            return V_STATUS
    except Exception:  # noqa: BLE001
        return V_STATUS
    if getattr(inst, "PeerShare", "") or "":
        return V_PEER
    if getattr(inst, "InPlace", False):
        return V_IN_PLACE
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        if not provider.launcher_spec().resume_flag:
            return V_NO_RESUME
    except Exception:  # noqa: BLE001
        return V_NO_RESUME
    row = row or {}
    if row.get("workspace_missing"):
        return V_WORKSPACE
    if (row.get("activity") or "") != "idle":
        return V_NOT_IDLE
    if row.get("background"):
        return V_BACKGROUND
    if (row.get("setup") or {}).get("state") == "running":
        return V_SETUP
    try:
        from backend.web.core import prompt_queue as _prompt_queue

        if _prompt_queue.get_state(title)["items"]:
            return V_QUEUE
    except Exception:  # noqa: BLE001
        return V_QUEUE
    try:
        from backend.web.core import mailbox as _mailbox

        if _mailbox.next_pending(title) is not None:
            return V_MAIL
    except Exception:  # noqa: BLE001
        return V_MAIL
    if not manual:
        # An unknown reading age (a row the tick has not probed yet) is not
        # "idle for ages".
        if float(row.get("activity_since") or 0.0) <= 0:
            return V_RECENT
        at, why = _quiet_since(srv, title, row)
        if now - at < idle_s:
            return why
    try:
        from backend.web.core import team_runs as _team_runs

        if _team_runs.owner_of_title(title) is not None:
            return V_TEAM
    except Exception:  # noqa: BLE001
        return V_TEAM
    if title in _busy_parents(rows):
        return V_WORKERS
    if _shares_folder(srv, inst, title) and not _thread_known(inst, title):
        # The wake would resume "the folder's newest conversation", which may
        # be a sibling window's: only sleep what comes back as itself.
        return V_THREAD
    try:
        if srv._autopilot_running(title):
            return V_AUTOPILOT
    except Exception:  # noqa: BLE001
        return V_AUTOPILOT
    # --- probes (shell out) ------------------------------------------------ #
    from backend.web.core import ownership as _ownership

    name = tmux.to_mindflock_tmux_name(title)
    if _ownership.terminals(title) > 0:
        return V_VIEWED
    if srv._live_session_name(name) is None:
        return V_NO_AGENT
    if _outside_clients(name) > 0:
        return V_VIEWED
    if not manual and srv._tmux_client_input_recent(title, idle_s):
        return V_TYPED
    try:
        from backend.providers import activity_markers

        if activity_markers.read_activity_marker(name) in ("working", "clarify"):
            return V_TURN
    except Exception:  # noqa: BLE001
        pass
    # Deliberately UNCACHED: the row is up to a tick old, and a turn that
    # started since must not be cut off.
    if srv._agent_activity(inst, title) != "idle":
        return V_NOT_IDLE
    if srv._background_jobs(inst, title):
        return V_BACKGROUND
    if not srv._pane_holds_agent(name, getattr(inst, "Program", "") or ""):
        return V_NO_AGENT  # the user quit it: a bare shell is not a sleeper
    if srv._dialog_on_screen(inst, name):
        return V_DIALOG
    return None


# --------------------------------------------------------------------------- #
# sleeping
# --------------------------------------------------------------------------- #
def _proc_stat(pid: int) -> Optional[Tuple[int, int]]:
    """``(ppid, starttime)`` of ``pid`` from ``/proc``, or None."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
        rest = raw.rsplit(")", 1)[1].split()
        return int(rest[1]), int(rest[19])
    except (OSError, IndexError, ValueError):
        return None


def _pane_tree(name: str) -> List[Tuple[int, int]]:
    """``[(pid, starttime)]`` of every process in tmux session ``name``'s
    panes and all their descendants. ``[]`` when unreadable (non-Linux)."""
    try:
        from backend.web.core import proc as _proc

        out = _proc.run_capped(
            ["tmux", "list-panes", "-s", "-t", "=" + name, "-F", "#{pane_pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if out.returncode != 0:
            return []
        roots = {
            int(x)
            for x in (out.stdout or b"").decode("ascii", "replace").split()
            if x.isdigit()
        }
    except Exception:  # noqa: BLE001
        return []
    if not roots:
        return []
    children: Dict[int, List[int]] = {}
    starts: Dict[int, int] = {}
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        st = _proc_stat(pid)
        if st is None:
            continue
        children.setdefault(st[0], []).append(pid)
        starts[pid] = st[1]
    out_list: List[Tuple[int, int]] = []
    todo = [p for p in roots if p in starts]
    seen = set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out_list.append((pid, starts[pid]))
        todo.extend(children.get(pid, ()))
    return out_list


def _tree_mb(tree: List[Tuple[int, int]]) -> int:
    """The memory ``tree`` holds, in MB: proportional set size (shared pages
    split between their users) where the kernel reports it, else RSS."""
    total = 0
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    for pid, _start in tree:
        kb = 0
        try:
            with open("/proc/%d/smaps_rollup" % pid, encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("Pss:"):
                        kb = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            try:
                with open("/proc/%d/statm" % pid, encoding="ascii") as fh:
                    kb = int(fh.read().split()[1]) * page // 1024
            except (OSError, ValueError, IndexError):
                kb = 0
        total += kb
    return int(total // 1024)


def _alive(pid: int, start: int) -> bool:
    st = _proc_stat(pid)
    return st is not None and st[1] == start


def _reap(tree: List[Tuple[int, int]], wait: float = REAP_WAIT_SECONDS) -> int:
    """After the hangup: SIGTERM whatever of ``tree`` still runs ``wait``
    seconds later (an MCP server that ignores a closed stdin), SIGKILL what
    survives that. A pid is only signalled while its start time still matches
    (never a recycled pid). Returns how many needed a signal."""
    deadline = time.monotonic() + wait
    left = list(tree)
    while left and time.monotonic() < deadline:
        left = [(p, s) for p, s in left if _alive(p, s)]
        if left:
            time.sleep(0.25)
    left = [(p, s) for p, s in left if _alive(p, s)]
    if not left:
        return 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, start in left:
            if _alive(pid, start):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
        end = time.monotonic() + wait
        while time.monotonic() < end and any(_alive(p, s) for p, s in left):
            time.sleep(0.25)
    return len(left)


def put_to_sleep(
    srv, inst, title: str, reason: str, idle_for: float = 0.0
) -> Optional[str]:
    """Stop ``title``'s agent and record it as asleep. None on success, else
    why not. Blocking (tmux + a few seconds of reaping) — off the loop."""
    from backend.web.core import ownership as _ownership

    name = tmux.to_mindflock_tmux_name(title)
    if srv._live_session_name(name) is None:
        return V_NO_AGENT
    # Last look before the kill: a terminal that attached since the veto
    # would be cut off under the person who just opened it.
    if _ownership.terminals(title) > 0 or _outside_clients(name) > 0:
        return V_VIEWED
    tree = _pane_tree(name)
    freed = _tree_mb(tree)
    mark(title, reason, freed)
    srv._kill_agent_session(title)
    if srv._live_session_name(name) is not None:
        drop(title)
        return "the agent did not stop"
    # The hangup may have left an exit code behind; the record, not the code,
    # says what happened (and the wake resumes either way).
    try:
        srv._clear_exit_marker(name)
    except Exception:  # noqa: BLE001
        pass
    signalled = _reap(tree) if tree else 0
    _info(
        "sleep %s: %s%s — freed ~%d MB%s",
        title,
        reason,
        (" after %s idle" % _span(idle_for)) if idle_for > 0 else "",
        freed,
        (" (%d stragglers signalled)" % signalled) if signalled else "",
    )
    try:
        srv._republish_session(title)
    except Exception:  # noqa: BLE001
        pass
    return None


def sleep_now(inst, title: str, srv=None) -> Optional[str]:
    """An explicit sleep (the route, ``mindflock sleep``): :func:`veto` with
    ``manual`` — every safety rule, none of the timing ones — then
    :func:`put_to_sleep`. None on success, else the veto. Blocking."""
    if srv is None:
        srv = _server()
    from backend.web.core import events as _events

    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    row = next(
        (r for r in rows if r.get("title") == title and not r.get("device")), None
    )
    if row is None:
        row = srv._republish_session(title) or {}
    why = veto(srv, inst, title, row, time.time(), 0.0, rows=rows, manual=True)
    if why is not None:
        return why
    return put_to_sleep(srv, inst, title, REASON_MANUAL)


def ensure_awake(inst, title: str, why: str, srv=None) -> Tuple[bool, Optional[str]]:
    """Wake ``title`` if it is asleep → ``(woke, error)``. Every path that
    types into a session calls this first (or relaunches through
    ``_ensure_agent_session``, which ends the sleep the same way). ``why`` is
    what the log line says woke it. Blocking — off the loop."""
    if not is_sleeping(title):
        return False, None
    if srv is None:
        srv = _server()
    _WAKE_WHY[title] = why
    _name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        _WAKE_WHY.pop(title, None)
        _info("wake %s: not started (%s)", title, err)
        return False, err
    # _ensure_agent_session ended the sleep (woke()); a relaunch that found
    # the agent already running (someone started it meanwhile) did too.
    woke(title)
    try:
        srv._republish_session(title)
    except Exception:  # noqa: BLE001
        pass
    return True, None


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #
def tick(srv=None, now: Optional[float] = None) -> List[str]:
    """One pass: put to sleep up to :data:`MAX_PER_PASS` eligible sessions,
    longest-idle first → their titles. Also heals a record whose agent is
    running again (relaunched by something that bypassed the wake path).
    Blocking — off the loop. Never raises."""
    if srv is None:
        srv = _server()
    if now is None:
        now = time.time()
    slept: List[str] = []
    try:
        # A record whose agent runs again is not asleep. (Not a brand-new
        # one: put_to_sleep writes the record a moment BEFORE the kill.)
        with _LOCK:
            asleep = [
                t for t, r in _load().items() if now - float(r["since"]) > HEAL_AFTER_S
            ]
        for title in asleep:
            if srv._live_session_name(tmux.to_mindflock_tmux_name(title)) is not None:
                _WAKE_WHY.setdefault(title, "found running")
                woke(title)
        level = memory_pressure.level()
        idle_s = threshold_seconds(level)
        if idle_s is None:
            return slept
        reason = (
            REASON_MEMORY
            if level in ("low", "critical") and idle_s < (idle_minutes() * 60.0)
            else REASON_IDLE
        )
        from backend.web.core import events as _events

        rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
        by_title = {r.get("title"): r for r in rows if not r.get("device")}
        with srv.ENGINE.lock:
            items = list(srv.ENGINE.instances.items())
        candidates: List[Tuple[float, str, object]] = []
        for title, inst in items:
            row = by_title.get(title)
            if row is None:
                continue
            try:
                why = veto(srv, inst, title, row, now, idle_s, rows=rows)
            except Exception:  # noqa: BLE001 — one bad session can't stop the pass
                continue
            if why is None:
                candidates.append(
                    (float(row.get("activity_since") or now), title, inst)
                )
        candidates.sort(key=lambda c: c[0])
        for i, (since, title, inst) in enumerate(candidates[:MAX_PER_PASS]):
            try:
                # The ones after the first waited out an earlier kill + reap:
                # ask again (the live probes re-read the agent) before acting.
                if i and veto(
                    srv, inst, title, by_title[title], now, idle_s, rows=rows
                ):
                    continue
                if put_to_sleep(srv, inst, title, reason, now - since) is None:
                    slept.append(title)
            except Exception as err:  # noqa: BLE001
                _info("sleep %s: failed (%s)", title, err)
    except Exception:  # noqa: BLE001 — the sweep must never die
        pass
    return slept


async def run_loop(srv=None) -> None:
    """Drive :func:`tick` forever (started by the server lifespan). Waits out
    the boot quiet window first: the rows it reads are not settled before."""
    if srv is None:
        srv = _server()
    try:
        left = srv._BOOT_QUIET_SECONDS - (time.monotonic() - srv._BOOT_MONO)
    except Exception:  # noqa: BLE001
        left = 0.0
    if left > 0:
        await asyncio.sleep(left + 1.0)
    while True:
        try:
            await asyncio.to_thread(tick, srv)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(TICK_SECONDS)
