"""Session state events: the *_changed diff, its settle windows, turn ends.

The poll handler and the /api/events state tick both report each session's
(status, activity, stage) here; :func:`_emit_state_changes` diffs it against
the last report and emits the ``session.*_changed`` events — flickers held
by the settle windows, everything swallowed inside the post-launch boot quiet
window. Its tail, :func:`_note_turn_boundary`, emits the notification-grade
``session.turn_ended`` once per observed work cycle, gated on human
presence (:func:`_note_human_input`, :func:`_tmux_client_input_recent`), the
queue's in-flight send and a running fast-track chain.

The snapshot, the human-input stamps and the tunables tests rebind on
``server`` (``_EVENT_SNAPSHOT``, ``_HUMAN_INPUT_AT``, ``_BOOT_QUIET_SECONDS``,
``_BOOT_MONO``, ``_ACTIVITY_SETTLE_SECONDS``, ``_TURN_END_DWELL_S`` …) are read
back through :func:`_server`, as is everything server-owned, so a patch on
``server`` (conftest zeroes the boot quiet window that way) steers this code.
server.py re-imports every name defined here.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Dict

from backend.session import tmux
from backend.web.core import agent_state as _agent_state
from backend.web.core import autopilot as _autopilot
from backend.web.core import events as _events
from backend.web.core import ownership as _ownership
from backend.web.core import pane_scroll_find as _pane_scroll_find
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import revive as _revive
from backend.web.core import sleep as _sleep
from backend.web.core import spoken as _spoken


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# Last status/activity/stage reported per title, so the poll handler and the
# /api/events state tick can emit session.*_changed events (B1/F6) by diffing.
# The lock makes diff+update atomic: the HTTP poll (worker thread) and the tick
# (to_thread) computing the same transition concurrently emit it exactly once —
# the shared snapshot IS the dedupe.
_EVENT_SNAPSHOT: Dict[str, dict] = {}
_EVENT_SNAPSHOT_LOCK = threading.Lock()

# Activity readings flap: one poll can misread a busy pane as "idle" (or
# "clarify"/"limit") and the next reads "working" again. The UI debounces its
# own *display* across 2 polls (frontend lib/stage.ts noteActivity), but events
# used to carry every raw diff — straight into ntfy pushes, desktop
# notifications, clarify toasts and shell hooks, and past the UI debounce via
# forceActivity ("authoritative push"). So transitions INTO these states are
# held until the reading has persisted this long; a flicker that reverts before
# then emits nothing at all. Transitions OUT (to "working"/"offline") stay
# instant — they are cheap to show and wrong to delay. The window is chosen
# > _PROBE_TTL (2.5s) so settling always requires a second INDEPENDENT probe,
# not the same memoized misread served twice; with the ~4s tick cadence a real
# stop settles one tick (~4s) later than it used to.
_SETTLE_ACTIVITIES = frozenset({"idle", "clarify", "limit"})
_ACTIVITY_SETTLE_SECONDS = 3.0
#: Stages that are TRANSIENT by nature (the commit's hooks running): a move
#: INTO one is held this long before it is announced, so a commit whose hooks
#: re-run (agent→precommit→agent→precommit→committed in a second — four bell
#: rows in the live run) announces only its net change. Short: only the
#: "hooks running" chip waits for it; every other stage is instant.
_TRANSIENT_STAGES = frozenset({"precommit"})
_STAGE_SETTLE_SECONDS = 2.0

# Boot quiet window: for this long after process start (a restart re-execs, so
# this is also "after every restart") the *_changed diff events are swallowed —
# the snapshot still updates, so post-window transitions diff against the truth.
# Rationale: rediscovered sessions first register as loading/offline and then
# "transition" to whatever state they were parked in before the restart —
# clarify, idle, limit — which re-announced the standing state of every session
# to every channel (ntfy, desktop, toasts, shell hooks) on every launch. Old
# news is not a transition. The UI never depended on these boot events (it
# polls /api/instances); a REAL transition inside the window is lost, which is
# the accepted cost of a quiet launch.
_BOOT_QUIET_SECONDS = 30.0
_BOOT_MONO = time.monotonic()

# A sleeping agent (core.sleep) was stopped on purpose, at the end of a turn,
# to give its memory back. Its reading is "offline" — the same as a crash —
# and "agent offline" is a default-on alarm (bell row, toast, push). So the
# emitter, the one place every channel listens to, records it as this word
# instead and announces neither the move INTO it nor the wake landing back
# at an idle prompt. Anything else out of it (a woken agent that starts
# working, or meets a dialog) is announced with ``old="sleeping"``.
_SLEEPING = "sleeping"


def _in_boot_quiet() -> bool:
    """Whether the process is still inside its post-launch quiet window."""
    srv = _server()
    return time.monotonic() - srv._BOOT_MONO < srv._BOOT_QUIET_SECONDS


# ---- the spoken line on an idle announcement ------------------------------ #
# Voice mode: when a session settles idle, the phone speaks the ``Say:`` line
# the session wrote (core.spoken.spoken_for). The words ride the two "it
# finished" events — ``session.activity_changed new="idle"`` (the chip, the
# page's instant cue) and ``session.turn_ended`` (notification-grade; the push
# body) — as ``data = {say, has_question, source}``. Each channel remembers
# the reply it last announced per session, and ``say`` is null when nothing
# newer exists: a session that goes idle again without a new reply (a /clear,
# a boot to a bare prompt, a wake, a non-Claude CLI that files no reply) must
# not re-speak its last words. Memory is per CHANNEL because the two events
# fire for the same turn, seconds to a minute apart, and both must carry it
# once. Dropped with the session (session_removal → ``_forget_spoken``).
_SPOKEN_ANNOUNCED: Dict[str, Dict[str, tuple]] = {}
_SPOKEN_ANNOUNCED_LOCK = threading.Lock()
_SPOKEN_NONE = {"say": None, "has_question": False, "source": "none"}


def _spoken_payload(title: str, channel: str) -> dict:
    """``{say, has_question, source}`` for ``title``'s idle announcement on
    ``channel`` (``"idle"`` for activity_changed, ``"turn"`` for turn_ended):
    the spoken line when a reply this channel has not announced exists, else
    ``say: null``. One small cached file read; never raises."""
    try:
        inst = _server().ENGINE.instances.get(title)
        if inst is None:
            return dict(_SPOKEN_NONE)
        sp = _spoken.spoken_for(title, inst)
    except Exception:  # noqa: BLE001 — the words are enrichment, never a gate
        return dict(_SPOKEN_NONE)
    say = sp.get("say") if isinstance(sp, dict) else None
    if not say:
        return dict(_SPOKEN_NONE)
    key = (sp.get("ts"), say)
    with _SPOKEN_ANNOUNCED_LOCK:
        seen = _SPOKEN_ANNOUNCED.setdefault(title, {})
        if seen.get(channel) == key:
            return dict(_SPOKEN_NONE)
        seen[channel] = key
    return {
        "say": say,
        "has_question": bool(sp.get("has_question")),
        "source": str(sp.get("source") or "none"),
    }


def _forget_spoken(title: str) -> None:
    """Drop ``title``'s announced-reply memory (session removal)."""
    with _SPOKEN_ANNOUNCED_LOCK:
        _SPOKEN_ANNOUNCED.pop(title, None)


