"""The memory shield: what MindFlock does when the machine is about to run out.

Why it exists: on 2026-11-05 the owner's machine (WSL2, 19.5 GB of RAM on a
32 GB Windows host) slowed until the PC had to be rebooted. About ten agent
sessions had started since noon (each ~0.5 GB of CLI plus ~0.5 GB of MCP
servers), an external app held ~5 GB, and Voice Lab's F5 voice
(``voicelab-backend.service``, socket-activated on port 8770) peaked at 4.1
GB plus 1 GB of swap. Its engine timed out at 14:23 because the box was
starved; voice use woke it again at 14:43; the reboot was at 14:52. Swap
inside WSL drags the whole Windows host down with it.

The launch gate (:mod:`backend.web.core.launch_gate`) keeps new agents out
when there is no room. This module handles what is already running:

* **Shed idle agents** (:func:`tick`, from the server's memory watch every
  10 s). When the level stays ``critical`` for :data:`SHED_AFTER_SECONDS`, or
  PSI ``full`` avg10 reaches :data:`FULL_AVG10_SHED` (every runnable task on
  the machine stalled on memory for a quarter of the last 10 s: a thrash
  already under way, where waiting out the minute would cost 15 s of total
  stall), idle agent sessions are put to sleep through
  :mod:`backend.web.core.sleep` (the same machinery as the memory dialog's
  "Put idle agents to sleep first"; Wake brings them back, resuming their
  conversation). The candidates are what :func:`sleep.veto` allows with a
  short idle window (:data:`SHED_IDLE_SECONDS`): never a session that is
  working, asking, on a dialog, open in a terminal, driven by a person,
  typed into or woken in the last two minutes, holding a background job, or
  waited on by its working workers. Largest and longest idle first
  (:func:`choose_shed`: measured size times idle time, capped at an hour),
  and only as many as it takes, at the learned per-session cost
  (:func:`launch_gate.learned_cost`) where a session is unmeasured, to bring
  available memory back over the ``low`` line. ONE notification per shed,
  and each decision is logged. ``general.agent_memory.shed_idle`` (default
  on) turns it off; :data:`SHED_COOLDOWN_SECONDS` separates two sheds so
  the next one acts on readings taken after the memory came back.

* **Voice Lab yields** (:func:`voice_lab_yield`, asked by
  :mod:`backend.web.core.speech` before any request to a local voice host).
  While the level reads ``low`` or ``critical``, MindFlock never wakes or
  calls a voice host on this machine or network (Voice Lab, or any other
  local model server): lines are said by the local fallback (piper, else the
  OS voice), and a client with neither uses its own. The log says why once
  per episode. When the level turns ``critical`` and Voice Lab's backend is
  running, it is stopped (:func:`stop_voice_lab`: ``systemctl --user stop
  voicelab-backend.service`` where that unit exists). It is
  socket-activated, so the next voice request would start it again, and
  that request only reaches it once memory allows. Everything works without
  Voice Lab installed: no unit, nothing to stop.

Stdlib only at import (plus :mod:`backend.memory_pressure`); everything
else is imported lazily inside the calls.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from backend import memory_pressure

__all__ = [
    "SHED_AFTER_SECONDS",
    "FULL_AVG10_SHED",
    "shed_enabled",
    "shed_due",
    "shed_need_mb",
    "choose_shed",
    "shed_notice_text",
    "tick",
    "voice_lab_blocked",
    "voice_lab_yield",
    "stop_voice_lab",
    "status",
]

_MiB = 1024**2

#: How long ``critical`` must hold before idle agents are shed.
SHED_AFTER_SECONDS = 60.0
#: PSI ``full`` avg10 (%) at or past which they are shed at once.
FULL_AVG10_SHED = 25.0
#: Between two sheds: the freed memory shows in MemAvailable within seconds,
#: PSI avg10 decays over ~20 s. The next shed acts on readings taken after.
SHED_COOLDOWN_SECONDS = 90.0
#: The shed's idle window for :func:`sleep.veto`: the turn ended this long
#: ago, nobody typed or woke it since.
SHED_IDLE_SECONDS = 120.0
#: At most this many sessions put to sleep per shed (each one is a kill plus
#: a few seconds of reaping).
MAX_SHED_PER_PASS = 6
#: Idle time counts up to this in the shed order (an agent idle all day is
#: not 24 times more sheddable than one idle an hour).
IDLE_SCORE_CAP_SECONDS = 3600.0
#: Rows whose ``activity`` is one of these are never shed (also vetoed).
_BUSY = frozenset({"working", "clarify", "limit", "asking", "dialog"})

#: The Voice Lab backend unit (``MINDFLOCK_VOICELAB_UNIT``; ``0``/``off`` = never stop it).
VOICELAB_UNIT = "voicelab-backend.service"

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "critical_since": None,
    "last_shed_at": 0.0,
    "last_shed": [],
    "last_decision": "",
    "voice_yield": "",
    "voicelab_stopped_at": 0.0,
    "voicelab_last": "",
}


def _info(fmt: str, *args) -> None:
    try:
        from backend import log as _log

        logger = _log.InfoLog or _log.ErrorLog
        if logger is not None:
            logger.Printf("memory shield: " + fmt, *args)
    except Exception:  # noqa: BLE001 — a log line never breaks the shield
        pass


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def shed_enabled() -> bool:
    """``general.agent_memory.shed_idle`` (default on). The
    ``MINDFLOCK_SHED_IDLE=0`` env var turns it off. Never raises."""
    if (os.environ.get("MINDFLOCK_SHED_IDLE") or "").strip().lower() in (
        "0",
        "off",
        "false",
        "no",
    ):
        return False
    try:
        from backend.providers import agent_limits

        return bool(agent_limits.config().get("shed_idle", True))
    except Exception:  # noqa: BLE001 — unreadable settings: the default
        return True


# --------------------------------------------------------------------------- #
# shedding: when, how much, which (pure)
# --------------------------------------------------------------------------- #
def shed_due(state: dict, snap: dict, now: float) -> Tuple[bool, str]:
    """Whether to shed now → ``(due, why)``; updates ``state``'s
    ``critical_since`` (pure apart from that). Due when the level has read
    ``critical`` for :data:`SHED_AFTER_SECONDS`, or PSI ``full`` avg10 is at
    :data:`FULL_AVG10_SHED` or past it; never within
    :data:`SHED_COOLDOWN_SECONDS` of the last shed."""
    lvl = snap.get("level")
    psi = snap.get("psi") or {}
    try:
        full10 = float(psi.get("full_avg10") or 0.0)
    except (TypeError, ValueError, AttributeError):
        full10 = 0.0
    if lvl == "critical":
        if state.get("critical_since") is None:
            state["critical_since"] = now
    else:
        state["critical_since"] = None
    if now - float(state.get("last_shed_at") or 0.0) < SHED_COOLDOWN_SECONDS:
        return False, ""
    if full10 >= FULL_AVG10_SHED and lvl in ("ok", "low", "critical"):
        return True, "every task stalled on memory %d%% of the last 10 s" % int(
            round(full10)
        )
    since = state.get("critical_since")
    if since is not None and now - float(since) >= SHED_AFTER_SECONDS:  # type: ignore[arg-type]
        return True, "memory has been critical for %ds" % int(now - float(since))  # type: ignore[arg-type]
    return False, ""


def shed_need_mb(snap: dict, low_line_bytes: Optional[float] = None) -> int:
    """MB to free to bring available memory back over the ``low`` line
    (``max(low share of RAM, absolute floor)``), 0 when already over."""
    total = float(snap.get("total") or 0)
    avail = float(snap.get("available") or 0)
    if low_line_bytes is None:
        try:
            th = memory_pressure.thresholds()["low"]
            low_line_bytes = max(float(th[0]) * total, float(th[1]))
        except Exception:  # noqa: BLE001
            low_line_bytes = 0.0
    return max(0, int(round((float(low_line_bytes) - avail) / _MiB)))


def _never(row: dict) -> str:
    """The row-level never-rule a candidate breaks ("" = none). The live
    probes are :func:`sleep.veto`'s; these hold even on a stale row."""
    act = str(row.get("activity") or "")
    if act != "idle":
        return "working" if act == "working" else "not idle (%s)" % (act or "?")
    if row.get("asking") or row.get("dialog"):
        return "asking"
    if row.get("background"):
        return "background job"
    if row.get("driver"):
        return "driven by a person"
    if row.get("viewers"):
        return "being viewed"
    if row.get("sleeping"):
        return "already asleep"
    return ""


