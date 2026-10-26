"""The prompt-queue drain: feed queued prompts and mail into idle agents.

:mod:`backend.web.core.prompt_queue` and :mod:`backend.web.core.mailbox` hold
the stores. This module is the always-on loop that gives them effect (started
by the server lifespan): every few seconds one decision per session, each
wrapped so a single bad session cannot stop the pass.

* :func:`_drain_one_queue` — the per-session queue drain: idle/armed gating,
  the send, consumption evidence, rebooting an agent the CLI dropped.
* :func:`_refresh_limit_state` / :func:`_live_limit_reset` — the usage-limit
  gate (pane banner + the provider's live meter) and its bounded expiry.
* :func:`_watch_limited_sessions` — resume sessions a usage reset freed.
* :func:`_drain_one_mailbox` — the inter-agent mail lane: one typed message
  per settled idle turn, held while a person is at the keys.

Anything server-owned (``ENGINE``, the agent-session and activity probes, the
emitters, the turn/human-input state) is reached lazily through
:func:`_server`, as are the names here that tests patch on ``server``
(``_refresh_limit_state``, ``_live_limit_reset``, ``_session_limited_until``,
``_drain_one_mailbox``). server.py re-imports every name defined here, so
``server._drain_one_queue`` and friends keep resolving.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Dict, Optional

from backend import log
from backend import providers
from backend import session
from backend.session import tmux
from backend.web.core import agent_io as _agent_io
from backend.web.core import agent_state as _agent_state
from backend.web.core import events as _events
from backend.web.core import mailbox as _mailbox
from backend.web.core import ports as _ports
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import sleep as _sleep
from backend.web.core import worktree_setup as _wt_setup


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# ---- Prompt-queue drain loop (M-series) ----------------------------------- #
# Feeds each session's queued prompts into its agent whenever it is idle, so a
# run keeps rolling unattended — and picks straight back up the moment usage
# returns after an outage. Runs always (not gated on browser clients): draining
# with nobody watching is the entire point.
#
# Per-session gating avoids double-sending and reboot storms:
#   * send only when activity is "idle" (finished a turn / at its prompt) and
#     the session is "armed" — armed starts True, clears on send, and re-arms
#     when we next observe "working" (the agent picked the prompt up), or on
#     other evidence the send was consumed (_send_consumed). This tracks the
#     real working→idle cycle instead of trusting a fixed cooldown.
#   * "working"/"clarify" are left alone (busy, or waiting on a human decision).
#   * "offline" for a started, non-paused session means the agent tmux died
#     (e.g. the CLI exited when usage ran out) — reboot it (resuming the
#     conversation), rate-limited by _QUEUE_REBOOT_COOLDOWN, and let a later
#     idle tick do the actual send.
_QUEUE_DRAIN_INTERVAL = 5.0
_QUEUE_SEND_COOLDOWN = 8.0  # min seconds between sends to one session
_QUEUE_REARM_IDLE = 300.0  # idle this long after a send -> the send never took; re-arm
_QUEUE_REBOOT_COOLDOWN = 30.0  # min seconds between reboots of a dead session
# Seconds "idle" must PERSIST before the first send. The activity signal (Claude's
# `claude agents --json` / Stop hook, Codex's Stop hook) flips to idle the instant a
# turn ends and is trusted at any age (no dwell in _agent_activity) — so a momentary
# Stop between turns, or the ~1s lull before the next UserPromptSubmit/PreToolUse
# re-marks "working", would otherwise let the drain fire a queued prompt prematurely.
# Requiring idle to hold this long absorbs that flicker while still resuming promptly.
#
# TIERED by the reading's source: an idle the CLI reported ITSELF (a Stop-hook
# marker, an exit marker — agent_state.reading_is_authoritative) is not a
# flicker candidate, so it takes the fast tier and the next queued prompt goes
# out one drain pass sooner. Pane-derived idle keeps the full settle: the pane
# can misread a quiet moment mid-turn, and typing into a live turn is the one
# failure this dwell exists to prevent. The premature-Stop lull the comment
# above describes is still absorbed at the fast tier — idle_since resets on any
# working sighting, and the drain's pass cadence plus this floor spans the
# ~1s Stop→UserPromptSubmit gap with margin.
_QUEUE_IDLE_SETTLE = 12.0
_QUEUE_IDLE_SETTLE_MARKER = 4.0
# After WE rebooted a dead agent, hold the queue this long regardless of what the
# activity probe says. A CLI relaunching with a large `--continue` transcript can
# spend twenty seconds on a quiet, I/O-bound start, and a quiet pane is exactly
# what the classifier now reports as idle — correctly, since nothing on that
# screen says work is happening. But typing into a CLI that has not drawn its
# input box loses the prompt, and `armed` is cleared by the send, so the retry
# only comes after _QUEUE_REARM_IDLE (five minutes). The old classifier hid this
# behind a guess ("a pane I have never seen is working"), which bought roughly
# this much grace by accident; stating it here says what is actually meant.
_QUEUE_BOOT_GRACE = 20.0
_QUEUE_STATE: Dict[str, dict] = (
    {}
)  # title -> {"armed", "sent_at", "sent_text", "rebooted_at", "idle_since"}

# Usage-limit gate (roadmap D): when the agent pane shows a "usage limit reached"
# screen, hold the queue until the limit resets instead of sending into a wall.
# Bounded + self-correcting: a detected limit sets an expiry (the parsed reset
# time, or a fallback), so it can NEVER stall the queue forever.
_LIMIT_STATE: Dict[str, float] = {}  # title -> epoch the limit is known to hold until
_LIMIT_FALLBACK = 600.0  # no reset time parsed -> retry after 10 min
_LIMIT_EXHAUSTED_PCT = 99.0  # live-meter %-used at/above which a window counts as spent


def _meter_is_about_this_session(inst) -> bool:
    """Whether the provider's usage meter describes the identity THIS session
    runs as.

    ``usage_live()`` reads whatever credentials the server process itself sees
    — the CLI's ambient login. A session pinned to an auth profile is metered
    somewhere else entirely, so the ambient reading is a different
    subscription's: trusting it would release a genuinely limited session early
    (burning the queued prompt on the limit screen) or hold a session whose own
    account still has headroom. Detection from the session's own pane text is
    unaffected and remains the gate for profiled sessions.
    """
    try:
        from backend.providers import auth_profiles

        return not auth_profiles.effective_profile_id(
            getattr(inst, "ProfileId", "") or ""
        )
    except Exception:  # noqa: BLE001 — never break the drain over settings
        return True


def _live_limit_reset(provider, now: float, inst=None):
    """Authoritative "limited until" from the provider's own usage meter
    (``usage_live()`` — for Claude that's Anthropic's OAuth usage endpoint, the
    same source as the CLI's ``/usage`` screen), independent of whatever text
    happens to be on the pane.

    Returns ``None`` when no live reading is available (callers fall back to
    the pane text), ``0.0`` when the meter says every window still has
    headroom (a lingering banner must be stale), or the epoch when the LAST
    exhausted window reopens — both the 5-hour and the weekly cap must have
    reset before a send can land.

    ``inst``, when given, scopes the reading: the meter is the *ambient*
    login's, so for a session running under an auth profile there is no live
    reading to be had and ``None`` is the honest answer (see
    :func:`_meter_is_about_this_session`)."""
    if inst is not None and not _meter_is_about_this_session(inst):
        return None
    try:
        live = provider.usage_live()
    except Exception:  # noqa: BLE001 — live usage is enrichment, never a gate
        live = None
    if not isinstance(live, dict) or not live:
        return None
    windows = [live]
    if isinstance(live.get("weekly"), dict):
        windows.append(live["weekly"])
    have_reading = False
    exhausted = False
    ends = []
    for w in windows:
        try:
            pct = float(w["percent_used"])
        except (KeyError, TypeError, ValueError):
            continue
        have_reading = True
        try:
            end = float(w.get("end") or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        if pct >= _LIMIT_EXHAUSTED_PCT:
            exhausted = True
            if end > now:
                ends.append(end)
    if ends:
        return max(ends)
    if exhausted:
        # A window reads spent but the meter gave no usable reset time
        # (``resets_at`` can be null right at exhaustion, or in a between-windows
        # payload — the same transient the fetch layer already tolerates). That is
        # NOT proof the window is open: returning 0.0 here would read as "meter
        # confirms open" and send the queued prompt straight into the wall. Hold on
        # a bounded fallback instead; the active-hold meter check releases it the
        # instant real headroom reappears, so an over-long hold self-corrects while
        # an early send eats a prompt.
        return now + _LIMIT_FALLBACK
    return 0.0 if have_reading else None


def _session_limited_until(title: str) -> float:
    """Epoch until which ``title`` is known usage-limited (0.0 = not). Read-only
    (no subprocess) so the /api/instances poll can surface a countdown cheaply.

    Non-mutating on purpose: an expired hold reports 0 (UI countdown ends) but
    the entry is LEFT for the drain's :func:`_refresh_limit_state` to see, so it
    can tell "hold I set has expired" (window reopened) apart from "brand-new
    limit" — the difference that lets the queue actually resume when the window
    reopens even if the CLI's limit banner is still on screen."""
    until = _LIMIT_STATE.get(title, 0.0)
    if until and time.time() >= until:
        return 0.0
    return until


def _refresh_limit_state(inst, title: str, name: str) -> float:
    """Read the agent pane and update/return the limited-until epoch (0 = clear).

    Only called from the drain loop right before a would-be send, so the pane
    capture happens solely when we're actually about to act."""
    srv = _server()
    now = time.time()
    try:
        cp = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        text = cp.stdout.decode("utf-8", "replace") if cp.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        text = ""
    provider = providers.resolve(getattr(inst, "Program", "") or "")
    try:
        state = provider.usage_limit_state(text, now)
    except Exception:  # noqa: BLE001 — detection must never break the drain
        state = {"limited": False, "reset_at": None}
    prev = _LIMIT_STATE.get(title, 0.0)
    if state.get("limited"):
        # An active hold for THIS limit episode is kept stable — never recompute
        # it from the pane. A relative "resets in 2h 30m" re-parses to now+2h30m
        # every 5s pass; recomputing would slide the deadline forward forever and
        # the queue would never send after the window reopened. The provider's
        # own meter may still END it early: if it now shows headroom, the window
        # reopened before our estimate did — release the queue.
        if prev > now:
            if srv._live_limit_reset(provider, now, inst) == 0.0:
                _LIMIT_STATE.pop(title, None)
                return 0.0
            return prev
        # A hold we set has already EXPIRED, yet the limit banner is still on
        # the pane (CLIs often leave the message up after the window reopens).
        # Ask the meter first: still spent -> re-arm to ITS reset instead of
        # burning a prompt on a send that lands on the limit screen. No meter,
        # or meter says open: treat the window as open so the queue sends now —
        # if the limit really persists a fresh detection re-arms on the next
        # pass (bounded retry), far better than holding forever on stale text.
        if prev:
            live = srv._live_limit_reset(provider, now, inst)
            if live and live > now:
                _LIMIT_STATE[title] = live
                return live
            _LIMIT_STATE.pop(title, None)
            return 0.0
        # Fresh detection (no prior hold): hold until the limit actually lifts.
        # The meter and the pane text usually agree; with both present take the
        # LATER one — sending early eats the queued prompt (a dud), while an
        # over-long hold self-corrects via the meter check above. A meter that
        # reads "open" is NOT trusted to skip arming: right after a limit hits,
        # the ~60s-cached reading can lag the banner.
        reset_at = state.get("reset_at")
        try:
            parsed = float(reset_at) if reset_at is not None else None
        except (TypeError, ValueError):
            parsed = None
        live = srv._live_limit_reset(provider, now, inst)
        candidates = [c for c in (parsed, live) if c and c > now]
        if candidates:
            until = max(candidates)
        elif parsed is not None:
            # Reset time already in the past (stale banner) -> window is open.
            _LIMIT_STATE.pop(title, None)
            return 0.0
        else:
            until = now + _LIMIT_FALLBACK
        _LIMIT_STATE[title] = until
        return until
    # No limit BANNER on screen — but the pane text alone isn't proof the window
    # is open. A session that runs out mid-turn usually exits and is rebooted
    # (drain's offline path) back to a FRESH idle prompt with no banner, and many
    # CLIs only reprint the limit after a submit. So consult the provider's own
    # meter, which is independent of whatever text is on the pane.
    if prev and now < prev:
        # Active hold: release early only if the meter says the window reopened.
        if srv._live_limit_reset(provider, now, inst) == 0.0:
            _LIMIT_STATE.pop(title, None)
            return 0.0
        return prev
    # No prior hold: arm one straight from the meter if a window is genuinely
    # exhausted (>= _LIMIT_EXHAUSTED_PCT used, reset still ahead), so the queue
    # holds instead of burning the queued prompt on a send that lands on the wall
    # while no banner happens to be visible. A meter that reads open — or is
    # unavailable (None) — leaves the queue free to send, exactly as before.
    live = srv._live_limit_reset(provider, now, inst)
    if live and live > now:
        _LIMIT_STATE[title] = live
        return live
    _LIMIT_STATE.pop(title, None)
    return 0.0


def _send_queued_item(title: str, name: str, nxt: dict, rec: dict, now: float) -> bool:
    """Send one queued item to the agent and record it: pop/requeue in the
    store, disarm + stamp the drain record, and emit ``session.prompt_sent``.
    Shared by the idle send path and the usage-limit auto-resume path. Returns
    whether the send landed (False leaves the item in place to retry).

    The screen-evidence guard runs last, on a fresh capture: a dialog on
    screen holds the item whatever the activity said (its Enter would pick
    the highlighted option)."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is not None and _dialog_on_screen(inst, name):
        rec["idle_since"] = None
        return False
    # A ``say`` item asks for its spoken reply only now, as it goes in
    # (server._voice_say); a send that does not land takes the request back.
    say = inst is not None and nxt.get("say") is True
    typed = srv._voice_say(inst, title, nxt["text"]) if say else nxt["text"]
    if not srv._send_to_agent(name, typed, submit=True):
        if say:
            srv._voice_say_cancel(title)
        return False
    entry = _prompt_queue.record_sent(title, nxt["id"])
    rec["armed"] = False
    rec["sent_at"] = now
    rec["sent_text"] = typed
    _events.BUS.emit(
        "session.prompt_sent",
        session=title,
        data={
            "text": nxt["text"][:200],
            "remaining": len(entry["items"]),
            "loop": entry["loop"],
        },
    )
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(
                "[MONITORING] queue drained a prompt to %s (%d remaining)",
                title,
                len(entry["items"]),
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def _dialog_on_screen(inst, name: str) -> bool:
    """THE SCREEN-EVIDENCE GUARD every automated typer checks right before it
    types: whether tmux session ``name``'s visible screen shows a live dialog,
    by the session's provider (``BaseProvider.dialog_on_screen``: a parse, or
    its waiting/trust phrases in the bottom lines).

    Screen evidence beats the activity reading. The reading is assembled
    from hook markers a background sub-agent can rewrite while its
    permission prompt is up, and the live E2E run lost exactly that race:
    the orchestrator read idle for 15 s with a sub-agent's ``answer_prompt``
    dialog on screen, the mailbox lane typed a worker's result into it, and
    the line's Enter approved the dialog (defect F). One ``capture-pane``;
    a True answer HOLDS the text for a later pass, never drops it.

    A capture that fails is no evidence either way (False): the session is
    gone or tmux is down, and the typer's own send fails on its own."""
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "")
    except Exception:  # noqa: BLE001 — an unresolvable CLI: the patterns are gone
        return False
    text, err = _agent_io.capture_screen(name)
    if err is not None or not text:
        return False
    probe = getattr(provider, "dialog_on_screen", None)
    try:
        return bool(probe(text)) if callable(probe) else False
    except Exception:  # noqa: BLE001 — evidence we can't read is no evidence
        return False


