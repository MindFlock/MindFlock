"""The one admission gate every agent launch asks before it starts an agent CLI.

Why it exists: on 2026-11-04 the machine (WSL, 20 GB) ran out of memory
several times. The kernel OOM killer took agents down mid-turn and WSL wedged.
Each time, many agents had been started within a minute: a "Start all" over 18
sessions, an orchestrator fanning out a dozen workers, the ticket pipeline
draining a queue. A memory reading taken right after a launch says nothing
about that launch. An agent CLI reads near zero the second it starts and
~350-800 MB a minute later (its MCP servers, its first turn). So every launch
passed its check on numbers that no longer held.

The gate answers **go / wait / refuse** from three things:

* **Memory** (:mod:`backend.memory_pressure`): the level (available share,
  absolute floor, PSI stall time, with hysteresis).
* **Agents still warming up.** These are agent tmux sessions created in the
  last :data:`WARMUP_SECONDS`, read from tmux's own ``session_created``, so a
  launch from ANY path counts: the ticket pipeline (another process), a
  flight, a schedule, a terminal attach. Launches this process admitted that
  have no tmux session yet are counted too. Each one is assumed to still take
  :func:`warmup_bytes` (``MINDFLOCK_AGENT_WARMUP_MB``, default 700). The gate
  works out what memory would read once they have grown.
* **What a session really costs** (:func:`learned_cost`): the 75th
  percentile of the steady per-session size the memory sampler measures
  (``core.memory_watch``, through :func:`set_sizes_source`'s seam), and of
  the peak sizes for agents still warming. With fewer than
  :data:`MIN_SAMPLES` readings the cost is :func:`warmup_bytes`. The
  admission **budget** (:func:`budget`) is ``room = (available - reserve -
  penalties - growth still to come of the warming agents) / session cost``:
  the reserve is the low line (``max(2 GB, general.memory_low_pct`` of RAM,
  15% by default)), the penalties are swap in use and a rising PSI stall.
  An automatic start waits while the room is 0; a person's start asks.
* **How many agents are live**, against a machine-wide cap
  (``MINDFLOCK_MAX_LIVE_AGENTS``; by default one agent per
  :data:`GB_PER_AGENT` GB of RAM, so 16 on 20 GB; ``0`` turns the cap off).
  The cap applies to automatic starts only.

Two kinds of caller:

* **Automatic** (:func:`decide` ``kind="auto"``): an agent-spawned worker
  (MCP ``spawn_session``), a revive sweep, a queued prompt relaunching or
  waking its agent, the ticket pipeline. On *wait* the launch goes into the
  FIFO queue here (:func:`enqueue` / :func:`gate_auto`) instead of starting.
  The session row says "Waiting for memory" and has a Start-anyway action.
  The queue drains by itself, one launch at a time, at least
  :data:`AUTO_SPACING_SECONDS` apart, as memory allows. Past
  :func:`queue_max` queued launches a new one is refused
  (:class:`QueueFull`).
* **A person** (``kind="person"``): New session, Duplicate, Start, Start all,
  Resume, Wake. A person is never queued or capped. *Confirm* (memory low, or
  would read low once the warming agents grow) and *refuse* (critical) mean
  the route answers 409 ``{"memory_confirm": true, ...}``. The app then shows
  its own dialog (Start anyway / Put idle agents to sleep first), and its
  Start anyway resends the request with ``start_anyway: true``.

Unknown memory (non-Linux, ``MINDFLOCK_MEMORY_GUARD=0``) never holds anything
back on memory grounds. The live cap still applies where tmux can be read.

Stdlib only at import: the ticket pipeline (a separate process) imports it
for :func:`decide`. That process has no server queue; it waits in its own
loop (``ticket_ingestion.session_runner``).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from backend import memory_pressure

__all__ = [
    "WAITING_FOR_MEMORY",
    "WARMUP_SECONDS",
    "AUTO_SPACING_SECONDS",
    "QueueFull",
    "assess",
    "decide",
    "note_start",
    "enqueue",
    "gate_auto",
    "admit_auto",
    "room",
    "register_kind",
    "restore",
    "waiting",
    "items",
    "cancel",
    "start_now",
    "drain_once",
    "status",
    "person_refusal",
    "budget",
    "learned_cost",
    "capacity",
    "set_sizes_source",
    "session_sizes",
]

#: The ``error`` a relaunch path answers when its launch was queued here
#: instead of started (not a failure: the queue starts it later).
WAITING_FOR_MEMORY = "waiting for memory"

_GiB = 1024**3
_MiB = 1024**2

#: How long a just-started agent counts as still warming up (growing towards
#: its working size).
WARMUP_SECONDS = 90.0
#: What one warming agent is assumed to still take (``MINDFLOCK_AGENT_WARMUP_MB``).
DEFAULT_WARMUP_MB = 700
#: The default live-agent cap is ``total RAM / GB_PER_AGENT`` (``MINDFLOCK_MAX_LIVE_AGENTS``).
GB_PER_AGENT = 1.2
#: Minimum gap between two automatic launches the gate lets through
#: (``MINDFLOCK_AUTO_LAUNCH_SPACING`` seconds).
AUTO_SPACING_SECONDS = 20.0
#: Queued launches past which a new automatic one is refused (``MINDFLOCK_LAUNCH_QUEUE_MAX``).
DEFAULT_QUEUE_MAX = 32
#: How often the drainer re-reads while something waits.
DRAIN_POLL_SECONDS = 5.0
#: The tmux probe is reused this long. The UI poll, the drainer and every
#: route may ask freely.
_PROBE_SECONDS = 2.0

_TMUX_PREFIX = "mindflock_"
#: A session's shell tab (``agent_sessions._shell_name``), not an agent.
_SHELL_SUFFIX = "_sh"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


#: How long a settings read is reused.
_SETTINGS_SECONDS = 30.0
_SETTINGS_CACHE: Dict[str, object] = {"at": -1e18, "val": {}}


def _setting(name: str) -> Optional[int]:
    """``general.<name>`` (``max_live_agents`` / ``agent_warmup_mb``), None
    when unset. Re-read at most every :data:`_SETTINGS_SECONDS`. Never
    raises: a LOCAL import, so a process without the settings module (or an
    unreadable store) reads None."""
    now = time.monotonic()
    if now - float(_SETTINGS_CACHE["at"]) >= _SETTINGS_SECONDS:  # type: ignore[arg-type]
        val: Dict[str, Optional[int]] = {}
        try:
            from backend.config.settings import load_settings

            g = load_settings().general
            for k in ("max_live_agents", "agent_warmup_mb"):
                v = getattr(g, k, None)
                val[k] = (
                    int(v) if isinstance(v, int) and not isinstance(v, bool) else None
                )
        except Exception:  # noqa: BLE001 — advisory: the defaults stand
            val = {}
        _SETTINGS_CACHE.update({"at": now, "val": val})
    return (_SETTINGS_CACHE["val"] or {}).get(name)  # type: ignore[union-attr]


def warmup_bytes() -> int:
    """What one warming agent is still expected to take, in bytes:
    ``MINDFLOCK_AGENT_WARMUP_MB``, else ``general.agent_warmup_mb``, else 700."""
    mb = _setting("agent_warmup_mb")
    default = float(mb) if mb is not None else float(DEFAULT_WARMUP_MB)
    return int(_env_float("MINDFLOCK_AGENT_WARMUP_MB", default) * _MiB)


def auto_spacing() -> float:
    return _env_float("MINDFLOCK_AUTO_LAUNCH_SPACING", AUTO_SPACING_SECONDS)


def queue_max() -> int:
    return int(_env_float("MINDFLOCK_LAUNCH_QUEUE_MAX", DEFAULT_QUEUE_MAX))


def live_cap(total_bytes: float) -> int:
    """The most live agents automatic starts may bring the machine to; 0 =
    no cap. ``MINDFLOCK_MAX_LIVE_AGENTS`` wins, then
    ``general.max_live_agents``; otherwise one per :data:`GB_PER_AGENT` GB of
    RAM (at least 2), and none when RAM is unknown."""
    raw = os.environ.get("MINDFLOCK_MAX_LIVE_AGENTS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    setting = _setting("max_live_agents")
    if setting is not None:
        return max(0, setting)
    if not total_bytes:
        return 0
    return max(2, int(float(total_bytes) / _GiB / GB_PER_AGENT))


# --------------------------------------------------------------------------- #
# What one session costs (learned), and how many more fit (the budget)
# --------------------------------------------------------------------------- #
#: Readings needed before the measured cost replaces :func:`warmup_bytes`.
MIN_SAMPLES = 3
#: A reading under this (MB) is a session with nothing running in it (asleep,
#: just closed), not a sample of what one costs.
SAMPLE_FLOOR_MB = 50
#: The learned cost never reads under this (MB): a dozen idle shells must not
#: make room look unlimited.
COST_FLOOR_MB = 200
#: A warming agent's learned peak is capped at this multiple of the steady
#: cost: the sampler's ``peak_mb`` is a session's whole-life peak, and one
#: that once ran an 8-worker test suite says nothing about a CLI starting.
PEAK_CAP_RATIO = 2.5
#: The reserve never reads under this, whatever ``general.memory_low_pct``.
RESERVE_FLOOR_BYTES = 2 * _GiB
#: Swap in use counts against the room (pages the running agents will fault
#: back in; on WSL a disk-backed swap that drags the whole Windows host
#: down), up to this share of RAM.
SWAP_PENALTY_SHARE = 0.10
#: A RISING PSI stall (``some`` avg10 above avg60, and at least
#: :data:`PSI_PENALTY_FROM` %) counts ``avg10 / PSI_PENALTY_DIVISOR`` of RAM
#: against the room, up to :data:`PSI_PENALTY_MAX` (20% stalled → 10% of RAM).
PSI_PENALTY_FROM = 5.0
PSI_PENALTY_DIVISOR = 200.0
PSI_PENALTY_MAX = 0.15

#: ``() -> ({title: mb}, {title: peak_mb})``: per-session memory readings.
SizesSource = Callable[[], Tuple[Dict[str, int], Dict[str, int]]]


def _int_map(raw) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if isinstance(v, bool):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out[str(k)] = n
    return out


def _default_sizes() -> Tuple[Dict[str, int], Dict[str, int]]:
    """The memory sampler's readings: ``core.memory_watch.session_mb()`` /
    ``session_peak_mb()`` when that module has them, else the ``memory``
    field (``{mb, peak_mb, ...}``) of this server's published rows, else
    nothing. Only in the server process (the ticket pipeline has no
    sampler: it keeps the default cost). Never raises."""
    import sys

    if "backend.web.server" not in sys.modules:
        return {}, {}
    try:
        from backend.web.core import memory_watch as _mw  # type: ignore[attr-defined]

        fn_mb = getattr(_mw, "session_mb", None)
        fn_peak = getattr(_mw, "session_peak_mb", None)
        if callable(fn_mb):
            mb = _int_map(fn_mb())
            peak = _int_map(fn_peak()) if callable(fn_peak) else {}
            if mb or peak:
                return mb, peak
    except Exception:  # noqa: BLE001 — no sampler (yet): the rows, or nothing
        pass
    try:
        from backend.web.core import events as _events

        rows = _events.sessions_snapshot()
    except Exception:  # noqa: BLE001
        return {}, {}
    mb: Dict[str, int] = {}
    peak: Dict[str, int] = {}
    for r in rows or ():
        if not isinstance(r, dict) or r.get("device") or r.get("sleeping"):
            continue
        m = r.get("memory")
        title = r.get("title")
        if not isinstance(m, dict) or not isinstance(title, str):
            continue
        got = _int_map({"mb": m.get("mb"), "peak_mb": m.get("peak_mb")})
        if "mb" in got:
            mb[title] = got["mb"]
        if "peak_mb" in got:
            peak[title] = got["peak_mb"]
    return mb, peak


_SIZES: SizesSource = _default_sizes
_SIZES_CACHE: Dict[str, object] = {"at": -1e18, "val": ({}, {})}


def set_sizes_source(fn: Optional[SizesSource]) -> None:
    """Replace where per-session readings come from (None restores the
    sampler). The seam the memory sampler plugs into; tests feed it."""
    global _SIZES
    _SIZES = fn or _default_sizes
    _SIZES_CACHE.update({"at": -1e18, "val": ({}, {})})


def _sizes() -> Tuple[Dict[str, int], Dict[str, int]]:
    now = time.monotonic()
    if now - float(_SIZES_CACHE["at"]) < _PROBE_SECONDS:  # type: ignore[arg-type]
        return _SIZES_CACHE["val"]  # type: ignore[return-value]
    try:
        mb, peak = _SIZES()
        val = (_int_map(mb), _int_map(peak))
    except Exception:  # noqa: BLE001 — a reading never fails a launch
        val = ({}, {})
    _SIZES_CACHE.update({"at": now, "val": val})
    return val


def session_sizes() -> Tuple[Dict[str, int], Dict[str, int]]:
    """``({title: mb}, {title: peak_mb})`` from the sizes source (cached
    :data:`_PROBE_SECONDS`). Never raises."""
    return _sizes()


def _p75(values: List[int]) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    pos = 0.75 * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def learned_cost(
    mb: Dict[str, int],
    peak: Dict[str, int],
    warming: Iterable[str] = (),
    default_mb: int = DEFAULT_WARMUP_MB,
) -> dict:
    """What one session costs, from the sampler's readings (pure)::

        {"session_mb": steady cost, "peak_mb": what a warming one grows to,
         "samples": n, "source": "measured" | "default"}

    Steady = the 75th percentile of the sessions NOT still warming (titles
    in ``warming``) that read at least :data:`SAMPLE_FLOOR_MB`; peak = the
    75th percentile of every peak reading, never under the steady cost nor
    over :data:`PEAK_CAP_RATIO` times it. Fewer than :data:`MIN_SAMPLES`
    readings: ``default_mb`` for both (``source: "default"``)."""
    warm = set(warming or ())
    steady = [v for t, v in mb.items() if t not in warm and v >= SAMPLE_FLOOR_MB]
    peaks = [v for v in peak.values() if v >= SAMPLE_FLOOR_MB]
    default_mb = max(1, int(default_mb))
    if len(steady) >= MIN_SAMPLES:
        session = max(float(COST_FLOOR_MB), _p75(steady))
        source = "measured"
    else:
        session = float(default_mb)
        source = "default"
    if len(peaks) >= MIN_SAMPLES:
        top = _p75(peaks)
    else:
        top = float(default_mb)
    top = min(max(top, session), session * PEAK_CAP_RATIO)
    return {
        "session_mb": int(round(session)),
        "peak_mb": int(round(top)),
        "samples": len(steady) if source == "measured" else 0,
        "source": source,
    }


def reserve_bytes(total: float) -> int:
    """What the machine always keeps: the low line (``max(share of RAM,
    absolute floor)`` from :func:`memory_pressure.thresholds`), never under
    :data:`RESERVE_FLOOR_BYTES`."""
    try:
        th = memory_pressure.thresholds()["low"]
        line = max(float(th[0]) * float(total), float(th[1]))
    except Exception:  # noqa: BLE001
        line = 0.0
    return int(max(float(RESERVE_FLOOR_BYTES), line))


def _penalties(snap: dict) -> Tuple[int, int]:
    """``(swap penalty, PSI penalty)`` in bytes for a memory snapshot."""
    total = float(snap.get("total") or 0)
    swap_used = max(
        0.0, float(snap.get("swap_total") or 0) - float(snap.get("swap_free") or 0)
    )
    swap_pen = min(swap_used, SWAP_PENALTY_SHARE * total)
    psi = snap.get("psi") or {}
    try:
        a10 = float(psi.get("some_avg10") or 0.0)
        a60 = float(psi.get("some_avg60") or 0.0)
    except (TypeError, ValueError, AttributeError):
        a10 = a60 = 0.0
    psi_pen = 0.0
    if a10 >= PSI_PENALTY_FROM and a10 > a60:
        psi_pen = total * min(PSI_PENALTY_MAX, a10 / PSI_PENALTY_DIVISOR)
    return int(swap_pen), int(psi_pen)


def _mb(n) -> int:
    return int(round(float(n or 0) / _MiB))


def budget(
    snap: dict,
    cost: dict,
    warming_mb: Iterable[Optional[int]] = (),
    *,
    reserve: Optional[int] = None,
    live: Optional[int] = None,
    cap: int = 0,
) -> dict:
    """How many more sessions fit now (pure; ``snap`` a
    :func:`memory_pressure.snapshot`, ``cost`` a :func:`learned_cost`,
    ``warming_mb`` the current size of each agent still warming, None when
    unmeasured)::

        {"room": n | None, "session_mb", "peak_mb", "available_mb",
         "reserve_mb", "penalty_mb", "to_come_mb", "free_mb", "short_mb",
         "level", "source", "samples", "live", "cap", "why"}

    ``free = available - reserve - penalties - to_come``, where each warming
    agent still has ``peak - its size now`` to come (all of ``peak`` when
    unmeasured); ``room = floor(free / session)``, 0 while the level is low
    or critical, never past the live cap. ``room`` None: memory unknown and
    no cap. ``short_mb``: what one more session is missing (0 when it fits).
    """
    lvl = str(snap.get("level") or "unknown")
    total = float(snap.get("total") or 0)
    session_b = max(1, int(cost.get("session_mb") or DEFAULT_WARMUP_MB)) * _MiB
    peak_b = max(session_b, int(cost.get("peak_mb") or 0) * _MiB)
    out = {
        "room": None,
        "session_mb": _mb(session_b),
        "peak_mb": _mb(peak_b),
        "available_mb": None,
        "reserve_mb": None,
        "penalty_mb": 0,
        "to_come_mb": 0,
        "free_mb": None,
        "short_mb": 0,
        "level": lvl,
        "source": cost.get("source") or "default",
        "samples": int(cost.get("samples") or 0),
        "live": live,
        "cap": int(cap or 0),
        "why": "",
    }
    left_by_cap: Optional[int] = None
    if cap and live is not None:
        left_by_cap = max(0, int(cap) - int(live))
    if total <= 0 or lvl not in ("ok", "low", "critical"):
        out["room"] = left_by_cap
        out["why"] = (
            "memory is not measured here; %d agents are running and automatic "
            "starts stop at %d" % (live or 0, cap)
            if left_by_cap is not None
            else "memory is not measured on this machine: nothing is held for it"
        )
        return out
    avail = float(snap.get("available") or 0)
    res = float(reserve_bytes(total) if reserve is None else reserve)
    swap_pen, psi_pen = _penalties(snap)
    to_come = 0.0
    for cur in warming_mb or ():
        if cur is None:
            to_come += peak_b
        else:
            to_come += max(0.0, peak_b - float(cur) * _MiB)
    free = avail - res - swap_pen - psi_pen - to_come
    room = max(0, int(free // session_b)) if free > 0 else 0
    if lvl in ("low", "critical"):
        room = 0
    out.update(
        available_mb=_mb(avail),
        reserve_mb=_mb(res),
        penalty_mb=_mb(swap_pen + psi_pen),
        to_come_mb=_mb(to_come),
        free_mb=_mb(free),
        short_mb=max(0, _mb(session_b - max(0.0, free))),
    )
    each = "each ~%s (%s)" % (
        _gb(session_b),
        (
            "measured over %d sessions" % out["samples"]
            if out["source"] == "measured"
            else "no measurements yet"
        ),
    )
    parts = ["%s available" % _gb(avail), "%s reserve" % _gb(res)]
    if swap_pen:
        parts.append("%s for swap in use" % _gb(swap_pen))
    if psi_pen:
        parts.append("%s for a rising memory stall" % _gb(psi_pen))
    if to_come:
        parts.append("%s still to come for agents starting" % _gb(to_come))
    sums = " − ".join(parts)
    if lvl in ("low", "critical"):
        out["why"] = memory_pressure.describe(snap) or "memory is %s" % lvl
    elif room == 0:
        out["why"] = (
            "one more session needs ~%s; only %s is free above the reserve (%s)"
            % (_gb(session_b), _gb(max(0.0, free)), sums)
        )
    else:
        out["why"] = "room for %d more session%s: %s, %s" % (
            room,
            "" if room == 1 else "s",
            sums,
            each,
        )
    if left_by_cap is not None and left_by_cap < room:
        room = left_by_cap
        out["why"] = "%d agents are running; automatic starts stop at %d" % (
            live,
            cap,
        )
    out["room"] = room
    return out


def capacity(now: Optional[float] = None) -> dict:
    """The budget for an orchestrator or the UI (``GET /api/memory/capacity``,
    MCP ``whoami`` / ``spawn_session`` / ``start_team_run``): :func:`budget`'s
    fields plus ``warming`` and ``hint``, one line to plan a worker count
    from. Never raises."""
    try:
        a = assess(now=now)
        b = dict(a["budget"])
        b["warming"] = a["warming"]
        b["hint"] = capacity_hint(b)
        return b
    except Exception as err:  # noqa: BLE001
        return {
            "room": None,
            "session_mb": DEFAULT_WARMUP_MB,
            "why": str(err),
            "hint": "",
        }


def capacity_hint(b: dict) -> str:
    """``"room for 2 more sessions now; more will queue"`` and its kin."""
    room = b.get("room")
    if room is None:
        return "memory is not measured here: spawns are not held back for it"
    if room > 0:
        return "room for %d more session%s now (~%d MB each); more will queue" % (
            room,
            "" if room == 1 else "s",
            int(b.get("session_mb") or 0),
        )
    if b.get("level") in ("low", "critical"):
        return "no room now (%s); a spawn will queue until memory allows" % (
            b.get("why") or "memory is low"
        )
    if b.get("short_mb"):
        return (
            "no room for another session now: one needs ~%d MB, %d MB more than "
            "is free above the reserve; a spawn will queue until memory allows"
            % (int(b.get("session_mb") or 0), int(b.get("short_mb") or 0))
        )
    return "no room for another session now (%s); a spawn will queue" % (
        b.get("why") or "the live-agent cap"
    )


# --------------------------------------------------------------------------- #
# Which agents are live, and which are still warming up
# --------------------------------------------------------------------------- #
def _tmux_agents() -> Optional[List[Tuple[str, float]]]:
    """``[(tmux session name, created epoch)]`` for every MindFlock agent tmux
    session, ``[]`` with no tmux server, None when tmux can't be asked."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name} #{session_created}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return []  # "no server running": no agents at all
    agents: List[Tuple[str, float]] = []
    for line in out.stdout.splitlines():
        name, _, created = line.strip().rpartition(" ")
        if not name.startswith(_TMUX_PREFIX) or name.endswith(_SHELL_SUFFIX):
            continue
        try:
            agents.append((name, float(created)))
        except ValueError:
            agents.append((name, 0.0))
    return agents


