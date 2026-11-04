"""Event-loop stall watchdog: a wedged server writes down why, by itself.

On 2026-11-04 the server twice stopped answering HTTP while keeping its
listen sockets (the accept backlog filled; curl got nothing for minutes),
and the only way out anyone found was "Restart WSL". A loop that never runs
never calls ``accept()``, so the question is always *what was the loop
thread doing* — and by the time a person thinks of ``kill -USR1`` (the
faulthandler dump in ``backend/web/run.py``) the moment has passed or the
VM is gone.

This module answers it unattended:

* :func:`heartbeat_loop` — a coroutine on the server's loop that stamps
  :data:`_BEAT` every :data:`BEAT_SECONDS`. If the loop is blocked (a sync
  call on the loop thread, a lock it waits on, the GIL starved by threads
  page-faulting under memory pressure) the stamp stops moving.
* a daemon thread (:func:`start`) that checks the stamp every second. When
  it is older than :data:`STALL_SECONDS` it appends a timestamped header —
  how long, memory level / free / PSI, load average, the server's RSS and
  thread count — then every thread's Python stack (``faulthandler``, which
  needs no GIL handoff from the loop thread) to ``<config dir>/logs/
  stacks.log``. A long stall is re-dumped every :data:`REPEAT_SECONDS`; the
  loop answering again appends one "recovered after N s" line.

Both are cheap (one monotonic read per second each). ``MINDFLOCK_LOOP_
WATCHDOG=0`` turns it off; ``MINDFLOCK_LOOP_STALL_S`` moves the line.
Never raises.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable, Optional, TextIO

#: How often the loop stamps its heartbeat.
BEAT_SECONDS = 1.0
#: A heartbeat this old means the loop is stalled.
STALL_SECONDS = 10.0
#: A stall still going this long after its last dump is dumped again.
REPEAT_SECONDS = 60.0
#: How often the watchdog thread looks.
CHECK_SECONDS = 1.0

_BEAT = time.monotonic()
_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def beat(now: Optional[float] = None) -> None:
    """Stamp the heartbeat (the loop calls this; tests call it directly)."""
    global _BEAT
    _BEAT = time.monotonic() if now is None else now


def enabled() -> bool:
    return os.environ.get("MINDFLOCK_LOOP_WATCHDOG", "1") != "0"


def stall_seconds() -> float:
    try:
        v = float(os.environ.get("MINDFLOCK_LOOP_STALL_S") or STALL_SECONDS)
    except ValueError:
        return STALL_SECONDS
    return v if v > 0 else STALL_SECONDS


async def heartbeat_loop() -> None:
    """Stamp :func:`beat` forever (started by the server lifespan)."""
    while True:
        beat()
        await asyncio.sleep(BEAT_SECONDS)


def _rss_threads() -> str:
    """``"rss 1.4 GB, 38 threads"`` for this process, or ``""``."""
    rss = threads = None
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    if threads is None:
        threads = threading.active_count()
    parts = []
    if rss is not None:
        parts.append("rss %.2f GB" % (rss / 1024.0**3))
    parts.append("%d threads" % threads)
    return ", ".join(parts)


def _context() -> str:
    """One line of what the machine looked like: memory, load, this process."""
    bits = []
    try:
        from backend import memory_pressure as _mem

        snap = _mem.snapshot(fresh=True)
        line = "memory %s" % snap.get("level")
        if snap.get("total"):
            line += ", %.1f of %.1f GB available" % (
                float(snap.get("available") or 0) / 1024.0**3,
                float(snap.get("total") or 0) / 1024.0**3,
            )
        psi = snap.get("psi") or {}
        if psi:
            line += ", PSI some %.0f%% full %.0f%% (avg10)" % (
                float(psi.get("some_avg10") or 0),
                float(psi.get("full_avg10") or 0),
            )
        bits.append(line)
    except Exception:  # noqa: BLE001 — context is best effort
        pass
    try:
        bits.append("load %.2f %.2f %.2f" % os.getloadavg())
    except (OSError, AttributeError):
        pass
    bits.append(_rss_threads())
    return "; ".join(b for b in bits if b)


class Watchdog:
    """The stall detector, clock and output injectable (tests drive
    :meth:`check` directly)."""

    def __init__(
        self,
        out: Callable[[], Optional[TextIO]],
        *,
        stall_s: Optional[float] = None,
        repeat_s: float = REPEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        context: Callable[[], str] = _context,
        dump: Optional[Callable[[TextIO], None]] = None,
    ) -> None:
        self._out = out
        self.stall_s = stall_seconds() if stall_s is None else stall_s
        self.repeat_s = repeat_s
        self._clock = clock
        self._wall = wall
        self._context = context
        self._dump = dump or _faulthandler_dump
        self.stalled_since: Optional[float] = None  # heartbeat time of the stall
        self._last_dump = 0.0
        self.dumps = 0

    def check(self) -> Optional[str]:
        """One look: ``"stalled"`` (a dump was written), ``"recovered"``,
        or None. Never raises."""
        try:
            now = self._clock()
            age = now - _BEAT
            if age >= self.stall_s:
                fresh = self.stalled_since is None or self.stalled_since != _BEAT
                if fresh or now - self._last_dump >= self.repeat_s:
                    self.stalled_since = _BEAT
                    self._last_dump = now
                    self._write_stall(age, again=not fresh)
                    return "stalled"
                return None
            if self.stalled_since is not None:
                lasted = _BEAT - self.stalled_since
                self.stalled_since = None
                self._write(
                    "=== %s event loop answered again after %.0f s ===\n"
                    % (self._stamp(), max(lasted, age))
                )
                return "recovered"
        except Exception:  # noqa: BLE001 — a watchdog must never die
            pass
        return None

    def _stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._wall()))

    def _write(self, text: str) -> Optional[TextIO]:
        fh = self._out()
        if fh is None:
            return None
        fh.write(text)
        fh.flush()
        return fh

    def _write_stall(self, age: float, again: bool) -> None:
        fh = self._write(
            "=== %s event loop %s for %.0f s (pid %d; %s) ===\n"
            % (
                self._stamp(),
                "STILL stalled" if again else "stalled",
                age,
                os.getpid(),
                self._context(),
            )
        )
        if fh is None:
            return
        self._dump(fh)
        fh.write("\n")
        fh.flush()
        self.dumps += 1
        _log(
            "event loop stalled %.0f s; stacks appended to %s"
            % (age, getattr(fh, "name", "stacks.log"))
        )


def _faulthandler_dump(fh: TextIO) -> None:
    """Every thread's stack into ``fh``: ``faulthandler`` for a real file (no
    allocation, no locks), a ``sys._current_frames`` rendering otherwise."""
    try:
        fh.fileno()
    except (AttributeError, OSError, ValueError):
        import sys
        import traceback

        names = {t.ident: t.name for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            fh.write("Thread %s (%s):\n" % (hex(ident), names.get(ident, "?")))
            fh.write("".join(traceback.format_stack(frame)))
        return
    import faulthandler

    fh.flush()
    faulthandler.dump_traceback(file=fh, all_threads=True)


def _log(msg: str) -> None:
    try:
        from backend import log

        logger = log.ErrorLog or log.InfoLog
        if logger is not None:
            logger.Printf("%s", msg)
    except Exception:  # noqa: BLE001
        pass


_FH: Optional[TextIO] = None


def _stacks_file() -> Optional[TextIO]:
    """``<config dir>/logs/stacks.log``, opened once (append, line-buffered)
    — the same file ``kill -USR1`` writes to."""
    global _FH
    if _FH is None:
        try:
            from backend import config

            logs = os.path.join(config.GetConfigDir(), "logs")
            os.makedirs(logs, exist_ok=True)
            _FH = open(os.path.join(logs, "stacks.log"), "a", buffering=1)
        except Exception:  # noqa: BLE001
            return None
    return _FH


def start() -> bool:
    """Start the watchdog thread once per process (the lifespan calls it
    next to :func:`heartbeat_loop`). → whether it is running."""
    global _THREAD
    if not enabled():
        return False
    if _THREAD is not None and _THREAD.is_alive():
        return True
    beat()
    _STOP.clear()
    dog = Watchdog(_stacks_file)

    def _run() -> None:
        while not _STOP.wait(CHECK_SECONDS):
            dog.check()

    _THREAD = threading.Thread(target=_run, name="loop-watchdog", daemon=True)
    _THREAD.start()
    return True


def stop() -> None:
    """Stop the thread (lifespan shutdown, tests)."""
    global _THREAD
    _STOP.set()
    _THREAD = None
