"""Process-wide session event bus + user shell hooks (roadmap B1/B3).

Today's UI polls ``/api/instances`` every 4s and diff-computes everything
client-side; nothing in the server *announces* that a session was created,
paused, or changed state. This module is that seam: a small, thread-safe bus
that the route handlers (and the ``list_instances`` poll diff) emit into, that
the ``/api/events`` websocket (B2) streams out of, and that runs the user's
``~/.mindflock/hooks/`` executables on every event (B3).

Event envelope (the exact JSON the websocket sends and the shell hooks get on
stdin)::

    {"seq": 42, "event": "session.status_changed", "session": "my-title",
     "old": "loading", "new": "running", "ts": 1719900000.0, "data": {}}

``seq`` increases monotonically per process; the last ~100 envelopes are kept
in a ring buffer so a (re)connecting websocket client can replay what it missed
(``/api/events?since=<seq>``).

Thread-safety: sync FastAPI routes run in the worker threadpool, so ``emit()``
can be called from any thread. Subscriber callbacks are invoked synchronously
on the emitting thread — a subscriber that lives on the event loop (the
websocket) must trampoline via ``loop.call_soon_threadsafe``. Shell hooks are
fire-and-forget (spawned on the loop, or a throwaway thread when none exists)
and never block the request path.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

from backend import log

# The complete vocabulary. Emitters stick to these; hook authors key their
# ~/.mindflock/hooks/<event-name>/ directories off them.
EVENT_NAMES = (
    "session.created",
    "session.deleted",
    "session.paused",
    "session.resumed",
    "session.status_changed",
    "session.activity_changed",
    "session.stage_changed",
    # J5 cost guardrail: emitted once per session when its estimated cost
    # crosses the configured budget (data: {"cost": float, "budget": float}).
    "session.budget_exceeded",
    # Prompt queue (M-series): a queued prompt was auto-sent to the agent
    # (data: {"text": str, "remaining": int, "loop": bool}); and the queue
    # contents/flags changed (data: {"pending": int, "enabled": bool,
    # "loop": bool}).
    "session.prompt_sent",
    "session.queue_changed",
    # Usage limits (roadmap D): the provider's window reopened for a session
    # that had run out — emitted once per reopening, by the drain-loop watcher
    # that also nudges such a session to carry on (data: {"resumed": bool}).
    # Running OUT needs no event of its own: that is
    # ``session.activity_changed`` with ``new == "limit"``.
    "session.usage_restored",
    # Autopilot: a fast-track / intake run changed state (``new`` is
    # "running"|"halted"|"done"; data: {"depth", "step", "state", "reason",
    # "item", "skipped"}). A halted run always carries a human-readable reason —
    # a chain that stops silently is the one failure mode that destroys trust in
    # the feature.
    "session.autopilot_changed",
)

_HISTORY = 100  # envelopes kept for ?since= replay
_HOOK_TIMEOUT = 10.0  # seconds a user hook may run before it is killed


def _log_error(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# User shell hooks (B3)
#
# ~/.mindflock/hooks/<event-name>/* and ~/.mindflock/hooks/all/* executables run
# on each event with MINDFLOCK_EVENT / MINDFLOCK_SESSION / MINDFLOCK_OLD /
# MINDFLOCK_NEW in the env and the full JSON envelope on stdin. Simple and
# language-agnostic (desktop notification on clarify, Slack ping on PR merge…).
# --------------------------------------------------------------------------- #
def _hooks_root() -> Path:
    return Path(
        os.environ.get(
            "MINDFLOCK_HOOKS_DIR",
            os.path.join(os.path.expanduser("~"), ".mindflock", "hooks"),
        )
    )


def hook_executables(event: str) -> List[Path]:
    """Executable files registered for ``event`` (its own dir, then ``all/``),
    each dir's entries in name order. Missing dirs are simply no hooks."""
    out: List[Path] = []
    root = _hooks_root()
    for sub in (event, "all"):
        if not sub:
            continue
        d = root / sub
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = d / n
            if p.is_file() and os.access(p, os.X_OK):
                out.append(p)
    return out