def _typing_held(inst, title: str) -> bool:
    """The session-level gates every automatic typer obeys (the prompt-queue
    drain and the mailbox delivery lane): not started / paused, over budget,
    or the worktree's setup pass still running or failed. An instance whose
    state cannot be read counts as held."""
    try:
        if not inst.Started() or inst.Status == session.Paused:
            return True
    except Exception:  # noqa: BLE001
        return True
    if _server()._budget_locked(title):
        return True  # over budget — hold until the user raises it
    # O2: hold while the worktree's setup pass is running or failed — the deps
    # the prompt needs aren't there yet (the held initial prompt from create
    # lands in the queue too, so it can't get lost: #2847).
    try:
        _wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        _wt = ""
    if _wt:
        _setup_st = _wt_setup.setup_status(_wt)
        if _setup_st and _setup_st.get("state") in ("running", "failed"):
            return True
    return False


def _idle_settled(rec: dict, title: str, now: float, since: float = 0.0) -> bool:
    """Whether ``title``'s idle reading has PERSISTED long enough to type into.

    ``rec["idle_since"]`` is the caller's dwell timer (the caller clears it on
    any non-idle reading). The first idle sighting only starts it; after that
    the settle is tiered by the reading's source — see _QUEUE_IDLE_SETTLE.
    ``since`` restarts a dwell that began at or before it: something else
    typed into the session then, so "idle ever since" is no longer known."""
    if rec.get("idle_since") is None or rec["idle_since"] <= since:
        rec["idle_since"] = now
        return False
    settle = (
        _QUEUE_IDLE_SETTLE_MARKER
        if _agent_state.reading_is_authoritative(title, "idle")
        else _QUEUE_IDLE_SETTLE
    )
    return now - rec["idle_since"] >= settle


