"""The Windows host's memory, read from inside a WSL guest.

A WSL2 guest sees only its own VM: ``/proc/meminfo`` can read "plenty
available" while Windows itself has a few hundred MB left, because the VM has
grown into everything ``.wslconfig``'s ``memory=`` ceiling allows and Windows
cannot take memory back from it. Once Windows runs short it pages its own
apps out and the whole PC stalls — the guest never sees a thing
(2026-11-05: the owner rebooted a "bricked" PC with WSL itself not even full).

This module keeps ONE long-lived ``powershell.exe`` that prints the host's
available and total physical memory, its commit charge and commit limit, and
its hard-paging rate every :data:`INTERVAL_SECONDS`; a daemon thread reads
its lines into :func:`reading`, which also carries the MEDIAN paging rate of
the last :data:`PAGING_WINDOW` readings (one burst of page-ins — an app
starting, a file scan — is not distress; a minute of them is. A mean would
keep one 20000 pages/s spike over the line for the whole window). Spawning PowerShell per sample
costs ~1.5 s of wall time and a process start each time; one loop costs one
idle process. The loop exits on its own when its stdout pipe breaks (the
server is gone), and the thread restarts it with a backoff if it dies.

:mod:`backend.memory_pressure` folds the reading into its level (the worse
of the guest's and the host's), so the launch gate, the memory shield and
the UI all act on a short Windows without knowing about it.

Only inside WSL, only once :func:`start` is called (the web server does, the
ticket pipeline and the tests do not). ``MINDFLOCK_HOST_MEMORY=0`` turns it
off. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Dict, Optional

__all__ = [
    "start",
    "stop",
    "reading",
    "parse_line",
    "INTERVAL_SECONDS",
    "PAGING_WINDOW",
]

#: How often the PowerShell loop prints a reading.
INTERVAL_SECONDS = 10
#: A reading older than this is ignored (the loop died or stalled).
STALE_SECONDS = 45.0
#: Restart backoff after the loop exits: doubles from the first to the cap.
_BACKOFF_FIRST = 15.0
_BACKOFF_CAP = 300.0
#: The paging rate in :func:`reading` is the median of this many readings
#: (~1 minute at :data:`INTERVAL_SECONDS`).
PAGING_WINDOW = 6

_WIN_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Win32_OperatingSystem: FreePhysicalMemory is what Windows calls
# "Available" (free + standby), TotalVisibleMemorySize the usable RAM; both kB.
# Win32_PerfFormattedData_PerfOS_Memory: CommittedBytes / CommitLimit (the
# commit charge — what Windows has promised — and RAM plus page file: at the
# limit an allocation FAILS), and PagesInputPersec (hard page faults read from
# disk, a rate over the interval since the previous query). The perf class
# can be missing (perf counters disabled): then the line is the short
# ``mem <avail> <total>`` form. A write to a closed pipe throws, and with
# Stop that ends the loop.
_SCRIPT = (
    "$ErrorActionPreference='Stop'; "
    "while ($true) { "
    "$o = Get-CimInstance Win32_OperatingSystem; "
    "$x = ''; "
    "try { $p = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; "
    "$x = (' {0} {1} {2}' -f [math]::Floor($p.CommittedBytes / 1024), "
    "[math]::Floor($p.CommitLimit / 1024), $p.PagesInputPersec) } catch { $x = '' }; "
    "[Console]::Out.WriteLine(('mem {0} {1}' -f $o.FreePhysicalMemory, $o.TotalVisibleMemorySize) + $x); "
    "[Console]::Out.Flush(); "
    "Start-Sleep -Seconds %d }" % INTERVAL_SECONDS
)

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "reading": None,
    "thread": None,
    "proc": None,
    "stop": False,
    "paging": [],
}


def _enabled() -> bool:
    if os.environ.get("MINDFLOCK_HOST_MEMORY", "1") == "0":
        return False
    try:
        from backend import osenv

        return osenv.is_wsl()
    except Exception:  # noqa: BLE001 - no osenv: not WSL as far as we know
        return False


def _powershell() -> Optional[str]:
    if os.path.exists(_WIN_POWERSHELL):
        return _WIN_POWERSHELL
    return shutil.which("powershell.exe")


def parse_line(line: str) -> Optional[Dict[str, int]]:
    """``"mem <available kB> <total kB>"`` → ``{"available", "total"}`` in
    bytes, or None for anything else (PowerShell banners, errors, garbage).

    The long form ``"mem <available kB> <total kB> <committed kB> <commit
    limit kB> <pages input/s>"`` adds ``"committed"``, ``"commit_limit"``
    (bytes) and ``"pages_in"`` (pages/s); impossible extras (a limit of 0, a
    negative rate) drop the extras and keep the short reading."""
    parts = (line or "").strip().split()
    if len(parts) not in (3, 6) or parts[0] != "mem":
        return None
    try:
        avail, total = int(parts[1]) * 1024, int(parts[2]) * 1024
    except ValueError:
        return None
    if total <= 0 or avail < 0 or avail > total:
        return None
    out = {"available": avail, "total": total}
    if len(parts) == 6:
        try:
            committed, limit = int(parts[3]) * 1024, int(parts[4]) * 1024
            pages_in = int(float(parts[5]))
        except ValueError:
            return out
        if limit > 0 and 0 <= committed and pages_in >= 0:
            out.update(committed=committed, commit_limit=limit, pages_in=pages_in)
    return out


def reading(now: Optional[float] = None) -> Optional[Dict[str, float]]:
    """The latest host reading ``{"available", "total", "at"}`` (bytes,
    epoch) — plus, from the long line, ``"committed"``, ``"commit_limit"``,
    ``"pages_in"`` (this reading's rate), ``"pages_in_median"`` (the median
    of the last :data:`PAGING_WINDOW` readings) and ``"pages_in_samples"`` (how
    many that mean holds) — or None: not WSL, not started, or nothing fresh
    within :data:`STALE_SECONDS`. Never raises, never blocks."""
    r = _STATE.get("reading")
    if not isinstance(r, dict):
        return None
    now = time.time() if now is None else now
    if now - float(r.get("at") or 0) > STALE_SECONDS:
        return None
    return dict(r)


def _set_reading(parsed: Dict[str, int], now: Optional[float] = None) -> None:
    out: Dict[str, float] = dict(parsed, at=time.time() if now is None else now)
    window = list(_STATE.get("paging") or [])  # type: ignore[arg-type]
    prev = _STATE.get("reading")
    if (
        not isinstance(prev, dict)
        or out["at"] - float(prev.get("at") or 0) > STALE_SECONDS
    ):
        window = []  # a gap (the loop restarted): the old minute is not this one
    if "pages_in" in parsed:
        window = (window + [float(parsed["pages_in"])])[-PAGING_WINDOW:]
        ordered = sorted(window)
        mid = len(ordered) // 2
        out["pages_in_median"] = (
            ordered[mid]
            if len(ordered) % 2
            else round((ordered[mid - 1] + ordered[mid]) / 2.0, 1)
        )
        out["pages_in_samples"] = len(window)
    else:
        window = []
    _STATE["paging"] = window
    _STATE["reading"] = out


def _run() -> None:
    backoff = _BACKOFF_FIRST
    while not _STATE.get("stop"):
        exe = _powershell()
        if not exe:
            return  # no interop: nothing to read, ever
        started = time.monotonic()
        got = False
        try:
            proc = subprocess.Popen(
                [exe, "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
                text=True,
                encoding="ascii",
                errors="replace",
            )
        except OSError:
            proc = None
        if proc is not None:
            _STATE["proc"] = proc
            for line in proc.stdout or ():
                parsed = parse_line(line)
                if parsed is not None:
                    _set_reading(parsed)
                    got = True
                if _STATE.get("stop"):
                    break
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
            _STATE["proc"] = None
        if _STATE.get("stop"):
            return
        # A loop that ran a while before dying starts over at the short wait.
        if got and time.monotonic() - started > _BACKOFF_CAP:
            backoff = _BACKOFF_FIRST
        time.sleep(backoff)
        backoff = min(_BACKOFF_CAP, backoff * 2)


def start() -> bool:
    """Start the reader once per process (inside WSL only). True when it is
    running after the call. Never raises."""
    if not _enabled():
        return False
    with _LOCK:
        t = _STATE.get("thread")
        if isinstance(t, threading.Thread) and t.is_alive():
            return True
        _STATE["stop"] = False
        t = threading.Thread(target=_run, name="host-memory", daemon=True)
        _STATE["thread"] = t
        t.start()
    return True


def stop() -> None:
    """Stop the loop (the thread ends at its next line or wait)."""
    _STATE["stop"] = True
    proc = _STATE.get("proc")
    if isinstance(proc, subprocess.Popen):
        try:
            proc.kill()
        except OSError:
            pass


def _reset_for_tests() -> None:
    stop()
    _STATE.update(
        {"reading": None, "thread": None, "proc": None, "stop": False, "paging": []}
    )
