"""Short-lived, per-session edge watchers that make a finished commit/push
observable in ~250ms instead of at the next 4s snapshot tick.

WHY THIS EXISTS. ``POST /commit`` and ``POST /push-branch`` type a shell
one-liner into the session's interactive tmux (so the user can watch the hooks
run live) and return immediately. There is no exit code and no completion
callback — the ONLY evidence a commit finished is on disk: the lock file in the
private git dir disappears, ``.mindflock_commit_status`` appears, HEAD moves.
Nothing reads that evidence except ``_session_stage``, which the snapshot tick
calls every 4 seconds. So "commit done" took up to a tick to become visible, and
the client's own poll added a second unsynchronised 4s window on top — which is
the multi-second lag between a commit finishing and the guided button offering
"Push".

WHAT IT DOES. For a bounded window after an action, poll a CHEAP local signature
(a file stat, a rev-parse, a status --porcelain — about 10ms total) four times a
second, and when the signature CHANGES, call ``server._republish_session`` once.
The split matters: the signature is cheap and polled often, the republish is
expensive (it can reach a ``gh pr list``) and happens only on an edge. Polling
the expensive thing four times a second would be indefensible; polling the cheap
thing is not.

Every client benefits, including /m and any addon reading the published
snapshot, because the fix lands at the publisher rather than in one client's
poll loop. Nothing here fast-polls on behalf of a browser.

SCOPE AND SELF-LIMITING. At most ``_MAX_WATCHERS`` run at once; each expires on
its own deadline, and each stops early the moment its reason is satisfied (the
commit landed, the push reached origin). A watcher is idempotent per title:
watching again extends the deadline and updates the reason instead of starting a
second task. Nothing here is required for correctness — if every watcher were
deleted the workflow would still advance, just at the old 4s cadence.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _events():
    """The event bus module, imported lazily for the same reason as
    :func:`_server` — this module is reachable from the server's import graph,
    and a top-level import here is how that graph acquires a cycle."""
    from backend.web.core import events

    return events


# Poll the cheap signature 4x/s while an action is settling, then back off: a
# commit whose hooks run for minutes does not need sub-second resolution for its
# whole life, only around the moment it finishes.
_POLL = 0.25
_BACKOFF_AFTER_S = 60.0
_BACKOFF_POLL = 1.0
# A commit is "done" when the lock is gone AND the status marker is present —
# but the one-liner writes the status and drops the lock in separate statements,
# so require the pair to hold still briefly before believing it.
_SETTLE_S = 2.0
# ls-remote is a NETWORK round-trip; it must not ride the 250ms cadence.
_ORIGIN_EVERY_S = 1.5
_MAX_WATCHERS = 4
_DEFAULT_SECONDS = 180.0

_WATCH: Dict[str, dict] = {}
#: The server's event loop, so a worker thread can hand work back to it. Set once
#: by the lifespan via :func:`set_loop`.
_MAIN_LOOP = None


def set_loop(loop) -> None:
    """Record the running event loop (called once from the server lifespan)."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def _alive(rec: dict) -> bool:
    """Whether a watcher record has a live task. Tolerates a missing/None task so
    a malformed entry can never wedge the module."""
    task = (rec or {}).get("task")
    return task is not None and not task.done()


def watch(title: str, wt: str, reason: str, seconds: float = _DEFAULT_SECONDS) -> None:
    """Start (or extend) a bounded edge watcher for one session.

    ``reason`` is ``"commit"`` or ``"push"`` and only decides which extra signal
    is sampled and when the watcher may stop early. Safe to call from a request
    handler: it creates an asyncio task and returns immediately. Never raises —
    a freshness nicety must not be able to fail an action that already happened.
    """
    if not title or not wt:
        return
    try:
        # WHERE ARE WE? A route calls this ON the event loop; the autopilot driver
        # calls it from a worker thread, where `asyncio.create_task` raises
        # RuntimeError. Hop to the loop in that case rather than failing: the
        # earlier version stored the record BEFORE creating the task, so a
        # thread-side call left a task=None entry behind and every subsequent
        # watch() in the whole process then died on `live["task"].done()` — which
        # silently returned the entire app to 4s stage latency after the first
        # autopilot commit.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = _MAIN_LOOP
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(watch, title, wt, reason, seconds)
            return
        now = time.monotonic()
        live = _WATCH.get(title)
        if live is not None and _alive(live):
            live["until"] = max(live["until"], now + seconds)
            live["reason"] = reason
            live["settle_since"] = None
            return
        if len([w for w in _WATCH.values() if _alive(w)]) >= _MAX_WATCHERS:
            return
        rec: dict = {
            "wt": wt,
            "reason": reason,
            "until": now + seconds,
            "lock_path": None,
            "last_sig": None,
            "settle_since": None,
            "origin_at": 0.0,
            "origin_sha": None,
            # One-shot latch for the "session.pushed" announcement, declared
            # here with every other key so the record shape stays readable in
            # one place. Re-watching a live record keeps the latch set, which is
            # what makes the announcement at-most-once per watcher.
            "pushed_emitted": False,
            "task": None,
        }
        # Create the task FIRST: a record without one is a landmine for every
        # later call, so it must never be reachable.
        task = asyncio.create_task(_loop(title, rec))
        rec["task"] = task
        _WATCH[title] = rec
    except Exception:  # noqa: BLE001
        pass


def stop(title: str) -> bool:
    """Cancel a live watcher. Returns whether one was running."""
    rec = _WATCH.pop(title, None)
    if rec is None:
        return False
    task = rec.get("task")
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