def _send_consumed(inst, title: str, rec: dict) -> bool:
    """Evidence that the drain's last send into ``title`` (``rec["sent_at"]``,
    ``rec["sent_text"]``) was taken by the agent although the drain never read
    ``working`` after it. Asked only with the session idle and SETTLED, so a
    turn that is merely slow to start has had its chance to show. Either:

    * the agent was corroborated working after the send
      (``agent_state.last_worked_at`` — the unspent twin of ``worked_at``: a
      turn short enough to start and end between two drain polls is still
      seen by the 4s tickers, which may announce it and spend ``worked_at``
      before the drain looks); or
    * the input box no longer holds the sent text — the CLI submitted it and
      handled it locally (``/clear``, ``/model``: no hook fires, no turn
      starts). One screen capture, only for a provider that can read its box
      (``BaseProvider.input_holds``); only an EMPTY box counts, since a box
      holding other text (a human's draft, a placeholder) proves nothing.

    False when neither holds; the caller's _QUEUE_REARM_IDLE fallback still
    applies. Never raises."""
    sent_at = float(rec.get("sent_at") or 0.0)
    worked = _agent_state.last_worked_at(title)
    if worked is not None and worked > sent_at:
        return True
    text = rec.get("sent_text")
    if not text:
        return False
    try:
        from backend.providers.base import BaseProvider

        provider = providers.resolve(getattr(inst, "Program", "") or "")
        if type(provider).input_holds is BaseProvider.input_holds:
            return False  # can't read its box: don't pay for a capture
        screen, err = _agent_io.capture_screen(tmux.to_mindflock_tmux_name(title))
        if err is not None or not screen:
            return False
        return provider.input_holds(screen, text) is False
    except Exception:  # noqa: BLE001 — no evidence: the fallback re-arms
        return False


