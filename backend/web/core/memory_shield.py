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
  10 s). Only on SUSTAINED critical distress: the level itself steps up to
  ``critical`` only once a distress signal (PSI stalls, swap-in, OOM kills,
  a tiny available floor, Windows' commit charge or hard paging — see
  :mod:`backend.memory_pressure`) has held for its
  :data:`~backend.memory_pressure.SUSTAIN_SECONDS`; it must then stay
  ``critical`` for :data:`SHED_AFTER_SECONDS` more — or, already critical,
  PSI ``full`` avg10 reaches :data:`FULL_AVG10_SHED` (every runnable task on
  the machine stalled on memory for a quarter of the last 10 s: a thrash
  under way, where waiting out the minute would cost 15 s of total stall).
  Then idle agent sessions are put to sleep through
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
  available memory back over the ``low`` floor (at least one: a stall or a
  swap-in storm has no byte target). ONE notification per shed, and each
  decision is logged with the signal that made the shield act. ``general.agent_memory.shed_idle`` (default
  on) turns it off; :data:`SHED_COOLDOWN_SECONDS` separates two sheds so
  the next one acts on readings taken after the memory came back.

* **Voice is left alone.** The owner's rule (2026-11-10: "I don't want
  voice models off when memory gets low"): the shield never stops Voice Lab
  (or any local voice host), and nothing here holds voice back — local
  recognition, local TTS and voice-host calls run whatever the level reads
  (:func:`backend.web.core.speech.host_held_back`). Room is made by
  shedding idle agents instead.

Stdlib only at import (plus :mod:`backend.memory_pressure`); everything
else is imported lazily inside the calls.
"""

from __future__ import annotations

import os
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
    "status",
]

_MiB = 1024**2

#: How long ``critical`` must hold before idle agents are shed (on top of the
#: gauge's own sustain before it reads critical at all).
SHED_AFTER_SECONDS = 60.0
#: PSI ``full`` avg10 (%) at or past which, while critical, they are shed at
#: once.
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

_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "critical_since": None,
    "last_shed_at": 0.0,
    "last_shed": [],
    "last_decision": "",
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
    ``critical`` for :data:`SHED_AFTER_SECONDS`, or reads ``critical`` with
    PSI ``full`` avg10 at :data:`FULL_AVG10_SHED` or past it; never within
    :data:`SHED_COOLDOWN_SECONDS` of the last shed. ``why`` names the signal
    (the snapshot's ``why``, see :func:`memory_pressure.describe`)."""
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
    signals = "; ".join(w for w in (snap.get("why") or []) if w)
    if full10 >= FULL_AVG10_SHED and lvl == "critical":
        return True, "every task stalled on memory %d%% of the last 10 s%s" % (
            int(round(full10)),
            " (critical: %s)" % signals if signals else "",
        )
    since = state.get("critical_since")
    if since is not None and now - float(since) >= SHED_AFTER_SECONDS:  # type: ignore[arg-type]
        return True, "memory has been critical for %ds%s" % (
            int(now - float(since)),  # type: ignore[arg-type]
            " (%s)" % signals if signals else "",
        )
    return False, ""


def shed_need_mb(snap: dict, low_line_bytes: Optional[float] = None) -> int:
    """MB to free to bring available memory back over the ``low`` floor
    (``max(low share of RAM, absolute floor)``), 0 when already over — the
    shed then takes one session at a time (critical came from a stall,
    swap-in or Windows' commit charge, which have no byte target; the
    cooldown lets the next pass see the effect). Inside WSL with a host
    reading, also the host's shortfall under its headroom floor
    (:func:`memory_pressure.host_low_line`), measured on its headroom
    (Windows' available plus what the guest can reuse,
    :func:`memory_pressure.host_headroom`): while the guest holds reusable
    memory, putting agents to sleep frees nothing Windows gets back soon, so
    only a shortfall past that is worth freeing."""
    total = float(snap.get("total") or 0)
    avail = float(snap.get("available") or 0)
    if low_line_bytes is None:
        try:
            th = memory_pressure.thresholds()["low"]
            low_line_bytes = max(float(th[0]) * total, float(th[1]))
        except Exception:  # noqa: BLE001
            low_line_bytes = 0.0
    need = float(low_line_bytes) - avail
    host = snap.get("host") or {}
    if host.get("total"):
        host_line = memory_pressure.host_low_line(float(host["total"]))
        need = max(need, host_line - memory_pressure.host_headroom(host))
    return max(0, int(round(need / _MiB)))


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
        "acting on %s%s: need ~%d MB to get back over the low line; %d idle "
        "candidate(s), chose %s%s",
        why,
        # The signals are in ``why`` already (shed_due); an older snapshot's
        # numbers are not.
        (
            ""
            if snap.get("why")
            else " (%s)" % (memory_pressure.describe(snap) or "memory critical")
        ),
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
) -> dict:
    """One look (the server's memory watch, every 10 s; blocking: off the
    loop) → ``{"shed": [titles], "why"}``. Sheds idle agents when
    :func:`shed_due` says so; never touches voice (Voice Lab keeps running
    at ``critical``). Under pytest nothing real is touched unless the test
    passes ``srv`` + ``sleeper``. Never raises."""
    now = time.time() if now is None else now
    out: dict = {"shed": [], "why": ""}
    try:
        snap = memory_pressure.snapshot() if snap is None else snap
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
        if sleeper is None:
            # Two servers on one state dir both run this tick over the same
            # agents: only the one holding the lease sheds (core.sleep).
            from backend.web.core import sleep as _sleep

            if not _sleep.hold_lease("memory-shield", 30.0):
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


def status() -> dict:
    """For ``GET /api/memory/capacity`` → ``shield``: ``{"shed_idle",
    "critical_since", "last_shed_at", "last_shed", "last_decision"}``."""
    with _LOCK:
        st = dict(_STATE)
    return {
        "shed_idle": shed_enabled(),
        "critical_since": st.get("critical_since"),
        "last_shed_at": st.get("last_shed_at") or None,
        "last_shed": list(st.get("last_shed") or []),
        "last_decision": st.get("last_decision") or "",
    }


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(
            {
                "critical_since": None,
                "last_shed_at": 0.0,
                "last_shed": [],
                "last_decision": "",
            }
        )
