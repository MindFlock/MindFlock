"""Agent-state inference — activity / stage / last-turn probes for a session.

Lifted verbatim from ``server.py``: the layered ``_agent_activity`` classifier
(exit marker / provider hook marker / process tree / CPU rate / pane-hash
hysteresis), the workflow-stage inference (``_session_stage``), the failed
pre-commit-step parser, and the last-turn snippet — plus their module-level
caches and tuning constants. The short-TTL ``_probe_cached`` memo that wraps
these probes stays in ``server.py`` next to its other per-session caches.

Cross-module seams (``_run_capped``, ``_pane_meta`` and friends, and the
git/PR helpers ``_session_stage`` leans on) are resolved through the *server
module's* attribute bindings at call time via :func:`_server`. That is
deliberate: tests (and the server itself) monkeypatch those seams as
``server._pane_meta`` etc., and rebinding the server attribute must keep
steering this code exactly as it did before the split.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Dict, Optional

from backend import providers
from backend import session
from backend.providers.usage_limits import is_limit_screen
from backend.session import tmux
from backend.session.storage import Loading
from backend.web.core import events as _events
from backend.web.core import stage_reset as _stage_reset
from backend.web.core.terminal import _exit_marker_path


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _session_last_turn(inst) -> Optional[str]:
    """L3: one-line snippet of the session's latest conversational turn (the
    provider reads its own transcript; Claude parses the newest JSONL entry,
    mtime-guard-cached ~10s provider-side). None when unavailable."""
    try:
        if not inst.Started():
            return None
        wt = inst.GetWorktreePath()
        if not wt:
            return None
        name = tmux.to_mindflock_tmux_name(inst.Title)
        return providers.resolve(getattr(inst, "Program", "") or "").last_turn_snippet(
            name, wt
        )
    except Exception:  # noqa: BLE001
        return None


def _session_last_prompt(inst) -> Optional[str]:
    """One-line snippet of the newest USER prompt — what the human last asked
    this session to do (the panes pin it above the terminal). Same sourcing
    and caching as :func:`_session_last_turn`. None when unavailable."""
    try:
        if not inst.Started():
            return None
        wt = inst.GetWorktreePath()
        if not wt:
            return None
        name = tmux.to_mindflock_tmux_name(inst.Title)
        return providers.resolve(
            getattr(inst, "Program", "") or ""
        ).last_prompt_snippet(name, wt)
    except Exception:  # noqa: BLE001
        return None


def _session_last_prompt_full(inst) -> Optional[str]:
    """The newest USER prompt's whole body — the pinned line's expansion.
    Same sourcing and caching as :func:`_session_last_prompt`."""
    try:
        if not inst.Started():
            return None
        wt = inst.GetWorktreePath()
        if not wt:
            return None
        name = tmux.to_mindflock_tmux_name(inst.Title)
        return providers.resolve(getattr(inst, "Program", "") or "").last_prompt_full(
            name, wt
        )
    except Exception:  # noqa: BLE001
        return None


def _session_find_prompt(inst, prefix: str) -> Optional[str]:
    """Full body of the newest USER prompt starting with ``prefix`` — lets the
    desktop prompt bar expand OLDER prompts than the latest (their full
    bodies aren't in the snapshot). None when unavailable/unmatched."""
    try:
        if not inst.Started():
            return None
        wt = inst.GetWorktreePath()
        if not wt:
            return None
        name = tmux.to_mindflock_tmux_name(inst.Title)
        return providers.resolve(getattr(inst, "Program", "") or "").find_prompt_full(
            name, wt, prefix
        )
    except Exception:  # noqa: BLE001
        return None


# Layered activity detection (roadmap A1). Per-title rolling record of the
# agent pane. Authoritative signals (exit marker, foreground process, provider
# hook marker) are consulted first; the pane-hash fallback is hardened with
# noise stripping + asymmetric hysteresis so a single changed frame can't flip
# the badge every poll.
#
# The record carries two independent groups of keys:
#
# * **pane-inspection bookkeeping**, written only by :func:`_pane_hash_activity`
#   and meaningful only to it — ``hash`` / ``changed`` / ``state`` / ``streak``
#   / ``size`` / ``cpu`` / ``cpu_at`` / ``busy_at`` / ``tokens`` /
#   ``hard_since`` / ``proof``. ``state`` is *that layer's* running verdict, not
#   the session's; ``hard_since`` is when its current run of proven-busy polls
#   began and ``proof`` is what proved it — ``"status"`` for the provider's own
#   live-turn status line (Claude's ``esc to interrupt``, a climbing token
#   counter), ``"cpu"`` for a busy process tree and nothing else. Both are None
#   between runs. The pair is what decides whether pane inspection may arm a
#   turn-end announcement; see :func:`_agent_activity`.
# * **layer-wide provenance**, written by :func:`_verdict` on EVERY return path
#   of :func:`_agent_activity` — ``created`` / ``reported`` / ``state_since`` /
#   ``worked_at`` / ``source`` (which layer produced the current reading —
#   "marker" / "exit" / "proc" / "trust" / "pane") / ``worked_evidence`` (what
#   corroborated the armed work — "marker" / "status" / "cpu"). Before these existed only the pane path wrote anything, so a
#   session reporting through its CLI's hooks (the common Claude case) left no
#   trail at all: ``activity_since`` was dead for exactly those sessions, and
#   nothing downstream could tell "this agent worked and then stopped" from
#   "this agent has never done anything", which is precisely the distinction a
#   notification needs (see ``server._note_turn_boundary``).
_ACTIVITY_CACHE: Dict[str, dict] = {}
_ACTIVITY_IDLE_AFTER = (
    8.0  # static-pane seconds before working -> idle (2x the UI's 4s poll)
)
_ACTIVITY_CONFIRM_POLLS = 2  # consecutive changed polls before idle -> working
# How long a CPU-ONLY ``working`` run must last before it may ARM a turn-end
# announcement (see :func:`_verdict`). The backstop for a CLI that does not
# report its own state, whose only other signal is a status-line regex that may
# simply be wrong. Well clear of the ~12s a single spike holds the state through
# `_ACTIVITY_IDLE_AFTER`, and far below any turn worth being told about.
_CPU_ONLY_ARMS_AFTER_S = 20.0
_ACTIVITY_NOISE_LINES = (
    2  # bottom pane lines ignored by the hash (cursor/spinner churn)
)
_MARKER_TRUST_WINDOW_S = 45.0  # a working/clarify hook marker older than this is re-verified against the live pane
# An IDLE verdict from an authoritative signal (the CLI's own Stop hook) is
# re-checked against the pane for a usage-limit screen at most this often per
# session. A turn that ends because the account ran out is reported by the CLI
# exactly like a turn that ends because the work is done — same Stop hook, same
# quiet pane — so without this the limit is invisible to everything downstream
# (the badge, the auto-resume watcher, and autopilot's "is the agent done?"
# gate, which committed a half-finished session because of it). Throttled
# because the marker path deliberately skips the pane capture.
_LIMIT_RECHECK_S = 15.0
_LIMIT_PROBE: Dict[str, dict] = {}  # title -> {"at": epoch, "limit": bool}
# Per-window resume-thread binding (provider.record_thread) runs at most this
# often per session — discovery stats on-disk stores and the id changes rarely.
_THREAD_RECORD_EVERY_S = 20.0
_THREAD_RECORD_AT: dict = {}


def _activity_record(title: str, created) -> dict:
    """The per-title rolling record, reset when the tmux INCARNATION changes.

    This used to be created ad hoc inside :func:`_pane_hash_activity` and
    POPPED on every ``offline`` poll. Both halves were wrong.

    Popping meant a session whose tmux merely hiccuped — or one the user closed
    and re-opened, which relaunches the agent through
    ``agent_sessions._ensure_agent_session`` — came back with no hysteresis, no
    CPU baseline and no history, straight into the pane layer's "first sighting"
    branch. It also defeated the resize rebaseline below, which exists precisely
    so that *clicking a tab must not read as activity*: that guard can only
    protect a record that still exists.

    But a record must not survive a RELAUNCH either. ``worked_at`` asserts "this
    agent process was observed doing work", and ``tmux new-session`` starts a
    different process; carrying the old answer over is how a freshly rebooted
    session inherits the dead run's history. tmux's own ``session_created`` is
    the incarnation id — the same fact :func:`_agent_exited` already uses to
    reject an exit marker left by a previous session of the same name.

    An EXISTING record with no ``created`` yet (a hand-seeded one in tests, or
    the pane layer's own re-fetch) simply adopts the stamp: adopting is not a
    reset. A record being MINTED without one is never stored at all — see below.
    """
    rec = _ACTIVITY_CACHE.get(title)
    try:
        stamp = None if created is None else float(created)
    except (TypeError, ValueError):
        stamp = None
    if rec is None:
        rec = {
            "created": stamp,
            "reported": None,
            "state_since": 0.0,
            "worked_at": None,
            "source": "",
        }
        if stamp is not None:
            _ACTIVITY_CACHE[title] = rec
        # A brand-new record with NO incarnation stamp is never kept. That
        # happens when `tmux display-message` fails while `has-session`
        # succeeded, so `_pane_meta` answers all-None: the poll can still
        # capture a pane and reach a verdict, but there is no identity to file
        # it under. Storing it would create a record that the next successful
        # stamp has to either adopt — smuggling a dead run's work evidence into
        # a new incarnation through the one door this function exists to shut —
        # or discard, which is what a throwaway does anyway, one poll earlier
        # and without the ambiguity. Losing a poll's memory fails quiet: no
        # evidence, so nothing can be announced.
        return rec
    if stamp is None:
        rec.setdefault("reported", None)
        rec.setdefault("state_since", 0.0)
        rec.setdefault("worked_at", None)
        rec.setdefault("source", "")
        return rec
    if rec.get("created") is None:
        # An EXISTING record with no stamp is a hand-seeded one (tests, and
        # `_pane_hash_activity`'s own re-fetch). Adopting is not a reset.
        rec.setdefault("reported", None)
        rec.setdefault("state_since", 0.0)
        rec.setdefault("worked_at", None)
        rec.setdefault("source", "")
        rec["created"] = stamp
        return rec
    if rec["created"] != stamp:
        # A new tmux session under the same name: every observation in the
        # record describes a process that no longer exists. The limit probe
        # goes with it — its verdict is cached for _LIMIT_RECHECK_S, and a
        # window reopened inside that span would otherwise report a fresh
        # process as usage-limited (and 'usage_limit' is a default-ON rule).
        rec.clear()
        rec.update(
            {
                "created": stamp,
                "reported": None,
                "state_since": 0.0,
                "worked_at": None,
                "source": "",
            }
        )
        _LIMIT_PROBE.pop(title, None)
    return rec


def _verdict(
    rec: Optional[dict], value: str, now: float, arms: bool = False, source: str = ""
) -> str:
    """Record ``value`` as this session's reading and return it.

    THE SINGLE EXIT POINT for every layer of :func:`_agent_activity`, so
    ``state_since`` and ``worked_at`` mean the same thing whether the answer
    came from the exit marker, a hook marker, the process tree or the pane.

    ``worked_at`` is stamped only by ``working`` — never by ``clarify``, and
    never by ``idle``. It is the evidence a notification needs before it may
    claim a turn ENDED, so it has to mean "we watched this agent work", not
    "something happened on this pane". A trust/MCP gate on a brand-new session
    reports ``clarify`` before the agent has been handed anything to do; a
    session that boots to a bare prompt reports ``idle`` forever. Neither is a
    turn, and neither may arm one.

    ``arms`` is the second half of that sentence, and every ``working`` reading
    now has to earn it. A reading is enough to paint the chip; it is not by
    itself enough to interrupt a human with "your agent finished". The caller
    passes True only where the reading CORROBORATES work:

    * the CLI's own report (a fresh hook marker / live agent query) — always;
      a hook fires because a prompt was submitted or a tool ran;
    * the provider's live-turn status line (Claude's ``esc to interrupt``, a
      climbing token counter), which is on the pane BECAUSE a turn is running —
      so it arms even where the CLI's own marker went stale mid-turn;
    * a busy process tree and nothing else, but ONLY for a CLI that cannot
      report for itself, and only after `_CPU_ONLY_ARMS_AFTER_S` of unbroken
      busy polls. See :func:`_agent_activity`, which decides all three.

    Everything else reports its state and arms nothing. The case that forced
    this: a parked Claude session whose pane burned one 4s poll above
    `_CPU_ACTIVE_JIFFIES_PER_S` (its own auto-updater, a `/clear`, GC) read as
    ``working`` for ~12s — long enough to clear the announce path's flicker
    settle — and 45s later announced a turn that never happened. Its transcript
    had not been written to in 19 hours. Duration thresholds cannot separate
    that from a short real turn; provenance can.

    ``source`` names WHICH layer produced the reading — ``"marker"`` (the CLI's
    own hook marker / live query, including the idle/limit reclassifications
    built on it), ``"exit"`` (the launch wrapper's exit marker), ``"proc"``
    (bare shell, no agent process), ``"trust"`` (the startup trust-gate
    auto-answer), ``"pane"`` (live-pane inspection). It is recorded with the
    value so downstream consumers can weigh the reading by where it came from:
    the announce path's settle skip and the queue drain's fast tier both key on
    :func:`reading_is_authoritative`, and the turn-end dwell tiers key on
    ``worked_evidence`` — stamped here alongside ``worked_at`` as "marker" for
    the CLI's own word and otherwise as the pane layer's own ``proof``
    ("status" / "cpu"), so the announcement can be as fast as its evidence is
    strong.

    ``limit`` actively DROPS the evidence. A turn the account's cap cut short is
    not a turn that finished, and the reading only says ``limit`` while the
    banner is on the pane — press a key, or let the throttled re-check expire
    against a redrawn prompt, and the session reads plain idle again with the
    cut-short turn's evidence still armed, ready to report "has finished" on
    work that was abandoned mid-thought. Autopilot drops its own idle dwell on a
    limit for exactly this reason. A resume re-earns the evidence on the next
    armed ``working``.
    """
    if rec is None:
        return value
    if rec.get("reported") != value:
        rec["reported"] = value
        rec["state_since"] = now
    rec["source"] = source
    # The (value, source) pair as ONE store, because it is read as one fact:
    # `reported` and `source` are written back-to-back with no lock while the
    # drain, autopilot and both tickers all call this concurrently, and two
    # interleaved calls can leave one caller's value paired with the other's
    # source — which would let a pane-derived clarify borrow marker authority
    # for up to a poll. :func:`reading_is_authoritative` reads only this tuple.
    rec["reading"] = (value, source)
    if value == "working":
        if arms:
            # Evidence label BEFORE the timestamp: `_note_turn_boundary` reads
            # them in the opposite order (evidence, then worked_at), so any
            # interleaving pairs an old label with an old stamp, or a fresh
            # stamp with either label — and a fresh stamp always fails the
            # dwell, which is the conservative outcome. The dangerous pairing
            # (old stamp judged by a new, faster label) needs the reader to see
            # the new label before the new stamp, which these two orderings
            # jointly rule out.
            rec["worked_evidence"] = (
                "marker" if source == "marker" else (rec.get("proof") or "cpu")
            )
            rec["worked_at"] = now
    else:
        # Any non-working reading ends the pane layer's proven-busy run,
        # WHATEVER layer delivered it. These used to be cleared only by the
        # pane path's own idle transition, so an idle delivered by the exit
        # marker (the CLI died), a Stop hook, or the process-tree probe left
        # `hard_since`/`proof` frozen — and a relaunch into the SAME tmux
        # session then armed on its first startup-CPU poll, the 20s unbroken
        # run "satisfied" by a stamp from a run that ended hours earlier.
        # A limit or clarify interrupts the run just as thoroughly as idle.
        rec["hard_since"] = None
        rec["proof"] = None
        if value == "limit":
            rec["worked_at"] = None
    return value


def worked_at(title: str) -> Optional[float]:
    """Epoch this session was last CORROBORATED working in its current tmux
    incarnation, or None — the provenance behind ``session.turn_ended``.

    Corroborated, not merely reported: only readings passed to :func:`_verdict`
    with ``arms=True`` stamp this. A pane that momentarily looks busy moves the
    badge and leaves no evidence behind.

    None means one of four things, and all four are "there is no turn to
    announce": the session has never been seen working, nothing has corroborated
    the work it appeared to do, its tmux session was relaunched since
    (:func:`_activity_record` resets the record), or a turn end has already been
    announced for the work we saw (:func:`claim_work`).

    A read, for gating. The announcement itself must go through
    :func:`claim_work`, which is the same question asked atomically.
    """
    val = (_ACTIVITY_CACHE.get(title) or {}).get("worked_at")
    return float(val) if isinstance(val, (int, float)) else None


#: Reading sources whose word is the CLI's own (or the definitive record of its
#: death) rather than an inference from what the pane looks like. These are the
#: sources that may skip the announce path's flicker settle and take the queue
#: drain's fast tier: a hook marker cannot flicker — it changes only when the
#: CLI fires a hook — and an exit marker is written exactly once, by the launch
#: wrapper, when the agent command ends. "proc" is deliberately absent: the
#: process-tree probe can misread transiently (a ps race, a missing pane pid),
#: which is precisely the failure the settle exists to absorb.
_AUTHORITATIVE_SOURCES = frozenset({"marker", "exit"})


def reading_is_authoritative(title: str, value: str) -> bool:
    """Whether ``title``'s CURRENT recorded reading is ``value`` AND came from
    an authoritative source (:data:`_AUTHORITATIVE_SOURCES`).

    Both halves matter. The value comparison guards the memo skew the server
    documents in ``_note_turn_boundary``: the tickers serve activity out of a
    2.5s probe memo while the queue drain and autopilot make UNCACHED calls on
    their own cadences, so at any instant the rolling record can describe a
    NEWER reading than the value a caller holds. Claiming authority for a value
    the record no longer reports would let a consumer skip its safety net on
    the strength of a different reading entirely — so a mismatch answers False,
    which every consumer treats as "take the slow, guarded path".
    """
    reading = (_ACTIVITY_CACHE.get(title) or {}).get("reading") or ()
    return (
        len(reading) == 2
        and reading[0] == value
        and reading[1] in _AUTHORITATIVE_SOURCES
    )


def work_evidence(title: str) -> str:
    """What corroborated this session's ``worked_at`` evidence: ``"marker"``
    (the CLI's own report), ``"status"`` (the provider's live-turn status
    line), ``"cpu"`` (the sustained-CPU backstop), or ``""`` (no armed
    evidence recorded). The turn-end announcement picks its dwell tier from
    this — the stronger the evidence, the less time it needs to buy
    confidence. Left in place by :func:`claim_work` on purpose: it describes
    the last armed cycle, and it is only ever read next to ``worked_at``.
    """
    val = (_ACTIVITY_CACHE.get(title) or {}).get("worked_evidence")
    return str(val) if isinstance(val, str) else ""


#: Guards the read-and-clear in :func:`claim_work`. Nothing else needs it —
#: every other access is a single GIL-atomic dict operation.
_WORK_CLAIM_LOCK = threading.Lock()


def claim_work(title: str) -> Optional[float]:
    """Atomically SPEND this session's work evidence, or None if it is gone.

    One turn-end announcement per observed work cycle; the next ``working``
    reading re-arms it. Atomic because two unsynchronised 4s tickers
    (``_instances_tick`` and ``_tick_state_changes``) plus an on-demand
    ``_republish_session`` all run this check, and a plain read-then-clear lets
    two of them satisfy the dwell in the same instant and announce twice — which
    the per-channel dedupe would hide on the phone and not in the bell, whose
    rows are keyed by event seq. Winning the claim IS the permission to emit.
    """
    with _WORK_CLAIM_LOCK:
        rec = _ACTIVITY_CACHE.get(title)
        val = (rec or {}).get("worked_at")
        if not isinstance(val, (int, float)):
            return None
        rec["worked_at"] = None
        return float(val)


def state_since(title: str) -> float:
    """Epoch the session's REPORTED activity last changed value, or 0.0.

    Feeds ``/api/instances``' ``activity_since`` (attention ordering and the
    wedged-session watchdog). Distinct from the notification layer's own
    ``activity_at``, which stamps when the *announced* value was adopted —
    the watchdog wants the pane's truth, the notification wants what the user
    was told, and neither may gate the other.
    """
    val = (_ACTIVITY_CACHE.get(title) or {}).get("state_since")
    return float(val) if isinstance(val, (int, float)) else 0.0


# Last live branch observed per session title, so the stage pill can follow a
# branch switch inside one workspace (in-place sessions especially): on drift,
# the branch-scoped flow artifacts (commit-status marker, PR / origin-SHA /
# base-branch / diff caches) are cleared so the pill reflects the branch the
# user is actually on, not a previous task's leftovers. Dict ops are
# GIL-atomic and every drift step is idempotent, so no lock is needed.
_LAST_BRANCH: Dict[str, str] = {}

# Last PR state observed per session title, as ``(branch, state)`` — the memory
# behind ``session.pr_state_changed``. Kept separate from the stage ladder on
# purpose: ``_session_stage`` drops off the "pr" rung whenever the tree goes
# dirty, a commit runs, or pre-commit blocks one, so "the stage left pr" is NOT
# "the PR closed". Consumers that inferred it that way announced a merge (and
# then a re-open) on every edit/commit/push cycle of a PR that never moved.
_LAST_PR_STATE: Dict[str, tuple] = {}
_LAST_PR_STATE_LOCK = threading.Lock()


def _track_pr_state(title: str, branch: str, info) -> None:
    """Emit ``session.pr_state_changed`` when ``branch``'s PR really moved.

    ``info`` is :func:`server._pr_info`'s answer — ``{"url", "state"}`` or None.
    A missing/None ``info`` is never a transition: it means the lookup did not
    happen (no commits beyond base, or the stage returned early), the branch has
    no PR, or the lookup FAILED — and ``_pr_info`` cannot distinguish the last
    two past its sticky window. Treating that as "closed" is exactly the false
    alarm this event exists to remove, so it is dropped instead.

    First sighting seeds silently (a restart must not re-announce a PR that was
    already merged), and so does a branch change (a different branch is a
    different PR, not a transition of this one). Never raises: a notification
    must never break the stage probe.
    """
    state = str((info or {}).get("state") or "").upper()
    if not title or not branch or not state:
        return
    with _LAST_PR_STATE_LOCK:
        prev = _LAST_PR_STATE.get(title)
        _LAST_PR_STATE[title] = (branch, state)
        if prev is None or prev[0] != branch or prev[1] == state:
            return
        old = prev[1]
    # Outside the lock: emit runs subscribers (the notify addon pushes to ntfy)
    # on this thread, and the poll must not serialize behind them.
    try:
        _events.BUS.emit(
            "session.pr_state_changed",
            session=title,
            old=old,
            new=state,
            data={"url": (info or {}).get("url") or ""},
        )
    except Exception:  # noqa: BLE001 — an event never breaks the stage probe
        pass


# Foreground commands that mean "no agent is running in this pane" — the CLI
# exited (or its launcher dropped to a shell), so the pane can't be 'working'.
_BARE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"})


def _pane_meta(name: str):
    """``(pane_current_command, session_created_epoch, pane_pid, pane_size)`` for
    tmux session ``name`` (one display-message call), or ``(None, None, None,
    None)`` on any failure. ``pane_size`` is ``"<width>x<height>"`` (e.g.
    ``"120x30"``) — used to tell a resize-driven repaint apart from real agent
    output in the activity classifier."""
    cp = _server()._run_capped(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_current_command}\t#{session_created}\t#{pane_pid}\t#{pane_width}x#{pane_height}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if cp.returncode != 0:
        return None, None, None, None
    parts = cp.stdout.decode("utf-8", "replace").strip().split("\t")
    fg = (parts[0].strip() or None) if parts else None
    created = None
    if len(parts) > 1:
        try:
            created = float(parts[1])
        except ValueError:
            created = None
    pane_pid = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    pane_size = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
    return fg, created, pane_pid, pane_size


# Process-tree probe (F1): provisioned sessions run through a bash launcher
# script, so the pane's foreground command reads as ``bash`` with ``claude``
# running as a CHILD — a bare-shell foreground alone must not mean "agent not
# running".
# One ``ps`` snapshot per lookup, cached ~one poll so several sessions (and
# the /api/events tick running alongside the HTTP poll) share the cost.
_PID_TREE_CACHE: Dict[str, tuple] = {}
_PID_TREE_TTL = 3.0


def _pane_has_agent_process(pane_pid) -> bool:
    """True when the pane's process tree holds a live NON-shell descendant.

    Walks children of ``pane_pid`` from a single ``ps -e -o pid=,ppid=,comm=``
    snapshot; only when every descendant is itself a bare shell (or there are
    none) is the agent really gone. Unknown pid / ps failure -> False (the
    conservative pre-F1 behaviour: bare shell reads idle)."""
    if not pane_pid:
        return False
    key = str(pane_pid)
    now = time.time()
    cached = _PID_TREE_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    result = False
    try:
        cp = _server()._run_capped(
            ["ps", "-e", "-o", "pid=,ppid=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode == 0:
            children: Dict[str, list] = {}
            comms: Dict[str, str] = {}
            for line in cp.stdout.decode("utf-8", "replace").splitlines():
                fields = line.split(None, 2)
                if len(fields) < 3:
                    continue
                pid, ppid, comm = fields[0], fields[1], fields[2].strip()
                children.setdefault(ppid, []).append(pid)
                comms[pid] = comm
            stack = list(children.get(key, []))
            seen = set()
            while stack:
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                comm = os.path.basename(comms.get(pid, "")).lower()
                if comm and comm not in _BARE_SHELLS:
                    result = True
                    break
                stack.extend(children.get(pid, []))
    except OSError:
        result = False
    _PID_TREE_CACHE[key] = (now + _PID_TREE_TTL, result)
    return result


# CPU-based activity (robust idle detection): an agent parked at its prompt is
# blocked reading stdin and burns ~0 CPU, no matter how its idle screen redraws
# (spinner / status line / cursor). A working/streaming agent burns CPU. We
# sample the pane's process-tree CPU jiffies per poll and compare the rate — far
# more reliable than screen-scraping. Linux-only (/proc); other OSes fall back
# to the pane-hash heuristic.
_PROC_SNAPSHOT_CACHE: dict = {"at": 0.0, "children": None, "jiffies": None}
# Must stay BELOW the server's 2.5s probe memo: a session's consecutive
# activity computations are ≥2.5s apart, and if both hit the same cached scan
# the CPU delta reads 0 (phantom idle). 2.0s halves the full-/proc scan rate
# vs the old 1.0s without that aliasing.
_PROC_SNAPSHOT_TTL = 2.0
# Pane-tree CPU rate (jiffies/s ≈ % of one core) above which the fallback calls
# the agent "working". Measured baseline: an IDLE Claude Code TUI still burns
# ~8 jiffies/s just redrawing its prompt, so the bar sits well above that. A
# genuinely working session is normally caught first by its fresh hook marker
# (Layer 1); this only decides when that marker is stale/absent, where erring
# toward idle matches the user's expectation (no phantom "running").
_CPU_ACTIVE_JIFFIES_PER_S = 25.0


def _proc_cpu_snapshot():
    """``(children_by_ppid, jiffies_by_pid)`` from one ``/proc`` scan, cached ~1s
    so all sessions in a poll share it. ``jiffies`` = utime+stime per pid.
    ``({}, {})`` on non-Linux / failure."""
    now = time.time()
    c = _PROC_SNAPSHOT_CACHE
    if c["children"] is not None and now - c["at"] < _PROC_SNAPSHOT_TTL:
        return c["children"], c["jiffies"]
    children: Dict[str, list] = {}
    jiffies: Dict[str, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}, {}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "r") as f:
                data = f.read()
        except OSError:
            continue
        # comm (in parens) can contain spaces/parens; everything after the last
        # ')' is the fixed-order field list: state ppid ... utime(14) stime(15).
        _, _, tail = data.rpartition(")")
        parts = tail.split()
        if len(parts) < 13:
            continue
        ppid = parts[1]
        try:
            jiffies[entry] = int(parts[11]) + int(parts[12])
        except ValueError:
            jiffies[entry] = 0
        children.setdefault(ppid, []).append(entry)
    c.update(at=now, children=children, jiffies=jiffies)
    return children, jiffies


def _pane_cpu_jiffies(pane_pid):
    """Total CPU jiffies of the pane's process subtree, or ``None`` when
    unavailable (non-Linux / no /proc)."""
    if not pane_pid:
        return None
    children, jiffies = _proc_cpu_snapshot()
    if not jiffies:
        return None
    key = str(pane_pid)
    total = jiffies.get(key, 0)
    stack = list(children.get(key, []))
    seen: set = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += jiffies.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


# Trust-gate auto-answer (F2): pre-trust in the Claude provider normally keeps
# the gate from ever appearing; this is the safety net for launches that missed
# it (adopted sessions, other providers, config resets). Rate-limited per title
# so a slow redraw isn't spammed with keystrokes.
_TRUST_DISMISS_AT: Dict[str, float] = {}
_TRUST_DISMISS_COOLDOWN = 5.0
# The trust gate only appears at session startup. For this long after a
# session is created we check for it on every poll BEFORE trusting any hook
# marker — otherwise a fresh clarify/idle marker (Claude can fire a
# Notification hook while the gate is up) short-circuits Layer 1 and the gate
# is reported as "needs input" but never auto-answered, so a seeded prompt
# sits behind it until a human presses Enter (the PR-ingestion symptom).
_TRUST_GATE_STARTUP_WINDOW_S = 300.0


def _dismiss_trust_prompt(inst, title: str, name: str, text: str) -> bool:
    """Auto-answer the provider's trust/MCP gate when it is on screen.

    This wires the previously dead trust handling (Instance.
    CheckAndHandleTrustPrompt / TrustSpec) into the web poll path: whenever the
    captured agent pane matches the provider's trust patterns, the provider's
    dismiss keystroke is sent straight to the tmux session (the web server has
    no attached PTY, so ``send-keys`` is the reliable channel). Returns True
    when a trust screen was seen (the caller reports 'clarify' so the moment
    is visible either way). Never raises."""
    try:
        spec = providers.resolve(getattr(inst, "Program", "") or "").trust_prompt()
        if not spec or not any(p in text for p in spec.patterns):
            return False
        now = time.time()
        if now - _TRUST_DISMISS_AT.get(title, 0.0) >= _TRUST_DISMISS_COOLDOWN:
            _TRUST_DISMISS_AT[title] = now
            subprocess.run(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    name,
                    "-l",
                    spec.keystroke.decode("utf-8", "replace"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        return True
    except Exception:  # noqa: BLE001 — trust handling is best-effort
        return False


def _agent_exited(name: str, session_created) -> bool:
    """True when the exit-recording launch wrapper says the agent command in
    tmux session ``name`` has ENDED (any recorded code — natural or not).

    Guarded by the marker's mtime vs the session's creation time: a leftover
    marker from a previous same-named session (which the engine's first launch
    doesn't clear) must not make a freshly started agent read as ended."""
    if _server()._read_exit_marker(name) is None:
        return False
    try:
        mtime = _exit_marker_path(name).stat().st_mtime
    except OSError:
        return False
    if session_created is not None and mtime < float(session_created):
        return False  # stale marker from a prior incarnation of this name
    return True


def _normalized_pane_hash(text: str) -> str:
    """Hash of the pane text with per-frame churn stripped.

    Per-line trailing whitespace and trailing blank lines are dropped, plus the
    bottom ``_ACTIVITY_NOISE_LINES`` lines, where Claude's cursor / spinner /
    token counter redraw every frame even while nothing is really happening —
    otherwise that churn alone reads as 'working' on every poll."""
    import hashlib

    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if _ACTIVITY_NOISE_LINES:
        lines = lines[:-_ACTIVITY_NOISE_LINES]
    return hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()


def _parse_progress_tokens(text: str, provider) -> Optional[int]:
    """The CLI's climbing turn-token counter parsed to an int, or None.

    Reads the provider's ``progress_token_pattern`` (one capture group) from the
    raw pane and normalizes a ``k``/``m`` suffix (``"15.3k"`` -> ``15300``). The
    caller treats an INCREASE across polls as proof of work — positive even when
    local CPU is ~0 (extended thinking runs server-side). Never raises; any
    provider without the pattern, or an unparseable capture, yields None."""
    try:
        pat = provider.progress_token_pattern()
        if not pat:
            return None
        m = re.search(pat, text)
        if not m:
            return None
        raw = m.group(1).strip().replace(",", "").replace(" ", "")
        mult = 1
        if raw and raw[-1] in "kKmM":
            mult = 1000 if raw[-1] in "kK" else 1_000_000
            raw = raw[:-1]
        return int(float(raw) * mult)
    except Exception:  # noqa: BLE001 — a triage signal, never break the poll
        return None


def _startup_trust_gate_clarify(srv, inst, title: str, name: str, created) -> bool:
    """F2 safety net (startup window only): auto-answer a trust/MCP gate.

    A trust/MCP gate on a freshly-created session is auto-answered BEFORE any
    hook-marker short-circuit in :func:`_agent_activity`. pre-trust normally
    prevents the gate, but the shared ``~/.claude.json`` can be clobbered by a
    concurrent Claude, and a gate masked by a fresh clarify marker would
    otherwise never be dismissed — leaving a seeded (PR/ticket) prompt parked
    behind it. Bounded to :data:`_TRUST_GATE_STARTUP_WINDOW_S` so steady-state
    sessions never pay the extra capture; the dismiss itself is rate-limited
    per title. Returns True when a gate was seen (the caller reports
    'clarify'). Never raises.
    """
    try:
        if (
            created is not None
            and (time.time() - float(created)) <= _TRUST_GATE_STARTUP_WINDOW_S
        ):
            cp0 = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if cp0.returncode == 0 and srv._dismiss_trust_prompt(
                inst, title, name, cp0.stdout.decode("utf-8", "replace")
            ):
                return True
    except Exception:  # noqa: BLE001 — safety net must never break the poll
        pass
    return False


def _maybe_record_thread(provider, inst, name: str, created) -> None:
    """Bind this window to its own conversation id while it runs (throttled).

    Feeds the per-window ``resume <id>`` after a crash — several windows on one
    directory must not all resume the newest thread. Claude records via its
    hooks; codex/antigravity discover from their on-disk stores here. Throttled
    to at most once per :data:`_THREAD_RECORD_EVERY_S` per session (discovery
    stats files, and the id changes rarely). Enrichment only; never raises.
    """
    try:
        now0 = time.time()
        if now0 - _THREAD_RECORD_AT.get(name, 0.0) >= _THREAD_RECORD_EVERY_S:
            _THREAD_RECORD_AT[name] = now0
            provider.record_thread(name, inst.GetWorktreePath() or "", created)
    except Exception:  # noqa: BLE001 — thread binding is enrichment only
        pass


def _limit_on_pane(srv, name: str, provider) -> bool:
    """True when the agent pane currently shows a usage-limit banner/menu.

    A standalone capture used to see *past* a fresh clarify marker: a CLI that
    hits its usage limit often prints a limit notice with an interactive
    "❯ 1. …" menu and fires a Notification/PermissionRequest hook — which would
    otherwise read as an ordinary 'clarify' (a human gate the queue never
    overrides). Reporting it as its own 'limit' state instead lets the drain
    hold + auto-resume (escape the menu, then send). The common pane-inspection
    path reuses its own capture (see :func:`_pane_hash_activity`); this helper
    only covers the marker short-circuit. Never raises."""
    try:
        cp = srv._run_capped(
            ["tmux", "capture-pane", "-p", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode != 0:
            return False
        text = cp.stdout.decode("utf-8", "replace")
        # High-precision screen match (not the looser drain gate): a clarify
        # marker means the agent is parked, so there is no live turn to misread,
        # but the pane can still carry stray "rate limited" text we must ignore.
        return is_limit_screen(text)
    except Exception:  # noqa: BLE001 — detection must never break the poll
        return False


def _marker_is_current(provider, name: str, created) -> bool:
    """Whether the CLI's hook marker was written by the tmux session that is
    running RIGHT NOW.

    A marker is otherwise trusted on AGE alone — an ``idle`` one at any age (a
    Stop hook from two hours ago on a still-running CLI is genuinely idle), a
    ``working``/``clarify`` one inside :data:`_MARKER_TRUST_WINDOW_S`. Both
    properties have to survive. What must not survive is a marker from a
    PREVIOUS incarnation. The marker file is keyed by tmux session name, nothing
    deletes it (``_ensure_agent_session`` clears the exit marker and the thread
    marker, never this one), no provider maps a session-start event to a state,
    and it only expires after six hours. So a window closed and re-opened
    reported the dead run's state for as long as its age allowed: an ``idle``
    announced a turn that had ended before the session existed, and a
    ``working`` — from a session killed mid-turn — painted a fresh CLI as
    running for up to 45s and stamped it with work evidence it had not earned.

    Same guard, same reason, as :func:`_agent_exited`'s mtime-vs-created check.
    Unknowable inputs answer True: without a creation stamp or a marker age
    there is nothing to compare, and the long-standing behaviour is to trust the
    CLI. Claude's live ``agents --json`` path reports age ``0.0``, so it always
    passes — it is real-time by construction.

    The clock is sampled HERE rather than taken from the caller: the caller's
    ``now`` predates the probes between it and this call (a pane capture, a
    ``claude agents --json`` shell-out), and subtracting a freshly-measured age
    from a stale ``now`` places the write earlier than it happened — rejecting,
    of all things, the first marker of a genuinely fresh session.
    """
    if created is None:
        return True
    try:
        age = provider.activity_state_age(name)
    except Exception:  # noqa: BLE001 — an unreadable age is not evidence
        return True
    if age is None:
        return True
    try:
        return (time.time() - float(age)) >= float(created)
    except (TypeError, ValueError):
        return True


def _idle_or_limit(srv, title: str, name: str, provider, force: bool = False) -> str:
    """``"limit"`` when a session that LOOKS idle is really parked on a
    usage-limit screen, else ``"idle"``.

    The CLI's own idle report (a Stop-hook marker) is authoritative about the
    turn having ENDED and says nothing about WHY — a turn cut short by the
    weekly cap fires the same hook as a finished one. Trusting it verbatim left
    a limited session reading "idle" forever: the badge never showed it, the
    resume watcher (which selects on ``activity == "limit"``) never saw it, and
    autopilot's usage-limit gate — written against exactly this state — was
    never armed, so the ladder committed and pushed a half-finished session.

    Throttled to one capture per :data:`_LIMIT_RECHECK_S` per session, because
    the marker path exists precisely to avoid a pane capture on every poll.

    ``force`` bypasses the throttle for one capture. The caller passes it on
    the working→idle marker TRANSITION — the first idle reading after a turn —
    because that is exactly where the throttle used to open a blind window: a
    cached "no banner" verdict taken before the turn started was served for up
    to 15s after a limit cut the turn short, and if the banner left the pane
    inside that window (a keypress, the prompt redrawing at reset time) the
    limit was never observed at all — the cut-short turn's work evidence
    stayed armed and a "has finished" went out for abandoned work. Probing
    fresh at the moment the turn ends closes the window at the moment it
    opens. Cost: one forced capture per caller that observes the transition —
    the first _verdict flips ``reported`` to idle, so steady-state polls never
    re-force, but the uncached drain/autopilot probes and any memo-missing
    poller landing inside the first capture's window each force their own
    (bounded by caller count, a handful at worst).
    Never raises."""
    now = time.time()
    rec = _LIMIT_PROBE.get(title)
    stale = rec is None or now - float(rec.get("at") or 0.0) >= _LIMIT_RECHECK_S
    if force or stale:
        if _limit_on_pane(srv, name, provider):
            # "Banner seen" always wins and always caches — the safe direction.
            _LIMIT_PROBE[title] = {"at": now, "limit": True}
            return "limit"
        if stale:
            _LIMIT_PROBE[title] = {"at": now, "limit": False}
        else:
            # A FORCED miss must not buy the throttle a fresh 15s: the one
            # forced capture can race the CLI's banner paint (the Stop-hook
            # marker write and the redraw are independent) or fail outright
            # (_limit_on_pane answers False on any capture error), and the
            # fast marker-tier dwell (12s) expires INSIDE the throttle window
            # — announcing a limit-cut turn as finished before any re-probe
            # could catch it. Zeroing the stamp makes the NEXT poll re-probe
            # at tick cadence instead: one extra capture per turn end, and a
            # late-painted banner is seen ~4s later, well inside every dwell.
            # Never clobber a concurrent capture that DID see the banner.
            cur = _LIMIT_PROBE.get(title)
            if not (cur and cur.get("limit")):
                _LIMIT_PROBE[title] = {"at": 0.0, "limit": False}
        return "idle"
    return "limit" if rec.get("limit") else "idle"


def _pane_hash_activity(
    srv, inst, title: str, name: str, provider, pane_pid, pane_size
) -> str:
    """Layers 3-4 of :func:`_agent_activity`: classify from the live pane.

    Reached only once the authoritative signals (exit marker, hook marker,
    process tree) are inconclusive. Captures the pane and decides:

    * First sighting of this PANE -> classify from what one frame can prove
      (limit banner / interrupt hint) and otherwise report 'idle'; never a
      guess. See the seeding block for why.
    * A pane RESIZE (a web tab attaching reflows tmux) rebaselines the hash /
      CPU / token counter and keeps the PRIOR state — clicking a tab must not
      read as activity.
    * Primary signal: pane process-tree CPU rate (jiffies/s). At or above
      :data:`_CPU_ACTIVE_JIFFIES_PER_S` -> 'working'. Below it, a visible
      waiting-prompt on a STABLE pane -> 'clarify'; otherwise a live status
      line (interrupt hint / climbing token counter, Layer 4) -> 'working'
      even at ~0 CPU (extended thinking, work runs server-side); else keep the
      prior state until it has been quiet for :data:`_ACTIVITY_IDLE_AFTER`
      seconds, then 'idle'.
    * Fallback where /proc CPU is unavailable (e.g. macOS): pane-hash
      hysteresis — a stable waiting-prompt -> 'clarify'; a live status line ->
      'working'; a pane changing on :data:`_ACTIVITY_CONFIRM_POLLS` consecutive
      polls -> 'working'; a pane static for :data:`_ACTIVITY_IDLE_AFTER`
      seconds -> 'idle'.

    A visible trust/MCP gate seen here is auto-answered (F2) and reported
    'clarify'. Returns 'offline' when the pane capture fails.
    """
    # Layer 3: hardened pane-hash fallback.
    cp = srv._run_capped(
        ["tmux", "capture-pane", "-p", "-t", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if cp.returncode != 0:
        return "offline"
    text = cp.stdout.decode("utf-8", "replace")
    # F2 safety net: a visible trust/MCP gate is auto-answered and shown
    # as clarify (pre-trust normally prevents it from appearing at all).
    if srv._dismiss_trust_prompt(inst, title, name, text):
        return "clarify"
    h = _normalized_pane_hash(text)
    now = time.time()
    cpu_now = srv._pane_cpu_jiffies(pane_pid)
    rec = _activity_record(title, None)
    if rec.get("hash") is None:
        # FIRST SIGHTING OF THIS PANE — classify from the evidence one frame
        # can actually carry, and never from optimism.
        #
        # This used to seed 'working' outright ("a fresh agent usually is"),
        # holding the pane text and the CPU counter and consulting neither.
        # That guess was the phantom behind "I click an offline session and it
        # goes running for a moment and then idle": the record is rebuilt from
        # scratch whenever a session's tmux comes back (clicking a pane
        # relaunches the agent through `_ensure_agent_session`), so the guess
        # fired on a CLI that had been handed nothing to do, held the badge at
        # 'running' for _ACTIVITY_IDLE_AFTER seconds, and then decayed — a
        # working->idle pair the agent never earned, and a "finished" the user
        # had no reason to be told about.
        #
        # Two of the four signals need two samples (CPU rate, a climbing token
        # counter) and so cannot speak here. The other two are single-frame
        # facts and do: a usage-limit banner, and the provider's interrupt hint
        # ("esc to interrupt") which means a turn is live right now. A pane
        # showing neither is, on all available evidence, parked — so we say so.
        # 'clarify' is deliberately NOT attempted: the established path only
        # matches a waiting prompt on a STABLE pane, and one frame cannot
        # establish stability. The next poll catches it 4s later.
        tokens = _parse_progress_tokens(text, provider)
        wpats = provider.working_pane_patterns()
        if is_limit_screen(text):
            state = "limit"
        elif wpats and any(re.search(p, text) for p in wpats):
            state = "working"
        else:
            state = "idle"
        rec.update(
            {
                "hash": h,
                "changed": now,
                "state": state,
                "streak": 0,
                "size": pane_size,
                "cpu": cpu_now,
                "cpu_at": now,
                # Only a session we have PROOF is busy gets an idle-settle
                # clock; absent it, `busy_at` stays unset and the hysteresis
                # below falls back to `changed`, which is now — so a pane that
                # stays quiet simply stays idle.
                "busy_at": now if state == "working" else None,
                # Same evidence, different question: busy_at runs the idle
                # hysteresis, hard_since runs the announce gate.
                "hard_since": now if state == "working" else None,
                # The only single-frame route to "working" here is the
                # provider's own interrupt hint, which is turn-specific proof.
                "proof": "status" if state == "working" else None,
                # Baseline the turn-token counter now so the NEXT poll can
                # already tell a climb (work) from a flat counter (idle).
                "tokens": tokens,
            }
        )
        return state
    # A pane RESIZE (opening/focusing the session in the web UI attaches a
    # terminal, which resizes tmux and reflows the pane) changes the captured
    # text without the agent doing anything. Rebaseline hash + CPU and DON'T
    # count it as activity, so clicking a tab can't flip idle -> working.
    if pane_size is not None and rec.get("size") not in (None, pane_size):
        rec["size"] = pane_size
        rec["hash"] = h
        rec["streak"] = 0
        rec["cpu"] = cpu_now
        rec["cpu_at"] = now
        # Rebaseline the token counter too: a reflow can move/redraw it, and a
        # stale prior value must not read as a climb on the next poll.
        rec["tokens"] = _parse_progress_tokens(text, provider)
        return rec.get("state", "idle")
    rec["size"] = pane_size if pane_size is not None else rec.get("size")

    # Primary signal: CPU rate of the pane's process tree (jiffies/second)
    # since the last poll. Robust against idle-screen redraws — a spinner or
    # status line on a parked prompt burns no CPU.
    rate = None
    if cpu_now is not None and rec.get("cpu") is not None:
        dt = now - rec.get("cpu_at", now)
        if dt > 0:
            rate = (cpu_now - rec["cpu"]) / dt
    rec["cpu"] = cpu_now if cpu_now is not None else rec.get("cpu")
    rec["cpu_at"] = now

    changed = rec.get("hash") != h
    if changed:
        rec["hash"] = h

    # Layer 4 — status-line proof of a live turn, read from the RAW pane
    # (NOT the change-hash, which strips these very bottom lines as per-frame
    # noise). Either the provider's interrupt hint (``esc to interrupt``) or a
    # climbing token counter means the agent is working even at ~0 local CPU —
    # the fix for extended thinking, whose work runs server-side while the
    # local process blocks on a network read and looks idle to CPU alone.
    tok_now = _parse_progress_tokens(text, provider)
    tok_prev = rec.get("tokens")
    tok_climbing = tok_now is not None and tok_prev is not None and tok_now > tok_prev
    rec["tokens"] = tok_now if tok_now is not None else tok_prev
    wpats = provider.working_pane_patterns()
    status_working = tok_climbing or bool(
        wpats and any(re.search(p, text) for p in wpats)
    )

    if rate is not None:
        if rate >= _CPU_ACTIVE_JIFFIES_PER_S:
            # Real computation -> working (and remember when we last saw it).
            rec["busy_at"] = now
            rec["changed"] = now
            rec["streak"] = 0
            rec["state"] = "working"
            if not rec.get("hard_since"):
                rec["hard_since"] = now
            # A busy process tree says SOMETHING is running, not that a turn is:
            # an auto-updater, a `/clear`, a GC pause and a compile all cross
            # this line. The status line, when the provider has one, says which.
            if status_working:
                rec["proof"] = "status"
            elif not rec.get("proof"):
                rec["proof"] = "cpu"
            return "working"
        # CPU quiet. On a STABLE pane, a usage-limit SCREEN or a visible
        # waiting-prompt means the agent has parked. The limit banner is checked
        # first (it outranks the '❯ 1.' selection menu a limit screen also
        # shows) and reported as its own 'limit' state so the queue drain rides
        # the window out. Requiring a STABLE, CPU-quiet pane is what keeps a
        # genuinely working turn — or stray "rate limited" text scrolling by in
        # output — from ever being misread as a limit (the status-line proof
        # below still wins for a live turn on a changing pane).
        if not changed:
            if is_limit_screen(text):
                rec["state"] = "limit"
                return "limit"
            pats = provider.waiting_prompt_patterns()
            if pats and any(re.search(p, text) for p in pats):
                rec["state"] = "clarify"
                return "clarify"
        # CPU is quiet but the status line still shows a live turn (interrupt
        # hint / climbing token counter) -> working. This is the extended-
        # thinking case: server-side work, ~0 local CPU. Refresh busy_at so
        # the idle-settle window restarts from now.
        if status_working:
            rec["busy_at"] = now
            rec["changed"] = now
            rec["streak"] = 0
            rec["state"] = "working"
            if not rec.get("hard_since"):
                rec["hard_since"] = now
            rec["proof"] = "status"
            return "working"
        # NO pane-change promotion here, deliberately. On this branch CPU is the
        # authoritative signal and a changing pane is noise: an idle Claude
        # redraws its spinner and token counter every frame at ~8 jiffies/s, so
        # promoting on "the text moved" is precisely the bug the CPU probe was
        # added to kill. A provider whose work is invisible to both CPU and the
        # status line needs a ``working_patterns`` regex (Settings → Providers),
        # not a looser rule here.
        # Otherwise keep the prior state through brief lulls (streaming
        # pauses / model round-trips), then settle to idle after the same
        # hysteresis window — regardless of cosmetic pane churn.
        # ``or`` rather than a dict default: a pane first seen parked stores
        # busy_at=None (no proof of work to run a clock from), and the honest
        # fallback there is when the record itself started.
        busy_at = rec.get("busy_at") or rec.get("changed") or now
        if now - busy_at >= _ACTIVITY_IDLE_AFTER:
            rec["streak"] = 0
            rec["state"] = "idle"
            # The run of proven-busy polls is over. The next one starts its own
            # clock and earns its own proof, so two spikes either side of a lull
            # never add up to a turn.
            rec["hard_since"] = None
            rec["proof"] = None
        return rec.get("state", "idle")

    # Fallback (no /proc CPU, e.g. macOS): pane-hash hysteresis. A usage-limit
    # SCREEN or a visible waiting-prompt on a STABLE pane still wins first
    # (matching the CPU branch: the limit banner outranks the selection menu,
    # and both outrank a stale interrupt hint).
    if not changed:
        if is_limit_screen(text):
            rec["streak"] = 0
            rec["state"] = "limit"
            return "limit"
        pats = provider.waiting_prompt_patterns()
        if pats and any(re.search(p, text) for p in pats):
            rec["streak"] = 0
            return "clarify"
    # The status line is the stronger positive signal where it exists — a
    # live interrupt hint / climbing token counter means working immediately,
    # even on a visually static pane (the extended-thinking case CPU can't
    # see either).
    if status_working:
        rec["changed"] = now
        rec["streak"] = 0
        rec["state"] = "working"
        if not rec.get("hard_since"):
            rec["hard_since"] = now
        rec["proof"] = "status"
        return "working"
    if changed:
        rec["changed"] = now
        rec["streak"] = rec.get("streak", 0) + 1
        if rec.get("state") != "working" and rec["streak"] >= _ACTIVITY_CONFIRM_POLLS:
            rec["state"] = "working"
            if not rec.get("hard_since"):
                rec["hard_since"] = now
            # "The text keeps moving" is the weakest evidence there is; it ranks
            # with CPU, never above it.
            if not rec.get("proof"):
                rec["proof"] = "cpu"
    else:
        rec["streak"] = 0
        if now - rec.get("changed", now) >= _ACTIVITY_IDLE_AFTER:
            rec["state"] = "idle"
            rec["hard_since"] = None
            rec["proof"] = None
    return rec.get("state", "idle")


def _agent_activity(inst, title: str) -> str:
    """'working' | 'clarify' | 'limit' | 'idle' | 'offline' for the agent's tmux
    session ('limit' = a usage-limit banner/menu is on screen).

    Layered, most-authoritative first:

    0. exit marker — the launch wrapper recorded the agent command ending
       inside the current tmux session -> idle (session gone -> offline);
    1. provider activity marker — the CLI's own hook-reported state (Claude
       Code Stop/UserPromptSubmit/Notification hooks, A2) -> returned as-is
       (above the shell heuristic on purpose: the CLI's own report outvotes
       what the pane's foreground command looks like). An ``idle`` marker is
       first checked for belonging to the CURRENT tmux incarnation
       (:func:`_marker_is_current`) and then re-checked against the pane for a
       usage-limit screen: the hook fires identically whether the turn ENDED or
       was CUT OFF by the account's limit (:func:`_idle_or_limit`);
    2. process tree — a bare shell holds the pane AND no non-shell descendant
       is alive -> the agent isn't running -> idle. A wrapper shell (the
       provisioned launcher script) with a live ``claude`` child falls through
       to pane inspection instead of being misread as idle (F1);
    3. pane-hash fallback with hysteresis — noise-stripped hash; a pane must
       change on ``_ACTIVITY_CONFIRM_POLLS`` consecutive polls to flip
       idle -> working, and must stay static ``_ACTIVITY_IDLE_AFTER`` seconds
       to fall back to idle (asymmetric on purpose: working is sticky).
       'clarify' (provider confirmation prompt on screen) is only reported on
       a STABLE pane, so a scrolling numbered list mid-generation can't match.
       A visible trust/MCP gate is auto-answered (F2) and reported 'clarify'.
    4. status-line proof — before CPU-quiet settles to idle, the provider's
       ``working_pane_patterns`` (Claude's ``esc to interrupt`` hint) and a
       climbing ``progress_token_pattern`` counter are read from the RAW pane.
       Either one means the turn is still live even at ~0 CPU — the fix for
       extended thinking (work runs server-side, so the local process blocks on
       a network read and looks exactly like an idle prompt to CPU alone).

    Every layer returns through :func:`_verdict`, which stamps the session's
    rolling record with what was reported and when — and, for ``working``, that
    the agent was OBSERVED doing something in this tmux incarnation. That trail
    is what lets the notification layer distinguish a turn that ended from a
    session that has simply never run one; see ``server._note_turn_boundary``.
    The two ``offline`` returns that precede :func:`_pane_meta` (not started /
    paused, and no tmux session) record nothing on purpose: there is no
    incarnation to attribute a reading to, and every consumer of
    ``activity_since`` already gates on a live state.
    """
    try:
        srv = _server()
        if not inst.Started() or inst.Status == session.Paused:
            return "offline"
        name = tmux.to_mindflock_tmux_name(title)
        exists = (
            srv._run_capped(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
        if not exists:
            # NOT a cache pop. The record used to be dropped here on every
            # offline poll, which guaranteed that the moment the session came
            # back the pane layer had no history and fell into its
            # first-sighting branch — the phantom "running" flash. A record
            # belongs to a tmux INCARNATION, and `_activity_record` retires it
            # against tmux's own `session_created` when a genuinely new one
            # appears; a dead session simply has no reading to report.
            return "offline"
        fg, created, pane_pid, pane_size = srv._pane_meta(name)
        now = time.time()
        rec = _activity_record(title, created)
        # Layer -1 (F2 safety net, startup only): auto-answer a trust/MCP gate
        # on a freshly-created session BEFORE any hook-marker short-circuit
        # below, so a seeded (PR/ticket) prompt can't sit parked behind it.
        if _startup_trust_gate_clarify(srv, inst, title, name, created):
            return _verdict(rec, "clarify", now, source="trust")
        # Layer 0: the agent command ENDED (exit marker written this session).
        if srv._agent_exited(name, created):
            return _verdict(rec, "idle", now, source="exit")
        # Layer 1: the CLI's own report (Claude Code hook marker).
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        # Bind this window to its own conversation id while it runs (throttled)
        # so a per-window `resume <id>` after a crash targets the right thread.
        _maybe_record_thread(provider, inst, name, created)
        marked = provider.activity_state(name)
        # A marker written before this tmux session existed describes a process
        # that no longer does — WHATEVER state it names. Discarded once, here,
        # rather than per-branch: a stale ``working`` is every bit as wrong as a
        # stale ``idle``, and worse in one way, because it is the reading that
        # stamps the work evidence a turn-end announcement is later built on.
        # (Relaunching clears the exit marker and the thread marker and not this
        # one; see :func:`_marker_is_current`.)
        if marked and not _marker_is_current(provider, name, created):
            marked = None
        if marked == "idle":
            # Stop hook fired -> the turn definitively ended; trust at any age
            # WITHIN THIS INCARNATION (see :func:`_marker_is_current`).
            # WHY it ended is another question: a turn the account's usage limit
            # cut short fires the same hook as a completed one, so the pane is
            # checked before this idle is passed on. The check is throttled in
            # steady state and FORCED fresh on the working→idle transition —
            # ``rec["reported"]`` still holds the previous verdict here, since
            # _verdict runs after _idle_or_limit — because the first idle after
            # a turn is exactly where a cached pre-turn "no banner" verdict
            # used to hide a limit that cut the turn short (and, if the banner
            # then left the pane inside the throttle window, hide it forever).
            # See :func:`_idle_or_limit`.
            reading = rec.get("reading") or ()
            ended_a_turn = (
                len(reading) == 2
                and reading[0] in ("working", "clarify")
                # A trust-gate clarify precedes any turn — a fresh session
                # resolving its gate to idle ended nothing worth a capture.
                and reading[1] != "trust"
            )
            return _verdict(
                rec,
                _idle_or_limit(srv, title, name, provider, force=ended_a_turn),
                now,
                source="marker",
            )
        if marked in ("working", "clarify"):
            # working/clarify markers only refresh on hook events (tool call,
            # prompt submit, notification). A long thinking/generating stretch
            # between tool calls, or an abandoned prompt, leaves the marker
            # stale — that is what makes an idle/working session read as the
            # previous state forever. Trust a FRESH marker (fast path, skips the
            # pane capture); once it goes stale, fall through to live pane
            # inspection, which reads the truth from the spinner/timer (working)
            # vs a bare idle prompt.
            age = provider.activity_state_age(name)
            if age is None or age <= _MARKER_TRUST_WINDOW_S:
                # A fresh clarify marker can mask a usage-limit menu: the CLI
                # prints a limit notice + selection box and fires a
                # Notification/PermissionRequest hook. Verify against the pane so
                # the limit claims its own 'limit' state — otherwise the drain
                # treats it as a human gate and the queue stalls behind a menu
                # that never clears on its own, even after the window reopens.
                if marked == "clarify" and _limit_on_pane(srv, name, provider):
                    return _verdict(rec, "limit", now, source="marker")
                # The CLI said so itself, so this is the one reading allowed to
                # arm a turn-end announcement — at any duration. A hook fires
                # because a prompt was submitted or a tool ran; there is no
                # cosmetic redraw or background CPU burst behind it.
                return _verdict(rec, marked, now, arms=True, source="marker")
        # Layer 2: a bare shell holds the pane AND nothing but shells lives
        # under it -> the agent isn't running, whatever the pane looks like.
        if (
            fg
            and fg.lower() in _BARE_SHELLS
            and not srv._pane_has_agent_process(pane_pid)
        ):
            return _verdict(rec, "idle", now, source="proc")
        # Layers 3-4: live-pane inspection (CPU rate + status-line proof under
        # asymmetric hysteresis, with a pane-hash fallback where /proc CPU is
        # unavailable). Kept in its own helper so the layered dispatch above
        # reads as a sequence of authoritative-first probes.
        pane_state = _pane_hash_activity(
            srv, inst, title, name, provider, pane_pid, pane_size
        )
        # What the pane looks like is enough to paint the chip. Whether it may
        # also announce that a TURN ENDED depends on what proved it busy:
        #
        # * The provider's own live-turn status line (Claude's ``esc to
        #   interrupt``, a climbing token counter) is turn-specific proof — it
        #   is on screen because a turn is running — so it arms, hooks or no
        #   hooks. That is the case where a CLI reports for itself but its
        #   marker went stale mid-turn.
        # * CPU alone does not. A busy process tree means something is running,
        #   not that a turn is: a parked Claude session's own auto-updater
        #   crossed `_CPU_ACTIVE_JIFFIES_PER_S` for ONE 4s poll, read as
        #   ``working`` for ~12s — past the announce path's flicker settle — and
        #   45s later announced a turn its transcript shows never happened.
        # * A CLI that does NOT report for itself keeps a CPU backstop, because
        #   a status-line regex is all such a provider has and a regex can be
        #   wrong — its CLI reworded the hint, or nobody filled one in. Losing
        #   the announcement entirely to a bad pattern is worse than the spike
        #   it protects against, so an UNBROKEN busy run of
        #   `_CPU_ONLY_ARMS_AFTER_S` arms there. No backstop where the CLI does
        #   report (Claude, Codex): those have two independent signals already,
        #   and the phantom this whole ladder exists to kill was one of theirs.
        arms = False
        if pane_state == "working" and rec:
            if rec.get("proof") == "status":
                arms = True
            elif marked is None or not provider.reports_activity():
                # The backstop is denied only to a CLI whose hooks are actually
                # SPEAKING (``marked`` non-None this poll — fresh or stale, the
                # marker machinery works). ``reports_activity`` alone is a
                # config declaration: a codex build without the hooks engine,
                # or a hook install that failed silently, still declares True —
                # and denying such a session the backstop leaves it with one
                # regex as its only route to a turn-end, forever.
                since = rec.get("hard_since")
                arms = bool(since) and (now - float(since)) >= _CPU_ONLY_ARMS_AFTER_S
        return _verdict(rec, pane_state, now, arms=arms, source="pane")
    except Exception:  # noqa: BLE001
        return "offline"


# Lines of our own commit plumbing (the typed command echo and its marker
# files) and shell prompts — never useful as a "what failed" detail (K3).
_COMMIT_PLUMBING_RE = re.compile(r"\.mindflock_\w|precommit\.lock")
_SHELL_PROMPT_RE = re.compile(r"[$%#❯>]\s*$")
# Fallback-tail triage: a line that names a problem vs. one that names nothing.
# Pointer/path echoes ("→ ~/repo", a bare path) and letterless decoration are
# common last lines of hook output but useless as a badge.
_ERRORISH_RE = re.compile(
    r"error|fail|fatal|abort|denied|reject|blocked|forbidden|✗", re.IGNORECASE
)
_NOISE_LINE_RE = re.compile(r"^(?:[→➜↳›»▶]|->)\s|^(?:~|\.{1,2})?/\S*$")


def _parse_failed_step(text: str) -> Optional[str]:
    """Extract a failed-step detail from captured shell-pane ``text``.

    Primary: pre-commit-framework output — one line per hook ending in
    ``Passed`` / ``Failed``; returns the most recently failed hook's name.

    Fallback (K3): raw git hooks print no ``name....Failed`` lines, which used
    to leave the interrupt stage nameless. Anchor on the last commit-plumbing
    command line (the guided commit typed into the pane), then surface the last
    error-looking output line after it — or, failing that, the last line that
    isn't pointer/path noise — truncated to 80 chars. Prompts, our own marker
    plumbing, and lines that name nothing (e.g. "→ ~/repo") are never used;
    returning None (generic badge) beats a garbage detail.
    """
    lines = text.splitlines()
    failed = []
    for line in lines:
        # e.g. "ruff.....................................................Failed"
        m = re.match(r"^(.*?)\.{3,}.*\bFailed\s*$", line)
        if m:
            nm = m.group(1).strip()
            if nm:
                failed.append(nm)
    if failed:
        last = failed[-1]
        others = len([f for f in failed if f != last])
        return last if others == 0 else "%s (+%d)" % (last, others)
    # Generic fallback: last meaningful line the failing hook printed.
    start = 0
    for i, line in enumerate(lines):
        if _COMMIT_PLUMBING_RE.search(line):
            start = i + 1
    tail = []
    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue
        if _COMMIT_PLUMBING_RE.search(s) or _SHELL_PROMPT_RE.search(s):
            continue
        tail.append(s)
    last = next((s for s in reversed(tail) if _ERRORISH_RE.search(s)), None)
    if last is None:
        last = next(
            (
                s
                for s in reversed(tail)
                if not _NOISE_LINE_RE.match(s) and re.search(r"[A-Za-z]", s)
            ),
            None,
        )
    if last is None:
        return None
    return last if len(last) <= 80 else last[:77] + "..."


_HOOK_ID_RE = re.compile(r"^\s*-\s*hook id:\s*(\S+)\s*$")
_HOOK_FAILED_RE = re.compile(r"^(.*?)\.{3,}.*\bFailed\s*$")


def _parse_failed_hook_id(text: str) -> Optional[str]:
    """The pre-commit hook ID that failed, from captured shell-pane ``text``.

    A SEPARATE parse from :func:`_parse_failed_step`, and not a refinement of it,
    because the two answer different questions and only one of them can key a
    decision. ``failed_step`` is the hook's user-editable display ``name:`` — for
    real configs that is "Black format", "Secret scan", "GitNexus index" against
    the IDs ``black``, ``detect-secrets``, ``gitnexus-index``. "Black format"
    does not slugify to ``black``, so a display name can never be matched against
    an allowlist of IDs or handed to ``SKIP=``.

    The ``- hook id:`` line is the reliable key: pre-commit prints it on every
    failure regardless of the hook's ``verbose`` setting (its run.py emits it
    when ``verbose or hook.verbose or retcode or files_modified``, and a failure
    means a non-zero retcode).

    Anchored at or after the last ``name.....Failed`` line so a hook id printed
    earlier in the run — by a hook that passed verbosely — is never mistaken for
    the failing one. Returns None when the pane holds no usable id (raw git hooks
    print none at all).
    """
    lines = text.splitlines()
    anchor = -1
    for i, line in enumerate(lines):
        if _HOOK_FAILED_RE.match(line):
            anchor = i
    if anchor < 0:
        return None
    found = None
    for line in lines[anchor:]:
        m = _HOOK_ID_RE.match(line)
        if m:
            found = m.group(1)
    return found


def _capture_shell_pane(title: str, lines: int = 400) -> Optional[str]:
    """The session's shell tmux pane as text, or None.

    ``-J`` joins tmux's wrapped lines (a long error captured one-line-per-pane-row
    breaks every tail heuristic downstream). Never raises.
    """
    try:
        name = _server()._shell_tmux_name(title)
        cp = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", "-%d" % int(lines), "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _failed_precommit_hook(title: str) -> Optional[str]:
    """The failing pre-commit hook's ID (see :func:`_parse_failed_hook_id`).

    Captures a 2000-line window rather than the 400 used for the display name:
    the ``- hook id:`` line is printed BEFORE the hook's own output, so a long
    pytest report or a verbose formatter can easily push it more than 400 rows
    back. ``_ensure_shell_session`` sets tmux's history-limit to 100000, so the
    wider window costs nothing.
    """
    return _parse_failed_hook_id(_capture_shell_pane(title, 2000) or "")


def _failed_precommit_step(title: str) -> Optional[str]:
    """Best-effort detail of the commit hook that failed, from the shell pane.

    The guided commit runs in the session's shell tmux; the pane is captured
    and parsed by :func:`_parse_failed_step` (pre-commit framework hook names
    first, generic raw-hook output fallback). Returns None if nothing useful
    was found. Never raises.
    """
    try:
        name = _server()._shell_tmux_name(title)
        cp = subprocess.run(
            # -J joins tmux's wrapped lines. Without it a long error is captured
            # as one line per PANE ROW, so the generic fallback below — which
            # takes the LAST error-looking line — surfaced the tail of a wrapped
            # line and the badge read as a mid-word fragment ("ffic-reduction'."
            # out of "…'traffic-reduction'."). tmux.py and server.py already
            # capture with -J for the same reason.
            ["tmux", "capture-pane", "-p", "-J", "-S", "-400", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode != 0:
            return None
        return _parse_failed_step(cp.stdout.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _handle_branch_drift(srv, inst, wt: str, live: str) -> None:
    """Reset branch-scoped flow state when the worktree's branch changes.

    A workspace used for several branches in series must not carry one
    branch's flow leftovers (a failed-commit status marker, cached PR /
    origin-SHA / base-branch / diff answers) onto the next — that is what
    made the stage pill stick on "interrupt"/"committed" from previous work.
    Never raises; every step is idempotent.
    """
    if not live:
        # Detached HEAD / non-repo / git error: can't judge drift — leave the
        # last observation alone so A -> detached -> A is not a false drift.
        return
    title = getattr(inst, "Title", "") or ""
    prev = _LAST_BRANCH.get(title)
    stored = getattr(inst, "Branch", "") or ""
    drifted = (prev is not None and live != prev) or (
        # First observation (fresh server): trust the stored branch as the
        # baseline so a pre-restart switch is still caught.
        prev is None
        and bool(stored)
        and live != stored
    )
    if drifted:
        # The last commit attempt's exit status belonged to the old branch's
        # flow; without this a failure there re-fires "interrupt" on any
        # dirty tree here. (The precommit lock and commit-msg files are
        # transient / reused deliberately — left alone.)
        try:
            os.unlink(os.path.join(wt, ".mindflock_commit_status"))
        except OSError:
            pass
        for b in {prev, live, stored}:
            if b:
                try:
                    srv._PR_CACHE.pop(b, None)
                    srv._ORIGIN_SHA_CACHE.pop((wt, b), None)
                except Exception:  # noqa: BLE001 — cache shape is server-owned
                    pass
        try:
            srv._BASE_BRANCH_CACHE.pop(wt, None)
            srv._DIFF_STAT_CACHE.pop(wt, None)
        except Exception:  # noqa: BLE001
            pass
        # In-place sessions live in the user's real repo where branches come
        # and go — keep the displayed branch (and the merge fallback) live.
        # Managed worktree/provisioned sessions keep their stored identity.
        if getattr(inst, "InPlace", False):
            inst.Branch = live
    _LAST_BRANCH[title] = live


# A commit runs its whole `touch lock; git add; git commit; …; rm lock` chain
# inside the SHELL tmux pane, so the lock is only removed if that chain runs to
# the end. Cancel it (Ctrl+C) or kill the process and the `rm` never fires — the
# lock lingers and the session wedges in the "precommit" stage forever, whose
# guided button is disabled. Treat the lock as stale (and clean it up) once the
# shell is back at its prompt with no non-shell descendant running: the commit is
# no longer in flight, so the button should light up again as a re-commit.
#
# BUT the liveness probe must not fire during the commit's own startup: the chain
# is sent into the shell a beat before `git` actually forks, so the first poll
# after Commit… (the endpoint drops the probe memo, forcing a fresh read) can
# catch the pane still at its bare prompt and read "not live" — which would DELETE
# the just-touched lock and drop the stage to "idle" for the whole commit. So a
# freshly-touched lock (younger than this grace) is always treated as live; only
# an older lock whose shell has gone quiet is the genuinely-abandoned case.
_PRECOMMIT_LOCK_GRACE_S = 15.0

# A live commit's liveness probe is not perfectly reliable: `_precommit_lock_is_live`
# reads False on ANY transient hiccup (a tmux/`ps` call timing out under load, a
# momentary bare-shell instant between hooks) — its `except -> False`. A long
# single `git commit` (e.g. a Claude-backed docs/tests hook running for minutes)
# only re-touches the lock at while-loop boundaries, so ONE unlucky not-live read
# used to be enough to delete the lock and strand the stage pill on "idle" for the
# rest of the commit. So a stale lock must be observed not-live *continuously* for
# this long before we treat it as genuinely abandoned (cancelled / killed) and
# self-heal it; transient blips keep the "pre-commit" pill up.
_PRECOMMIT_LOCK_STALE_HEAL_S = 25.0
# title -> epoch when this lock was FIRST seen stale-and-not-live (reset the moment
# it reads live again, or the lock is gone). Drives the debounce above.
_LOCK_STALE_SINCE: Dict[str, float] = {}


def _precommit_lock_is_live(title: str) -> bool:
    """True while a commit is actually running in ``title``'s shell pane.

    Missing shell session, an idle bare-shell prompt, or any tmux failure ->
    False (stale lock). Conservative on the "still running" side only when we can
    positively see a non-shell process (git / pre-commit / a hook) alive."""
    srv = _server()
    try:
        name = srv._live_session_name(srv._shell_tmux_name(title))
        if name is None:
            return False  # shell gone -> whatever it was doing is over
        fg, _created, pane_pid, _size = srv._pane_meta(name)
        # A non-shell foreground (git/python/node/…) is the commit chain running.
        if fg and fg.lower() not in _BARE_SHELLS:
            return True
        # Bare-shell foreground: live only if a non-shell descendant survives
        # (e.g. a hook backgrounded a helper). Otherwise we're at the prompt.
        return srv._pane_has_agent_process(pane_pid)
    except Exception:  # noqa: BLE001 — liveness is best-effort; err toward stale
        return False


def _commit_shell_live(inst, wt: str) -> bool:
    """True if the commit lock for ``wt`` is backed by a live commit in ANY
    session that shares it — not only ``inst``'s own shell.

    The lock lives in the worktree's git dir, so every session pointing at the
    SAME worktree path shares one lock (linked worktrees get distinct
    ``.git/worktrees/<name>`` dirs, so they do not). Without this, a sibling
    session sitting idle on the same worktree runs its own liveness probe against
    its quiet shell, reads "not live", and self-heals away the lock that ANOTHER
    session's commit is actively holding — dropping the committing session's pill
    to "idle". So we treat the lock as live when the polling session OR any
    same-worktree sibling shows a running commit. Own shell is checked first (the
    common single-session case stays a single probe); never raises."""
    srv = _server()
    self_title = getattr(inst, "Title", None)
    order = [self_title] if self_title else []
    try:
        for other in list(srv.ENGINE.instances.values()):
            if other is inst:
                continue
            try:
                if other.GetWorktreePath() != wt:
                    continue
                t = getattr(other, "Title", None)
            except Exception:  # noqa: BLE001 — one bad instance must not abort
                continue
            if t and t not in order:
                order.append(t)
    except Exception:  # noqa: BLE001 — engine snapshot is best-effort
        pass
    for t in order:
        try:
            # Via the server module (not the bare name) so it resolves through
            # the same reference the rest of the stage code — and the tests —
            # patch.
            if srv._precommit_lock_is_live(t):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# Legacy location: the lock used to live at the WORKTREE ROOT, but an ignored
# scratch file there does not survive a long commit whose hooks run in-tree
# tooling (``git add -A`` / ``git clean -fdx`` / nested agents operating in the
# worktree) — it gets swept, ``_session_stage`` loses its only "commit running"
# signal, and the stage pill wrongly falls through to "idle". The lock now lives
# inside the worktree's PRIVATE git dir, which is never staged, never cleaned,
# and never traversed by worktree-local tooling.
_LEGACY_PRECOMMIT_LOCK = ".mindflock_precommit.lock"


def _precommit_lock_path(wt: str) -> str:
    """Absolute path to the commit lock, inside ``wt``'s private git dir so a
    running commit's own hooks (and any agent working in the tree) can't sweep
    it. Falls back to the legacy worktree-root path when the git dir can't be
    resolved (e.g. ``wt`` is not a repo)."""
    try:
        cp = _server()._run_capped(
            ["git", "-C", wt, "rev-parse", "--absolute-git-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode == 0:
            git_dir = cp.stdout.decode("utf-8", "replace").strip()
            if git_dir:
                return os.path.join(git_dir, "mindflock_precommit.lock")
    except Exception:  # noqa: BLE001 — resolution is best-effort
        pass
    return os.path.join(wt, _LEGACY_PRECOMMIT_LOCK)


def _clear_precommit_locks(wt: str) -> None:
    """Remove the pre-commit lock, best-effort. Clears BOTH the git-dir lock and
    any lingering legacy worktree-root lock left by an older build."""
    for path in (_precommit_lock_path(wt), os.path.join(wt, _LEGACY_PRECOMMIT_LOCK)):
        try:
            os.unlink(path)
        except OSError:
            pass


def _precommit_lock_active(inst, wt: str) -> bool:
    """True while ``wt``'s commit lock should hold the stage pill at 'precommit'.

    Encapsulates the lock's liveness + self-heal debounce so :func:`_session_stage`
    reads as a plain stage ladder:

    * No lock -> not active (and any prior stale timer is forgotten so a future
      commit starts clean).
    * A freshly-touched lock (younger than :data:`_PRECOMMIT_LOCK_GRACE_S`) or a
      genuinely live commit shell -> active; the debounce timer is cleared.
    * Stale AND not-live: never healed on a single reading (the liveness probe
      reads False on any transient tmux/ps hiccup). The 'precommit' pill is kept
      until the not-live condition has PERSISTED for
      :data:`_PRECOMMIT_LOCK_STALE_HEAL_S`; only then is the lock treated as
      abandoned (cancelled / killed), cleared, and reported inactive.
    """
    lock_path = _precommit_lock_path(wt)
    if not os.path.exists(lock_path):
        # No lock -> nothing to debounce; forget any prior stale timer so a
        # future commit starts clean.
        _LOCK_STALE_SINCE.pop(inst.Title, None)
        return False
    # Only stay in "precommit" while the commit is genuinely running in the
    # shell. If the user cancelled it (Ctrl+C) or killed the process, the
    # chain's own `rm -f lock` never fired and the lock lingers — self heal it
    # so the guided button lights up again instead of wedging on a disabled
    # "pre-commit" chip. (The status file was never written on a cancel, so the
    # caller falls through to the git-based agent/committed check.)
    #
    # A freshly-touched lock is always live (grace window): the commit chain
    # forks `git` a beat after the lock appears, so the liveness probe would
    # otherwise race the startup and delete the lock, dropping the whole commit
    # to "idle" instead of "pre-commit". Only an older lock over a quiet shell
    # is the abandoned case worth self-healing.
    try:
        lock_age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        lock_age = _PRECOMMIT_LOCK_GRACE_S + 1.0  # can't stat -> not fresh
    if lock_age < _PRECOMMIT_LOCK_GRACE_S or _commit_shell_live(inst, wt):
        # Fresh or genuinely live -> commit is running. Clear the debounce
        # timer so a later blip starts counting from scratch.
        _LOCK_STALE_SINCE.pop(inst.Title, None)
        return True
    # Stale AND not-live on THIS read. Do not delete the lock on a single
    # not-live reading — the probe fails False on any transient tmux/ps hiccup
    # or momentary bare-shell instant between hooks, and a long commit would
    # then fall to "idle" for the rest of its run. Keep the "pre-commit" pill up
    # until the not-live condition has PERSISTED past the debounce window; only
    # then is the lock genuinely abandoned (cancelled / killed) and safe to
    # self-heal.
    now = time.time()
    since = _LOCK_STALE_SINCE.get(inst.Title)
    if since is None:
        _LOCK_STALE_SINCE[inst.Title] = now
        return True
    if now - since < _PRECOMMIT_LOCK_STALE_HEAL_S:
        return True
    _LOCK_STALE_SINCE.pop(inst.Title, None)
    _clear_precommit_locks(wt)
    return False


def _session_stage(inst) -> dict:
    """Best-effort workflow stage for the left-rail badge + guided buttons.

    provisioning -> agent -> precommit -> committed -> pushed -> pr.
    pushed/pr are exact (git upstream / gh); precommit is the UI commit lock;
    agent/committed are inferred from git. Never raises.

    ``stage_reset`` rides alongside (never instead of) the stage: it is the
    owner's "put this window back to idle" pin, which only the UI's guided
    ladder honours — see :mod:`backend.web.core.stage_reset`.
    """
    res = {"stage": "agent", "pr_url": None, "stage_reset": False}
    try:
        srv = _server()
        if not inst.Started():
            res["stage"] = "provisioning" if inst.Status == Loading else "agent"
            return res
        wt = inst.GetWorktreePath()
        if not wt or not os.path.isdir(wt):
            return res
        # Branch drift first: the interrupt path below returns early, so a
        # stale status marker must be cleared before it is consulted.
        live_branch = srv._current_branch(wt)
        _handle_branch_drift(srv, inst, wt, live_branch)
        # Commit lock (with liveness + self-heal debounce) -> hold at precommit.
        if _precommit_lock_active(inst, wt):
            res["stage"] = "precommit"
            return res
        # Last commit attempt was blocked by pre-commit and changes still remain
        # uncommitted -> show an interrupt stage (not "agent") so the user knows
        # to re-commit (which re-stages the auto-fixes with the same message).
        status_file = os.path.join(wt, ".mindflock_commit_status")
        if os.path.isfile(status_file):
            try:
                with open(status_file) as f:
                    last_status = f.read().strip()
            except OSError:
                last_status = "0"
            if last_status not in ("", "0"):
                if srv._is_dirty(wt):
                    res["stage"] = "interrupt"
                    res["failed_step"] = srv._failed_precommit_step(inst.Title)
                    # The display name above is for humans; the hook ID is what
                    # the fast-track retry policy can actually match on.
                    res["failed_hook"] = srv._failed_precommit_hook(inst.Title)
                    return res
                # Clean tree: the failure is history (the user committed or
                # stashed past it) — self-heal so it can't re-fire later.
                try:
                    os.unlink(status_file)
                except OSError:
                    pass
        base = srv._session_base_branch(inst)
        # Key push/PR detection off the branch the worktree is actually on, so
        # the stage chip and Make PR target the same (current) branch.
        branch = live_branch or inst.Branch or ""
        beyond = srv._commits_beyond_base(wt, base)
        # Only ask gh once there are commits (bounded + cached). PR detection is
        # by head branch alone (not base): make-pr can target any base — a
        # configured default like `staging`, or one picked in the dialog the
        # server never learns — so filtering the lookup by the fork-point would
        # miss the real PR and wedge the chip on "pushed". `beyond` still keys
        # off the fork-point ("is there work to push"), not where the PR goes.
        info = srv._pr_info(wt, branch) if beyond > 0 else None
        # Announce a REAL PR-state transition from the lookup itself, here where
        # the answer is in hand and BEFORE the dirty-tree branch below returns
        # the stage to "agent". Subscribers used to infer "merged or closed"
        # from the stage leaving "pr", which that very return makes it do on
        # every edit — see _track_pr_state.
        _track_pr_state(getattr(inst, "Title", "") or "", branch, info)
        # Surface the PR's URL so the chip can link to it whatever its state
        # (open / merged / closed) — a PR-review session has one from the start.
        if info and info.get("url"):
            res["pr_url"] = info.get("url")

        # Only a *currently open* PR changes the workflow stage. Merged/closed PR
        # history never blocks Make PR: once new commits are pushed beyond the
        # base, "pushed" (Make PR) is offered again. "pushed" is confirmed against
        # origin — not the local @{u} tracking ref, which can be set before the
        # branch is really on the remote. So:
        #   dirty tree                         -> agent      (next: Commit)
        #   commits not confirmed on origin    -> committed  (next: Push)
        #   confirmed on origin + PR open      -> pr         (next: Merge / View PR)
        #   confirmed on origin + no open PR   -> pushed     (next: Make PR)
        if srv._is_dirty(wt) or beyond <= 0:
            res["stage"] = "agent"
            # Reality is already at the start of the ladder, so a pin has nothing
            # left to do: release it here rather than leave it to expire on a
            # later read that may never come (this branch returns early).
            _stage_reset.clear(getattr(inst, "Title", "") or "")
            return res
        local_head = srv._git_head_sha(wt)
        remote_sha = srv._origin_branch_sha(wt, branch)
        confirmed_pushed = (
            bool(remote_sha) and bool(local_head) and remote_sha == local_head
        )
        if not confirmed_pushed:
            res["stage"] = "committed"
        elif info and info.get("state") == "OPEN":
            res["stage"] = "pr"
        else:
            res["stage"] = "pushed"
        # The one place a pin can hold: a clean tree with commits beyond the base,
        # i.e. exactly the window where git says the branch is finished. `dirty` is
        # False by construction here (the branch above returned), and `local_head`
        # was measured in this same pass — so the release check costs nothing.
        res["stage_reset"] = _stage_reset.active(
            getattr(inst, "Title", "") or "", local_head, False
        )
        return res
    except Exception:  # noqa: BLE001
        return res
