"""A terminal websocket's PTY client, kept off the event loop.

Incident 2026-11-10: opening a session's terminal ran ``mux.attach`` →
ptyprocess → ``pty.fork()`` ON the event loop. Forking a 1.7 GB, ~50-thread
server under WSL memory pressure never returned, and every request hung
until the server was SIGKILLed; an app reload reconnects every pane, so it
forked once per pane, back to back.

Everything here that touches the PTY client blocks, so none of it runs on
the loop:

* :func:`attach_off_loop` — the spawn, on a daemon thread, at most
  :data:`MAX_CONCURRENT_ATTACHES` at a time (a reload's ten panes queue
  instead of ten forks at once), within :data:`ATTACH_TIMEOUT_S` including
  the wait for a slot. Past it the caller gets :class:`AttachTimeout` and
  closes the socket with its reason; a client that turns up after that is
  reaped, never leaked. A spawn that never returns keeps its slot (and its
  thread) — so at most :data:`MAX_CONCURRENT_ATTACHES` threads can be
  stuck, and later attaches time out cleanly instead of piling up more.
* :class:`PtyWriter` — keystrokes to the client in order on one thread per
  connection: a blocking write to a PTY whose client is not reading (a
  stopped or wedged ``tmux attach``, a long paste) would otherwise hold the
  loop.
* :func:`reap` — the detach: ``ptyprocess``'s ``terminate`` sleeps 0.1 s per
  signal it tries and ``close`` another 0.1 s, so a reload closing ten panes
  stalled the loop for seconds. :func:`detaching` wraps a client for code
  that terminates it itself (``core.terminal.pump_pty``).

Reads stay on the loop: ``add_reader`` fires only when the fd is readable,
so its ``os.read`` returns at once. A resize is one ``ioctl``.

A fork itself holds the GIL, so a thread alone does not stop a slow fork
stalling the loop: the spawn's own cost is :func:`backend.session.mux.spawn_pty`
(``posix_spawn``, no page-table copy).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from backend.web.core.terminal import pty_write

__all__ = [
    "ATTACH_TIMEOUT_S",
    "AttachTimeout",
    "MAX_CONCURRENT_ATTACHES",
    "PtyWriter",
    "attach_off_loop",
    "detaching",
    "reap",
]

_log = logging.getLogger("mindflock.terminal")

#: How long an attach may take — queueing for a slot included — before the
#: socket closes with :class:`AttachTimeout`'s reason.
ATTACH_TIMEOUT_S = 15.0
#: PTY clients being spawned at once, server-wide.
MAX_CONCURRENT_ATTACHES = 2
#: The slots (module-level so a test can swap in its own).
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_ATTACHES)
#: Close code for a timed-out attach: RFC 6455 "try again later". The web
#: terminal retries a first-time close once, then shows the reason.
TIMEOUT_CLOSE = 1013


class AttachTimeout(Exception):
    """The PTY client did not start in time; ``str()`` is the reason the
    socket closes with."""


async def attach_off_loop(
    spawn: Callable[[], Any], *, timeout: Optional[float] = None
) -> Any:
    """``spawn()`` (a ``mux.attach`` call) on a daemon thread, at most
    :data:`MAX_CONCURRENT_ATTACHES` at once, within ``timeout`` seconds
    (default :data:`ATTACH_TIMEOUT_S`) from now — waiting for a slot
    included. Returns the client; re-raises the spawn's own error; raises
    :class:`AttachTimeout` past the deadline. Cancelled or timed out, the
    client is reaped whenever the spawn returns (never handed to anyone)."""
    limit = ATTACH_TIMEOUT_S if timeout is None else float(timeout)
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future" = loop.create_future()
    slots = _SLOTS
    lock = threading.Lock()
    # phase: queued → spawning → done; gave_up: the waiter left; proc: a
    # client the thread produced (reaped by whichever side sees gave_up).
    state: dict = {"phase": "queued", "gave_up": False, "proc": None}
    deadline = time.monotonic() + limit

    def _settle(ok: bool, value: Any) -> None:
        if fut.done():
            return
        if ok:
            fut.set_result(value)
        else:
            fut.set_exception(value)

    def _post(ok: bool, value: Any) -> bool:
        try:
            loop.call_soon_threadsafe(_settle, ok, value)
            return True
        except RuntimeError:  # the loop closed under us
            return False

    def _work() -> None:
        if not slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return  # the waiter's own timeout reports it
        try:
            with lock:
                if state["gave_up"]:
                    return
                state["phase"] = "spawning"
            try:
                proc = spawn()
            except BaseException as err:  # noqa: BLE001 — handed to the waiter
                _post(False, err)
                return
            with lock:
                state["phase"] = "done"
                late = state["gave_up"]
                if not late:
                    state["proc"] = proc
            if late or not _post(True, proc):
                _log.warning(
                    "terminal: a PTY client started after its socket gave up; reaped"
                )
                _terminate(proc)
        finally:
            slots.release()

    threading.Thread(target=_work, name="pty-attach", daemon=True).start()
    try:
        return await asyncio.wait_for(fut, max(0.0, deadline - time.monotonic()))
    except BaseException as err:
        with lock:
            state["gave_up"] = True
            phase = state["phase"]
            orphan, state["proc"] = state["proc"], None
        if orphan is not None:  # started, but this waiter never took it
            reap(orphan)
        if isinstance(err, asyncio.TimeoutError):
            if phase == "queued":
                why = "the server is busy starting other terminals"
            else:
                why = "the terminal client did not start"
            _log.warning("terminal: attach timed out after %.0f s (%s)", limit, phase)
            raise AttachTimeout(
                "terminal attach timed out after %.0f s: %s — try again" % (limit, why)
            ) from None
        raise


def _terminate(proc: Any, writer: "Optional[PtyWriter]" = None) -> None:
    """Detach ``proc`` (blocking): terminate the client, let ``writer``
    finish (a write blocked on a dead client ends with it), then close the
    PTY so ``PtyProcess.__del__`` has nothing left to do — its ``close``
    sleeps, on whichever thread drops the last reference (often the loop).
    The fd is closed only once the writer is done with it: a closed fd
    number can be reused, and a late write would land in someone else's
    file."""
    try:
        proc.terminate(force=True)
    except Exception:  # noqa: BLE001
        pass
    if writer is not None:
        writer.close()
        if not writer.join(2.0):
            _log.warning("terminal: PTY writer still blocked; leaving its fd open")
            return
    close = getattr(proc, "close", None)
    if close is not None:
        try:
            close(force=True)
        except Exception:  # noqa: BLE001
            pass


def reap(proc: Any, writer: "Optional[PtyWriter]" = None) -> None:
    """:func:`_terminate` on a daemon thread: returns at once."""
    threading.Thread(
        target=_terminate, args=(proc, writer), name="pty-reap", daemon=True
    ).start()


class detaching:
    """``proc`` for code that detaches it itself (``pump_pty``'s
    ``proc.terminate(force=True)``): everything passes through except
    ``terminate``, which :func:`reap`\\ s off the loop and returns."""

    def __init__(self, proc: Any) -> None:
        self._proc = proc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._proc, name)

    def terminate(self, force: bool = False) -> bool:
        reap(self._proc)
        return True


class PtyWriter:
    """Writes to a PTY client, in order, on one daemon thread (started on
    the first write). :meth:`write` never blocks. A client that is gone
    ends the thread; later writes are dropped."""

    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self._q: "queue.SimpleQueue[Optional[bytes]]" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="pty-writer", daemon=True
                )
                self._thread.start()
            self._q.put(data)

    def _run(self) -> None:
        while True:
            data = self._q.get()
            if data is None:
                return
            try:
                pty_write(self._proc, data)
            except OSError:  # EIO/EBADF: the client is gone
                with self._lock:
                    self._closed = True
                return

    def close(self) -> None:
        """No more writes; the thread ends after what is queued."""
        with self._lock:
            self._closed = True
            if self._thread is not None:
                self._q.put(None)

    def join(self, timeout: float) -> bool:
        """Whether the thread is done (or never started) within ``timeout``."""
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()
