"""Background jobs a Claude Code session left running, and what each is doing.

Claude Code reports a session ``busy`` in ``claude agents --json`` for as long
as ANY background task it started is alive. That is the right answer while a
backgrounded test run is crunching — the agent will be woken with its result —
and the wrong one for a job that will never finish. Live incident: an agent
backgrounded ``until ! pgrep -f "git commit -q -F"; do sleep 5; done`` to wait
for its own commit. ``pgrep -f`` matches full command lines, the loop's own
shell carries the pattern in its command line, so the loop waited on itself
forever. The turn ended, the Stop hook wrote ``idle``, and the session read
"working" for as long as nobody noticed, because the live ``busy`` outranks
the hook marker.

This module is what the Claude provider consults when those two disagree (the
live signal says busy, the Stop hook says the turn ended): it finds the
session's background job processes and classifies each one.

* **Finding them.** Claude Code runs every Bash tool call — foreground or
  background — in its own shell whose stdout is
  ``…/tasks/<task-id>.output``. Once the Stop hook has fired no foreground call
  is in flight, so every such shell still alive under the pane is a background
  job. The job ROOT is the process holding that file on fd 1 whose parent does
  not (its children inherit the fd).
* **running** — the job's process tree burned CPU or its output file grew in
  the last :data:`ACTIVE_GRACE_S`. Real work: the session stays ``working``.
* **waiting** — quiet (a polling loop, a blocked read) but not yet for
  :data:`STUCK_AFTER_S`. The session reads ``idle``; finishing the job wakes
  the agent, so autopilot holds off shipping it.
* **stuck** — quiet for :data:`STUCK_AFTER_S`, or PROVEN unable to finish: a
  ``while pgrep -f P`` / ``until ! pgrep -f P`` loop where nothing outside the
  job itself matches ``P``. The session reads ``idle`` and the row offers to
  stop the job.

Linux only (``/proc``): elsewhere :func:`scan` returns None and the provider
keeps trusting the live signal, exactly as before.

Known gap: a turn the CLI starts on its own (a finished job's notification
wakes the agent) fires no UserPromptSubmit hook, so until its first tool call
the marker still says ``idle``; if OTHER quiet jobs are alive in that window
the session reads idle for those few seconds.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

# A job whose process tree used at least this fraction of one core since the
# previous sample is doing work. A `sleep 5` polling loop costs well under 1%.
ACTIVE_CPU_FRACTION = 0.03
# How long after its last CPU burst / output write a job still counts as running.
ACTIVE_GRACE_S = 60.0
# Quiet this long and a job is presumed stuck rather than waiting.
STUCK_AFTER_S = 15 * 60.0
# A session's last scan is served to the row builder for this long.
_RESULT_TTL_S = 30.0

_TASK_OUT_RE = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output$")
# Claude Code wraps each command as ``… eval '<command>' < /dev/null && pwd -P …``.
_EVAL_RE = re.compile(r"\beval '(.*)' < /dev/null", re.DOTALL)
# A loop that runs until NO process matches: `while pgrep …` / `until ! pgrep …`.
_PGREP_WAIT_RE = re.compile(r"\b(?:while|until\s+!)\s+pgrep\b([^;&|\n]*)")

_lock = threading.Lock()
# (pid, starttime) -> {"ts", "cpu", "active"}: the previous CPU sample per job.
_samples: Dict[Tuple[int, int], dict] = {}
# session name -> (scanned_at, jobs): the last scan, for the row builder.
_last: Dict[str, Tuple[float, list]] = {}


def _hz() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK")) or 100
    except (ValueError, OSError, AttributeError):
        return 100


def _read_stat(pid: int) -> Optional[dict]:
    """ppid, cpu ticks (self + reaped children) and start time from
    ``/proc/<pid>/stat``, or None for a vanished process."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm sits in parentheses and may itself contain spaces or parentheses.
    rest = raw[raw.rfind(")") + 2 :].split()
    try:
        return {
            "ppid": int(rest[1]),
            "cpu": sum(int(x) for x in rest[11:15]),  # utime stime cutime cstime
            "start": int(rest[19]),
        }
    except (IndexError, ValueError):
        return None


def _process_table() -> Dict[int, dict]:
    table: Dict[int, dict] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if name.isdigit():
            st = _read_stat(int(name))
            if st is not None:
                table[int(name)] = st
    return table


