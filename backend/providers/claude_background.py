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
* **stuck** — quiet for its threshold (below), or PROVEN unable to finish: a
  ``while pgrep -f P`` / ``until ! pgrep -f P`` loop where nothing outside the
  job itself matches ``P``. The session reads ``idle`` and the row offers to
  stop the job.

**The stuck threshold adapts** (``threshold_s`` on each job)::

    threshold_s = max(STUCK_AFTER_S, 2 × longest_gap_s, 1.5 × expect_s)

* ``longest_gap_s`` is LEARNED: the longest quiet stretch the job has come
  back from (CPU or output again after a quiet spell), kept per
  ``(pid, start)`` in the ``_samples`` table. A job that went quiet for 41
  minutes and resumed is not stuck at 20. Lost on a server restart: the first
  sighting judges by the lifetime average, as before.
* ``expect_s`` is DECLARED: how long the job should take. A person sets it
  from the row (``POST /api/instances/{title}/jobs/{id}/expect``, ``by``
  "you"), the agent with the MCP tool ``expect_background_job`` ("agent"), or
  the command itself carries it as a prefix env assignment —
  ``MINDFLOCK_EXPECT=6h python train.py`` ("command", no file write).
  Precedence: you > agent > command. Set ones live in
  ``$MINDFLOCK_HOME/job_expectations.json`` (default ``~/.mindflock``)::

      {"version": 1,
       "jobs": {"<job id>": {"expect_s", "by", "set_at", "title", "seen_at"}},
       "announced": {"<job id>": <epoch>}}

  pruned once a job has been gone (unseen) for a day. ``announced`` is the
  once-per-job record of the ``session.job_past_expected`` event
  (:func:`announce_past_expected`), so a restart doesn't repeat it.
  Durations (:func:`parse_duration`): ``90s``, ``20m``, ``6h``, ``2d``,
  ``1h30m``, or plain seconds; 1 min to 14 days.

Each job also carries ``expect_s``, ``expected_by``, ``eta`` (``started +
expect_s``), ``longest_gap_s``, ``threshold_s`` and ``past_expected`` (more
than ``expect_s`` since it started). The ``reason`` says which rule spoke:
``"2h 10m of ~6h expected"``, ``"quiet 22m — it has resumed after 41m
before"``, ``"expected ~6h, now 9h 10m in and quiet for 3h"``, or the plain
``"no CPU or output for 15m"``.

Linux only (``/proc``): elsewhere :func:`scan` returns None and the provider
keeps trusting the live signal, exactly as before.

Known gap: a turn the CLI starts on its own (a finished job's notification
wakes the agent) fires no UserPromptSubmit hook, so until its first tool call
the marker still says ``idle``; if OTHER quiet jobs are alive in that window
the session reads idle for those few seconds.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# A job whose process tree used at least this fraction of one core since the
# previous sample is doing work. A `sleep 5` polling loop costs well under 1%.
ACTIVE_CPU_FRACTION = 0.03
# How long after its last CPU burst / output write a job still counts as running.
ACTIVE_GRACE_S = 60.0
# Quiet this long and a job is presumed stuck rather than waiting.
STUCK_AFTER_S = 15 * 60.0
# A session's last scan is served to the row builder for this long.
_RESULT_TTL_S = 30.0
# The stuck threshold is at least this many times the longest quiet stretch
# the job has resumed from, and this many times its expected duration.
GAP_FACTOR = 2.0
EXPECT_FACTOR = 1.5
# The range a declared expected duration may take.
EXPECT_MIN_S = 60.0
EXPECT_MAX_S = 14 * 86400.0
# An expectation whose job has not been seen for this long is pruned.
_EXPECT_PRUNE_S = 86400.0
# A seen job's ``seen_at`` is refreshed on disk at most this often.
_SEEN_REFRESH_S = 3600.0