#: The probe :func:`assess` uses. Tests replace it (``set_probe``).
_PROBE: Callable[[], Optional[List[Tuple[str, float]]]] = _tmux_agents
_PROBE_CACHE: Dict[str, object] = {"at": -1e18, "val": None}
_LOCK = threading.RLock()
#: Launches admitted by this process: ``{key: epoch}``. The key is the
#: session's tmux name when known (deduped against the probe), else its title.
_ADMITTED: Dict[str, float] = {}
#: The last automatic launch the gate let through (epoch), for the spacing.
_LAST_AUTO = {"at": 0.0}


def set_probe(fn: Optional[Callable[[], Optional[List[Tuple[str, float]]]]]) -> None:
    """Replace the agent probe (None restores tmux). Test hook."""
    global _PROBE
    _PROBE = fn or _tmux_agents
    _PROBE_CACHE.update({"at": -1e18, "val": None})


def _agents(fresh: bool = False) -> Optional[List[Tuple[str, float]]]:
    now = time.monotonic()
    if not fresh and now - float(_PROBE_CACHE["at"]) < _PROBE_SECONDS:  # type: ignore[arg-type]
        return _PROBE_CACHE["val"]  # type: ignore[return-value]
    try:
        val = _PROBE()
    except Exception:  # noqa: BLE001 — a probe never fails a launch
        val = None
    _PROBE_CACHE.update({"at": now, "val": val})
    return val


