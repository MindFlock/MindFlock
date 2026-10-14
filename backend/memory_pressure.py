"""How much memory headroom the machine has for another agent.

A big job (a team run, an orchestrator fanning out workers, a ticket queue
draining) can start agents faster than the machine can hold them: each agent
CLI is a node process plus whatever it runs (test suites, builds), and once
RAM runs out the kernel swaps — on WSL a disk-backed swap that stalls every
session — and then the OOM killer takes agents down mid-turn. This module is
the one place that reads the signal; callers decide what to hold back:

* ``level()`` — ``"ok"`` / ``"low"`` / ``"critical"`` (``"unknown"`` where
  the probe has nothing to read: non-Linux, or a kernel without ``/proc``).
  Unknown never gates anything.
* :func:`snapshot` — the numbers behind the level, for the UI.

Two signals, either can raise the level:

* **MemAvailable** from ``/proc/meminfo`` (the kernel's own estimate of what
  a new process can get without swapping), as a share of MemTotal and as an
  absolute floor — a 64 GB box at 10% still has room, an 8 GB one at 20% may
  not.
* **PSI** from ``/proc/pressure/memory`` — the share of the last minute that
  tasks stalled waiting on memory. Available memory can look fine while the
  machine is already thrashing through swap; PSI is the direct reading of that.
  ``avg60`` rather than ``avg10`` so one burst (a test run starting) doesn't
  flip the level.

Hysteresis: once ``low``/``critical``, the level only steps down when the
readings clear the *recovery* thresholds, which sit a margin past the entry
ones — otherwise a box hovering at the line would flap the banner and the
spawn gate every poll.

Stdlib only: the ticket pipeline (a separate process) imports it too.
``MINDFLOCK_MEMORY_GUARD=0`` turns the guard off (level reads ``"unknown"``).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional

__all__ = ["snapshot", "level", "is_low", "describe", "LEVELS"]

LEVELS = ("ok", "low", "critical")

_GiB = 1024**3

# Entry thresholds: a reading at or past ANY of these raises the level.
#: (available share of total, absolute available bytes, PSI some avg60, PSI full avg60)
_LOW = (0.15, 2 * _GiB, 20.0, 5.0)
_CRITICAL = (0.07, 1 * _GiB, 40.0, 15.0)
# Recovery thresholds: to step DOWN from a level, every reading must clear
# these (a margin past the entry line of the level below it).
_RECOVER_FROM_LOW = (0.20, int(2.5 * _GiB), 10.0, 2.0)
_RECOVER_FROM_CRITICAL = (0.10, int(1.5 * _GiB), 30.0, 10.0)

#: Readings are cached this long: callers (the create route, the revive
#: sweep, the run planner, the UI poll) can ask freely.
_CACHE_SECONDS = 2.0

_MEMINFO = "/proc/meminfo"
_PSI = "/proc/pressure/memory"

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {"level": "ok", "at": 0.0, "snap": None}


def _enabled() -> bool:
    return os.environ.get("MINDFLOCK_MEMORY_GUARD", "1") != "0"


def _read_meminfo(path: str = _MEMINFO) -> Optional[Dict[str, int]]:
    """``{"total", "available", "swap_total", "swap_free"}`` in bytes, or None."""
    want = {
        "MemTotal": "total",
        "MemAvailable": "available",
        "SwapTotal": "swap_total",
        "SwapFree": "swap_free",
    }
    out: Dict[str, int] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in want:
                    out[want[key]] = int(rest.split()[0]) * 1024  # kB
    except (OSError, ValueError, IndexError):
        return None
    if "total" not in out or "available" not in out or out["total"] <= 0:
        return None
    return out


def _read_psi(path: str = _PSI) -> Optional[Dict[str, float]]:
    """``{"some_avg10", "some_avg60", "full_avg10", "full_avg60"}``, or None."""
    out: Dict[str, float] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if not parts or parts[0] not in ("some", "full"):
                    continue
                for field in parts[1:]:
                    k, _, v = field.partition("=")
                    if k in ("avg10", "avg60"):
                        out["%s_%s" % (parts[0], k)] = float(v)
    except (OSError, ValueError):
        return None
    return out or None


def _past(th, share: float, avail: int, psi: Optional[Dict[str, float]]) -> bool:
    """Whether the readings are at or past entry thresholds ``th``."""
    if share <= th[0] or avail <= th[1]:
        return True
    if psi:
        if psi.get("some_avg60", 0.0) >= th[2] or psi.get("full_avg60", 0.0) >= th[3]:
            return True
    return False


def _clear(th, share: float, avail: int, psi: Optional[Dict[str, float]]) -> bool:
    """Whether every reading clears recovery thresholds ``th``."""
    if share < th[0] or avail < th[1]:
        return False
    if psi:
        if psi.get("some_avg60", 0.0) > th[2] or psi.get("full_avg60", 0.0) > th[3]:
            return False
    return True


def _classify(prev: str, share: float, avail: int, psi) -> str:
    """The level for these readings, given the level before them."""
    if _past(_CRITICAL, share, avail, psi):
        return "critical"
    if prev == "critical" and not _clear(_RECOVER_FROM_CRITICAL, share, avail, psi):
        return "critical"
    if _past(_LOW, share, avail, psi):
        return "low"
    if prev in ("low", "critical") and not _clear(_RECOVER_FROM_LOW, share, avail, psi):
        return "low"
    return "ok"


def snapshot(*, fresh: bool = False) -> dict:
    """The current reading::

        {"level": "ok"|"low"|"critical"|"unknown",
         "available": bytes, "total": bytes, "available_pct": 0-100,
         "swap_total": bytes, "swap_free": bytes,
         "psi": {"some_avg10", "some_avg60", "full_avg10", "full_avg60"} | None,
         "at": epoch}

    Cached for :data:`_CACHE_SECONDS` (``fresh`` skips the cache). Never
    raises; ``"unknown"`` (with no numbers) where nothing can be read."""
    now = time.time()
    with _LOCK:
        cached = _STATE.get("snap")
        if (
            not fresh
            and isinstance(cached, dict)
            and now - float(_STATE["at"]) < _CACHE_SECONDS
        ):
            return dict(cached)
        if not _enabled():
            snap = {"level": "unknown", "at": now}
        else:
            mem = _read_meminfo()
            if mem is None:
                snap = {"level": "unknown", "at": now}
            else:
                psi = _read_psi()
                share = mem["available"] / float(mem["total"])
                lvl = _classify(str(_STATE["level"]), share, mem["available"], psi)
                _STATE["level"] = lvl
                snap = {
                    "level": lvl,
                    "available": mem["available"],
                    "total": mem["total"],
                    "available_pct": round(share * 100.0, 1),
                    "swap_total": mem.get("swap_total", 0),
                    "swap_free": mem.get("swap_free", 0),
                    "psi": psi,
                    "at": now,
                }
        _STATE["snap"] = snap
        _STATE["at"] = now
        return dict(snap)


def level(*, fresh: bool = False) -> str:
    """``"ok"`` / ``"low"`` / ``"critical"`` / ``"unknown"``. Never raises."""
    return str(snapshot(fresh=fresh).get("level") or "unknown")


def is_low(*, fresh: bool = False) -> bool:
    """Whether new agents should be held back (``low`` or ``critical``)."""
    return level(fresh=fresh) in ("low", "critical")


def describe(snap: Optional[dict] = None) -> str:
    """One human line for a refusal or a log: ``"memory is low: 2.1 GB free
    of 24.0 GB (9%)"``. Empty for ok/unknown."""
    snap = snap if snap is not None else snapshot()
    lvl = snap.get("level")
    if lvl not in ("low", "critical"):
        return ""
    line = "memory is %s: %.1f GB free of %.1f GB (%d%%)" % (
        "critically low" if lvl == "critical" else "low",
        float(snap.get("available") or 0) / _GiB,
        float(snap.get("total") or 0) / _GiB,
        int(round(float(snap.get("available_pct") or 0))),
    )
    psi = snap.get("psi") or {}
    stall = float(psi.get("some_avg60") or 0.0)
    if stall >= 5.0:
        line += ", processes stalled on memory %d%% of the last minute" % int(
            round(stall)
        )
    return line


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.update({"level": "ok", "at": 0.0, "snap": None})