_DURATION_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# ``MINDFLOCK_EXPECT=6h python train.py`` (a prefix env assignment, optionally
# quoted). The lookbehind keeps ``X_MINDFLOCK_EXPECT=`` from counting.
_EXPECT_ENV_RE = re.compile(
    r"(?<![\w$])MINDFLOCK_EXPECT=(['\"]?)([0-9][0-9A-Za-z.]*)\1"
)

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
# (pid, starttime) of jobs whose ``session.job_past_expected`` went out.
_announced: set = set()

# The expectations file, cached on its (mtime, size).
_store_lock = threading.Lock()
_store_cache: Dict[str, Any] = {"sig": None, "data": None}


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


def format_duration(seconds: float) -> str:
    """``45s``, ``41m``, ``6h``, ``2h 10m``, ``2d 3h``: the expectation copy."""
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return "%ds" % s
    m = (s + 30) // 60
    if m < 60:
        return "%dm" % m
    h, m = divmod(m, 60)
    if h < 24:
        return "%dh %dm" % (h, m) if m else "%dh" % h
    d, h = divmod(h, 24)
    return "%dd %dh" % (d, h) if h else "%dd" % d


def parse_duration(value: Union[str, int, float, None]) -> Optional[float]:
    """Seconds for ``90s`` / ``20m`` / ``6h`` / ``2d`` / ``1h30m`` (units may
    repeat and carry decimals, ``1.5h``), or a plain number of seconds; None
    for anything else or outside :data:`EXPECT_MIN_S` … :data:`EXPECT_MAX_S`."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        secs = float(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        try:
            secs = float(text)
        except ValueError:
            pos, secs = 0, 0.0
            for m in _DURATION_RE.finditer(text):
                if m.start() != pos:
                    return None
                secs += float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
                pos = m.end()
            if pos == 0 or text[pos:].strip():
                return None
    else:
        return None
    if secs != secs or not EXPECT_MIN_S <= secs <= EXPECT_MAX_S:  # NaN, range
        return None
    return secs


def command_expectation(command: str) -> Optional[float]:
    """The ``MINDFLOCK_EXPECT=<duration>`` a command line carries, in seconds."""
    m = _EXPECT_ENV_RE.search(command or "")
    return parse_duration(m.group(2)) if m else None


# --- the expectations file ---------------------------------------------------
def expectations_path() -> Path:
    """``$MINDFLOCK_JOB_EXPECTATIONS_FILE``, else ``job_expectations.json``
    under ``$MINDFLOCK_HOME`` or the config dir (``~/.mindflock``)."""
    override = os.environ.get("MINDFLOCK_JOB_EXPECTATIONS_FILE", "").strip()
    if override:
        path = os.path.expanduser(override)
    else:
        home = os.environ.get("MINDFLOCK_HOME", "").strip()
        if home:
            base = os.path.expanduser(home)
        else:
            from backend.config import config as _config

            base = _config.GetConfigDir()
        path = os.path.join(base, "job_expectations.json")
    from backend.config import home_guard

    return Path(home_guard.guard(path, "job expectations"))


def _empty_store() -> dict:
    return {"version": 1, "jobs": {}, "announced": {}, "gaps": {}}


def _load_store() -> dict:
    """The expectations file (a fresh empty one when missing or unreadable).
    Callers hold :data:`_store_lock` and must not mutate the cached copy."""
    try:
        path = expectations_path()
        st = path.stat()
    except OSError:
        return _empty_store()
    sig = (str(path), st.st_mtime_ns, st.st_size)
    if _store_cache["sig"] == sig and _store_cache["data"] is not None:
        return _store_cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = _empty_store()
    for key in ("jobs", "announced", "gaps"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    _store_cache.update(sig=sig, data=data)
    return data


def _save_store(data: dict, now: float) -> None:
    """Prune, then write atomically. Callers hold :data:`_store_lock`."""
    jobs = {
        k: v
        for k, v in data["jobs"].items()
        if isinstance(v, dict)
        and max(float(v.get("set_at") or 0), float(v.get("seen_at") or 0))
        >= now - _EXPECT_PRUNE_S
    }
    # An announced job can't outlive the longest expectation by much.
    announced = {
        k: v
        for k, v in data["announced"].items()
        if isinstance(v, (int, float)) and v >= now - EXPECT_MAX_S - _EXPECT_PRUNE_S
    }
    # A learned rhythm outlives a restart (the whole point of keeping it), but
    # not the longest expectation: a job gone that long is a different job.
    gaps = {
        k: v
        for k, v in data.get("gaps", {}).items()
        if isinstance(v, dict) and float(v.get("at") or 0) >= now - EXPECT_MAX_S
    }
    out = {"version": 1, "jobs": jobs, "announced": announced, "gaps": gaps}
    path = expectations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    _store_cache.update(sig=None, data=None)


def _effective_expectation(
    job_id: str, command: str
) -> Tuple[Optional[float], Optional[str]]:
    """``(expect_s, by)`` for one job: the file's entry (you / agent), else
    the command's ``MINDFLOCK_EXPECT=``, else ``(None, None)``."""
    with _store_lock:
        entry = _load_store()["jobs"].get(job_id)
    if isinstance(entry, dict):
        secs = parse_duration(entry.get("expect_s"))
        by = entry.get("by")
        if secs is not None and by in ("you", "agent"):
            return secs, by
    secs = command_expectation(command)
    return (secs, "command") if secs is not None else (None, None)