def _tmux_name(title: str) -> str:
    try:
        from backend.session.tmux import tmux as _tmux

        return _tmux.to_mindflock_tmux_name(title)
    except Exception:  # noqa: BLE001 — the pipeline may lack the engine path
        return _TMUX_PREFIX + "".join(title.split()).replace(".", "_")


def note_start(title: str, *, auto: bool = False, now: Optional[float] = None) -> None:
    """Record that ``title``'s agent is being launched now, so it counts as
    warming before its tmux session exists. ``auto``: the start was
    automatic, so the next automatic one is spaced from it."""
    now = time.time() if now is None else now
    with _LOCK:
        _ADMITTED[_tmux_name(title)] = now
        for k, t in list(_ADMITTED.items()):
            if now - t >= WARMUP_SECONDS:
                _ADMITTED.pop(k, None)
        if auto:
            _LAST_AUTO["at"] = now
    _PROBE_CACHE.update({"at": -1e18})


def assess(*, fresh: bool = False, now: Optional[float] = None) -> dict:
    """The numbers behind a decision::

        {"memory": <memory_pressure.snapshot()>, "level": str,
         "live": int | None, "warming": int, "cap": int,
         "warmup_bytes": int, "projected_available": bytes | None,
         "projected_level": "ok"|"low"|"critical"|"unknown",
         "cost": <learned_cost()>, "to_come": bytes, "budget": <budget()>}

    ``projected_*``: what memory would read once the warming agents have
    grown — each to the learned peak (``cost["peak_mb"]``) from the size it
    reads now (all of it when unmeasured); ``warmup_bytes`` is that peak in
    bytes. ``live`` is None when tmux can't be read. Never raises."""
    now = time.time() if now is None else now
    snap = memory_pressure.snapshot(fresh=fresh)
    agents = _agents(fresh=fresh)
    with _LOCK:
        admitted = {k: t for k, t in _ADMITTED.items() if now - t < WARMUP_SECONDS}
    names = {n for n, _c in agents or []}
    warming_names = {n for n, c in agents or [] if c and now - c < WARMUP_SECONDS}
    warming_names |= set(admitted)
    live = None if agents is None else len(names | set(admitted))
    total = float(snap.get("total") or 0)
    mb, peak = _sizes()
    by_tmux = {_tmux_name(t): t for t in set(mb) | set(peak)}
    warming_titles = {by_tmux[n] for n in warming_names if n in by_tmux}
    cost = learned_cost(mb, peak, warming_titles, _mb(warmup_bytes()))
    warming_mb: List[Optional[int]] = [
        mb.get(by_tmux[n]) if n in by_tmux else None for n in sorted(warming_names)
    ]
    peak_b = int(cost["peak_mb"]) * _MiB
    to_come = sum(
        peak_b if cur is None else max(0, peak_b - int(cur) * _MiB)
        for cur in warming_mb
    )
    cap = live_cap(total)
    out = {
        "memory": snap,
        "level": str(snap.get("level") or "unknown"),
        "live": live,
        "warming": len(warming_names),
        "cap": cap,
        "warmup_bytes": peak_b,
        "projected_available": None,
        "projected_level": "unknown",
        "cost": cost,
        "to_come": int(to_come),
        "budget": budget(snap, cost, warming_mb, live=live, cap=cap),
    }
    if snap.get("level") in ("ok", "low", "critical") and not warming_names:
        # Nothing warming: the projection IS the reading (its hysteresis and
        # settings included).
        out["projected_available"] = snap.get("available")
        out["projected_level"] = out["level"]
    elif total and snap.get("level") in ("ok", "low", "critical"):
        projected = float(snap.get("available") or 0) - to_come
        out["projected_available"] = int(max(0.0, projected))
        th = memory_pressure.thresholds()
        share = projected / total

        def _past(line) -> bool:
            return (line[0] > 0 and share <= line[0]) or projected <= line[1]

        if _past(th["critical"]):
            out["projected_level"] = "critical"
        elif _past(th["low"]):
            out["projected_level"] = "low"
        else:
            out["projected_level"] = "ok"
    return out


