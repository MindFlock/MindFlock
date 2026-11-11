"""Whether the machine is in memory DISTRESS, and how close it is.

A big job (a team run, an orchestrator fanning out workers, a ticket queue
draining) can start agents faster than the machine can hold them: each agent
CLI is a node process plus whatever it runs (test suites, builds), and once
RAM runs out the kernel swaps — on WSL a disk-backed swap that stalls every
session — and then the OOM killer takes agents down mid-turn. This module is
the one place that reads the signal; callers decide what to hold back:

* ``level()`` — ``"ok"`` / ``"low"`` / ``"critical"`` (``"unknown"`` where
  the probe has nothing to read: macOS, or a kernel without ``/proc``;
  native Windows reads psutil instead, with only the available floor).
  Unknown never gates anything. ``low`` holds automatic starts (the launch
  gate's "wait"); ``critical``, held, lets the memory shield put idle agents
  to sleep.
* :func:`snapshot` — the numbers behind the level, for the UI, with ``why``:
  the signals that tripped, in plain words (:func:`describe` joins them).

**Distress, not a share of "available".** Until 2026-11-10 the level was
mostly MemAvailable as a share of RAM (low at 15%, critical at 7%) plus the
Windows host's available memory — and it read critical at 9.7 of 24 GB
used, with PSI at 0.2% and no swap-in, because Windows' "available" swings
by GBs a minute while nothing is short. The level now rests on readings that
mean the machine IS in trouble, or about to be:

* **PSI** (``/proc/pressure/memory``, ``avg60``): the share of the last
  minute tasks stalled waiting on memory. ``full`` (every non-idle task
  stalled at once) is the strong one: low at 5%, critical at 15%. ``some``
  (at least one task stalled; one process thrashing can drive it alone):
  low at 20%, critical at 50%.
* **Swap-in rate** (``pswpin`` in ``/proc/vmstat``, pages per second over the
  last :data:`RATE_WINDOW_SECONDS`): pages the running programs need coming
  back from disk. Swap merely IN USE is not distress (cold pages parked there
  cost nothing); faulting them back at a sustained rate is. Low at
  :data:`SWAPIN_LOW` pages/s (~1 MB/s), critical at :data:`SWAPIN_CRITICAL`
  (~8 MB/s: on WSL's VHD-backed swap that rate stalls every session).
* **OOM kills** (``oom_kill`` in ``/proc/vmstat`` going up in the last
  :data:`OOM_WINDOW_SECONDS`): low on its own, critical together with any
  other signal past its low ENTRY line (a reading held only by the recovery
  band does not count: the kill itself made the level low). Not critical alone: the counter also counts kills inside
  a memory-capped cgroup (a test suite in a 6 GB systemd scope), which say
  nothing about the machine.
* **An available floor** that is genuinely small: MemAvailable (the kernel's
  own estimate of what a new process can get without swapping, page cache
  included) at or under ``max(general.memory_critical_pct of RAM, 1 GB)``
  reads critical, under ``max(general.memory_low_pct, 1.5 GB)`` low
  (defaults 4% / 8%). Under ~1 GB one agent starting (~0.7 GB) or a test
  run pushes the kernel into direct reclaim and swap whatever the box's
  size; the share keeps the line proportional on a big box, where the
  kernel's own watermarks and the agents' working sets are bigger too.

**Sustain before stepping up.** A reading past a line only raises the level
once it has held for :data:`SUSTAIN_SECONDS` (every reading in between past
it too): one bad sample — a burst of page-ins, Windows' standby list being
trimmed — moves nothing. While it waits the snapshot carries ``pending``.

**Hysteresis stepping down:** once ``low``/``critical``, the level only
steps down when every reading clears the *recovery* lines, which sit a
margin past the entry ones — otherwise a box hovering at a line would flap
the banner and the spawn gate every poll. Stepping down is immediate.

The available-share lines are settings (``general.memory_low_pct``, default
8; ``general.memory_critical_pct``, default 4; ``0`` drops the share,
leaving the absolute floors and the other signals). The rest is fixed.

**The Windows host (WSL only).** Inside WSL the guest's numbers are only
half the story: Windows can run out while the VM still reads "plenty
available" (it cannot take memory back from the VM). When
:mod:`backend.host_memory` is running (the web server starts it), the host
gets its own level on its own signals and the level is the WORSE of the two;
the snapshot carries the host reading under ``"host"``. Windows' raw
"available" (FreePhysicalMemory) is NOT one of them: it swung between 0.7
and 3.4 GB within minutes on 2026-11-10 with nothing short. The host's
signals:

* **Commit charge** against the commit limit (RAM plus page file): at the
  limit an allocation fails — the real "can't allocate" danger. Never
  ``low``: the WSL VM commits its whole ``memory=`` size, so on a 31 GB
  laptop with a 26 GB VM the charge sits at 75-85 % all day (on 2026-11-10
  "low" at 85 % held every start with 11.5 GB of headroom — the owner:
  "we have so much memory left"). Critical only at
  :data:`HOST_COMMIT_CRITICAL` %, where an allocation can really fail.
* **Hard paging** (PagesInputPersec, the median of the last minute) is
  REPORTED (``pages_in_median``) but never sets the level: the counter
  includes file-backed reads, and on 2026-11-10 it read 30-100 MB/s with
  WSL barely stalled and 13 GB of headroom, holding every start for nothing.
  Paging that hurts shows up as guest stalls (PSI) — which count.
* **A headroom floor** (:func:`host_headroom`): Windows' available PLUS the
  guest memory the VM already holds and the guest can hand to a new process
  without asking Windows for more — MemAvailable minus MemFree, the guest's
  page cache and other reclaimable memory. Those pages are already backed by
  Windows (part of the VM's footprint), so a new agent that reuses them
  costs Windows nothing. Windows reads them as used, so on 2026-11-10 it read
  "0.8 GB free of 31.4 GB" while WSL held ~11 GB of cache. Freeing that cache
  does not hand it back to Windows soon either (a 3 GB ``memory.reclaim``
  raised Windows' available by nothing in 30 s), so it is headroom for the
  guest, not for Windows' own apps. Critical under 1 GB of headroom, low
  under 2 GB.
* **The raw floor** (:data:`HOST_RAW_CRITICAL_BYTES`, 256 MB): below it
  Windows is paging its own apps out whatever the guest holds — the one place
  raw available alone reads critical.

Stdlib only at import: the ticket pipeline (a separate process) imports it
too, and reads the settings through the same lazy, never-raising path.
``MINDFLOCK_MEMORY_GUARD=0`` turns the guard off (level reads ``"unknown"``).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional, Tuple

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
    "classify_guest",
    "classify_host",
    "replay",
    "HOST_RAW_CRITICAL_BYTES",
    "SUSTAIN_SECONDS",
]

LEVELS = ("ok", "low", "critical")

_GiB = 1024**3
_MiB = 1024**2
#: ``pswpin`` counts pages; the kernel's swap page is 4 KiB everywhere we run.
_PAGE = 4096

#: ``general.memory_low_pct`` / ``general.memory_critical_pct`` when unset:
#: the available share of RAM (percent) at or below which memory reads low /
#: critical. A floor, not the gauge (see the module docstring).
DEFAULT_LOW_PCT = 8
DEFAULT_CRITICAL_PCT = 4
#: Recovery sits this many points past the entry share (hysteresis).
_RECOVER_LOW_MARGIN = 2
_RECOVER_CRITICAL_MARGIN = 2
#: How long a settings read is reused (the snapshot itself caches 2 s).
_SETTINGS_SECONDS = 30.0

#: A reading past a line must hold this long before the level steps up.
SUSTAIN_SECONDS = 30.0
#: The swap-in rate is measured over (about) this much history.
RATE_WINDOW_SECONDS = 60.0
#: ...and needs at least this much of it (one 2 s delta is noise).
_RATE_MIN_SPAN = 10.0
#: An ``oom_kill`` increment counts as a signal this long.
OOM_WINDOW_SECONDS = 300.0

#: Swap-in lines, pages/s (x 4 KiB): ~1 MB/s and ~8 MB/s.
SWAPIN_LOW = 256
SWAPIN_CRITICAL = 2048

# Each guest line: (available share, available bytes, PSI some avg60,
# PSI full avg60, swap-in pages/s). The available SHARE comes from the
# settings; index 0/1 are read by the launch gate and the revive sweep.
_LOW_FIXED = (int(1.5 * _GiB), 20.0, 5.0, SWAPIN_LOW)
_CRITICAL_FIXED = (1 * _GiB, 50.0, 15.0, SWAPIN_CRITICAL)
_RECOVER_FROM_LOW_FIXED = (2 * _GiB, 10.0, 2.0, SWAPIN_LOW // 4)
_RECOVER_FROM_CRITICAL_FIXED = (int(1.5 * _GiB), 30.0, 8.0, SWAPIN_CRITICAL // 4)

# The Windows host's lines (WSL only).
#: Commit charge, percent of the commit limit.
#: Commit charge is never a ``low`` signal (a line no reading reaches).
HOST_COMMIT_LOW = 1000.0
HOST_COMMIT_CRITICAL = 97.0
#: Hard paging (PagesInputPersec, the minute's median), pages/s: ~12 / ~32 MB/s.
HOST_PAGING_LOW = 3000.0
HOST_PAGING_CRITICAL = 8000.0
#: The paging mean counts only once it holds this many readings...
HOST_PAGING_MIN_SAMPLES = 3
#: ...and only while Windows' own available is under this.
HOST_PAGING_RELEVANT_BYTES = 2 * _GiB
#: Headroom floors (Windows' available plus what the guest can reuse).
HOST_HEADROOM_LOW = 2 * _GiB
HOST_HEADROOM_CRITICAL = 1 * _GiB
#: Windows' own available at or below which the host reads critical even
#: when the guest holds plenty it could reuse (Windows is paging its apps).
HOST_RAW_CRITICAL_BYTES = 256 * _MiB
#: ...and over which that floor has cleared.
_HOST_RAW_RECOVER_BYTES = 512 * _MiB

# Each host line: (headroom bytes, commit %, hard paging pages/s).
_HOST_LOW = (HOST_HEADROOM_LOW, HOST_COMMIT_LOW, HOST_PAGING_LOW)
_HOST_CRITICAL = (HOST_HEADROOM_CRITICAL, HOST_COMMIT_CRITICAL, HOST_PAGING_CRITICAL)
_HOST_RECOVER_FROM_LOW = (3 * _GiB, HOST_COMMIT_LOW, HOST_PAGING_LOW / 2)
_HOST_RECOVER_FROM_CRITICAL = (int(1.5 * _GiB), 94.0, HOST_PAGING_CRITICAL / 2)

_RANK = {"ok": 0, "low": 1, "critical": 2}

_SHARE_CACHE: Dict[str, object] = {"at": -1e18, "val": None}

#: Readings are cached this long: callers (the create route, the revive
#: sweep, the run planner, the UI poll) can ask freely.
_CACHE_SECONDS = 2.0

_MEMINFO = "/proc/meminfo"
_PSI = "/proc/pressure/memory"
_VMSTAT = "/proc/vmstat"

#: The sustain clock (monotonic; tests replace it).
_clock = time.monotonic

_LOCK = threading.Lock()


def _fresh_state() -> Dict[str, object]:
    return {
        "level": "ok",
        "host_level": "ok",
        "at": 0.0,
        "snap": None,
        # per side: since when the RAW reading has been at/past each level
        "since": {"guest": {}, "host": {}},
        # (monotonic, pswpin) readings for the swap-in rate
        "vm": [],
        "oom": None,  # (last oom_kill count, monotonic of its last rise)
    }


_STATE: Dict[str, object] = _fresh_state()


def _enabled() -> bool:
    return os.environ.get("MINDFLOCK_MEMORY_GUARD", "1") != "0"


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def _read_meminfo(path: str = _MEMINFO) -> Optional[Dict[str, int]]:
    """``{"total", "available", "free", "swap_total", "swap_free"}`` in
    bytes, or None (``free`` is MemFree; absent from psutil's reading).

    Native Windows has no ``/proc``: the same numbers come from psutil
    (``GlobalMemoryStatusEx``'s available physical memory; the page file as
    swap). There is no PSI or vmstat there, so the level rests on the
    available floor alone."""
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


def _read_vmstat(path: str = _VMSTAT) -> Optional[Dict[str, int]]:
    """``{"pswpin", "oom_kill"}`` (cumulative counters), or None. Either can
    be missing (``oom_kill`` arrived in 4.13)."""
    if os.name == "nt" and path == _VMSTAT:
        return None
    out: Dict[str, int] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("pswpin", "oom_kill"):
                    out[parts[0]] = int(parts[1])
    except (OSError, ValueError):
        return None
    return out or None


# --------------------------------------------------------------------------- #
# the guest's counters → rates (stateful; call under _LOCK)
# --------------------------------------------------------------------------- #
def _swapin_rate(vm: Optional[Dict[str, int]], mono: float) -> Optional[float]:
    """Pages/s swapped in over the last ~:data:`RATE_WINDOW_SECONDS`
    (from the newest reading at least that old, else the oldest kept), or
    None until :data:`_RATE_MIN_SPAN` of history exists."""
    hist: List[Tuple[float, int]] = _STATE["vm"]  # type: ignore[assignment]
    if not vm or "pswpin" not in vm:
        return None
    cur = int(vm["pswpin"])
    if hist and cur < hist[-1][1]:
        hist.clear()  # the counter went back: a different kernel (tests)
    hist.append((mono, cur))
    while len(hist) > 2 and mono - hist[1][0] >= RATE_WINDOW_SECONDS:
        hist.pop(0)
    if len(hist) > 240:
        del hist[: len(hist) - 240]
    t0, c0 = hist[0]
    span = mono - t0
    if span < _RATE_MIN_SPAN:
        return None
    return max(0.0, (cur - c0) / span)


def _oom_recent(vm: Optional[Dict[str, int]], mono: float) -> int:
    """How many OOM kills the kernel made since the counter last rose, when
    that was within :data:`OOM_WINDOW_SECONDS` (0 otherwise). The first
    reading only sets the baseline."""
    if not vm or "oom_kill" not in vm:
        return 0
    cur = int(vm["oom_kill"])
    prev = _STATE.get("oom")
    if not isinstance(prev, tuple):
        _STATE["oom"] = (cur, None, 0)
        return 0
    count, at, kills = prev
    if cur > count:
        kills = (kills if at is not None and mono - at < OOM_WINDOW_SECONDS else 0) + (
            cur - count
        )
        at = mono
    elif cur < count:
        at, kills = None, 0
    _STATE["oom"] = (cur, at, kills)
    if at is not None and mono - at < OOM_WINDOW_SECONDS:
        return int(kills)
    return 0


# --------------------------------------------------------------------------- #
# the guest's lines
# --------------------------------------------------------------------------- #
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
    ``general.memory_critical_pct`` (defaults 8 / 4), re-read at most every
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
    """The guest's four lines as ``(available share, available bytes, PSI
    some avg60, PSI full avg60, swap-in pages/s)``: ``low`` / ``critical``
    (entry) and ``recover_low`` / ``recover_critical`` (what every reading
    must clear to step down). A share of 0 never trips (the setting turned
    that signal off)."""
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


def _gb(n) -> str:
    return "%.1f GB" % (float(n or 0) / _GiB)


def _guest_reasons(th, r: dict, recover: bool = False) -> List[str]:
    """Each guest reading at or past line ``th`` (``recover``: each reading
    NOT clear of recovery line ``th``), in plain words."""
    total = float(r.get("total") or 0)
    avail = float(r.get("available") or 0)
    share = avail / total if total else 1.0
    psi = r.get("psi") or {}
    swapin = r.get("swapin")
    out: List[str] = []

    def over(v: float, line: float) -> bool:
        return v > line if recover else v >= line

    def under(v: float, line: float) -> bool:
        return v < line if recover else v <= line

    if (th[0] > 0 and under(share, th[0])) or under(avail, th[1]):
        out.append(
            "only %s of %s available (%d%%)"
            % (_gb(avail), _gb(total), int(round(share * 100)))
        )
    full = float(psi.get("full_avg60") or 0.0)
    some = float(psi.get("some_avg60") or 0.0)
    if psi and over(full, th[3]):
        out.append(
            "every running process stalled on memory %d%% of the last minute"
            % int(round(full))
        )
    elif psi and over(some, th[2]):
        out.append(
            "processes stalled on memory %d%% of the last minute" % int(round(some))
        )
    if swapin is not None and over(float(swapin), th[4]):
        out.append(
            "swapping %.1f MB/s back in from disk" % (float(swapin) * _PAGE / 1e6)
        )
    return out


def _oom_reason(kills: int) -> str:
    return "the kernel's OOM killer ended %d process%s in the last %d min" % (
        kills,
        "" if kills == 1 else "es",
        int(OOM_WINDOW_SECONDS // 60),
    )


def classify_guest(prev: str, r: dict) -> Tuple[str, List[str]]:
    """The guest's level for readings ``r`` (``{"total", "available",
    "psi", "swapin" (pages/s | None), "oom" (recent kills)}``), given the
    level before them → ``(level, why)``. Pure apart from the settings read;
    no sustain (:func:`_sustain` adds that)."""
    th = thresholds()
    lvl, why = "ok", []
    crit = _guest_reasons(th["critical"], r)
    low = _guest_reasons(th["low"], r)
    if crit:
        lvl, why = "critical", crit
    elif prev == "critical" and _guest_reasons(th["recover_critical"], r, True):
        lvl, why = "critical", _guest_reasons(th["recover_critical"], r, True)
    elif low:
        lvl, why = "low", low
    elif prev in ("low", "critical") and _guest_reasons(th["recover_low"], r, True):
        lvl, why = "low", _guest_reasons(th["recover_low"], r, True)
    kills = int(r.get("oom") or 0)
    if kills:
        # Alone: low (a capped cgroup's kill counts too). With another
        # signal past an ENTRY line: the machine itself ran out. A
        # recovery-band hold is not "another signal": the OOM kill itself
        # made the level low, so prev=low plus PSI some at 11% would read
        # critical for the whole OOM window with GBs free.
        why = [_oom_reason(kills)] + why
        lvl = "critical" if (lvl == "critical" or crit or low) else "low"
    return lvl, why


# --------------------------------------------------------------------------- #
# the host's lines (WSL)
# --------------------------------------------------------------------------- #
def host_thresholds() -> Dict[str, tuple]:
    """The host's lines as ``(headroom bytes, commit %, hard paging
    pages/s)``: ``low`` / ``critical`` (entry) and ``recover_low`` /
    ``recover_critical``. Headroom is :func:`host_headroom`."""
    return {
        "low": _HOST_LOW,
        "critical": _HOST_CRITICAL,
        "recover_low": _HOST_RECOVER_FROM_LOW,
        "recover_critical": _HOST_RECOVER_FROM_CRITICAL,
    }


def host_low_line(total: float = 0.0) -> float:
    """Headroom (bytes) the host must keep to read ``ok`` on that signal:
    its low floor (``total`` is accepted for older callers; the floor no
    longer scales with it)."""
    return float(_HOST_LOW[0])


def _host_reasons(th, h: dict, recover: bool = False) -> List[str]:
    """Each host reading at or past line ``th`` (``recover``: not clear of
    recovery line ``th``), in plain words."""
    out: List[str] = []

    def over(v: float, line: float) -> bool:
        return v > line if recover else v >= line

    room = float(h.get("headroom") or 0)
    avail = float(h.get("available") or 0)
    if (room < th[0]) if recover else (room <= th[0]):
        out.append(
            "Windows has only %s to give (%s available, %s WSL can reuse)"
            % (_gb(room), _gb(avail), _gb(h.get("guest_reusable") or 0))
        )
    commit = h.get("commit_pct")
    if commit is not None and over(float(commit), th[1]):
        out.append(
            "Windows commit charge is at %d%% of its limit (%s of %s)"
            % (
                int(round(float(commit))),
                _gb(h.get("committed")),
                _gb(h.get("commit_limit")),
            )
        )
    # Hard paging (``pages_in_median``) is reported, never a level on its
    # own: PagesInputPersec counts file-backed reads too, and on 2026-11-10
    # it read 30-100 MB/s with WSL barely stalled (PSI full 3.75 %) and
    # 13 GB of headroom — "critical" there held every start for nothing
    # (owner: "pretty arbitrary"). Real trouble shows as guest stalls,
    # swap-in, OOM kills or the commit charge, which do count.
    return out


def classify_host(prev: str, h: dict) -> Tuple[str, List[str]]:
    """The host's level for reading ``h`` (``{"available", "headroom",
    "guest_reusable", "commit_pct"?, "committed"?, "commit_limit"?,
    "pages_in_median"?, "pages_in_samples"?}``), given its level before →
    ``(level, why)``. Pure; no sustain."""
    th = host_thresholds()
    avail = float(h.get("available") or 0)
    raw = []
    if avail <= HOST_RAW_CRITICAL_BYTES or (
        prev == "critical" and avail <= _HOST_RAW_RECOVER_BYTES
    ):
        raw = ["Windows is down to %d MB available" % int(avail // _MiB)]
    crit = _host_reasons(th["critical"], h)
    if raw or crit:
        return "critical", raw + crit
    if prev == "critical":
        held = _host_reasons(th["recover_critical"], h, True)
        if held:
            return "critical", held
    low = _host_reasons(th["low"], h)
    if low:
        return "low", low
    if prev in ("low", "critical"):
        held = _host_reasons(th["recover_low"], h, True)
        if held:
            return "low", held
    return "ok", []


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


# --------------------------------------------------------------------------- #
# sustain (stateful; call under _LOCK)
# --------------------------------------------------------------------------- #
def _sustain(
    side: str, raw: str, mono: float, state: Optional[dict] = None
) -> Tuple[str, Optional[str]]:
    """The level ``side`` steps to, given the raw level its readings point
    at → ``(level, pending)``. Up only once every reading for
    :data:`SUSTAIN_SECONDS` pointed at least that high; down at once (the
    recovery lines already held it). ``pending``: the raw level while it is
    higher than the level and still waiting, else None. ``state``: the
    module's own (default) or a :func:`replay`'s."""
    st = _STATE if state is None else state
    key = "level" if side == "guest" else "host_level"
    cur = str(st.get(key) or "ok")
    since: Dict[str, Optional[float]] = st["since"][side]
    rank = _RANK.get(raw, 0)
    for name in ("low", "critical"):
        if rank >= _RANK[name]:
            if since.get(name) is None:
                since[name] = mono
        else:
            since[name] = None
    out = "ok"
    for name in ("low", "critical"):
        t = since.get(name)
        if t is not None and (
            _RANK.get(cur, 0) >= _RANK[name] or mono - t >= SUSTAIN_SECONDS
        ):
            out = name
    st[key] = out
    return out, (raw if rank > _RANK[out] else None)


def _host(mem: Optional[dict], mono: float) -> Optional[dict]:
    """The host reading with its level, or None (not WSL / not running /
    stale). ``mem`` is the guest's own reading: what it can reuse counts as
    headroom (:func:`guest_reusable`). Updates the host's state. Call under
    ``_LOCK``."""
    try:
        from backend import host_memory

        r = host_memory.reading()
    except Exception:  # noqa: BLE001 - advisory: no host reading
        return None
    if not r:
        _STATE["host_level"] = "ok"
        _STATE["since"]["host"] = {}  # type: ignore[index]
        return None
    total, avail = float(r["total"]), float(r["available"])
    reuse = float(guest_reusable(mem))
    room = min(total, avail + reuse)
    out = {
        "available": int(avail),
        "total": int(total),
        "available_pct": round(avail / total * 100.0, 1),
        "guest_reusable": int(reuse),
        "headroom": int(room),
        "headroom_pct": round(room / total * 100.0, 1),
        "commit_pct": None,
        "pages_in_median": r.get("pages_in_median"),
        "at": r.get("at"),
    }
    if r.get("commit_limit"):
        out["committed"] = int(r.get("committed") or 0)
        out["commit_limit"] = int(r["commit_limit"])
        out["commit_pct"] = round(
            float(out["committed"]) / float(out["commit_limit"]) * 100.0, 1
        )
    raw, why = classify_host(
        str(_STATE.get("host_level") or "ok"),
        dict(out, pages_in_samples=r.get("pages_in_samples") or 0),
    )
    lvl, pending = _sustain("host", raw, mono)
    out.update(level=lvl, pending=pending, why=why if lvl != "ok" or pending else [])
    return out


def _worst(a: str, b: str) -> str:
    return a if _RANK.get(a, -1) >= _RANK.get(b, -1) else b


def snapshot(*, fresh: bool = False) -> dict:
    """The current reading::

        {"level": "ok"|"low"|"critical"|"unknown",
         "available": bytes, "total": bytes, "available_pct": 0-100,
         "swap_total": bytes, "swap_free": bytes,
         "psi": {"some_avg10", "some_avg60", "full_avg10", "full_avg60"} | None,
         "swap_in_mb_s": float | None, "oom_kills": recent OOM kills,
         "host": {"available", "total", "available_pct", "guest_reusable",
                  "headroom", "headroom_pct", "commit_pct", "committed"?,
                  "commit_limit"?, "pages_in_median", "level", "pending",
                  "why", "at"} | None,
         "guest_level": the guest's own level,
         "pending": the level the readings point at while it is not yet
                    sustained (else None),
         "why": [the signals behind ``level``, in plain words],
         "at": epoch}

    ``level`` is the worse of the guest's and the host's (WSL only; ``host``
    is None elsewhere). ``why`` is the reasons of whichever side(s) set it;
    while a step up waits on :data:`SUSTAIN_SECONDS`, the reasons of the
    pending side.

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
                snap = _measure(mem, now)
        _STATE["snap"] = snap
        _STATE["at"] = now
        return dict(snap)