def active_titles() -> list:
    """Titles with a live watcher (diagnostics/tests)."""
    return [t for t, w in _WATCH.items() if _alive(w)]


def _signature(title: str, rec: dict) -> tuple:
    """The cheap local evidence that a commit/push moved, as a comparable tuple.

    Runs in a worker thread. Resolves the lock path ONCE per watcher and caches
    it: ``_precommit_lock_path`` shells out to ``git rev-parse
    --absolute-git-dir`` on every call, which at a 250ms cadence would be four
    subprocesses a second spent deciding where to stat a file.
    """
    srv = _server()
    wt = rec["wt"]
    if rec["lock_path"] is None:
        try:
            rec["lock_path"] = srv._precommit_lock_path(wt)
        except Exception:  # noqa: BLE001
            rec["lock_path"] = os.path.join(wt, ".mindflock_precommit.lock")
    lock_live = os.path.exists(rec["lock_path"])
    status: Optional[bytes] = None
    try:
        with open(os.path.join(wt, srv._COMMIT_STATUS_FILE), "rb") as fh:
            status = fh.read(32).strip()
    except OSError:
        status = None
    head = srv._git_head_sha(wt)
    dirty = srv._is_dirty(wt)
    origin = rec["origin_sha"]
    if rec["reason"] == "push":
        now = time.monotonic()
        if now - rec["origin_at"] >= _ORIGIN_EVERY_S:
            rec["origin_at"] = now
            try:
                branch = srv._current_branch(wt)
                origin = srv._origin_branch_sha(wt, branch) if branch else None
            except Exception:  # noqa: BLE001
                origin = rec["origin_sha"]
            rec["origin_sha"] = origin
    return (lock_live, status, head, dirty, origin)


def _satisfied(rec: dict, sig: tuple) -> bool:
    """Whether this watcher's reason has been met, so it can stop early."""
    lock_live, status, head, _dirty, origin = sig
    if rec["reason"] == "commit":
        done = (not lock_live) and status is not None
        if not done:
            rec["settle_since"] = None
            return False
        now = time.monotonic()
        if rec["settle_since"] is None:
            rec["settle_since"] = now
            return False
        return now - rec["settle_since"] >= _SETTLE_S
    if rec["reason"] == "push":
        return bool(origin) and origin == head
    return False


def _announce_push(title: str, rec: dict, sig: tuple) -> None:
    """Emit ``session.pushed`` once, at the instant a push reached origin.

    THIS IS THE ONLY PLACE the process learns that a push actually LANDED.
    Everywhere else knows only that one was *asked for*: ``POST /push-branch``
    types a shell one-liner into the session's tmux and returns with no exit
    code, and the stage ladder's "pushed" rung is recomputed from a snapshot —
    it falls off again the moment the tree goes dirty, so it can neither name
    the sha that landed nor say it exactly once. Here the push watcher has just
    compared the remote branch head against the local one and found them equal;
    that comparison IS the fact, and this is the edge it happens on.

    Runs in a worker thread (``_current_branch`` shells out to git, and this
    module's whole discipline is that nothing expensive rides the event loop).

    Guarded twice over, because a freshness nicety must never be able to hurt
    an action that already succeeded: a flag on the watcher record keeps it to
    at most one emit per watcher — ``watch()`` is idempotent per title, so a
    session that pushes twice inside one window shares this record and must not
    re-announce the first sha — and the whole body is wrapped, so a subscriber
    or user hook blowing up cannot escape into ``_loop``.
    """
    if rec["reason"] != "push" or rec.get("pushed_emitted"):
        return
    try:
        # Set the flag BEFORE emitting: "at most once" is the guarantee that
        # matters here (the consumer creates a test plan), so a failed emit must
        # not be retried on a later tick of the same watcher.
        rec["pushed_emitted"] = True
        # sig is _signature's tuple: (lock_live, status, head, dirty, origin).
        # The head is the sha we just proved equal to origin's, so it is the one
        # that landed — not a fresh rev-parse, which could already have moved.
        head = sig[2] if len(sig) > 2 else None
        branch = _server()._current_branch(rec["wt"])
        _events().BUS.emit(
            "session.pushed",
            session=title,
            data={
                "branch": branch or "",
                "sha": head or "",
                "wt": rec["wt"],
            },
        )
    except Exception:  # noqa: BLE001 — the watcher must never surface an error
        pass


async def _loop(title: str, rec: dict) -> None:
    """Watch one session until its reason is met or its deadline passes."""
    started = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now >= rec["until"]:
                return
            if _server().ENGINE.instances.get(title) is None:
                return
            try:
                sig = await asyncio.to_thread(_signature, title, rec)
            except Exception:  # noqa: BLE001 — a bad sample is not fatal
                sig = rec["last_sig"]
            if sig is not None and sig != rec["last_sig"]:
                rec["last_sig"] = sig
                try:
                    await asyncio.to_thread(_server()._republish_session, title)
                except Exception:  # noqa: BLE001
                    pass
            if sig is not None and _satisfied(rec, sig):
                # The watcher is about to exit, and for a push this is the last
                # moment anything holds the evidence that the sha reached
                # origin — announce it before returning, not after.
                await asyncio.to_thread(_announce_push, title, rec, sig)
                return
            elapsed = now - started
            await asyncio.sleep(_POLL if elapsed < _BACKOFF_AFTER_S else _BACKOFF_POLL)
    except asyncio.CancelledError:  # pragma: no cover — cooperative stop
        raise
    except Exception:  # noqa: BLE001 — the watcher must never surface an error
        pass
    finally:
        if _WATCH.get(title) is rec:
            _WATCH.pop(title, None)