def _gb(n) -> str:
    return "%.1f GB" % (float(n or 0) / _GiB)


def _worst(a: dict) -> str:
    """The worse of the level now and the projected level."""
    rank = {"unknown": 0, "ok": 0, "low": 1, "critical": 2}
    lvl, proj = a["level"], a["projected_level"]
    return lvl if rank.get(lvl, 0) >= rank.get(proj, 0) else proj


def _memory_reason(a: dict) -> str:
    snap = a["memory"]
    lvl = a["level"]
    if lvl in ("low", "critical"):
        return memory_pressure.describe(snap)
    # Ok now, but not once the warming agents have grown.
    return "%d agent%s still starting (~%s to come): only %s would be left" % (
        a["warming"],
        "" if a["warming"] == 1 else "s",
        _gb(a.get("to_come", a["warming"] * a["warmup_bytes"])),
        _gb(a["projected_available"]),
    )


def _budget_full(a: dict) -> bool:
    """Whether the budget has no room for one more session while the level
    itself reads ok (one more would take the machine past its reserve)."""
    b = a.get("budget") or {}
    return b.get("room") == 0 and b.get("level") == "ok" and bool(b.get("short_mb"))


def decide(
    kind: str = "auto", *, now: Optional[float] = None, fresh: bool = False
) -> dict:
    """Whether a launch may start now → ``{"decision", "reason", "why",
    "level", "assess"}``. ``why`` names what holds an automatic start:
    ``"memory"`` (the level now), ``"warming"`` (the level once the agents
    still starting have grown), ``"cap"``, ``"spacing"``; ``""`` on go.

    ``kind="auto"``: ``"go"`` or ``"wait"`` (memory low/critical now or once
    the warming agents grow; the live cap reached; or within
    :func:`auto_spacing` of the previous automatic launch).
    ``kind="person"``: ``"go"``, ``"confirm"`` (low, or no room left in the
    budget) or ``"refuse"`` (critical). Both need the person's Start anyway;
    they differ only in wording. ``level`` is the worse of now and
    projected. Never raises.

    The budget (:func:`budget`): an automatic start also waits (``why``
    ``"memory"``) while the level reads ok but one more session would not
    fit above the reserve — checked after the cap."""
    now = time.time() if now is None else now
    a = assess(fresh=fresh, now=now)
    worst = _worst(a)
    out = {"decision": "go", "reason": "", "why": "", "level": worst, "assess": a}
    if kind == "person":
        if worst == "critical":
            out.update(decision="refuse", reason=_memory_reason(a))
        elif worst == "low":
            out.update(decision="confirm", reason=_memory_reason(a))
        elif _budget_full(a):
            out.update(decision="confirm", reason=a["budget"]["why"])
        return out
    if worst in ("low", "critical"):
        out.update(
            decision="wait",
            reason=_memory_reason(a),
            why="memory" if a["level"] in ("low", "critical") else "warming",
        )
        return out
    cap, live = a["cap"], a["live"]
    if cap and live is not None and live >= cap:
        out.update(
            decision="wait",
            why="cap",
            reason="%d agents are running; automatic starts stop at %d on this "
            "machine" % (live, cap),
        )
        return out
    if _budget_full(a):
        out.update(decision="wait", why="memory", reason=a["budget"]["why"])
        return out
    spacing = auto_spacing()
    with _LOCK:
        last = _LAST_AUTO["at"]
    if spacing and last and now - last < spacing:
        out.update(
            decision="wait",
            why="spacing",
            reason="the last agent started %ds ago; automatic starts are %ds apart"
            % (int(now - last), int(spacing)),
        )
    return out


