"""The Windows host's memory, read from inside a WSL guest.

A WSL2 guest sees only its own VM: ``/proc/meminfo`` can read "plenty
available" while Windows itself has a few hundred MB left, because the VM has
grown into everything ``.wslconfig``'s ``memory=`` ceiling allows and Windows
cannot take memory back from it. Once Windows runs short it pages its own
apps out and the whole PC stalls — the guest never sees a thing
(2026-11-05: the owner rebooted a "bricked" PC with WSL itself not even full).

This module keeps ONE long-lived ``powershell.exe`` that prints the host's
available and total physical memory every :data:`INTERVAL_SECONDS`; a daemon
thread reads its lines into :func:`reading`. Spawning PowerShell per sample
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

__all__ = ["start", "stop", "reading", "parse_line", "INTERVAL_SECONDS"]

#: How often the PowerShell loop prints a reading.
INTERVAL_SECONDS = 10
#: A reading older than this is ignored (the loop died or stalled).
STALE_SECONDS = 45.0
#: Restart backoff after the loop exits: doubles from the first to the cap.
_BACKOFF_FIRST = 15.0
_BACKOFF_CAP = 300.0

_WIN_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Win32_OperatingSystem: FreePhysicalMemory is what Windows calls
# "Available" (free + standby), TotalVisibleMemorySize the usable RAM; both kB.
# A write to a closed pipe throws, and with Stop that ends the loop.
_SCRIPT = (
    "$ErrorActionPreference='Stop'; "
    "while ($true) { "
    "$o = Get-CimInstance Win32_OperatingSystem; "
    "[Console]::Out.WriteLine(('mem {0} {1}' -f $o.FreePhysicalMemory, $o.TotalVisibleMemorySize)); "
    "[Console]::Out.Flush(); "
    "Start-Sleep -Seconds %d }" % INTERVAL_SECONDS
)

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "reading": None,
    "thread": None,
    "proc": None,
    "stop": False,
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
    bytes, or None for anything else (PowerShell banners, errors, garbage)."""
    parts = (line or "").strip().split()
    if len(parts) != 3 or parts[0] != "mem":
        return None
    try:
        avail, total = int(parts[1]) * 1024, int(parts[2]) * 1024
    except ValueError:
        return None
    if total <= 0 or avail < 0 or avail > total:
        return None
    return {"available": avail, "total": total}


def reading(now: Optional[float] = None) -> Optional[Dict[str, float]]:
    """The latest host reading ``{"available", "total", "at"}`` (bytes,
    epoch), or None: not WSL, not started, or nothing fresh within
    :data:`STALE_SECONDS`. Never raises, never blocks."""
    r = _STATE.get("reading")
    if not isinstance(r, dict):
        return None
    now = time.time() if now is None else now
    if now - float(r.get("at") or 0) > STALE_SECONDS:
        return None
    return dict(r)


def _set_reading(parsed: Dict[str, int], now: Optional[float] = None) -> None:
    _STATE["reading"] = dict(parsed, at=time.time() if now is None else now)


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
    _STATE.update({"reading": None, "thread": None, "proc": None, "stop": False})
