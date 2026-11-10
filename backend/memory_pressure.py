"""How much memory headroom the machine has for another agent.

A big job (a team run, an orchestrator fanning out workers, a ticket queue
draining) can start agents faster than the machine can hold them: each agent
CLI is a node process plus whatever it runs (test suites, builds), and once
RAM runs out the kernel swaps — on WSL a disk-backed swap that stalls every
session — and then the OOM killer takes agents down mid-turn. This module is
the one place that reads the signal; callers decide what to hold back:

* ``level()`` — ``"ok"`` / ``"low"`` / ``"critical"`` (``"unknown"`` where
  the probe has nothing to read: macOS, or a kernel without ``/proc``;
  native Windows reads psutil instead, without PSI).
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

The available-share lines are settings (``general.memory_low_pct``, default
15; ``general.memory_critical_pct``, default 7 — percent of RAM the kernel
says is available; ``0`` drops that signal, leaving the floor and PSI). The
absolute floors and the PSI lines are fixed.

Inside WSL the guest's numbers are only half the story: Windows can run out
while the VM still reads "plenty available" (it cannot take memory back from
the VM). When :mod:`backend.host_memory` is running (the web server starts
it), the host's available physical memory is classified on its own lines
(:func:`host_thresholds`, fixed) and the level is the WORSE of the two; the
snapshot carries the host reading under ``"host"``.

What the host's lines are checked against is NOT Windows' "available" alone
but :func:`host_headroom`: Windows' available PLUS the guest memory the VM
already holds and the guest can hand to a new process without asking Windows
for more — MemAvailable minus MemFree, i.e. the guest's page cache and other
reclaimable memory. Those pages are already backed by Windows (they are part
of the VM's footprint), so a new agent that reuses them costs Windows
nothing. Windows reads them as used, so on 2026-11-10 Windows read "0.8 GB
free of 31.4 GB" while WSL held ~11 GB of cache, and every start and the
accurate voice pass were held back while nothing was short. Freeing that
cache does not hand it back to Windows soon either (a 3 GB
``memory.reclaim`` raised Windows' available by nothing in 30 s), so it is
headroom for the guest, not for Windows' own apps. A raw Windows floor
(:data:`HOST_RAW_CRITICAL_BYTES`) still reads critical whatever the guest
holds: below it Windows is paging its own apps out.

Stdlib only at import: the ticket pipeline (a separate process) imports it
too, and reads the settings through the same lazy, never-raising path.
``MINDFLOCK_MEMORY_GUARD=0`` turns the guard off (level reads ``"unknown"``).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional, Tuple

__all__ = [
    "snapshot",
    "level",
    "is_low",
    "describe",
    "LEVELS",
    "thresholds",
    "host_thresholds",
    "host_low_line",
    "host_headroom",
    "guest_reusable",
    "HOST_RAW_CRITICAL_BYTES",
]

LEVELS = ("ok", "low", "critical")

_GiB = 1024**3

#: ``general.memory_low_pct`` / ``general.memory_critical_pct`` when unset:
#: the available share of RAM (percent) at or below which memory reads low /
#: critical.
DEFAULT_LOW_PCT = 15
DEFAULT_CRITICAL_PCT = 7
#: Recovery sits this many points past the entry share (hysteresis).
_RECOVER_LOW_MARGIN = 5
_RECOVER_CRITICAL_MARGIN = 3
#: How long a settings read is reused (the snapshot itself caches 2 s).
_SETTINGS_SECONDS = 30.0

# The fixed parts of each line: (absolute available bytes, PSI some avg60,
# PSI full avg60). The available SHARE comes from the settings.
_LOW_FIXED = (2 * _GiB, 20.0, 5.0)
_CRITICAL_FIXED = (1 * _GiB, 40.0, 15.0)
_RECOVER_FROM_LOW_FIXED = (int(2.5 * _GiB), 10.0, 2.0)
_RECOVER_FROM_CRITICAL_FIXED = (int(1.5 * _GiB), 30.0, 10.0)

# The Windows host's lines (WSL only): (available share, available bytes).
# Lower than the guest's: Windows keeps working at a few GB "available"
# (standby cache counts), and below ~1 GB it pages apps out.
_HOST_LOW = (0.08, int(2.5 * _GiB))
_HOST_CRITICAL = (0.04, int(1.25 * _GiB))
_HOST_RECOVER_FROM_LOW = (0.11, int(3.5 * _GiB))
_HOST_RECOVER_FROM_CRITICAL = (0.06, 2 * _GiB)
#: Windows' own available at or below which the host reads critical even
#: when the guest holds plenty it could reuse (Windows is paging its apps).
HOST_RAW_CRITICAL_BYTES = 256 * 1024**2

_RANK = {"ok": 0, "low": 1, "critical": 2}

_SHARE_CACHE: Dict[str, object] = {"at": -1e18, "val": None}

#: Readings are cached this long: callers (the create route, the revive
#: sweep, the run planner, the UI poll) can ask freely.
_CACHE_SECONDS = 2.0

_MEMINFO = "/proc/meminfo"
_PSI = "/proc/pressure/memory"

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {"level": "ok", "host_level": "ok", "at": 0.0, "snap": None}


def _enabled() -> bool:
    return os.environ.get("MINDFLOCK_MEMORY_GUARD", "1") != "0"


def _read_meminfo(path: str = _MEMINFO) -> Optional[Dict[str, int]]:
    """``{"total", "available", "free", "swap_total", "swap_free"}`` in
    bytes, or None (``free`` is MemFree; absent from psutil's reading).

    Native Windows has no ``/proc``: the same numbers come from psutil
    (``GlobalMemoryStatusEx``'s available physical memory; the page file as
    swap). There is no PSI there, so the level rests on the available share
    and the absolute floors alone."""
    if os.name == "nt" and path == _MEMINFO:
        return _read_meminfo_psutil()
    want = {
        "MemTotal": "total",
        "MemAvailable": "available",
        "MemFree": "free",
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


def _read_meminfo_psutil() -> Optional[Dict[str, int]]:
    try:
        import psutil

        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
    except Exception:  # noqa: BLE001 - never raises: unknown instead
        return None
    if not vm.total:
        return None
    return {
        "total": int(vm.total),
        "available": int(vm.available),
        "swap_total": int(sw.total),
        "swap_free": int(sw.free),
    }


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


def _pct(v, default: int) -> int:
    """A setting's percent: unset/garbage = ``default``, clamped to 0..90."""
    if v is None or isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(0, min(90, n))


def _share_settings() -> Tuple[int, int]:
    """``(low %, critical %)`` from ``general.memory_low_pct`` /
    ``general.memory_critical_pct`` (defaults 15 / 7), re-read at most every
    :data:`_SETTINGS_SECONDS`. Critical never sits above low. Never raises: a
    LOCAL import, so an unreadable store (or a process without the settings
    module) keeps the defaults."""
    now = time.monotonic()
    val = _SHARE_CACHE.get("val")
    if val is not None and now - float(_SHARE_CACHE["at"]) < _SETTINGS_SECONDS:
        return val  # type: ignore[return-value]
    low, crit = DEFAULT_LOW_PCT, DEFAULT_CRITICAL_PCT
    try:
        from backend.config.settings import load_settings

        g = load_settings().general
        low = _pct(getattr(g, "memory_low_pct", None), DEFAULT_LOW_PCT)
        crit = _pct(getattr(g, "memory_critical_pct", None), DEFAULT_CRITICAL_PCT)
    except Exception:  # noqa: BLE001 — advisory: the defaults stand
        pass
    crit = min(crit, low)
    _SHARE_CACHE.update({"at": now, "val": (low, crit)})
    return low, crit


def thresholds() -> Dict[str, tuple]:
    """The four lines as ``(available share, available bytes, PSI some avg60,
    PSI full avg60)``: ``low`` / ``critical`` (entry) and ``recover_low`` /
    ``recover_critical`` (what every reading must clear to step down). A share
    of 0 never trips (the setting turned that signal off)."""
    low, crit = _share_settings()

    def _share(pct: int, margin: int) -> float:
        return (pct + margin) / 100.0 if pct > 0 else 0.0

    return {
        "low": (_share(low, 0),) + _LOW_FIXED,
        "critical": (_share(crit, 0),) + _CRITICAL_FIXED,
        "recover_low": (_share(low, _RECOVER_LOW_MARGIN),) + _RECOVER_FROM_LOW_FIXED,
        "recover_critical": (_share(crit, _RECOVER_CRITICAL_MARGIN),)
        + _RECOVER_FROM_CRITICAL_FIXED,
    }


def _past(th, share: float, avail: int, psi: Optional[Dict[str, float]]) -> bool:
    """Whether the readings are at or past entry thresholds ``th``."""
    if (th[0] > 0 and share <= th[0]) or avail <= th[1]:
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
    th = thresholds()
    if _past(th["critical"], share, avail, psi):
        return "critical"
    if prev == "critical" and not _clear(th["recover_critical"], share, avail, psi):
        return "critical"
    if _past(th["low"], share, avail, psi):
        return "low"
    if prev in ("low", "critical") and not _clear(th["recover_low"], share, avail, psi):
        return "low"
    return "ok"


def host_thresholds() -> Dict[str, tuple]:
    """The host's lines as ``(available share, available bytes)``: ``low`` /
    ``critical`` (entry) and ``recover_low`` / ``recover_critical``."""
    return {
        "low": _HOST_LOW,
        "critical": _HOST_CRITICAL,
        "recover_low": _HOST_RECOVER_FROM_LOW,
        "recover_critical": _HOST_RECOVER_FROM_CRITICAL,
    }


def host_low_line(total: float) -> float:
    """Bytes the host must keep available to read ``ok``: the low line."""
    share, floor = _HOST_LOW
    return max(share * float(total), float(floor))


def _classify_host(prev: str, share: float, avail: int) -> str:
    """The host's level for these readings, given its level before them."""
    th = host_thresholds()

    def past(line) -> bool:
        return share <= line[0] or avail <= line[1]

    def clear(line) -> bool:
        return share >= line[0] and avail >= line[1]

    if past(th["critical"]):
        return "critical"
    if prev == "critical" and not clear(th["recover_critical"]):
        return "critical"
    if past(th["low"]):
        return "low"
    if prev in ("low", "critical") and not clear(th["recover_low"]):
        return "low"
    return "ok"


def guest_reusable(mem: Optional[dict]) -> int:
    """Bytes the guest can give a new process out of memory the VM already
    holds: MemAvailable minus MemFree (page cache and other reclaimable
    memory), 0 when MemFree was not read."""
    if not mem or mem.get("free") is None:
        return 0
    return max(0, int(mem.get("available") or 0) - int(mem.get("free") or 0))


def host_headroom(host: Optional[dict]) -> float:
    """What a new agent can really get from Windows, in bytes: the host
    reading's ``headroom`` (Windows' available plus what the guest can reuse,
    :func:`_host`), else its raw ``available`` (an older snapshot)."""
    host = host or {}
    val = host.get("headroom")
    if val is None:
        val = host.get("available")
    return float(val or 0)


def _host(mem: Optional[dict] = None) -> Optional[dict]:
    """The host reading with its level, or None (not WSL / not running /
    stale). ``mem`` is the guest's own reading: what it can reuse counts as
    headroom (:func:`guest_reusable`). Updates the host's hysteresis state.
    Call under ``_LOCK``."""
    try:
        from backend import host_memory

        r = host_memory.reading()
    except Exception:  # noqa: BLE001 - advisory: no host reading
        return None
    if not r:
        _STATE["host_level"] = "ok"
        return None
    total, avail = float(r["total"]), float(r["available"])
    reuse = float(guest_reusable(mem))
    room = min(total, avail + reuse)
    share = room / total
    lvl = _classify_host(str(_STATE.get("host_level") or "ok"), share, int(room))
    if avail <= HOST_RAW_CRITICAL_BYTES:
        lvl = "critical"
    _STATE["host_level"] = lvl
    return {
        "available": int(avail),
        "total": int(total),
        "available_pct": round(avail / total * 100.0, 1),
        "guest_reusable": int(reuse),
        "headroom": int(room),
        "headroom_pct": round(share * 100.0, 1),
        "level": lvl,
        "at": r.get("at"),
    }


def _worst(a: str, b: str) -> str:
    return a if _RANK.get(a, -1) >= _RANK.get(b, -1) else b


def snapshot(*, fresh: bool = False) -> dict:
    """The current reading::

        {"level": "ok"|"low"|"critical"|"unknown",
         "available": bytes, "total": bytes, "available_pct": 0-100,
         "swap_total": bytes, "swap_free": bytes,
         "psi": {"some_avg10", "some_avg60", "full_avg10", "full_avg60"} | None,
         "host": {"available", "total", "available_pct", "guest_reusable",
                  "headroom", "headroom_pct", "level", "at"} | None,
         "guest_level": the guest's own level, "at": epoch}

    ``level`` is the worse of the guest's and the host's (WSL only; ``host``
    is None elsewhere); the host's level reads its ``headroom``
    (:func:`host_headroom`), not Windows' available alone.

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
                guest = _classify(str(_STATE["level"]), share, mem["available"], psi)
                _STATE["level"] = guest
                host = _host(mem)
                lvl = _worst(guest, host["level"]) if host else guest
                snap = {
                    "level": lvl,
                    "guest_level": guest,
                    "host": host,
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
    host = snap.get("host") or {}
    if host.get("level") == lvl and snap.get("guest_level", lvl) != lvl:
        line = "Windows memory is %s: %.1f GB free of %.1f GB (%d%%)" % (
            "critically low" if lvl == "critical" else "low",
            float(host.get("available") or 0) / _GiB,
            float(host.get("total") or 0) / _GiB,
            int(round(float(host.get("available_pct") or 0))),
        )
        reuse = float(host.get("guest_reusable") or 0)
        if reuse >= 0.1 * _GiB:
            line += ", and WSL can reuse %.1f GB it already holds" % (reuse / _GiB)
        return line
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
        _STATE.update({"level": "ok", "host_level": "ok", "at": 0.0, "snap": None})
    _SHARE_CACHE.update({"at": -1e18, "val": None})