def room(now: Optional[float] = None) -> Optional[int]:
    """How many more agents automatic starts may begin now (:func:`budget`):
    the room above the reserve at the learned per-session cost, after the
    growth still to come of the agents warming, never past the live cap.
    None = no limit known (memory unknown, no cap). For a caller that starts
    several at once (a team run's pass). Never raises."""
    try:
        return assess(now=now)["budget"]["room"]
    except Exception:  # noqa: BLE001 — advisory
        return None


def person_refusal(verdict: dict) -> Optional[dict]:
    """The 409 body for a person's start that needs their Start anyway, or
    None when ``verdict`` (from :func:`decide` ``kind="person"``) is go.
    ``{"error", "memory_confirm": true, "level", "message", "free",
    "memory", "warming", "live"}``. ``message`` is the dialog's own line."""
    if verdict.get("decision") == "go":
        return None
    a = verdict.get("assess") or {}
    snap = a.get("memory") or {}
    critical = verdict.get("decision") == "refuse"
    free = _gb(snap.get("available"))
    tight = verdict.get("level") == "ok"  # the budget, not the level
    head = "Memory is %s: %s free." % (
        "critically low" if critical else ("tight" if tight else "low"),
        free,
    )
    if a.get("warming"):
        head += " %d agent%s still starting will take ~%s more." % (
            a["warming"],
            "" if a["warming"] == 1 else "s",
            _gb(a.get("to_come", a["warming"] * a.get("warmup_bytes", 0))),
        )
    head += " " + _room_sentence(a.get("budget") or {})
    tail = (
        " Starting another agent will likely freeze the machine."
        if critical
        else " Starting another agent may freeze the machine."
    )
    b = a.get("budget") or {}
    return {
        "capacity": {
            "room": b.get("room"),
            "session_mb": b.get("session_mb"),
            "reserve_mb": b.get("reserve_mb"),
        },
        # What a client without the app's dialog shows (the phone, the CLI).
        "error": "%s%s (send it again with start_anyway: true to start it "
        "regardless)" % (head, tail),
        "memory_confirm": True,
        "level": "critical" if critical else "low",
        "message": head + tail,
        "free": snap.get("available"),
        "total": snap.get("total"),
        "warming": a.get("warming", 0),
        "live": a.get("live"),
        "memory": snap,
    }