def _touch_seen(job_ids: List[str], now: float) -> None:
    """Keep a live job's expectation from being pruned (``seen_at``), writing
    at most once per :data:`_SEEN_REFRESH_S` per job."""
    try:
        with _store_lock:
            data = _load_store()
            stale = [
                j
                for j in job_ids
                if isinstance(data["jobs"].get(j), dict)
                and now - float(data["jobs"][j].get("seen_at") or 0) >= _SEEN_REFRESH_S
            ]
            if not stale:
                return
            data = json.loads(json.dumps(data))
            for j in stale:
                data["jobs"][j]["seen_at"] = round(now, 1)
            _save_store(data, now)
    except OSError:
        pass  # a read-only home never fails a scan


def set_expectation(
    job_id: str, expect_s: float, by: str, title: str = "", now: Optional[float] = None
) -> Tuple[bool, dict]:
    """Record that job ``job_id`` should take ``expect_s`` seconds, as said by
    ``by`` ("you" or "agent"). A person's expectation stands against the
    agent's: then nothing is written and the result is ``(False, theirs)``.
    → ``(written, the entry in force)``."""
    if by not in ("you", "agent"):
        raise ValueError("by must be 'you' or 'agent'")
    now = time.time() if now is None else now
    with _store_lock:
        data = json.loads(json.dumps(_load_store()))
        cur = data["jobs"].get(job_id)
        if by == "agent" and isinstance(cur, dict) and cur.get("by") == "you":
            return False, cur
        entry = {
            "expect_s": round(float(expect_s), 1),
            "by": by,
            "set_at": round(now, 1),
            "seen_at": round(now, 1),
            "title": title,
        }
        data["jobs"][job_id] = entry
        _save_store(data, now)
    return True, entry