def _measure(mem: dict, now: float) -> dict:
    """One reading → the snapshot (call under ``_LOCK``)."""
    mono = _clock()
    psi = _read_psi()
    vm = _read_vmstat()
    swapin = _swapin_rate(vm, mono)
    kills = _oom_recent(vm, mono)
    raw, gwhy = classify_guest(
        str(_STATE["level"]),
        {
            "total": mem["total"],
            "available": mem["available"],
            "psi": psi,
            "swapin": swapin,
            "oom": kills,
        },
    )
    guest, gpending = _sustain("guest", raw, mono)
    host = _host(mem, mono)
    lvl = _worst(guest, host["level"]) if host else guest
    why: List[str] = []
    if lvl != "ok":
        if guest == lvl:
            why += gwhy
        if host and host["level"] == lvl:
            why += host.get("why") or []
    pending = None
    for side_pending, side_why in (
        (gpending, gwhy),
        ((host or {}).get("pending"), (host or {}).get("why") or []),
    ):
        if side_pending and _RANK[side_pending] > _RANK.get(lvl, 0):
            if pending is None or _RANK[side_pending] > _RANK[pending]:
                pending = side_pending
            if lvl == "ok":
                why += side_why
    return {
        "level": lvl,
        "guest_level": guest,
        "host": host,
        "available": mem["available"],
        "total": mem["total"],
        "available_pct": round(mem["available"] / float(mem["total"]) * 100.0, 1),
        "swap_total": mem.get("swap_total", 0),
        "swap_free": mem.get("swap_free", 0),
        "psi": psi,
        "swap_in_mb_s": None if swapin is None else round(swapin * _PAGE / 1e6, 2),
        "oom_kills": kills,
        "pending": pending,
        "why": why,
        "at": now,
    }