def _cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _stdout_target(pid: int) -> str:
    try:
        return os.readlink("/proc/%d/fd/1" % pid)
    except OSError:
        return ""


def _descendants(table: Dict[int, dict], root: int) -> List[int]:
    kids: Dict[int, List[int]] = {}
    for pid, st in table.items():
        kids.setdefault(st["ppid"], []).append(pid)
    out: List[int] = []
    stack = list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return out


def command_of(cmdline: str) -> str:
    """The command the agent asked for, unwrapped from Claude Code's shell
    preamble when it is recognisable (the whole command line otherwise)."""
    m = _EVAL_RE.search(cmdline)
    if not m:
        return cmdline
    # A literal single quote is spelled '\'' or '"'"' inside the eval string.
    return m.group(1).replace("'\\''", "'").replace("'\"'\"'", "'")


def _pgrep_wait_pattern(command: str) -> Optional[str]:
    """The ``-f`` pattern of a wait-until-gone pgrep loop, else None.

    Only ``-f`` can self-match: without it pgrep compares process NAMES, and
    the loop's own shell is named ``bash``/``zsh``."""
    m = _PGREP_WAIT_RE.search(command)
    if not m:
        return None
    try:
        args = shlex.split(m.group(1))
    except ValueError:
        return None
    full = False
    pattern = None
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-") and len(arg) > 1:
            letters = arg.lstrip("-")
            if arg == "--full" or ("f" in letters and not arg.startswith("--")):
                full = True
            # pgrep options that take a value (-u user, -P ppid, ...).
            if (
                letters[-1:] in ("u", "U", "g", "G", "P", "s", "t", "d")
                and "=" not in arg
            ):
                skip_next = True
            continue
        if (
            arg.startswith(">")
            or arg.startswith("<")
            or arg[:1].isdigit()
            and ">" in arg
        ):
            break
        pattern = arg
        break
    if not (full and pattern):
        return None
    try:
        re.compile(pattern)
    except re.error:
        # pgrep itself exits 2 on a bad regex, so such a loop ends at once: it
        # can't be the one sitting here quietly. Never call it self-waiting.
        return None
    return pattern