def _emit_state_changes(title: str, status: str, activity: str, stage: str) -> None:
    """Diff a session's freshly computed state against the last snapshot and
    emit ``session.status/activity/stage_changed`` events on the bus. The first
    sighting only seeds the snapshot (creation is announced by its endpoint;
    created sessions are pre-seeded so their first real transition emits, F6).

    Activity transitions into :data:`_SETTLE_ACTIVITIES` are debounced: the
    candidate value parks in the snapshot's ``pending_activity`` (value,
    first-seen monotonic ts) while the snapshot keeps announcing the old
    activity, and only a reading that persists ``_ACTIVITY_SETTLE_SECONDS``
    emits. A flicker that reverts first is dropped without a trace — the whole
    point: a one-tick "idle" misread must not push a phone notification."""
    srv = _server()
    now = time.monotonic()
    if activity == "offline" and _sleep.is_sleeping(title):
        activity = _SLEEPING
    with _EVENT_SNAPSHOT_LOCK:
        prev = srv._EVENT_SNAPSHOT.get(title)
        snap = {
            "status": status,
            "activity": activity,
            "stage": stage,
        }
        if (
            prev is not None
            and activity != prev.get("activity")
            and activity in _SETTLE_ACTIVITIES
            # An AUTHORITATIVE idle/clarify skips the settle. The 3s parking
            # exists for pane-derived MISREADS — a scrolling menu matched as a
            # waiting prompt, a redraw matched as idle — and a hook marker
            # cannot misread a frame: it changes only when the CLI fires a
            # hook. Holding it 3s+a tick was pure latency on the chip and on
            # the default-on "needs your input" push. Deliberately NOT
            # "limit": both marker-branch limit reclassifications are really
            # pane facts (a capture matched against the limit patterns — see
            # _idle_or_limit / _limit_on_pane), and a single frame CAN misread
            # those (scrollback in copy-mode, agent output quoting a banner),
            # so the default-on "ran out of usage" push keeps its two-sighting
            # guard. What a marker CAN legitimately do is flip idle→working at
            # a chained turn boundary (Stop then an immediate
            # UserPromptSubmit); the skip lets that pair through where the
            # settle sometimes swallowed it — an extra chip flick per chained
            # turn, accepted for the latency. The check binds source to THIS
            # value atomically (see reading_is_authoritative) so a stale
            # memoized reading can never borrow a newer reading's authority —
            # any mismatch takes the settle, the guarded path.
            and not (
                activity in ("idle", "clarify")
                and _agent_state.reading_is_authoritative(title, activity)
            )
        ):
            pending = prev.get("pending_activity")
            if pending is None or pending[0] != activity:
                # New candidate (or the candidate changed): start settling.
                snap["activity"] = prev.get("activity")
                snap["pending_activity"] = (activity, now)
            elif now - pending[1] < srv._ACTIVITY_SETTLE_SECONDS:
                snap["activity"] = prev.get("activity")
                snap["pending_activity"] = pending
            # else: settled — snap keeps the new activity and the diff below
            # emits the transition.
        # An agent the kernel killed that is coming back by itself
        # (core.revive auto-revive): its trip through Offline is not news, so
        # the snapshot keeps announcing what it was doing. The row still reads
        # Offline. Given up on (rate limit, a skip rule), it is announced.
        if (
            prev is not None
            and activity == "offline"
            and prev.get("activity") != "offline"
            and _revive.holds_offline(title)
        ):
            snap["activity"] = prev.get("activity")
        # A move INTO a transient stage parks like a settling activity; a
        # flicker back out of it (or on through it) within the window announces
        # only the net change. Suppressed HERE, at the one emitter, so the
        # bell, toasts, ntfy and hooks all see the same single event.
        if (
            prev is not None
            and stage != prev.get("stage")
            and stage in _TRANSIENT_STAGES
        ):
            pending = prev.get("pending_stage")
            if pending is None or pending[0] != stage:
                snap["stage"] = prev.get("stage")
                snap["pending_stage"] = (stage, now)
            elif now - pending[1] < _STAGE_SETTLE_SECONDS:
                snap["stage"] = prev.get("stage")
                snap["pending_stage"] = pending
        # When the ANNOUNCED activity was adopted. Deliberately not
        # agent_state's ``state_since`` (when the raw reading changed): the
        # dwell below must measure how long we have been telling people this,
        # and the wedged-session watchdog must measure what the pane is
        # actually doing. Neither may gate the other.
        if prev is None or snap["activity"] != prev.get("activity"):
            snap["activity_at"] = now
        else:
            snap["activity_at"] = prev.get("activity_at", now)
        srv._EVENT_SNAPSHOT[title] = snap
    if prev is None:
        return
    if not srv._in_boot_quiet():
        for field, event, new in (
            ("status", "session.status_changed", status),
            ("activity", "session.activity_changed", snap["activity"]),
            ("stage", "session.stage_changed", snap["stage"]),
        ):
            old = prev.get(field)
            if old == new:
                continue
            if field == "activity" and (
                new == _SLEEPING or (old == _SLEEPING and new == "idle")
            ):
                continue  # a sleep, or a wake back to its prompt: not news
            if field == "activity" and new == "idle":
                # The turn's spoken line rides the idle announcement (voice).
                _events.BUS.emit(
                    event,
                    session=title,
                    old=old,
                    new=new,
                    data=_spoken_payload(title, "idle"),
                )
                continue
            _events.BUS.emit(event, session=title, old=old, new=new)
    # Verify — the SECOND of two triggers that write a test plan for freshly
    # pushed work. The first is the ``session.pushed`` subscriber below, fed by
    # the live_stage push watcher, which is the better signal (it knows the sha
    # actually reached origin) but is not a guarantee: watchers cap at
    # ``live_stage._MAX_WATCHERS`` (4) and expire after 180s, so a flock pushing
    # five sessions at once, or a push whose hooks run past the deadline, simply
    # never announces. The stage ladder has neither limit — it is recomputed for
    # every session on every tick — so it catches what the watcher drops.
    # Deliberately redundant, and safe precisely because it is:
    # ``test_plans.ensure_plan_for`` is idempotent per (session, branch) — and
    # per (repo, branch) across sessions, so two windows on one branch also
    # make one plan — so the loser of the race does no work at all. Cheap
    # enough to be unconditional: this fires on a stage TRANSITION, not on
    # every tick.
    if prev.get("stage") != stage and stage == "pushed":
        srv._ensure_test_plan(title)
    # Turn boundaries are decided on every pass, not only on a transition: the
    # dwell below is a duration, and the tick that satisfies it is by
    # definition one where nothing changed.
    srv._note_turn_boundary(title, snap, now)