def _room_sentence(b: dict) -> str:
    """The person's dialog line about the room: ``"No room for another
    session: one needs ~0.7 GB, 0.3 GB is free above the 3.0 GB this
    machine keeps in reserve."``."""
    room = b.get("room")
    each = float(b.get("session_mb") or 0) / 1024.0
    if room is None or b.get("reserve_mb") is None:
        return ""
    if room > 0:
        return "There is room for %d more session%s (~%.1f GB each)." % (
            room,
            "" if room == 1 else "s",
            each,
        )
    return (
        "No room for another session: one needs ~%.1f GB, %.1f GB is free "
        "above the %.1f GB this machine keeps in reserve."
        % (
            each,
            max(0.0, float(b.get("free_mb") or 0)) / 1024.0,
            float(b.get("reserve_mb") or 0) / 1024.0,
        )
    )


# --------------------------------------------------------------------------- #
# The FIFO queue of automatic launches waiting for memory
# --------------------------------------------------------------------------- #
class QueueFull(Exception):
    """More launches are already waiting than :func:`queue_max` allows."""


#: A queued launch: called with no arguments in the drainer thread. It returns
#: an error string ("" / None = started) or raises.
Launch = Callable[[], Optional[str]]

_IDS = itertools.count(1)
#: FIFO. Each item is ``{"id", "title", "kind", "source", "since", "reason",
#: "launch", "parent"}``. ``launch`` stays server-side.
_QUEUE: List[dict] = []
#: The most recent outcome per title (``{"title", "at", "started", "error"}``),
#: so a caller can tell "started" from "failed" once an item has left the queue.
_DONE: Dict[str, dict] = {}
_DONE_MAX = 64
_DRAINER: Dict[str, object] = {"thread": None}
#: Whether :func:`enqueue` starts the background drainer. Never under pytest
#: (tests call :func:`drain_once`).
AUTO_DRAIN = True
#: Called (no args) whenever the queue changes, so the server can republish
#: rows and the memory reading. Set by the server.
on_change: Optional[Callable[[], None]] = None


def _public(it: dict, position: int) -> dict:
    return {
        "id": it["id"],
        "title": it["title"],
        "kind": it["kind"],
        "source": it.get("source", ""),
        "parent": it.get("parent", ""),
        "since": it["since"],
        "reason": it.get("reason", ""),
        "position": position,
        "creates": bool(it.get("creates")),
    }


def _changed() -> None:
    cb = on_change
    if cb is None:
        return
    try:
        cb()
    except Exception:  # noqa: BLE001 — a UI nudge never breaks the gate
        pass


def _log(fmt: str, *args) -> None:
    try:
        from backend import log as _mlog

        if _mlog.InfoLog is not None:
            _mlog.InfoLog.Printf("launch gate: " + fmt, *args)
    except Exception:  # noqa: BLE001
        pass


def enqueue(
    title: str,
    launch: Launch,
    *,
    kind: str,
    source: str = "",
    reason: str = "",
    parent: str = "",
    creates: bool = False,
    args: Optional[dict] = None,
    since: Optional[float] = None,
) -> dict:
    """Queue ``title``'s launch (FIFO) → its public record (``position`` 1 =
    next). A title already queued keeps its place; the newer ``launch``
    replaces the older, so two relaunch paths don't double-launch.
    ``creates``: the launch creates the session (no row exists yet).
    ``args``: what a restart needs to rebuild the launch (JSON); an item with
    ``args`` is saved to disk and comes back on the next boot through its
    kind's factory (:func:`register_kind`), one without is not.
    Raises :class:`QueueFull` past :func:`queue_max`."""
    with _LOCK:
        for i, it in enumerate(_QUEUE):
            if it["title"] == title:
                it["launch"] = launch
                if reason:
                    it["reason"] = reason
                if args is not None:
                    it["args"] = args
                return _public(it, i + 1)
        cap = queue_max()
        if cap and len(_QUEUE) >= cap:
            raise QueueFull(
                "%d launches are already waiting for memory (the most is %d)"
                % (len(_QUEUE), cap)
            )
        it = {
            "id": "lq%d" % next(_IDS),
            "title": title,
            "kind": kind,
            "source": source,
            "parent": parent,
            "since": time.time() if since is None else since,
            "reason": reason,
            "launch": launch,
            "creates": creates,
            "args": args,
        }
        _QUEUE.append(it)
        pub = _public(it, len(_QUEUE))
    _log("%s waits for memory (%s): %s", title, kind, reason or "queued")
    _save()
    _ensure_drainer()
    _changed()
    return pub