def choose_shed(
    candidates: Iterable[dict],
    need_mb: int,
    cost_mb: int,
    *,
    limit: int = MAX_SHED_PER_PASS,
) -> List[dict]:
    """Which sessions to put to sleep (pure). ``candidates``: ``{"title",
    "row", "idle_s", "mb"}`` (``mb`` None = unmeasured, counted at
    ``cost_mb``). Rows breaking a never-rule (:func:`_never`) are dropped;
    the rest are ordered largest-and-longest-idle first (``mb ×
    min(idle_s, 1 h)``, then the larger), and taken until what they free
    reaches ``need_mb``, at least one when ``need_mb`` is positive, at most
    ``limit``. → ``[{"title", "mb", "idle_s", "measured"}]``."""
    pool = []
    for c in candidates or ():
        row = c.get("row") or {}
        if _never(row):
            continue
        mb = c.get("mb")
        measured = isinstance(mb, int) and mb > 0
        size = int(mb) if measured else max(1, int(cost_mb))
        idle = max(0.0, float(c.get("idle_s") or 0.0))
        score = size * min(idle, IDLE_SCORE_CAP_SECONDS)
        pool.append((score, size, idle, str(c.get("title") or ""), measured))
    pool.sort(key=lambda p: (-p[0], -p[1], -p[2], p[3]))
    out: List[dict] = []
    freed = 0
    for _score, size, idle, title, measured in pool:
        if len(out) >= max(0, int(limit)) or (out and freed >= need_mb):
            break
        if need_mb <= 0:
            break
        out.append({"title": title, "mb": size, "idle_s": idle, "measured": measured})
        freed += size
    return out


