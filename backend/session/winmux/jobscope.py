"""``systemd-run --scope`` for native Windows: one agent's memory ceiling.

On Linux an agent launch runs inside its own systemd user scope with
``MemoryMax`` (:mod:`backend.providers.agent_limits`): above it the kernel's
OOM killer acts inside that agent's tree, the CLI dies to SIGKILL, its exit
marker reads 137 and the server's auto-revive (:mod:`backend.web.core.revive`)
relaunches it once memory has recovered. Windows has no OOM killer and no
cgroups — a Job Object is the equivalent container, but at its memory limit
Windows only fails allocations; nothing is killed and nothing says why.

So this wrapper does both halves::

    python jobscope.py --max <bytes> -- <argv…>

* puts itself in a new Job Object with ``JOB_OBJECT_LIMIT_JOB_MEMORY`` =
  ``--max`` (committed memory of every process in the job together), so
  everything the command starts — the CLI, its MCP servers, the test suite
  it runs — is inside;
* watches the job's completion port, and on
  ``JOB_OBJECT_MSG_JOB_MEMORY_LIMIT`` ends the whole tree with
  ``TerminateJobObject(job, 137)`` — :data:`KILLED_EXIT_CODE`, the code
  ``launch_script.unless_killed``, the exit marker and the auto-revive read
  as "killed for memory" on every OS;
* otherwise runs the command in this console (the pane's ConPTY: same
  terminal, environment and working directory) and exits with its status.

Fail-open like every agent guard: no Job Object (an older Windows, a parent
job that forbids nesting) → the command just runs. Stdlib only and run by
path (``agent_limits.wrap_launch`` names this file), so it needs nothing
importable; off Windows it simply runs the command (tests).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import List, Optional, Tuple

__all__ = ["KILLED_EXIT_CODE", "main", "parse_args"]

#: 128 + SIGKILL: what Linux's OOM killer leaves (launch_script.KILLED_EXIT_CODE).
KILLED_EXIT_CODE = 137

_JobObjectExtendedLimitInformation = 9
_JobObjectAssociateCompletionPortInformation = 7
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
_INFINITE = 0xFFFFFFFF


def parse_args(argv: List[str]) -> Tuple[int, List[str]]:
    """``(max_bytes, command)`` from ``--max <bytes> -- <command…>``
    (``0`` = no limit). ``ValueError`` on anything else."""
    limit = 0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            cmd = argv[i + 1 :]
            if not cmd:
                raise ValueError("no command after --")
            return limit, cmd
        if a == "--max" and i + 1 < len(argv):
            limit = max(0, int(argv[i + 1]))
            i += 2
            continue
        if a.startswith("--max="):
            limit = max(0, int(a.split("=", 1)[1]))
            i += 1
            continue
        raise ValueError("unknown argument: %s" % a)
    raise ValueError("usage: jobscope.py --max <bytes> -- <command…>")


def _job(limit: int):  # pragma: no cover - Windows
    """``(kernel32, job, port)`` with this process inside the job, or None."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateIoCompletionPort.restype = wintypes.HANDLE
    k32.CreateIoCompletionPort.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_size_t,
        wintypes.DWORD,
    )
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, ctypes.c_uint)
    k32.GetQueuedCompletionStatus.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
    )

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class PORT(ctypes.Structure):
        _fields_ = [
            ("CompletionKey", ctypes.c_void_p),
            ("CompletionPort", wintypes.HANDLE),
        ]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_JOB_MEMORY
    info.JobMemoryLimit = limit
    if not k32.SetInformationJobObject(
        job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        return None
    port = k32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
    if not port:
        return None
    assoc = PORT(None, port)
    if not k32.SetInformationJobObject(
        job,
        _JobObjectAssociateCompletionPortInformation,
        ctypes.byref(assoc),
        ctypes.sizeof(assoc),
    ):
        return None
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        return None
    return k32, job, port


def _watch(k32, job, port) -> None:  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    msg = wintypes.DWORD()
    key = ctypes.c_size_t()
    ov = ctypes.c_void_p()
    while k32.GetQueuedCompletionStatus(
        port, ctypes.byref(msg), ctypes.byref(key), ctypes.byref(ov), _INFINITE
    ):
        if msg.value == _JOB_OBJECT_MSG_JOB_MEMORY_LIMIT:
            try:
                sys.stderr.write(
                    "\r\n[mindflock] the agent went over its memory limit "
                    "(general.agent_memory.max) and was stopped\r\n"
                )
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
            # Every process in the job, this one included, exits 137.
            k32.TerminateJobObject(job, KILLED_EXIT_CODE)
            return


def _exit(code: int) -> None:
    """Exit with ``code`` as the OS reports it — an NTSTATUS crash code
    (0xC0000005) does not fit ``sys.exit``'s C long on Windows."""
    if os.name == "nt":  # pragma: no cover - Windows
        import ctypes

        sys.stdout.flush()
        sys.stderr.flush()
        ctypes.windll.kernel32.ExitProcess(ctypes.c_uint(code & 0xFFFFFFFF))
    sys.exit(code)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        limit, cmd = parse_args(list(sys.argv[1:] if argv is None else argv))
    except ValueError as e:
        sys.stderr.write("jobscope: %s\n" % e)
        return 2
    if os.name == "nt" and limit > 0:  # pragma: no cover - Windows
        try:
            scope = _job(limit)
        except Exception:  # noqa: BLE001 — fail-open: no scope, still launch
            scope = None
        if scope is not None:
            threading.Thread(
                target=_watch, args=scope, name="jobscope", daemon=True
            ).start()
    try:
        child = subprocess.Popen(cmd)
    except OSError as e:
        sys.stderr.write("jobscope: %s: %s\n" % (cmd[0], e))
        return 127
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Ctrl+C in the pane goes to every process on the console: it is
            # the agent's to handle, never this wrapper's.
            continue


if __name__ == "__main__":  # pragma: no cover
    _exit(main())