def admit_auto(key: str) -> Tuple[bool, str]:
    """Check and note in one step → ``(go, reason)``. Go only when nothing
    waits ahead (FIFO) and :func:`decide` says go; the start is then noted
    (``key``: the title, or a placeholder) before the lock is let go, so two
    concurrent automatic starts can't both pass on the same reading."""
    # Warm the two cached reads first, so the lock is never held across a
    # tmux call or a /proc read (every row's waiting() takes it too).
    _agents()
    memory_pressure.snapshot()
    with _LOCK:
        if _QUEUE:
            head = _QUEUE[0]
            return False, head.get("reason") or "others are waiting ahead of it"
        verdict = decide("auto")
        if verdict["decision"] != "go":
            return False, verdict["reason"]
        note_start(key, auto=True)
        return True, ""


def gate_auto(
    title: str,
    launch: Launch,
    *,
    kind: str,
    source: str = "",
    parent: str = "",
    creates: bool = False,
    args: Optional[dict] = None,
) -> Optional[dict]:
    """An automatic start's one call: None means go (the start is noted and
    the caller launches inline now), else the queued record. Someone already
    waiting goes first (FIFO): a launch never jumps the queue, and one
    already queued keeps its place."""
    with _LOCK:
        queued = next((it for it in _QUEUE if it["title"] == title), None)
        if queued is None:
            go, reason = admit_auto(title)
            if go:
                return None
        else:
            reason = queued.get("reason") or ""
        return enqueue(
            title,
            launch,
            kind=kind,
            source=source,
            reason=reason,
            parent=parent,
            creates=creates,
            args=args,
        )


def waiting(title: str) -> Optional[dict]:
    """``title``'s queued record, or None."""
    with _LOCK:
        for i, it in enumerate(_QUEUE):
            if it["title"] == title:
                return _public(it, i + 1)
    return None


def items() -> List[dict]:
    with _LOCK:
        return [_public(it, i + 1) for i, it in enumerate(_QUEUE)]


def last_outcome(title: str) -> Optional[dict]:
    with _LOCK:
        rec = _DONE.get(title)
        return dict(rec) if rec else None


def _take(title: str) -> Optional[dict]:
    with _LOCK:
        for i, it in enumerate(_QUEUE):
            if it["title"] == title or it["id"] == title:
                it = _QUEUE.pop(i)
                break
        else:
            return None
    _save()
    return it


def cancel(title: str) -> bool:
    """Drop ``title`` (or an item id) from the queue. True if it was there."""
    it = _take(title)
    if it is None:
        return False
    _log("%s no longer waits (cancelled)", it["title"])
    _record(it["title"], False, "cancelled")
    _changed()
    return True


def _record(title: str, started: bool, error: str) -> None:
    with _LOCK:
        _DONE[title] = {
            "title": title,
            "at": time.time(),
            "started": started,
            "error": error,
        }
        while len(_DONE) > _DONE_MAX:
            _DONE.pop(next(iter(_DONE)))


def _run(it: dict, *, auto: bool) -> Optional[str]:
    note_start(it["title"], auto=auto)
    try:
        err = it["launch"]() or None
    except Exception as exc:  # noqa: BLE001 — one bad launch never stops the queue
        err = str(exc) or exc.__class__.__name__
    _record(it["title"], err is None, err or "")
    if err:
        _log("%s could not start: %s", it["title"], err)
    else:
        _log("%s started (waited %ds)", it["title"], int(time.time() - it["since"]))
    _changed()
    return err


def start_now(title: str) -> Tuple[bool, Optional[str]]:
    """Start anyway: launch ``title`` (or an item id) now, past the gate →
    ``(was_queued, error)``. Blocking (the launch runs in this thread)."""
    it = _take(title)
    if it is None:
        return False, None
    _log("%s: started anyway by a person", it["title"])
    return True, _run(it, auto=False)


def drain_once(now: Optional[float] = None) -> Optional[str]:
    """One drain step: when the gate says go for the head of the queue, launch
    it (in this thread) → its title, else None. One launch per step; the
    spacing keeps the next one at least :func:`auto_spacing` away."""
    with _LOCK:
        if not _QUEUE:
            return None
    verdict = decide("auto", now=now, fresh=True)
    with _LOCK:
        if not _QUEUE:
            return None
        if verdict["decision"] != "go":
            # Keep the shown reason current (memory → cap → spacing).
            changed = _QUEUE[0].get("reason") != verdict["reason"]
            for it in _QUEUE:
                it["reason"] = verdict["reason"]
            head = None
        else:
            head = _QUEUE.pop(0)
            changed = False
    if head is not None:
        _save()
    if head is None:
        if changed:
            _changed()
        return None
    _run(head, auto=True)
    return head["title"]


def _drain_loop() -> None:
    try:
        while True:
            with _LOCK:
                if not _QUEUE:
                    return
            try:
                drain_once()
            except Exception:  # noqa: BLE001 — the loop outlives one bad step
                pass
            time.sleep(DRAIN_POLL_SECONDS)
    finally:
        with _LOCK:
            _DRAINER["thread"] = None
            again = bool(_QUEUE)
        if again:
            _ensure_drainer()


def _ensure_drainer() -> None:
    if not AUTO_DRAIN or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _LOCK:
        th = _DRAINER.get("thread")
        if th is not None and th.is_alive():  # type: ignore[union-attr]
            return
        th = threading.Thread(target=_drain_loop, name="launch-gate", daemon=True)
        _DRAINER["thread"] = th
    th.start()