def shed_notice_text(titles: List[str]) -> str:
    """``Memory critical — put 3 idle sessions to sleep (Wake brings them
    back): a, b, c``."""
    names = ", ".join(titles[:6]) + (
        " and %d more" % (len(titles) - 6) if len(titles) > 6 else ""
    )
    return "Memory critical — put %s to sleep (Wake brings %s back): %s" % (
        "1 idle session" if len(titles) == 1 else "%d idle sessions" % len(titles),
        "it" if len(titles) == 1 else "them",
        names,
    )


# --------------------------------------------------------------------------- #
# shedding: the live pass
# --------------------------------------------------------------------------- #
def _candidates(srv, now: float) -> Tuple[List[dict], Dict[str, int]]:
    """Every session :func:`sleep.veto` allows with the shed's idle window,
    as :func:`choose_shed` candidates → ``(candidates, {veto: count})``."""
    from backend.web.core import events as _events
    from backend.web.core import launch_gate as _gate
    from backend.web.core import sleep as _sleep

    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    by_title = {r.get("title"): r for r in rows if not r.get("device")}
    mb, _peak = _gate.session_sizes()
    with srv.ENGINE.lock:
        items = list(srv.ENGINE.instances.items())
    out: List[dict] = []
    vetoed: Dict[str, int] = {}
    for title, inst in items:
        row = by_title.get(title)
        if row is None:
            continue
        why = _never(row)
        if not why:
            try:
                why = (
                    _sleep.veto(
                        srv, inst, title, row, now, SHED_IDLE_SECONDS, rows=rows
                    )
                    or ""
                )
            except Exception as err:  # noqa: BLE001 — one bad session can't stop it
                why = "error: %s" % err
        if why:
            vetoed[why] = vetoed.get(why, 0) + 1
            continue
        since = float(row.get("activity_since") or now)
        out.append(
            {
                "title": title,
                "row": row,
                "inst": inst,
                "idle_s": max(0.0, now - since),
                "mb": mb.get(title),
            }
        )
    return out, vetoed


