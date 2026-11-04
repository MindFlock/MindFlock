"""Held offline: a session whose agent died stays down until a person starts it.

After a crash (a WSL restart under memory pressure, the OOM killer taking an
agent) every way MindFlock used to bring an agent back by itself — the boot
revive sweep, the auto-revive of a killed agent, a pane attaching, a queued
prompt — refilled the memory that had just run out, and the machine went down
again. So, while ``general.revive_on_start`` is off (the default), a session
whose agent DIED is **held**: nothing starts it but a person's explicit Start.

* **When** — at server start, every session whose persisted state says its
  agent was running but whose agent tmux is gone
  (:func:`backend.web.core.revive.hold_on_boot`); and while the server runs,
  an agent it saw running that dies uncleanly (:func:`backend.web.core.revive.
  observe`). A clean quit, a pause and a sleep are never held.
* **What it does** — :func:`backend.web.core.agent_sessions.
  _ensure_agent_session` refuses a held session with ``HELD_REFUSED`` (the
  terminal socket closes 4424, ``/send`` queues the prompt and answers 409
  ``{"held": true}``, the queue drain keeps its items), and the sweeps skip it.
* **Release** — ``POST /api/instances/{title}/revive`` and ``POST
  /api/server/revive-all`` (the person's Start / Start all), removing or
  closing the session, pausing it, moving it, and seeing its agent alive again
  (something outside MindFlock started it).

The records live in ``<config dir>/held.json`` (``title -> {since, reason}``),
so a hold survives the next crash. ``MINDFLOCK_HELD_FILE`` points it elsewhere
(the test suite does). Keyed by title like :mod:`backend.web.core.sleep`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

from backend import config
from backend.web.core import title_store

#: Why a session is held (the record's ``reason``; the row's ``held_reason``).
REASON_RESTART = "the server restarted"
REASON_KILLED = "out of memory (killed)"
REASON_CRASH = "the agent crashed"
REASONS = (REASON_RESTART, REASON_KILLED, REASON_CRASH)

_LOCK = threading.Lock()
#: ``(path, {title: record})`` — keyed by path so a per-test store reloads.
_CACHE: Optional[Tuple[str, Dict[str, dict]]] = None


def _path() -> str:
    env = os.environ.get("MINDFLOCK_HELD_FILE", "")
    if env:
        return env
    return os.path.join(config.GetConfigDir(), "held.json")


def _clean(d: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            since = float(v.get("since") or 0.0)
        except (TypeError, ValueError):
            continue
        reason = str(v.get("reason") or "")
        out[str(k)] = {
            "since": since,
            "reason": reason if reason in REASONS else REASON_CRASH,
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
        title_store.persist(lambda: path, _CACHE[1], "held sessions")


def hold(title: str, reason: str) -> bool:
    """Hold ``title`` offline. A session already held keeps its first record
    (when and why it went down). True when this call made the hold. Never
    raises."""
    if reason not in REASONS:
        reason = REASON_CRASH
    try:
        with _LOCK:
            recs = _load()
            if str(title) in recs:
                return False
            recs[str(title)] = {"since": time.time(), "reason": reason}
            _persist()
        return True
    except Exception:  # noqa: BLE001
        return False


def release(title: str) -> bool:
    """Drop ``title``'s hold (missing is fine). True when there was one.
    Never raises."""
    try:
        with _LOCK:
            if _load().pop(str(title or ""), None) is None:
                return False
            _persist()
        return True
    except Exception:  # noqa: BLE001
        return False


def info(title: str) -> Optional[dict]:
    """``{"since", "reason"}`` while ``title`` is held, else None. Never
    raises — a row must never fail to build over this."""
    try:
        with _LOCK:
            rec = _load().get(str(title or ""))
            return dict(rec) if rec else None
    except Exception:  # noqa: BLE001
        return None


def is_held(title: str) -> bool:
    """Whether ``title`` is held offline. A dict read."""
    return info(title) is not None


def held() -> Dict[str, dict]:
    """Every hold: ``{title: {"since": epoch, "reason": str}}`` (a copy)."""
    try:
        with _LOCK:
            return {t: dict(r) for t, r in _load().items()}
    except Exception:  # noqa: BLE001
        return {}


def release_all() -> int:
    """Drop every hold (``general.revive_on_start`` on: nothing is held).
    Returns how many there were."""
    try:
        with _LOCK:
            recs = _load()
            n = len(recs)
            if n:
                recs.clear()
                _persist()
        return n
    except Exception:  # noqa: BLE001
        return 0


def row_fields(title: str) -> dict:
    """The instance row's hold fields: ``{"held_offline": True, "held_since",
    "held_reason"}`` while held, else ``{}`` (omitted)."""
    rec = info(title)
    if rec is None:
        return {}
    return {
        "held_offline": True,
        "held_since": rec["since"],
        "held_reason": rec["reason"],
    }


def drop(title: str) -> None:
    """Forget a removed session: titles are reused, and a namesake must not
    come back held."""
    release(title)


def prune(live_titles: Iterable[str]) -> None:
    """Drop the holds of sessions no longer in the engine."""
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


def notice_text(n: int) -> str:
    """The one boot notice: ``N sessions stayed offline after the restart —
    press Start on the ones you need.``"""
    return "%d %s offline after the restart — press Start on the ones you need." % (
        n,
        "session stayed" if n == 1 else "sessions stayed",
    )