# --------------------------------------------------------------------------- #
# Persistence: the queue survives a server restart
# --------------------------------------------------------------------------- #
#: Whether the queue is saved to disk. The server turns it on at boot
#: (:func:`restore`); the ticket pipeline (another process, no queue) and the
#: test suite leave it off.
PERSIST = False
#: ``kind -> factory(record) -> launch | None``: how a saved item becomes a
#: launch again after a restart. Each module that queues a kind registers it.
_FACTORIES: Dict[str, Callable[[dict], Optional[Launch]]] = {}
_SAVE_LOCK = threading.Lock()


def register_kind(kind: str, factory: Callable[[dict], Optional[Launch]]) -> None:
    """How a saved ``kind`` comes back: ``factory(record)`` gets the saved
    record (``title``, ``kind``, ``source``, ``parent``, ``since``,
    ``creates``, ``args``) and returns its launch, or None to drop it (the
    session is gone, say)."""
    _FACTORIES[kind] = factory


def queue_path() -> str:
    """``$MINDFLOCK_LAUNCH_QUEUE_FILE``, else ``<config dir>/launch_queue.json``."""
    env = os.environ.get("MINDFLOCK_LAUNCH_QUEUE_FILE", "")
    if env:
        return env
    from backend import config

    return os.path.join(config.GetConfigDir(), "launch_queue.json")


def _saved(it: dict) -> dict:
    return {
        k: it.get(k)
        for k in (
            "title",
            "kind",
            "source",
            "parent",
            "since",
            "reason",
            "creates",
            "args",
        )
    }


def _save() -> None:
    """Rewrite the saved queue (the items with ``args``), atomically. Never
    raises."""
    if not PERSIST:
        return
    with _LOCK:
        doc = {
            "v": 1,
            "items": [_saved(it) for it in _QUEUE if it.get("args") is not None],
        }
    try:
        from backend.web.core import title_store

        with _SAVE_LOCK:
            title_store.persist(queue_path, doc, "the launch queue")
    except Exception:  # noqa: BLE001 — a failed save never breaks a launch
        pass


def _clean(doc: dict) -> Dict:
    items = doc.get("items") if isinstance(doc, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if (
            isinstance(it, dict)
            and isinstance(it.get("title"), str)
            and it["title"]
            and isinstance(it.get("kind"), str)
            and isinstance(it.get("args"), dict)
        ):
            out.append(it)
    return {"items": out}


def restore() -> int:
    """Boot: put every saved item back in the queue, in its old order, through
    its kind's factory (an unknown kind, or a factory answering None, drops
    it), and turn saving on. They are automatic starts, so they wait for the
    gate like any other. → how many came back. Never raises."""
    global PERSIST
    PERSIST = True
    try:
        from backend.web.core import title_store

        doc = title_store.load(queue_path, _clean)
    except Exception:  # noqa: BLE001
        doc = {}
    back = 0
    for rec in doc.get("items") or []:
        factory = _FACTORIES.get(rec["kind"])
        try:
            launch = factory(dict(rec)) if factory else None
        except Exception:  # noqa: BLE001 — one bad record never stops the boot
            launch = None
        if launch is None:
            _log(
                "%s: dropped from the launch queue at boot (%s)",
                rec["title"],
                rec["kind"],
            )
            continue
        try:
            enqueue(
                rec["title"],
                launch,
                kind=rec["kind"],
                source=str(rec.get("source") or ""),
                reason=str(rec.get("reason") or "waiting since before the restart"),
                parent=str(rec.get("parent") or ""),
                creates=bool(rec.get("creates")),
                args=rec["args"],
                since=float(rec.get("since") or time.time()),
            )
            back += 1
        except QueueFull:
            break
    _save()
    return back


def rename(old: str, new: str) -> None:
    """A session was renamed ``old`` → ``new`` (core.rename_stores): its
    queued launch keeps its place under the new title, a worker waiting to be
    created keeps ``new`` as its parent, and the in-memory outcome and warming
    entries move. A renamed item's launch is rebuilt through its kind's
    factory (:func:`register_kind`), since a launch closes over the title it
    was queued with and would look up a session that no longer has that name;
    one with no factory, or whose factory declines, keeps the launch it had.
    A create's own title is left alone: no session has it yet to rename.
    Then the saved queue is rewritten."""
    if not old or not new or old == new:
        return
    rebuild: List[dict] = []
    changed = False
    with _LOCK:
        for it in _QUEUE:
            if it.get("parent") == old:
                it["parent"] = new
                changed = True
            payload = (it.get("args") or {}).get("payload")
            if isinstance(payload, dict) and payload.get("parent") == old:
                # In place: a queued create's launch reads this same dict.
                payload["parent"] = new
                changed = True
            if it["title"] == old and not it.get("creates"):
                it["title"] = new
                rebuild.append(it)
                changed = True
        if old in _DONE:
            rec = _DONE.pop(old)
            _DONE[new] = dict(rec, title=new)
        old_n = _tmux_name(old)
        if old_n in _ADMITTED:
            _ADMITTED[_tmux_name(new)] = _ADMITTED.pop(old_n)
    for it in rebuild:
        factory = _FACTORIES.get(it["kind"])
        try:
            launch = factory(_saved(it)) if factory else None
        except Exception:  # noqa: BLE001 — keep the old launch, and its place
            launch = None
        if launch is not None:
            with _LOCK:
                it["launch"] = launch
    if changed:
        _save()
        _changed()


def status() -> dict:
    """For the UI (``GET /api/system/memory`` → ``gate``): ``{"holding",
    "queued": [...], "reason", "live", "cap", "warming",
    "projected_available", "projected_level", "room", "session_mb"}``.
    ``holding`` is true while anything waits. Never raises."""
    a = assess()
    q = items()
    b = a.get("budget") or {}
    return {
        "room": b.get("room"),
        "session_mb": b.get("session_mb"),
        "holding": bool(q),
        "queued": q,
        "reason": q[0]["reason"] if q else "",
        "live": a["live"],
        "cap": a["cap"],
        "warming": a["warming"],
        "projected_available": a["projected_available"],
        "projected_level": a["projected_level"],
    }


def _reset_for_tests() -> None:
    global PERSIST
    PERSIST = False
    _SETTINGS_CACHE.update({"at": -1e18, "val": {}})
    with _LOCK:
        _QUEUE.clear()
        _DONE.clear()
        _ADMITTED.clear()
        _LAST_AUTO["at"] = 0.0
    _PROBE_CACHE.update({"at": -1e18, "val": None})
    set_sizes_source(lambda: ({}, {}))