def _drain_one_queue(title: str) -> None:
    """One drain decision for a single session. Never raises."""
    srv = _server()
    st = _prompt_queue.get_state(title)
    # "held": parked by its worker order — only the order loop releases it
    # (after its fence lands), even when someone switched the queue on.
    if not st["enabled"] or not st["items"] or st.get("held"):
        return
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return
    if _typing_held(inst, title):
        return
    rec = _QUEUE_STATE.setdefault(
        title,
        {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None},
    )
    now = time.time()
    # Deliberately UNCACHED: a memoized "idle" from before the last send could
    # double-feed a prompt inside the memo window. The probes are per queued
    # session only, on the 5s drain cadence.
    activity = srv._agent_activity(inst, title)
    if activity == "working":
        rec["armed"] = True  # agent picked the last prompt up; ok to send again
        rec["idle_since"] = None  # not parked — reset the idle-dwell timer
        return
    if activity == "clarify":
        rec["idle_since"] = None
        return  # waiting on a human — never override a confirmation prompt
    if activity == "limit":
        # A usage-limit banner/menu is on screen. Unlike 'clarify' (a human gate
        # we never override) the queue should ride the window out and resume on
        # its own: hold while the limit is closed, and the moment it reopens send
        # a single Esc to drop the lingering limit menu — the CLI leaves it up
        # until a key is pressed — then submit the next prompt so it lands on a
        # clean input instead of selecting a menu entry.
        rec["idle_since"] = None
        if not st.get("wait_for_limit", True):
            # Not waiting it out: stop the queue so it sits until the user
            # resumes by hand (mirrors the idle-path limit gate).
            if st["enabled"]:
                _prompt_queue.set_flags(title, enabled=False)
                srv._emit_queue_changed(title)
            return
        name, err = srv._ensure_agent_session(inst, title)
        if err is not None:
            return
        if srv._refresh_limit_state(inst, title, name) > now:
            return  # still limited — hold; the UI shows the reset countdown
        # Window reopened (parsed reset passed / live meter shows headroom) but
        # activity=='limit' means the menu is still on the pane this very tick.
        # Gate the escape+send on ``armed`` exactly like the idle path: send at
        # most once, then wait for the agent to actually pick the prompt up (it
        # transitions to 'working', which re-arms) before sending again. Without
        # this a menu that doesn't clear on the first Esc — or a live meter that
        # lags the real reset — would let every 5s pass pop and burn another
        # queued prompt into the limit screen. A send that never took re-arms
        # after _QUEUE_REARM_IDLE so a stuck menu still gets a bounded retry.
        if (
            not rec.get("armed", True)
            and now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
        ):
            rec["armed"] = True
        if (
            not rec.get("armed", True)
            or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
        ):
            return
        nxt = _prompt_queue.peek_next(title)
        if not nxt:
            return
        srv._send_escape_to_agent(name)  # single Esc to leave the limit menu
        time.sleep(0.15)  # let the CLI redraw its prompt before we type
        _send_queued_item(title, name, nxt, rec, now)
        return
    if activity == "offline":
        # Agent tmux is gone though the session is live — reboot (resume) it,
        # rate-limited, and send on a later idle tick.
        rec["idle_since"] = None
        if now - rec.get("rebooted_at", 0.0) < _QUEUE_REBOOT_COOLDOWN:
            return
        if _sleep.is_sleeping(title):
            # Asleep (core.sleep): the queued prompt is what wakes it.
            _sleep.ensure_awake(inst, title, "a queued prompt")
        name, err = srv._ensure_agent_session(inst, title)
        if err is None:
            rec["rebooted_at"] = now
            rec["armed"] = True
        return
    # activity == "idle": the CLI's idle signal (Claude `agents --json` / Stop hook,
    # Codex Stop hook) flips the instant a turn ends and _agent_activity trusts it at
    # any age — so require idle to PERSIST for _QUEUE_IDLE_SETTLE before the first send.
    # This absorbs a transient Stop between turns / the brief lull before the next
    # working marker, which otherwise let the drain fire a prompt prematurely.
    if now - rec.get("rebooted_at", 0.0) < _QUEUE_BOOT_GRACE:
        return  # we only just relaunched it; let the CLI draw its prompt
    # A mailbox delivery typed into this session restarts the dwell too.
    mail_sent = (_MAIL_STATE.get(title) or {}).get("sent_at", 0.0)
    if not _idle_settled(rec, title, now, since=mail_sent):
        return
    # A send the drain never SAW picked up leaves ``armed`` False with no
    # "working" reading coming. Two ways that happens while the send was in
    # fact consumed — a turn that started and ended between two 5s polls, and
    # a slash command the CLI handled locally (no hook, no turn) — re-arm as
    # soon as the evidence says so (_send_consumed). The third — the send
    # landed on a usage-limit screen and was never taken — has no evidence to
    # offer: an agent still idle _QUEUE_REARM_IDLE after it clearly isn't
    # going to pick it up, so re-arm then and let the next pass (behind the
    # usage-limit gate below) retry.
    if not rec.get("armed", True) and (
        now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
        or _send_consumed(inst, title, rec)
    ):
        rec["armed"] = True
    # send the next prompt if armed + past the send cooldown (which counts a
    # mailbox delivery as a send: two typers, one keyboard).
    if (
        not rec.get("armed", True)
        or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
        or now - mail_sent < _QUEUE_SEND_COOLDOWN
    ):
        return
    # Loop timer: with loop on and an interval set, only send every N minutes
    # (a timed self-improving cycle). The first send is immediate (last_sent 0).
    interval = int(st.get("loop_interval") or 0)
    if st["loop"] and interval > 0:
        last = st.get("last_sent") or 0.0
        if last and now - last < interval * 60:
            return
    nxt = _prompt_queue.peek_next(title)
    if not nxt:
        return
    name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        return
    # Never type into a bare shell: a provisioned launcher that dropped to
    # ``bash -i`` (or a shell/wrapper program) reads idle under the bare-shell
    # rule, and the prompt — plus Enter — would run as commands. The looser
    # test (no ``program``): a queued prompt is the user's own text, and a
    # positive name match that missed some CLI would stall the queue for good.
    if not _pane_holds_agent(name):
        rec["idle_since"] = None
        return
    # Usage-limit gate: if the pane shows a limit that hasn't reset yet, either
    # hold (wait_for_limit on — the UI shows the countdown and a later pass sends
    # once the window reopens) or stop the queue (wait_for_limit off — flip
    # auto-run off so it sits until the user resumes it by hand).
    if srv._refresh_limit_state(inst, title, name) > now:
        if not st.get("wait_for_limit", True) and st["enabled"]:
            _prompt_queue.set_flags(title, enabled=False)
            srv._emit_queue_changed(title)
        return
    _send_queued_item(title, name, nxt, rec, now)


