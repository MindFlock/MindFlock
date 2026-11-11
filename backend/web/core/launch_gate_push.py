"""Push the launch gate's queue changes to the UI (core.launch_gate.on_change).

The gate calls ``launch_gate.on_change`` (no args) whenever its queue changes:
an item joins or leaves, starts, is cancelled, or the shown reason moves on.
It may call from any thread (the drainer, a route's worker thread, a request
on the loop) and sometimes with the gate's lock held, so the nudge itself only
sets a flag and schedules one push on the server's loop. A burst (a restore
of ten items, a drain step that starts one and re-words the rest) is one push
:data:`DELAY_SECONDS` later.

One push (:func:`push`, blocking, run on the loop's default executor):

* a row that is still waiting has its ``waiting_for_memory`` patched into the
  published ``/api/instances`` snapshot (a dict copy — no probes);
* a row that stopped waiting (its launch started, failed or was cancelled) is
  republished in full (``server._republish_session``), since its status and
  activity move with it;
* ``system.memory_changed`` is emitted with ``old`` == ``new`` == the level
  now and ``data: {"cause": "launch_gate", "gate": launch_gate.status(),
  "message"}`` when the queue's shape changed (who waits, their positions,
  what holds them) — not for a reason's wording alone, which changes every
  drain step while spacing holds ("the last agent started 12s ago"). The UI
  re-reads the rows and the memory reading on it; a user hook on
  ``system.memory_changed`` that only cares about level changes skips
  ``old == new``.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from backend.web.core import events as _events
from backend.web.core import launch_gate as _launch_gate

#: The coalescing window of one push.
DELAY_SECONDS = 0.3

_LOCK = threading.Lock()
#: Serializes pushes (two executor threads must not interleave their diffs).
_PUSH_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "pending": False,
    # title -> (position, why, reason) as of the last push
    "shown": {},
}


def _shape(rec: dict) -> Tuple:
    return (rec.get("position"), rec.get("why") or "", rec.get("reason") or "")


def install(srv, loop, delay: float = DELAY_SECONDS) -> None:
    """Set ``launch_gate.on_change`` to a thread-safe, coalescing nudge that
    runs :func:`push` on ``loop``'s default executor ``delay`` s later."""

    def _fire() -> None:
        with _LOCK:
            _STATE["pending"] = False
        try:
            loop.run_in_executor(None, push, srv)
        except RuntimeError:  # the loop is closing
            pass

    def _nudge() -> None:
        with _LOCK:
            if _STATE["pending"]:
                return
            _STATE["pending"] = True
        try:
            loop.call_soon_threadsafe(loop.call_later, delay, _fire)
        except RuntimeError:  # the loop is closed: nothing left to tell
            with _LOCK:
                _STATE["pending"] = False

    _launch_gate.on_change = _nudge


def _patch_waiting(title: str, rec: dict) -> None:
    """Patch ``title``'s published row with its fresh waiting record (only
    when a row for it is published already)."""
    for row in _events.sessions_snapshot():
        if row.get("title") == title:
            row["waiting_for_memory"] = rec
            _events.patch_session_snapshot(title, row)
            return


def _level_now() -> Tuple[str, str]:
    try:
        from backend import memory_pressure as _mem

        snap = _mem.snapshot()
        return str(snap.get("level") or "unknown"), _mem.describe(snap)
    except Exception:  # noqa: BLE001 — the push still goes out
        return "unknown", ""


def push(srv) -> Optional[dict]:
    """One push (see the module doc) → the emitted envelope, or None when the
    queue's shape had not changed. Blocking; never raises."""
    try:
        with _PUSH_LOCK:
            queued = {it["title"]: it for it in _launch_gate.items()}
            now = {t: _shape(r) for t, r in queued.items()}
            with _LOCK:
                before: Dict[str, Tuple] = dict(_STATE["shown"])  # type: ignore[arg-type]
                _STATE["shown"] = now
            instances = srv.ENGINE.instances
            for title, rec in queued.items():
                if before.get(title) != now[title] and title in instances:
                    _patch_waiting(title, rec)
            for title in before:
                if title not in queued and title in instances:
                    srv._republish_session(title)
            # (position, why) only: a reason's wording alone is no news.
            if {t: s[:2] for t, s in before.items()} == {
                t: s[:2] for t, s in now.items()
            }:
                return None
            level, message = _level_now()
            return _events.BUS.emit(
                "system.memory_changed",
                old=level,
                new=level,
                data={
                    "cause": "launch_gate",
                    "gate": _launch_gate.status(),
                    "message": message,
                },
            )
    except Exception:  # noqa: BLE001 — a UI nudge never breaks the gate
        return None


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE["pending"] = False
        _STATE["shown"] = {}
