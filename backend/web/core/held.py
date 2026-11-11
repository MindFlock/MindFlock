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
Two servers on one config dir share the file (:class:`title_store.SharedMap`):
each sees the other's holds without a restart and never drops them on save.

A hold outranks a sleep: a title is never both held and asleep. :func:`hold`
drops the sleep record, and ``core.sleep`` drops any it finds on a held title
(the pair two servers' lost updates used to leave on disk).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Iterable, Optional

from backend import config
from backend.web.core import title_store

#: Why a session is held (the record's ``reason``; the row's ``held_reason``).
REASON_RESTART = "the server restarted"
REASON_KILLED = "out of memory (killed)"
REASON_CRASH = "the agent crashed"
#: A boot found it down and its exit marker says nothing about why.
REASON_NOT_RUNNING = "it was not running when the server started"
REASONS = (REASON_RESTART, REASON_KILLED, REASON_CRASH, REASON_NOT_RUNNING)

_LOCK = threading.Lock()


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


#: The records, shared with every other server on this config dir
#: (:class:`title_store.SharedMap`: re-read when the file changes, saved
#: read-merge-write under ``held.json.lock``, so one server's save never
#: drops another's hold).
_STORE = title_store.SharedMap(_path, _clean, "held sessions")


def _load() -> Dict[str, dict]:
    return _STORE.data()


def _persist() -> None:
    _STORE.persist()


def _sleep_yields(title: str) -> None:
    """A hold outranks a sleep (the agent is down and stays down until a
    person's Start): drop ``title``'s sleep record, if any, so the pair —
    "Sleeping, opening it wakes it" over a wake that is refused — never
    stands. Called outside :data:`_LOCK`: ``core.sleep`` reads the holds
    under its own lock, never the other way round."""
    try:
        from backend.web.core import sleep as _sleep

        _sleep.reconcile([title])
    except Exception:  # noqa: BLE001
        pass


def hold(title: str, reason: str) -> bool:
    """Hold ``title`` offline. A session already held keeps its first record
    (when and why it went down); a sleep record it had is dropped (a hold
    outranks a sleep). True when this call made the hold. Never raises."""
    if reason not in REASONS:
        reason = REASON_CRASH
    try:
        with _LOCK, _STORE.transaction() as recs:
            made = str(title) not in recs
            if made:
                recs[str(title)] = {"since": time.time(), "reason": reason}
    except Exception:  # noqa: BLE001
        return False
    _sleep_yields(str(title))
    return made


def release(title: str) -> bool:
    """Drop ``title``'s hold (missing is fine). True when there was one.
    Never raises."""
    try:
        t = str(title or "")
        with _LOCK:
            if t not in _load():
                return False
            with _STORE.transaction() as recs:
                return recs.pop(t, None) is not None
    except Exception:  # noqa: BLE001
        return False


def rename(old: str, new: str) -> None:
    """Move ``old``'s hold to ``new`` (``core.rename_stores`` calls this)."""
    with _LOCK:
        if str(old) not in _load():
            return
        with _STORE.transaction() as recs:
            if str(old) in recs:
                recs[str(new)] = recs.pop(str(old))


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
            if not _load():
                return 0
            with _STORE.transaction() as recs:
                n = len(recs)
                recs.clear()
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
    """Drop the holds of sessions no longer in the engine — nor in the shared
    ``state.json``: a session another server on this config dir just created
    or renamed reaches this server's engine only on its next state sync."""
    live = set(live_titles or ())
    try:
        with _LOCK:
            gone = [t for t in _load() if t not in live]
            if gone:
                on_disk = title_store.state_titles(_STORE.path())
                gone = [t for t in gone if t not in on_disk]
            if gone:
                with _STORE.transaction() as recs:
                    for t in gone:
                        recs.pop(t, None)
    except Exception:  # noqa: BLE001
        pass


def notice_text(n: int) -> str:
    """The one boot notice: ``N sessions stayed offline after the restart —
    press Start on the ones you need.``"""
    return "%d %s offline after the restart — press Start on the ones you need." % (
        n,
        "session stayed" if n == 1 else "sessions stayed",
    )