async def _run_hook(path: Path, env: dict, payload: bytes) -> None:
    """Run one hook: envelope on stdin, 10s budget, failures logged not raised.

    The executable is spawned directly (no shell interpolation of the payload),
    so event fields can never be shell-injected into the hook invocation.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(proc.communicate(payload), timeout=_HOOK_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            _log_error("event hook %s timed out after %ss", str(path), _HOOK_TIMEOUT)
    except Exception as err:  # noqa: BLE001 — a broken hook must never surface
        _log_error("event hook %s failed: %v", str(path), err)


async def run_shell_hooks(envelope: dict) -> None:
    """Run every user hook registered for this envelope's event (awaitable form
    — the bus schedules this fire-and-forget; tests can await it directly)."""
    paths = hook_executables(str(envelope.get("event") or ""))
    if not paths:
        return
    payload = json.dumps(envelope).encode("utf-8")
    env = {
        **os.environ,
        "MINDFLOCK_EVENT": str(envelope.get("event") or ""),
        "MINDFLOCK_SESSION": str(envelope.get("session") or ""),
        "MINDFLOCK_OLD": "" if envelope.get("old") is None else str(envelope["old"]),
        "MINDFLOCK_NEW": "" if envelope.get("new") is None else str(envelope["new"]),
    }
    await asyncio.gather(*(_run_hook(p, env, payload) for p in paths))


# --------------------------------------------------------------------------- #
# The bus (B1)
# --------------------------------------------------------------------------- #
class EventBus:
    """Thread-safe pub/sub with a replayable ring buffer of recent events."""

    def __init__(self, history: int = _HISTORY) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque = deque(maxlen=history)
        self._subs: Dict[int, Callable[[dict], None]] = {}
        self._next_id = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Event loop shell hooks are spawned on (the server's, via lifespan).
        Without one, hooks run on a throwaway daemon thread instead."""
        self._loop = loop

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Register ``callback(envelope)``; returns an unsubscribe callable.

        The callback runs synchronously on whatever thread emits — keep it tiny
        (the websocket subscriber just trampolines onto its loop)."""
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subs[sid] = callback

        def _unsubscribe() -> None:
            with self._lock:
                self._subs.pop(sid, None)

        return _unsubscribe

    def backlog(self, since: int = 0) -> List[dict]:
        """Buffered envelopes with ``seq > since`` (all of them for since=0)."""
        with self._lock:
            return [e for e in self._ring if e.get("seq", 0) > since]

    def emit(
        self,
        event: str,
        *,
        session: str = "",
        old=None,
        new=None,
        data: Optional[dict] = None,
    ) -> dict:
        """Publish one event; returns the sequenced envelope.

        Never raises: a failing subscriber or hook is logged and skipped, so
        emit() is safe to sprinkle through request handlers."""
        envelope = {
            "seq": 0,  # assigned under the lock below
            "event": event,
            "session": session,
            "old": old,
            "new": new,
            "ts": time.time(),
            "data": data or {},
        }
        with self._lock:
            self._seq += 1
            envelope["seq"] = self._seq
            self._ring.append(envelope)
            subs = list(self._subs.values())
        for cb in subs:
            try:
                cb(envelope)
            except (
                Exception
            ) as err:  # noqa: BLE001 — one bad subscriber can't stop the rest
                _log_error("event subscriber failed for %s: %v", event, err)
        self._dispatch_hooks(envelope)
        return envelope

    # --- shell-hook dispatch ------------------------------------------------ #
    def _dispatch_hooks(self, envelope: dict) -> None:
        """Fire-and-forget the user's shell hooks off the request path."""
        try:
            if not hook_executables(str(envelope.get("event") or "")):
                return  # cheap early-out: no hooks installed (the common case)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            target = running or self._loop
            if target is not None and not target.is_closed():
                if running is target:
                    target.create_task(run_shell_hooks(envelope))
                else:
                    asyncio.run_coroutine_threadsafe(run_shell_hooks(envelope), target)
                return
            # No usable loop (bare sync context, e.g. unit tests / CLI): run the
            # hooks on a throwaway daemon thread with its own loop.
            threading.Thread(
                target=lambda: asyncio.run(run_shell_hooks(envelope)), daemon=True
            ).start()
        except Exception as err:  # noqa: BLE001 — hooks are best-effort
            _log_error(
                "event hook dispatch failed for %s: %v", envelope.get("event"), err
            )


# The process-wide bus every emitter/subscriber shares (mirrors ENGINE's
# module-singleton pattern in core.engine).
BUS = EventBus()


# --------------------------------------------------------------------------- #
# Last-known sessions snapshot (Addon API v2, roadmap B4)
#
# The /api/instances poll is where session state (status/activity/stage/tokens)
# is actually computed; it publishes its freshly built payload here so
# AppContext.sessions() can hand addons a read-only view WITHOUT duplicating
# that computation. Empty until the first poll after startup.
# --------------------------------------------------------------------------- #
_SESSIONS_LOCK = threading.Lock()
_SESSIONS_SNAPSHOT: List[dict] = []


def set_sessions_snapshot(sessions: List[dict]) -> None:
    """Publish the latest full ``/api/instances`` payload (the server's poll
    handler calls this on every list)."""
    global _SESSIONS_SNAPSHOT
    with _SESSIONS_LOCK:
        _SESSIONS_SNAPSHOT = [dict(s) for s in sessions]


def patch_session_snapshot(title: str, row: dict) -> None:
    """Replace ONE session's published row (append it if it wasn't there).

    :func:`sessions_snapshot` deliberately hands out copies, so a caller that
    recomputes a single session cannot otherwise reach the list ``GET
    /api/instances`` serves. Without this publish-through the UI flickers
    BACKWARDS: the on-demand fresh read says "pushed", then the next poll serves
    the tick's list, still saying "committed".

    This is a SECOND writer to a structure that had exactly one (the instances
    tick). Last writer wins, and the worst case is one row that is one tick
    stale — the same staleness the tick already has.
    """
    if not title:
        return
    with _SESSIONS_LOCK:
        for i, s in enumerate(_SESSIONS_SNAPSHOT):
            if s.get("title") == title:
                _SESSIONS_SNAPSHOT[i] = dict(row)
                return
        _SESSIONS_SNAPSHOT.append(dict(row))


def sessions_snapshot() -> List[dict]:
    """Copies of the last-known session dicts (mutating them affects nothing)."""
    with _SESSIONS_LOCK:
        return [dict(s) for s in _SESSIONS_SNAPSHOT]