def level(*, fresh: bool = False) -> str:
    """``"ok"`` / ``"low"`` / ``"critical"`` / ``"unknown"``. Never raises."""
    return str(snapshot(fresh=fresh).get("level") or "unknown")


def is_low(*, fresh: bool = False) -> bool:
    """Whether new agents should be held back (``low`` or ``critical``)."""
    return level(fresh=fresh) in ("low", "critical")


def describe(snap: Optional[dict] = None) -> str:
    """One human line for a refusal or a log, naming the signals that
    tripped: ``"memory is critical: processes stalled on memory 34% of the
    last minute"``. Empty for ok/unknown. A snapshot without ``why`` (an
    older shape) reads its numbers: ``"memory is low: 2.1 GB free of 24.0 GB
    (9%)"``."""
    snap = snap if snap is not None else snapshot()
    lvl = snap.get("level")
    if lvl not in ("low", "critical"):
        return ""
    why = [w for w in (snap.get("why") or []) if w]
    if why:
        return "memory is %s: %s" % (lvl, "; ".join(why))
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


def replay(rows, *, guest_reusable_share: float = 0.0) -> List[dict]:
    """Run the rule over memory history (``memory.jsonl``'s ``"system"``
    dicts, each with its line's ``"at"``) → one ``{"at", "level",
    "guest_level", "host_level", "why"}`` per row, with the sustain and the
    hysteresis carried row to row (its own state; the live one is
    untouched). For a back-test.

    Rows written before the distress signals were logged lack some of them,
    so this approximates: PSI ``avg60`` is the mean of the logged ``avg10``
    over the last minute of rows (rows are 30 s apart, so a lone 10 s burst
    weighs half a minute: it overstates distress, never hides it); no swap-in rate, no OOM count, no commit
    charge or paging; the host's headroom is Windows' available plus
    ``guest_reusable_share`` of the guest's available (MemFree was not
    logged; 0 = the worst case, Windows' own available alone)."""
    st = _fresh_state()
    out: List[dict] = []
    recent: List[Tuple[float, float, float]] = []  # (at, some10, full10)
    for row in rows or ():
        at = float(row.get("at") or 0.0)
        total = float(row.get("total_mb") or 0) * _MiB
        if total <= 0 or row.get("available_mb") is None:
            out.append(
                {
                    "at": at,
                    "level": "unknown",
                    "guest_level": None,
                    "host_level": None,
                    "why": [],
                }
            )
            continue
        avail = float(row["available_mb"]) * _MiB
        some10 = float(row.get("psi_some_avg10") or 0.0)
        full10 = float(row.get("psi_full_avg10") or 0.0)
        recent = [r for r in recent if at - r[0] < 60.0] + [(at, some10, full10)]
        psi = {
            "some_avg60": (
                row["psi_some_avg60"]
                if row.get("psi_some_avg60") is not None
                else sum(r[1] for r in recent) / len(recent)
            ),
            "full_avg60": (
                row["psi_full_avg60"]
                if row.get("psi_full_avg60") is not None
                else sum(r[2] for r in recent) / len(recent)
            ),
        }
        swapin = row.get("swap_in_mb_s")
        raw, gwhy = classify_guest(
            str(st["level"]),
            {
                "total": total,
                "available": avail,
                "psi": psi,
                "swapin": None if swapin is None else float(swapin) * 1e6 / _PAGE,
                "oom": int(row.get("oom_kills") or 0),
            },
        )
        guest, _p = _sustain("guest", raw, at, st)
        host_lvl, hwhy = None, []
        if row.get("host_total_mb") and row.get("host_available_mb") is not None:
            havail = float(row["host_available_mb"]) * _MiB
            room = row.get("host_headroom_mb")
            room = (
                float(room) * _MiB
                if room is not None
                else havail + guest_reusable_share * avail
            )
            h = {
                "available": havail,
                "headroom": room,
                "guest_reusable": max(0.0, room - havail),
                "commit_pct": row.get("host_commit_pct"),
                "pages_in_median": row.get("host_pages_in_median"),
                "pages_in_samples": (
                    HOST_PAGING_MIN_SAMPLES
                    if row.get("host_pages_in_median") is not None
                    else 0
                ),
            }
            hraw, hwhy = classify_host(str(st["host_level"]), h)
            host_lvl, _p = _sustain("host", hraw, at, st)
        else:
            st["host_level"] = "ok"
            st["since"]["host"] = {}
        lvl = _worst(guest, host_lvl) if host_lvl else guest
        why = (gwhy if guest == lvl else []) + (hwhy if host_lvl == lvl else [])
        out.append(
            {
                "at": at,
                "level": lvl,
                "guest_level": guest,
                "host_level": host_lvl,
                "why": why if lvl != "ok" else [],
            }
        )
    return out


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
        _STATE.update(_fresh_state())
    _SHARE_CACHE.update({"at": -1e18, "val": None})