# ---- "a turn ended" vs "the activity value is idle" ----------------------- #
# ``session.activity_changed new="idle"`` is a UI fact: paint the chip grey. It
# fires at the end of EVERY assistant turn (claude.py's _HOOK_EVENT_STATES maps
# both Stop and SessionEnd to idle), on /clear, on a session that merely booted
# to a bare prompt, and on any reading that settles to idle out of offline. It
# is the wrong fact to interrupt a human with — a ten-turn conversation
# produced ten "finished" alerts, and re-opening a closed window produced one
# for a session that had run nothing at all.
#
# ``session.turn_ended`` is the notification-grade fact, and it asserts three
# things at once: this agent was OBSERVED working in this tmux incarnation, it
# has been continuously idle since, and nothing is queued to wake it back up.
# Every "the agent is done" channel keys on THIS; nothing keys on the raw idle.
#
# The dwell is TIERED by how the work evidence was corroborated
# (``agent_state.work_evidence``), because the dwell only has to buy the
# confidence the evidence itself lacks. It used to be a flat 45s chosen as a
# blanket over three hazards — a human typing a follow-up, the queue's send
# path, autopilot's own settle — each of which is now checked EXACTLY by its
# own gate below (recent human keystrokes, the drain's sent_at grace, the
# autopilot record's state), so the time-padding survives only where the
# evidence is weak:
#
#   * "marker" (the CLI's own hook report) — 12s. The Stop hook is
#     authoritative that the turn ended; the working→idle transition also
#     forces a FRESH limit-screen probe (see agent_state._idle_or_limit), so
#     the old 15s probe-cache blind window no longer bounds this tier. 12s is
#     three 4s ticks: enough that two unsynchronised tickers plus an on-demand
#     `_republish_session` shift the answer by a tick, not by half the dwell,
#     and enough to ride out hook-chained auto-continues (a Stop followed
#     immediately by a UserPromptSubmit reads working again within a tick).
#   * "status" (the provider's live-turn status line) — 25s. The turn-END
#     detection here is itself pane-based (`_ACTIVITY_IDLE_AFTER` of static
#     pane before idle is even reported), so the dwell carries more of the
#     burden than it does for a hook CLI.
#   * "cpu" (the sustained-CPU backstop), or no recorded evidence — 45s,
#     unchanged. The weakest tier stays the slowest; that is the deal the
#     backstop was admitted on.
#
# What the dwell deliberately no longer covers, because the exact gates do:
#   * the inter-turn gap of an INTERACTIVE conversation — `_HUMAN_HOLD_S`
#     holds the announcement while keystrokes have recently reached this
#     session (terminal, /send, send-now, voice, mobile compose);
#   * the queue's pop→visible-working window — `_QUEUE_SEND_GRACE_S` holds
#     while a just-sent prompt has not yet been seen working (peek_next reads
#     None the instant the last prompt is POPPED, which is at tmux-typing
#     time, not at pickup time);
#   * fast-track — held exactly while the session's autopilot record says
#     state == "running", so the order is always "the chain decided the agent
#     was done, THEN we said so", at any dwell.
# `_ACTIVITY_SETTLE_SECONDS` (3.0) is a FLICKER filter — it can only suppress
# a reading that reverts, and a session parked at its prompt never reverts. It
# is structurally incapable of answering this question at any tier.
_TURN_END_DWELL_S = 45.0
_TURN_END_DWELL_MARKER_S = 12.0
_TURN_END_DWELL_STATUS_S = 25.0
#: Dwell per work-evidence tier; unknown/absent evidence gets the slow lane.
_TURN_END_DWELLS = {
    "marker": _TURN_END_DWELL_MARKER_S,
    "status": _TURN_END_DWELL_STATUS_S,
}