# --- Usage-limit watch: sessions parked on the limit screen with no queue --- #
# The drain above resumes a limited session that has something QUEUED. A session
# that simply ran out mid-task has an empty queue, so nothing was watching it —
# it sat on the limit screen until a human came back hours later, long after the
# window reopened. This watcher covers exactly that gap, and pays for itself:
# the candidate list comes from the snapshot the /api/events tick already keeps
# (a dict read), so a flock with nothing limited does no work at all.
_LIMIT_RESUME_PROMPT = "continue"

#: "Usage is back" is one fact about the account, not one per session — several
#: sessions typically unblock in the same pass. Announce it once per window.
_LIMIT_RESTORE_QUIET = 300.0
#: Rebound by :func:`_watch_one_limited` (``global``), so server.py does not
#: re-import it: a copy there would be stale. Patch it here.
_LAST_RESTORE_EMIT = 0.0


def _resume_on_usage_reset() -> bool:
    """Whether to nudge a limited session once its window reopens
    (``general.resume_on_usage_reset``; unset = on). Never raises."""
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().general.resume_on_usage_reset is not False
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return True


def _limited_titles() -> list:
    """Sessions whose last observed activity was ``limit``, from the event
    snapshot — free (no probes), and refreshed every ~4s by the state tick."""
    srv = _server()
    with srv._EVENT_SNAPSHOT_LOCK:
        return [
            t
            for t, snap in srv._EVENT_SNAPSHOT.items()
            if snap.get("activity") == "limit"
        ]