def _shed(srv, snap: dict, why: str, now: float, sleeper=None) -> List[str]:
    """Pick and sleep (see the module docstring) → the titles slept."""
    from backend.web.core import launch_gate as _gate
    from backend.web.core import sleep as _sleep

    need = shed_need_mb(snap)
    cost = _gate.assess(now=now)["cost"]
    cands, vetoed = _candidates(srv, now)
    picks = choose_shed(cands, max(need, 1), int(cost["session_mb"]))
    skipped = ", ".join("%s ×%d" % (k, n) for k, n in sorted(vetoed.items()))
    _info(
        "%s (%s): need ~%d MB to get back over the low line; %d idle "
        "candidate(s), chose %s%s",
        why,
        memory_pressure.describe(snap) or "memory critical",
        need,
        len(cands),
        ", ".join(
            "%s (%d MB%s, idle %dm)"
            % (p["title"], p["mb"], "" if p["measured"] else " est.", p["idle_s"] // 60)
            for p in picks
        )
        or "none",
        ("; kept awake: " + skipped) if skipped else "",
    )
    by_title = {c["title"]: c for c in cands}
    put = sleeper or (
        lambda inst, title, idle: _sleep.put_to_sleep(
            srv, inst, title, _sleep.REASON_MEMORY, idle
        )
    )
    slept: List[str] = []
    for i, p in enumerate(picks):
        c = by_title[p["title"]]
        try:
            if i and sleeper is None:
                # The kills before this one took seconds: ask the live probes
                # again before acting.
                from backend.web.core import events as _events

                rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
                again = _sleep.veto(
                    srv,
                    c["inst"],
                    p["title"],
                    c["row"],
                    time.time(),
                    SHED_IDLE_SECONDS,
                    rows=rows,
                )
                if again:
                    _info("%s: kept awake after all (%s)", p["title"], again)
                    continue
            err = put(c["inst"], p["title"], p["idle_s"])
        except Exception as exc:  # noqa: BLE001
            err = str(exc) or exc.__class__.__name__
        if err:
            _info("%s: not put to sleep (%s)", p["title"], err)
        else:
            slept.append(p["title"])
    if slept:
        _announce(slept, snap)
    return slept


def _announce(titles: List[str], snap: dict) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(
            "system.sessions_slept",
            new=str(len(titles)),
            data={
                "count": len(titles),
                "titles": list(titles),
                "reason": "memory",
                "memory": snap,
                "detail": shed_notice_text(titles),
            },
        )
    except Exception:  # noqa: BLE001 — a notice never breaks the shed
        pass


def tick(
    srv=None,
    snap: Optional[dict] = None,
    now: Optional[float] = None,
    *,
    sleeper: Optional[Callable] = None,
    runner: Optional[Callable] = None,
) -> dict:
    """One look (the server's memory watch, every 10 s; blocking: off the
    loop) → ``{"shed": [titles], "why", "voicelab": str}``. Stops Voice
    Lab's backend at ``critical`` (:func:`stop_voice_lab`) and sheds idle
    agents when :func:`shed_due` says so. Under pytest nothing real is
    touched unless the test passes ``srv`` + ``sleeper`` / ``runner``.
    Never raises."""
    now = time.time() if now is None else now
    out: dict = {"shed": [], "why": "", "voicelab": ""}
    try:
        snap = memory_pressure.snapshot() if snap is None else snap
        lvl = snap.get("level")
        _note_voice_level(snap)
        if lvl == "critical":
            out["voicelab"] = stop_voice_lab(runner) or ""
        with _LOCK:
            due, why = shed_due(_STATE, snap, now)
        out["why"] = why
        if not due:
            return out
        if not shed_enabled():
            with _LOCK:
                _STATE["last_shed_at"] = now
                _STATE["last_decision"] = (
                    "%s; shedding is off (agent_memory.shed_idle)" % why
                )
            _info("%s; idle agents are not put to sleep: shed_idle is off", why)
            return out
        if srv is None:
            if _under_pytest():
                return out
            from backend.web import server as srv  # type: ignore[no-redef]
        elif sleeper is None and _under_pytest():
            return out
        slept = _shed(srv, snap, why, now, sleeper)
        out["shed"] = slept
        with _LOCK:
            _STATE["last_shed_at"] = now
            _STATE["last_shed"] = list(slept)
            _STATE["last_decision"] = "%s: put %d to sleep" % (why, len(slept))
    except Exception as err:  # noqa: BLE001 — the watch must never die
        _info("pass failed: %s", err)
    return out


# --------------------------------------------------------------------------- #
# Voice Lab
# --------------------------------------------------------------------------- #
def voice_lab_blocked(snap: dict) -> str:
    """Why a local voice host must not be woken or called now ("" = it may):
    the level reads ``low`` or ``critical`` (pure)."""
    if snap.get("level") not in ("low", "critical"):
        return ""
    return memory_pressure.describe(snap) or "memory is %s" % snap.get("level")


def _note_voice_level(snap: dict) -> str:
    """Log once per episode that voice hosts are held back (and once when
    they may be used again). → the current reason."""
    why = voice_lab_blocked(snap)
    with _LOCK:
        prev = str(_STATE.get("voice_yield") or "")
        _STATE["voice_yield"] = why
    if why and not prev:
        _info(
            "voice: Voice Lab (a local voice host) is not woken or called while "
            "%s; lines are said by the local voice instead",
            why,
        )
    elif prev and not why:
        _info("voice: memory recovered; Voice Lab may be used again")
    return why


def voice_lab_yield() -> str:
    """The live question :mod:`backend.web.core.speech` asks before a request
    to a local voice host: why not ("" = go). Never raises."""
    try:
        return _note_voice_level(memory_pressure.snapshot())
    except Exception:  # noqa: BLE001 — the guard never breaks a request
        return ""


def _unit() -> str:
    raw = os.environ.get("MINDFLOCK_VOICELAB_UNIT")
    if raw is None:
        return VOICELAB_UNIT
    raw = raw.strip()
    return "" if raw.lower() in ("", "0", "off", "none", "false") else raw


def stop_voice_lab(runner: Optional[Callable] = None) -> str:
    """Stop Voice Lab's backend when its unit exists and runs → what was
    done (``"stopped voicelab-backend.service"``), or "" (no systemd, no
    such unit, not running, or turned off).
    ``runner`` is ``subprocess.run``'s shape (tests inject a fake; under
    pytest the real one is never used). Never raises."""
    unit = _unit()
    if not unit:
        return ""
    if runner is None:
        if _under_pytest():
            return ""
        runner = subprocess.run
    import shutil

    systemctl = shutil.which("systemctl") if runner is subprocess.run else "systemctl"
    if not systemctl:
        return ""
    try:
        shown = runner(
            [systemctl, "--user", "show", "-p", "LoadState", "-p", "ActiveState", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        props = dict(
            line.split("=", 1)
            for line in (getattr(shown, "stdout", "") or "").splitlines()
            if "=" in line
        )
        if getattr(shown, "returncode", 1) != 0 or props.get("LoadState") != "loaded":
            return ""
        if props.get("ActiveState") not in ("active", "activating", "reloading"):
            return ""
        done = runner(
            [systemctl, "--user", "stop", unit],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        ok = getattr(done, "returncode", 1) == 0
    except Exception as err:  # noqa: BLE001
        _info("voice: could not stop %s: %s", unit, err)
        return ""
    msg = ("stopped %s" if ok else "could not stop %s") % unit
    with _LOCK:
        _STATE["voicelab_last"] = msg
        if ok:
            _STATE["voicelab_stopped_at"] = time.time()
    _info(
        "voice: memory is critical: %s (socket-activated: the next voice use "
        "starts it again, once memory allows)",
        msg,
    )
    return msg


def status() -> dict:
    """For ``GET /api/memory/capacity`` → ``shield``: ``{"shed_idle",
    "critical_since", "last_shed_at", "last_shed", "last_decision",
    "voice_yield", "voicelab_last"}``."""
    with _LOCK:
        st = dict(_STATE)
    return {
        "shed_idle": shed_enabled(),
        "critical_since": st.get("critical_since"),
        "last_shed_at": st.get("last_shed_at") or None,
        "last_shed": list(st.get("last_shed") or []),
        "last_decision": st.get("last_decision") or "",
        "voice_yield": st.get("voice_yield") or "",
        "voicelab_last": st.get("voicelab_last") or "",
    }


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(
            {
                "critical_since": None,
                "last_shed_at": 0.0,
                "last_shed": [],
                "last_decision": "",
                "voice_yield": "",
                "voicelab_stopped_at": 0.0,
                "voicelab_last": "",
            }
        )