# Hold "has finished" while a human has recently typed into this session — a
# person mid-composition is about to start the next turn, and a person typing
# in the window does not need a push about the window. Stamped by
# `_note_human_input` from every human input path (both terminal websockets,
# /send, queue send-now; voice dictation and the mobile composer ride the
# terminal socket) and never by automation (queue drain, limit auto-resume and
# the trust-gate dismissal all go through `tmux send-keys`, which touches none
# of those paths). Presence only tunes TIMING: the hard gates above it
# guarantee the announcement's sentence is true, so a missed stamp (a user
# typing into tmux directly, outside the web UI) merely announces sooner, and
# an over-eager stamp (scroll wheels ride the same socket) merely announces
# later.
_HUMAN_HOLD_S = 45.0
#: Ignore terminal input for this long after a websocket attaches: tmux probes
#: a newly attached client and xterm.js auto-answers through the same data
#: path as keystrokes, so attach/reconnect bursts are not presence.
_ATTACH_PROBE_GRACE_S = 2.0
_HUMAN_INPUT_AT: Dict[str, float] = {}


def _note_human_input(title: str, person: str = "", device: str = "") -> None:
    """Record that a human just put input into ``title``'s window — per
    title for the announcement gate below, and per (title, person, device)
    for the session's ``viewers`` (core.ownership) when the caller knows
    who."""
    _server()._HUMAN_INPUT_AT[title] = time.time()
    if person:
        try:
            _ownership.note_input(title, person, device)
        except Exception:  # noqa: BLE001 — presence stamping must never fail
            pass
    # A person in the pane: a background find index there stops at once.
    try:
        _pane_scroll_find.cancel_background(tmux.to_mindflock_tmux_name(title))
    except Exception:  # noqa: BLE001 — presence stamping must never fail
        pass