def _matches(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def waits_on_itself(
    command: str, own: set, cmdlines: Optional[Dict[int, str]] = None
) -> bool:
    """True when ``command`` loops until ``pgrep -f P`` finds nothing, and the
    only processes ``P`` matches are the job's own (``own``) or other copies of
    the same wait loop: the loop's exit condition can never become true."""
    pattern = _pgrep_wait_pattern(command)
    if not pattern:
        return False
    if cmdlines is None:
        cmdlines = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            return False
        for name in names:
            if name.isdigit():
                cmdlines[int(name)] = _cmdline(int(name))
    me = os.getpid()
    for pid, text in cmdlines.items():
        if pid in own or pid == me or not text:
            continue
        if not _matches(pattern, text):
            continue
        if _pgrep_wait_pattern(command_of(text)) == pattern:
            continue  # another copy of the same loop: waiting on each other
        if os.path.basename(text.split(" ", 1)[0]) == "pgrep":
            continue  # some loop's transient probe
        return False
    return True


def _span(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return "%ds" % s
    if s < 90 * 60:
        return "%dm" % round(s / 60)
    return "%.1fh" % (s / 3600)


def jobs_under(pane_pid, now: Optional[float] = None) -> Optional[List[dict]]:
    """Every live background-job root under ``pane_pid``, classified.

    Each job: ``{"id", "pid", "command", "state", "reason", "started",
    "quiet_s"}`` with ``state`` one of running / waiting / stuck. None when it
    cannot be known (no ``/proc``, no pane pid)."""
    if not pane_pid or not os.path.isdir("/proc/self"):
        return None
    try:
        pane_pid = int(pane_pid)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    table = _process_table()
    if pane_pid not in table:
        return None
    hz = _hz()
    try:
        with open("/proc/uptime") as fh:
            boot = now - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        boot = None
    tree = _descendants(table, pane_pid)
    outs = {pid: _stdout_target(pid) for pid in tree}
    jobs: List[dict] = []
    seen_keys = set()
    for pid in tree:
        m = _TASK_OUT_RE.search(outs.get(pid) or "")
        if not m or outs.get(table[pid]["ppid"]) == outs[pid]:
            continue
        st = table[pid]
        own = set(_descendants(table, pid)) | {pid}
        cpu_s = sum(table[p]["cpu"] for p in own if p in table) / hz
        started = (boot + st["start"] / hz) if boot is not None else now
        try:
            out_mtime = os.stat(outs[pid]).st_mtime
        except OSError:
            out_mtime = 0.0
        key = (pid, st["start"])
        seen_keys.add(key)
        with _lock:
            prev = _samples.get(key)
            if prev is None:
                # First sighting (a fresh job, or a server restart): judge by
                # the lifetime average, since there is no previous sample.
                age = max(1.0, now - started)
                active = now if cpu_s / age >= ACTIVE_CPU_FRACTION else started
            else:
                dt = max(1e-3, now - prev["ts"])
                busy = (cpu_s - prev["cpu"]) / dt >= ACTIVE_CPU_FRACTION
                active = now if busy else prev["active"]
            active = max(active, out_mtime)
            _samples[key] = {
                "ts": now,
                "cpu": max(cpu_s, prev["cpu"] if prev else 0.0),
                "active": active,
            }
        command = command_of(_cmdline(pid))
        quiet = max(0.0, now - active)
        if quiet >= ACTIVE_GRACE_S and waits_on_itself(command, own):
            state = "stuck"
            reason = "waits for itself: its pgrep -f pattern matches only its own command line"
        elif quiet < ACTIVE_GRACE_S:
            state, reason = "running", "busy within the last minute"
        elif quiet < STUCK_AFTER_S:
            state, reason = "waiting", "no CPU or output for " + _span(quiet)
        else:
            state, reason = "stuck", "no CPU or output for " + _span(quiet)
        jobs.append(
            {
                "id": m.group(1),
                "pid": pid,
                "command": command[:300],
                "state": state,
                "reason": reason,
                "started": round(started, 1),
                "quiet_s": round(quiet, 1),
            }
        )
    with _lock:
        # Samples of jobs no longer under THIS pane are left for their own
        # pane's scan; prune only ones whose process is gone.
        for key in [
            k for k in _samples if k[0] not in table or table[k[0]]["start"] != k[1]
        ]:
            _samples.pop(key, None)
    jobs.sort(key=lambda j: j["started"])
    return jobs


def _pane_pid(session_name: str) -> Optional[str]:
    try:
        cp = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_name, "#{pane_pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.decode("utf-8", "replace").strip() or None


def scan(session_name: str) -> Optional[List[dict]]:
    """Classify the background jobs of tmux session ``session_name`` and keep
    the answer for :func:`recent`. None when it cannot be known."""
    jobs = jobs_under(_pane_pid(session_name))
    with _lock:
        if jobs is None:
            _last.pop(session_name, None)
        else:
            _last[session_name] = (time.time(), jobs)
    return jobs


def forget(session_name: str) -> None:
    """Drop the session's last scan (its live and hook signals agree again)."""
    with _lock:
        _last.pop(session_name, None)


def recent(session_name: str) -> Optional[List[dict]]:
    """The session's last :func:`scan`, while fresh; None otherwise. Never
    scans: the row builder must not add a /proc walk per session per tick."""
    with _lock:
        hit = _last.get(session_name)
    if not hit or time.time() - hit[0] > _RESULT_TTL_S:
        return None
    return hit[1]


def stop(session_name: str, pid: int, grace_s: float = 2.0) -> bool:
    """Stop background job ``pid`` of ``session_name``: SIGTERM its process
    group (the job's shell leads its own), SIGKILL after ``grace_s``.

    Refuses (False) unless ``pid`` is a CURRENT job root of that session, so a
    stale or forged pid can never signal an unrelated process."""
    jobs = jobs_under(_pane_pid(session_name)) or []
    if not any(j["pid"] == pid for j in jobs):
        return False
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return False
    # Only signal the group when the job leads it; never the server's own.
    target = pgid if pgid == pid and pgid != os.getpgrp() else None
    try:
        if target:
            os.killpg(target, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if _read_stat(pid) is None:
            break
        time.sleep(0.1)
    else:
        try:
            if target:
                os.killpg(target, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    forget(session_name)
    return True