def _watch_one_limited(title: str) -> None:
    """One usage-limit decision for a session sitting on the limit screen.

    Runs only while the session's *observed* activity is ``limit``, so the pane
    capture in :func:`_refresh_limit_state` happens for genuinely stuck sessions
    only. Never raises."""
    srv = _server()
    global _LAST_RESTORE_EMIT
    st = _prompt_queue.get_state(title)
    if st["enabled"] and st["items"]:
        return  # the drain owns this one — it has a prompt to send on reopening
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return
    try:
        if not inst.Started() or inst.Status == session.Paused:
            return
    except Exception:  # noqa: BLE001
        return
    name, err = srv._ensure_agent_session(inst, title)
    if err is not None:
        return
    now = time.time()
    if srv._refresh_limit_state(inst, title, name) > now:
        return  # still limited — the UI shows the countdown
    # The window reopened. Reuse the drain's per-session send bookkeeping so the
    # nudge obeys the same "once, then wait to see it picked up" rule: an Esc +
    # prompt that doesn't take (a menu that needs a second key, a meter that
    # lags the real reset) retries after _QUEUE_REARM_IDLE instead of every 5s.
    rec = _QUEUE_STATE.setdefault(
        title,
        {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None},
    )
    if (
        not rec.get("armed", True)
        and now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
    ):
        rec["armed"] = True
    if (
        not rec.get("armed", True)
        or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
    ):
        return
    resume = _resume_on_usage_reset()
    if resume:
        srv._send_escape_to_agent(name)  # drop the lingering limit menu
        time.sleep(0.15)  # let the CLI redraw its prompt before we type
        if _dialog_on_screen(inst, name):
            return  # the menu (or another prompt) is still up: next pass
        if srv._send_to_agent(name, _LIMIT_RESUME_PROMPT, submit=True):
            rec["armed"] = False
            rec["sent_at"] = now
            rec["sent_text"] = _LIMIT_RESUME_PROMPT
            if log.ErrorLog is not None:
                try:
                    log.ErrorLog.Printf(
                        "[MONITORING] usage window reopened — resumed %s", title
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            return  # tmux gone: no resume, and no "usage is back" to announce
    # Announce the reopening whether or not we acted on it — "your usage is
    # back" is worth knowing even with auto-resume off, and it can only fire for
    # a session that had actually run out (that is what put it on this list).
    if now - _LAST_RESTORE_EMIT >= _LIMIT_RESTORE_QUIET:
        _LAST_RESTORE_EMIT = now
        _events.BUS.emit(
            "session.usage_restored", session=title, data={"resumed": resume}
        )


def _watch_limited_sessions() -> None:
    """One pass over every session parked on a usage-limit screen."""
    for title in _limited_titles():
        try:
            _watch_one_limited(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the pass
            pass


# --- Mailbox delivery lane --------------------------------------------------- #
# Types inter-agent messages (core.mailbox) into idle recipients. A lane of its
# own, NOT the prompt queue: routing messages through the queue would re-enable
# a queue the user paused, re-type them forever under ``loop``, put them in the
# user's Queue tab, and — through the drain's offline path — reboot agents the
# user stopped. Here a message stays ``pending`` in the mailbox until every
# gate below passes, and is typed at most once (claimed under the mailbox lock
# before typing, so an inbox fetch that got there first wins outright):
#   * the session-level gates the queue obeys (_typing_held): started, not
#     paused, not over budget, setup not running/failed;
#   * no long-poll on the recipient's inbox (that poll is about to hand the
#     message over itself), no fast-track chain mid-flight;
#   * not inside the queue's post-reboot grace, and the CLI's tmux session
#     alive — the lane NEVER boots an agent; offline / paused / loading wait
#     (the one exception: a SLEEPING recipient, core.sleep, is woken — it
#     was stopped to free memory, not closed);
#   * activity ``idle``, settled exactly as the queue settles it, and past the
#     queue's send cooldown counted across BOTH typers; clarify / limit wait;
#   * human intent first: a queued user prompt that is ready goes before any
#     message (the message waits for the next settled idle).
# Typing goes straight through _send_to_agent: never _note_human_input (an
# agent wrote this, not the person at the keys) and never the user's queue.
_MAIL_STATE: Dict[str, dict] = {}  # title -> {"idle_since", "sent_at"}
#: ``_mail_type``'s answer when the recipient's screen showed a dialog.
_MAIL_ON_PROMPT = "on_prompt"
#: Boxes whose recipient is no longer live are pruned only once their newest
#: message is this old — a title another server just created may not have
#: reached this process's engine yet, and its first message must survive that.
_MAIL_PRUNE_AFTER_S = 600.0


def _mail_provider_name(inst) -> str:
    """The recipient's provider name, which picks the reply-hint wording."""
    try:
        return providers.resolve(getattr(inst, "Program", "") or "").name
    except Exception:  # noqa: BLE001 — the hint degrades to the generic wording
        return ""


def _mail_type(inst, title: str, name: str, msg: dict, now: float):
    """Claim ``msg`` and type its one-line rendering into tmux session ``name``
    → the message's new state (``"delivered"``, or ``"held"`` when only a
    long-body notice was typed), :data:`_MAIL_ON_PROMPT` when the screen
    showed a dialog (nothing claimed, nothing typed — the screen-evidence
    guard, which outranks the activity reading the caller gated on), or None
    when nothing was typed (an inbox fetch consumed it first, or tmux refused
    — the claim is then released so a later pass retries). Shared by the
    lane and ``delivery: "now"``."""
    if _dialog_on_screen(inst, name):
        rec = _MAIL_STATE.setdefault(title, {"idle_since": None, "sent_at": 0.0})
        rec["idle_since"] = None
        return _MAIL_ON_PROMPT
    line, full = _mailbox.render_delivery(msg, _mail_provider_name(inst))
    state = "delivered" if full else "held"
    claimed = _mailbox.claim(
        title,
        msg["id"],
        state=state,
        detail="" if full else _mailbox.long_notice_detail(),
        now=now,
    )
    if claimed is None:
        return None
    if not _server()._send_to_agent(name, line, submit=True):
        _mailbox.release(title, msg["id"])
        return None
    rec = _MAIL_STATE.setdefault(title, {"idle_since": None, "sent_at": 0.0})
    rec["sent_at"] = now
    rec["idle_since"] = None
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(
                "[MONITORING] mailbox typed %s from %r into %s (%s)",
                msg["id"],
                msg.get("from") or "",
                title,
                state,
            )
        except Exception:  # noqa: BLE001
            pass
    return state


def _queue_wants_turn(title: str, now: float) -> bool:
    """Whether the user's prompt queue would send into ``title`` at its next
    settled idle — in which case a pending message waits behind it. Mirrors
    the queue's own send conditions (enabled, an item, armed, outside its loop
    interval and any usage-limit hold) so a queue that is NOT going to send (a
    timed loop between rounds, a paused queue) cannot starve the mailbox."""
    st = _prompt_queue.get_state(title)
    if not st["enabled"] or not st["items"]:
        return False
    if not (_QUEUE_STATE.get(title) or {}).get("armed", True):
        return False
    interval = int(st.get("loop_interval") or 0)
    if st["loop"] and interval > 0:
        last = st.get("last_sent") or 0.0
        if last and now - last < interval * 60:
            return False
    return _server()._session_limited_until(title) <= now


def _pane_holds_agent(name: str, program: Optional[str] = None) -> bool:
    """Whether an agent CLI (not a bare shell) holds tmux session ``name``'s
    pane — the precondition for typing a mailbox line or a queued prompt.

    A provisioned launcher drops to ``exec bash -i`` when the user quits the
    agent on purpose, and that pane still reads ``idle`` (a SessionEnd/Stop
    marker, or the bare-shell rule): typing there hands agent-written text to
    the shell, which runs it.

    With ``program`` (the session's ``Program``) the answer needs POSITIVE
    evidence: an agent CLI's executable somewhere in the pane's process tree
    (:func:`agent_state._pane_runs_agent`). Any non-shell foreground used to
    count — so after a quit, ``vim``/``ssh prod``/``psql`` started in that
    shell read as the agent and a sibling's message was typed into them, with
    Enter (over ssh, a backticked command in it ran on the remote host).
    Without ``program`` the older, looser test applies: a non-shell
    foreground, or a bare-shell foreground with a live non-shell descendant.
    When tmux cannot say, the exit marker for this incarnation decides."""
    srv = _server()
    try:
        fg, created, pane_pid, _size = srv._pane_meta(name)
        if fg:
            if program is not None:
                names = _agent_state.agent_process_names(program)
                if names:
                    return bool(_agent_state._pane_runs_agent(pane_pid, names))
            if fg.lower() not in srv._BARE_SHELLS:
                return True
            return bool(srv._pane_has_agent_process(pane_pid))
        return not srv._agent_exited(name, created)
    except Exception:  # noqa: BLE001 — can't tell: don't type blind
        return False


def _human_at_keys(title: str, probe_tmux: bool = True) -> bool:
    """A person typed into ``title``'s window within ``_HUMAN_HOLD_S`` — a
    half-written prompt fires no hook, so activity still reads idle, and the
    lane's Enter would submit it merged with an agent's message. The web
    stamp is a dict read; the tmux client probe (raw ``tmux attach`` / SSH)
    shells out, so callers ask for it last."""
    srv = _server()
    if time.time() - srv._HUMAN_INPUT_AT.get(title, 0.0) < srv._HUMAN_HOLD_S:
        return True
    return bool(probe_tmux and srv._tmux_client_input_recent(title, srv._HUMAN_HOLD_S))


def _drain_one_mailbox(title: str) -> None:
    """One delivery decision for one recipient: type at most its oldest pending
    message. Never reboots (but wakes a sleeper), never types into
    clarify/limit/offline."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return
    msg = _mailbox.next_pending(title)
    if msg is None:
        return
    rec = _MAIL_STATE.setdefault(title, {"idle_since": None, "sent_at": 0.0})
    now = time.time()
    if _typing_held(inst, title) or _mailbox.waiter_active(title, now):
        rec["idle_since"] = None
        return
    if _sleep.is_sleeping(title):
        # The one boot this lane does: a SLEEPING recipient (core.sleep) was
        # stopped on purpose to free memory, not closed — a message for it is
        # exactly what should wake it. The wake opens the queue's boot grace,
        # so the line below holds the typing until the CLI is ready.
        rec["idle_since"] = None
        _sleep.ensure_awake(inst, title, "a message arrived")
        return
    q = _QUEUE_STATE.get(title) or {}
    if now - q.get("rebooted_at", 0.0) < _QUEUE_BOOT_GRACE:
        return  # the queue just relaunched it; let the CLI draw its prompt
    # Deliberately UNCACHED, for the queue's reason: a memoized idle from
    # before the last send could type a second message into a live turn.
    activity = srv._agent_activity(inst, title)
    if activity != "idle":
        rec["idle_since"] = None  # working / clarify / limit / offline: wait
        return
    if _human_at_keys(title, probe_tmux=False):
        rec["idle_since"] = None  # someone is mid-composition in this pane
        return
    last_typed = max(q.get("sent_at", 0.0), rec.get("sent_at", 0.0))
    if not _idle_settled(rec, title, now, since=last_typed):
        return
    if now - last_typed < _QUEUE_SEND_COOLDOWN:
        return
    if _queue_wants_turn(title, now) or srv._autopilot_running(title):
        return
    name = srv._live_session_name(tmux.to_mindflock_tmux_name(title))
    if name is None:
        return  # never boot it: delivery resumes once someone starts it
    if not _pane_holds_agent(name, getattr(inst, "Program", "") or ""):
        rec["idle_since"] = None  # the agent quit: a bare shell would RUN it
        return
    if srv._refresh_limit_state(inst, title, name) > now:
        return  # an idle prompt sitting under a usage-limit banner
    if srv._tmux_client_input_recent(title, srv._HUMAN_HOLD_S):
        rec["idle_since"] = None  # typing through a raw tmux client / SSH
        return
    _mail_type(inst, title, name, msg, now)


def _drain_mailboxes() -> None:
    """One pass of the mailbox delivery lane — run after the prompt queue's,
    so a user prompt sent this pass holds the lane through the shared
    cooldown. Never raises."""
    srv = _server()
    try:
        _mailbox.prune(
            list(srv.ENGINE.instances.keys()),
            older_than=time.time() - _MAIL_PRUNE_AFTER_S,
        )
        titles = _mailbox.pending_titles()
    except Exception:  # noqa: BLE001 — an unreadable store skips this pass
        return
    for title in titles:
        try:
            srv._drain_one_mailbox(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the lane
            pass
    for gone in [t for t in _MAIL_STATE if t not in srv.ENGINE.instances]:
        _MAIL_STATE.pop(gone, None)


def _drain_prompt_queues() -> None:
    """One pass over every session with a queue, plus every session parked on a
    usage-limit screen, then the mailbox delivery lane. Runs in a worker thread
    (it shells out to tmux) so it never blocks the event loop."""
    srv = _server()
    titles = _prompt_queue.all_titles()
    if not titles:
        _watch_limited_sessions()
        _drain_mailboxes()
        return
    _prompt_queue.prune(list(srv.ENGINE.instances.keys()))
    _ports.prune(list(srv.ENGINE.instances.keys()))
    for title in titles:
        try:
            _drain_one_queue(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the drain
            pass
    _watch_limited_sessions()
    _drain_mailboxes()
    # Forget in-memory drain state for sessions that vanished.
    for gone in [t for t in _QUEUE_STATE if t not in srv.ENGINE.instances]:
        _QUEUE_STATE.pop(gone, None)
    for gone in [t for t in _LIMIT_STATE if t not in srv.ENGINE.instances]:
        _LIMIT_STATE.pop(gone, None)
    # Drop budget overrides for sessions that no longer exist (so a reused title
    # doesn't inherit an old raise).
    for gone in [t for t in srv._budget_overrides() if t not in srv.ENGINE.instances]:
        srv._forget_budget(gone)


async def _prompt_queue_drain_loop() -> None:
    """Drive the prompt-queue drain forever (started by the lifespan)."""
    while True:
        try:
            await asyncio.to_thread(_drain_prompt_queues)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_QUEUE_DRAIN_INTERVAL)