def _tmux_client_input_recent(title: str, within: float) -> bool:
    """Whether any tmux client attached to ``title``'s session saw input in
    the last ``within`` seconds — the presence signal for people the web
    surfaces cannot see: a Windows Terminal tab, a raw ``tmux attach``, SSH.
    Their keystrokes never cross a MindFlock route, so without this the
    presence gate would buzz a direct-tmux conversation once per turn.

    Asked only at announce time (one ``list-clients`` per announce-eligible
    tick, after every cheaper gate has passed), never on the poll path.
    Over-counts in one known way: the web terminal is itself an attached
    client, and its terminal emulator answers tmux's attach-time device
    probes as client input — so a reconnect storm (wifi blip, laptop wake)
    reads as presence and defers announcements up to ``within``. That errs
    the direction this whole pipeline errs: late, never wrong. Best-effort:
    any failure reads as "nobody there" and the announcement proceeds.
    """
    try:
        name = tmux.to_mindflock_tmux_name(title)
        cp = _server()._run_capped(
            ["tmux", "list-clients", "-t", name, "-F", "#{client_activity}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode != 0:
            return False
        now = time.time()
        for tok in cp.stdout.decode("utf-8", "replace").split():
            try:
                if now - float(tok) < within:
                    return True
            except ValueError:
                continue
    except Exception:  # noqa: BLE001 — presence is a courtesy, never a gate
        pass
    return False


# Hold "has finished" while a queued prompt is in flight: `record_sent` POPS
# the item the moment tmux typing succeeds, so `peek_next` reads None seconds
# to minutes before the agent visibly starts the turn (or never, for a send
# that didn't take). The drain's own record carries the exact facts — `armed`
# flips back True only when the drain OBSERVES working, and `sent_at` stamps
# the send — so the gate is "sent, not yet seen working, recently": precise
# where the old flat dwell was a blanket. Past the grace with the agent never
# seen working, the send is presumed lost (the drain's own recovery for that
# is `_QUEUE_REARM_IDLE`) and the announcement falls through, exactly as the
# flat 45s used to behave.
_QUEUE_SEND_GRACE_S = 60.0


def _autopilot_running(title: str) -> bool:
    """Whether a fast-track chain is genuinely mid-flight on ``title``: its
    record reads ``running`` AND a driver still holds the lease. Bounded by
    that lease: the driver early-returns WITHOUT claiming on the paths it
    cannot step (budget lock, missing worktree), so a chain nobody is actually
    advancing goes lease-stale — "running" alone must not be able to mute or
    hold a session forever. A file read; call it after the cheaper gates."""
    run = _autopilot.get(title)
    if run is None or (run.get("state") or "") != "running":
        return False
    try:
        owner_at = float(run.get("owner_at") or 0.0)
    except (TypeError, ValueError):
        return True  # a running record we cannot read: hold, as before
    return time.time() - owner_at <= _autopilot.LEASE_STALE_S * 2


def _note_turn_boundary(title: str, snap: dict, now: float) -> None:
    """Emit ``session.turn_ended`` once per observed work cycle. Never raises.

    Called from the tail of :func:`_emit_state_changes` with the settled
    snapshot, so it sees exactly the activity the rest of the app was told
    about. Eight gates, in cheapest-first order:

    1. **The settled activity is idle.** ``limit`` is deliberately excluded — a
       turn the usage cap cut short is not a turn that ended, and the
       default-on ``usage_limit`` rule already covers it. So is ``offline``.
    2. **Boot quiet**, for the same reason the diff events observe it: a
       restart rediscovers every parked session and none of that is news.
    3. **Provenance.** ``agent_state.worked_at`` is None unless work by this
       agent was CORROBORATED in its CURRENT tmux incarnation — the CLI's own
       hook/live report, or (only for a CLI that has none) a sustained run of
       proven-busy pane polls. Two failures live here. Re-opening a window
       relaunches its CLI, and the fresh incarnation has no work behind it, so
       no amount of idling produces a "finished". And a parked session that
       merely LOOKS busy for a poll — its own auto-updater, a ``/clear``, a GC
       pause, anything that crosses the CPU threshold — moves the badge and
       arms nothing: the reading is enough to paint a chip, never to interrupt
       a human.
    4. **Dwell**, tiered by the evidence's own strength (`_TURN_END_DWELLS`
       via ``agent_state.work_evidence``) and asked of both clocks: since the
       announced activity became idle, AND since the agent was last seen
       working. They can disagree, and the second is what the sentence
       actually claims.
    5. **Nothing queued.** An exact check of the pending list. Exact about
       prompts still WAITING — the next gate covers the one already sent.
    6. **No send in flight.** The drain popped a prompt (peek reads None) but
       has not yet observed the agent working: `_QUEUE_SEND_GRACE_S` against
       the drain's own ``sent_at``/``armed`` record.
    7. **No human at the keys.** `_HUMAN_HOLD_S` since the last keystroke
       reached this session — a person mid-follow-up is not a finished run,
       and a person typing in the window does not need a push about it.
    8. **Fast-track is not mid-chain.** Held exactly while the autopilot
       record reads ``running`` — the chain announces its own outcome, and
       "finished" before its commit/push/PR lands would be mistimed at best.

    Emitting SPENDS the evidence (``agent_state.claim_work``). That, not a
    seconds window, is the real dedupe: one announcement per cycle of observed
    work, however long the session then sits there. The next ``working`` reading
    re-arms it. The claim is atomic because two 4s tickers and an on-demand
    republish all run this, and winning the claim is what grants the right to
    speak.
    """
    srv = _server()
    try:
        if snap.get("activity") != "idle" or srv._in_boot_quiet():
            return
        # Evidence label BEFORE the timestamp — the mirror of _verdict's write
        # order, so no interleaving can pair an old stamp with a newer, faster
        # label (a fresh stamp always fails the dwell; see _verdict).
        evidence = _agent_state.work_evidence(title)
        worked = _agent_state.worked_at(title)
        if worked is None:
            return
        # The tier is keyed on how the WORK was corroborated, but the fast
        # lane also needs the END to be the CLI's own word: marker-armed work
        # whose idle reading came from the PANE (the marker went stale mid-
        # turn and a scrolled-back capture read as parked) is a pane-detected
        # end wearing marker-armed evidence, and 12s of that can announce —
        # and SPEND — a turn that is still running. Mismatch takes the slow
        # lane outright.
        if evidence == "marker" and not _agent_state.reading_is_authoritative(
            title, "idle"
        ):
            evidence = ""
        dwell = _TURN_END_DWELLS.get(evidence, srv._TURN_END_DWELL_S)
        idle_for = now - float(snap.get("activity_at") or now)
        if idle_for < dwell:
            return
        # The same dwell against the EVIDENCE's own clock. The two can disagree:
        # ``activity_at`` moves only when a settled reading reaches this
        # function, while ``worked_at`` is stamped by every ``_agent_activity``
        # call — including the deliberately UNCACHED ones the prompt-queue drain
        # and the autopilot driver make on their own 5s cadences, which never
        # feed a snapshot. So a session that has been idle for ten minutes and
        # then picks up a queued prompt can have fresh work evidence while the
        # tickers are still serving "idle" out of the 2.5s probe memo, and the
        # announcement would land at the moment the agent STARTED a turn. Asking
        # both clocks closes the whole class — memo skew, snapshot lag, and the
        # two uncached probes — with one comparison. (Epoch here, monotonic
        # above; each is compared only against its own kind.)
        if time.time() - float(worked) < dwell:
            return
        if _prompt_queue.peek_next(title) is not None:
            return
        # A popped-but-not-picked-up prompt: the drain disarms on send and
        # re-arms only when it OBSERVES working, so "disarmed and recently
        # sent" is precisely the in-flight window peek_next cannot see.
        q = srv._QUEUE_STATE.get(title)
        if (
            q
            and not q.get("armed", True)
            and time.time() - float(q.get("sent_at") or 0.0) < _QUEUE_SEND_GRACE_S
        ):
            return
        if time.time() - srv._HUMAN_INPUT_AT.get(title, 0.0) < _HUMAN_HOLD_S:
            return
        if srv._tmux_client_input_recent(title, _HUMAN_HOLD_S):
            return
        # Exact, not a bigger number: while the chain runs, its own events are
        # the narrative, and its completion releases this gate. A file read,
        # but only reachable once every cheaper gate has passed — at most a
        # couple of reads per announce, not per tick. Bounded by the driver's
        # own lease: _autopilot_observe early-returns WITHOUT claiming on the
        # paths it cannot step (budget lock, missing worktree), so a chain no
        # driver is actually advancing goes lease-stale and announcements
        # resume — "running" alone must not be able to mute a session forever.
        if srv._autopilot_running(title):
            return
        # Claiming the evidence is what grants permission to speak: two
        # unsynchronised tickers can reach this line in the same instant, and
        # only one of them can win the clear.
        if _agent_state.claim_work(title) is None:
            return
        data = {"idle_for": round(idle_for, 1)}
        data.update(_spoken_payload(title, "turn"))
        _events.BUS.emit("session.turn_ended", session=title, data=data)
    except Exception:  # noqa: BLE001 — a notification never breaks the tick
        pass


def _seed_event_snapshot(title: str) -> None:
    """Seed the diff snapshot with a freshly created session's initial state
    (it registers as Loading before Start runs in the background), so the FIRST
    real transition (loading->running, offline->working, …) emits instead of
    being swallowed by first-sighting seeding above (F6)."""
    with _EVENT_SNAPSHOT_LOCK:
        _server()._EVENT_SNAPSHOT.setdefault(
            title,
            {"status": "loading", "activity": "offline", "stage": "provisioning"},
        )