def remembered_gap(job_id: str) -> float:
    """The longest quiet gap this job was seen to resume from, across server
    restarts (``gaps`` in the expectations file); 0 when none."""
    try:
        with _store_lock:
            v = _load_store()["gaps"].get(job_id)
    except Exception:  # noqa: BLE001 — unreadable store: learn afresh
        return 0.0
    try:
        return max(0.0, float(v.get("gap_s") or 0.0)) if isinstance(v, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def remember_gap(job_id: str, gap_s: float, now: Optional[float] = None) -> None:
    """Write a grown gap through to the file, so the rhythm survives a
    restart. Only called when the gap GREW (rare), so no throttle is needed."""
    now = time.time() if now is None else now
    try:
        with _store_lock:
            data = json.loads(json.dumps(_load_store()))
            data["gaps"][job_id] = {"gap_s": round(float(gap_s), 1), "at": now}
            _save_store(data, now)
    except Exception:  # noqa: BLE001 — the in-memory sample still carries it
        pass


def clear_expectation(job_id: str, now: Optional[float] = None) -> bool:
    """Drop the file's expectation for ``job_id`` (a command's own
    ``MINDFLOCK_EXPECT=`` still applies). False when there was none."""
    now = time.time() if now is None else now
    with _store_lock:
        data = json.loads(json.dumps(_load_store()))
        if data["jobs"].pop(job_id, None) is None:
            return False
        _save_store(data, now)
    return True


def _boot_time(now: float) -> Optional[float]:
    try:
        with open("/proc/uptime") as fh:
            return now - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _judge(
    quiet: float,
    elapsed: float,
    gap: float,
    expect_s: Optional[float],
    self_waiting: bool,
) -> Tuple[str, str, float]:
    """``(state, reason, threshold_s)`` for one job (see the module doc)."""
    threshold = max(STUCK_AFTER_S, GAP_FACTOR * gap)
    if expect_s:
        threshold = max(threshold, EXPECT_FACTOR * expect_s)
    if quiet >= ACTIVE_GRACE_S and self_waiting:
        return (
            "stuck",
            "waits for itself: its pgrep -f pattern matches only its own command line",
            threshold,
        )
    if quiet < ACTIVE_GRACE_S:
        return "running", "busy within the last minute", threshold
    if quiet < threshold:
        if expect_s and elapsed > expect_s:
            reason = "%s in, past the ~%s expected; quiet %s" % (
                format_duration(elapsed),
                format_duration(expect_s),
                format_duration(quiet),
            )
        elif expect_s:
            reason = "%s of ~%s expected" % (
                format_duration(elapsed),
                format_duration(expect_s),
            )
        elif gap >= ACTIVE_GRACE_S:
            reason = "quiet %s — it has resumed after %s before" % (
                format_duration(quiet),
                format_duration(gap),
            )
        else:
            reason = "no CPU or output for " + _span(quiet)
        return "waiting", reason, threshold
    if expect_s:
        reason = "expected ~%s, now %s in and quiet for %s" % (
            format_duration(expect_s),
            format_duration(elapsed),
            format_duration(quiet),
        )
    else:
        reason = "no CPU or output for " + _span(quiet)
    return "stuck", reason, threshold


def jobs_under(pane_pid, now: Optional[float] = None) -> Optional[List[dict]]:
    """Every live background-job root under ``pane_pid``, classified.

    Each job: ``{"id", "pid", "command", "state", "reason", "started",
    "quiet_s", "expect_s", "expected_by", "eta", "longest_gap_s",
    "threshold_s", "past_expected"}`` with ``state`` one of running /
    waiting / stuck. None when it cannot be known (no ``/proc``, no pane
    pid)."""
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
    boot = _boot_time(now)
    tree = _descendants(table, pane_pid)
    outs = {pid: _stdout_target(pid) for pid in tree}
    jobs: List[dict] = []
    seen_keys = set()
    remembered: List[str] = []  # job ids with an expectation on file
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
        job_id = m.group(1)
        grew = False
        with _lock:
            prev = _samples.get(key)
            # A restart forgets the sample table: seed the rhythm the file
            # remembers, so a job that resumes every 40 min is not "stuck"
            # 15 min after every server restart.
            gap = prev.get("gap", 0.0) if prev else remembered_gap(job_id)
            if prev is None:
                # First sighting (a fresh job, or a server restart): judge by
                # the lifetime average, since there is no previous sample — or,
                # when the file remembers this job's rhythm, give it one full
                # threshold window from now: a restart must not turn a job
                # that resumes every 40 min into "stuck" on the spot.
                age = max(1.0, now - started)
                active = (
                    now if (cpu_s / age >= ACTIVE_CPU_FRACTION or gap > 0) else started
                )
            else:
                dt = max(1e-3, now - prev["ts"])
                busy = (cpu_s - prev["cpu"]) / dt >= ACTIVE_CPU_FRACTION
                active = now if busy else prev["active"]
            active = max(active, out_mtime)
            since = prev.get("since", 0.0) if prev else now
            if prev is not None and active > prev["active"]:
                # It came back from a quiet stretch: learn how long one can
                # be. Only the stretch this process OBSERVED counts — a first
                # sighting's "active" is a guess from the lifetime average.
                seen_gap = active - max(prev["active"], since)
                if seen_gap > gap:
                    gap, grew = seen_gap, True
            _samples[key] = {
                "ts": now,
                "cpu": max(cpu_s, prev["cpu"] if prev else 0.0),
                "active": active,
                "gap": gap,
                "since": since,
            }
        if grew:
            remember_gap(job_id, gap, now)
        command = command_of(_cmdline(pid))
        quiet = max(0.0, now - active)
        elapsed = max(0.0, now - started)
        expect_s, expected_by = _effective_expectation(job_id, command)
        if expected_by in ("you", "agent"):
            remembered.append(job_id)
        state, reason, threshold = _judge(
            quiet,
            elapsed,
            gap,
            expect_s,
            quiet >= ACTIVE_GRACE_S and waits_on_itself(command, own),
        )
        jobs.append(
            {
                "id": job_id,
                "pid": pid,
                "command": command[:300],
                "state": state,
                "reason": reason,
                "started": round(started, 1),
                "quiet_s": round(quiet, 1),
                "expect_s": round(expect_s, 1) if expect_s else None,
                "expected_by": expected_by,
                "eta": round(started + expect_s, 1) if expect_s else None,
                "longest_gap_s": round(gap, 1),
                "threshold_s": round(threshold, 1),
                "past_expected": bool(expect_s) and elapsed > expect_s,
            }
        )
    with _lock:
        # Samples of jobs no longer under THIS pane are left for their own
        # pane's scan; prune only ones whose process is gone.
        for key in [
            k for k in _samples if k[0] not in table or table[k[0]]["start"] != k[1]
        ]:
            _samples.pop(key, None)
        for key in [
            k for k in _announced if k[0] not in table or table[k[0]]["start"] != k[1]
        ]:
            _announced.discard(key)
    if remembered:
        _touch_seen(remembered, now)
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


def find_job(session_name: str, job_id: str) -> Optional[dict]:
    """The CURRENT background job ``job_id`` of ``session_name`` (a fresh
    scan), or None."""
    for job in jobs_under(_pane_pid(session_name)) or []:
        if job["id"] == job_id:
            return job
    return None


def announce_past_expected(
    jobs: Optional[list], now: Optional[float] = None
) -> List[dict]:
    """The jobs in ``jobs`` that are past their expected duration and have
    not been announced yet; marks them announced — in memory per
    ``(pid, start)``, and by job id in the expectations file so a restart
    does not repeat it. The caller emits ``session.job_past_expected``."""
    due = [j for j in jobs or [] if isinstance(j, dict) and j.get("past_expected")]
    if not due:
        return []
    now = time.time() if now is None else now
    out: List[dict] = []
    for job in due:
        st = _read_stat(job.get("pid") or 0)
        if st is None:
            continue
        key = (job["pid"], st["start"])
        with _lock:
            if key in _announced:
                continue
            _announced.add(key)
        try:
            with _store_lock:
                data = _load_store()
                if job["id"] in data["announced"]:
                    continue
                data = json.loads(json.dumps(data))
                data["announced"][job["id"]] = round(now, 1)
                _save_store(data, now)
        except OSError:
            pass  # unrecorded: at worst a restart says it once more
        out.append(job)
    return out


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
