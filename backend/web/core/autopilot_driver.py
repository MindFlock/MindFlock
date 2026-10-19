"""The impure half of the autopilot (Fast-track): observe, act, announce.

:mod:`backend.web.core.autopilot` holds the record store and the pure
``next_action`` decision. This module is the loop that gives it effect: a 5s
pass (started by the server lifespan), one decision per armed session, every
step wrapped so a single bad session cannot stop the pass.

* :func:`_autopilot_snapshot` / :func:`_autopilot_observe` — one snapshot of
  everything a decision needs (stage, dirty tree, commits beyond base, origin).
* :func:`_autopilot_act` — the side effects: committing (through the agent's
  shell, so hooks run where the user would see them), the verification check,
  push / PR / merge through the server's own route handlers.
* :func:`_publish_autopilot` / :func:`_emit_autopilot` — the row refresh and
  the ``autopilot.*`` event.
* the row helpers the session snapshot shares (``_row_lane``, ``_row_run``,
  ``_fasttrack_depth`` …).

The routes stay in ``server.py`` and call in here. Anything server-owned
(``ENGINE``, the emit helpers, the route handlers it drives, git probes tests
patch on ``server``) is reached lazily through :func:`_server`, and server.py
re-imports every name defined here, so ``server._autopilot_step`` and friends
keep resolving.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from backend import session
from backend.session import tmux
from backend.web.core import autopilot as _autopilot
from backend.web.core import commit_message as _commit_message
from backend.web.core import events as _events
from backend.web.core import lanes as _lanes
from backend.web.core import live_stage as _live_stage
from backend.web.core import mailbox as _mailbox
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import team_runs as _team_runs
from backend.web.core import worktree_setup as _wt_setup


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# --- Autopilot: advance armed sessions toward their target rung -------------- #
# The impure half of backend.web.core.autopilot (the store and the decision
# function live there). Structured exactly like the prompt-queue drain
# (core.queue_driver): a 5s pass, one decision per armed session, every step
# wrapped so a single bad session cannot stop the pass.
_AUTOPILOT_INTERVAL = 5.0
#: Floor between two actions on one session, so a stage that has not yet caught
#: up cannot cause a double-push.
_AUTOPILOT_ACTION_COOLDOWN = 15.0
#: Per-step wall clock. Expiry HALTS with a reason — it never silently retries.
_AUTOPILOT_DEADLINES = {
    "agent": 5400.0,  # 90min: an agent working a whole ticket
    "commit": 1800.0,  # 30min: hook stacks can include tests and doc passes
    "check": 1800.0,  # 30min: a verification command may run the whole suite
    # These three were far too tight. A slow remote, a rate-limited API or a
    # required-check queue must read as "still going", not as a failure — the
    # deadline exists to stop a WEDGED chain, not to race the network.
    "push": 900.0,
    "pr": 600.0,
    "merge": 5400.0,  # 90min: the merge rung genuinely waits for CI now
}


def _autopilot_dto(title: str):
    """The session row's autopilot block — see :func:`autopilot.dto`.

    Kept as a thin alias because it is referenced from both snapshot paths and by
    tests; the shaping itself is shared with ``core.pending``.
    """
    return _autopilot.dto(title)


def _run_member(title: str):
    """``title``'s entry in the team-run index — only when the live session
    IS that member (its incarnation), never a later session that merely
    reused the title of a finished group's member."""
    try:
        member = _team_runs.title_index().get(title)
    except Exception:  # noqa: BLE001 — enrichment only
        return None
    if not member:
        return None
    inc = float(member.get("incarnation") or 0.0)
    inst = _server().ENGINE.instances.get(title)
    if inc and inst is not None:
        created = _server()._created_epoch(inst)
        if created is not None and abs(float(created) - inc) > 1.0:
            return None
    return member


def _row_lane(title: str):
    """The row's ``lane`` block (see :func:`core.lanes.lane_of`), or None.

    A team run's member shows its group's lane even while nothing is armed on
    it — a "leave it" lane arms nothing at all, and a paused group holds its
    members' autopilot — so the rail never loses the line it leads with."""
    lane = _lanes.lane_dto(title)
    if lane is not None:
        return lane
    member = _run_member(title)
    if not member or not member.get("lane"):
        return None
    return {
        "target": member["lane"],
        "ask_first": bool(member.get("ask_first")),
        "owner": title,
    }


def _row_run(title: str):
    """The row's ``run`` block — ``{id, name, task, role, grouping}`` for a
    session a team run owns — or None."""
    member = _run_member(title)
    if not member:
        return None
    return {k: member[k] for k in ("id", "name", "task", "role", "grouping")}


def _fasttrack_depth() -> str:
    """The rung an EXPLICIT ``POST /fast-track`` with no ``depth`` arms.

    That request already says "arm it", so it needs a real rung: the stored
    setting when it names one, else the built-in "pr" (an Off or unset setting
    cannot be armed). This is an explicit-arm convenience only — nothing that
    SEEDS a choice reads it; they read :func:`_fasttrack_default`, where unset
    means Off. Read fresh at decision time (never memoized at startup), the
    house rule for every settings consumer.
    """
    try:
        from backend.config import settings as _settings

        d = _autopilot.normalize_depth(
            _settings.load_settings().repository.fasttrack_depth
        )
        return d if d in _autopilot.DEPTHS else "pr"
    except Exception:  # noqa: BLE001
        return "pr"


def _fasttrack_default() -> str:
    """THE fast-track default — Settings → Workspace "Fast-track goes as far
    as" — for everything that SEEDS a choice: the New dialog's list mode,
    Intake's Start together, and a team run created with no lane (the MCP's
    ``start_team_run``). A single new session never reads it: it starts Off
    unless the user picks.

    The stored rung (``commit`` / ``push`` / ``pr`` / ``merge``) when one is
    set, else ``"off"``: out of the box nothing is fast-tracked unless asked.
    An explicit ``"off"``, a blank, junk and an unreadable settings file all
    read as Off. Read fresh, like every settings consumer.
    """
    try:
        from backend.config import settings as _settings

        d = _autopilot.normalize_depth(
            _settings.load_settings().repository.fasttrack_depth
        )
    except Exception:  # noqa: BLE001
        return "off"
    return d if d in ("commit", "push", "pr", "merge") else "off"


def _precommit_retry_hooks() -> list:
    """Pre-commit hook IDs whose failure the driver may retry, then skip.

    Sourced from settings, never from a client: the value ends up inside a shell
    command, so it is charset-filtered here and anything in
    :data:`autopilot.NEVER_SKIP` (tests, secret scanners) is dropped whatever the
    settings file says.
    """
    try:
        from backend.config import settings as _settings

        raw = _settings.load_settings().repository.precommit_retry_hooks or ""
    except Exception:  # noqa: BLE001
        return []
    out = []
    for part in str(raw).split(","):
        h = part.strip()
        if not h or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", h):
            continue
        if h in _autopilot.NEVER_SKIP or h in out:
            continue
        out.append(h)
    return out[:8]


def _autopilot_snapshot(inst, title: str, wt: str, stage: dict) -> dict:
    """The observation :func:`autopilot.next_action` decides from.

    Every probe here is deliberately UNCACHED for the same reason the drain reads
    activity uncached: a 2.5s-stale "committed" immediately after a push would
    make the driver push again.
    """
    srv = _server()
    now = time.time()
    try:
        queue_st = _prompt_queue.get_state(title)
        # A task parked by its worker order is pending too: the idle agent
        # has not been given its work yet, so it is not "done".
        queue_pending = bool(
            queue_st.get("items") and (queue_st.get("enabled") or queue_st.get("held"))
        )
    except Exception:  # noqa: BLE001
        queue_pending = False
    activity = srv._agent_activity(inst, title)
    # THE MOST IMPORTANT FIELD HERE. "The agent stopped" and "the agent ran out"
    # are the same observation to every cheap probe: a turn the account's usage
    # limit cuts short ends with the CLI's Stop hook, a quiet pane and an idle
    # badge, exactly like a finished one. `_agent_activity` now re-checks the
    # pane before reporting that idle, but the pane is not the whole story —
    # a session that ran out mid-turn can be sitting at a redrawn prompt with no
    # banner in view — so an idle verdict is confirmed against the drain's own
    # limit state, which adds the provider's usage METER (Anthropic's usage
    # endpoint: the weekly cap included, independent of any pane text) and
    # carries a bounded, self-correcting expiry that also feeds the UI countdown.
    # This is what stops the ladder committing and pushing a half-finished
    # session because the weekly window closed under it.
    #
    # Only on idle: a working session is not about to be committed, and the
    # confirmation costs a tmux capture. `_refresh_limit_state` takes the tmux
    # name directly, so there is no `_ensure_agent_session` reboot side effect.
    limited = activity == "limit"
    if not limited and activity == "idle":
        try:
            limited = (
                srv._refresh_limit_state(
                    inst, title, tmux.to_mindflock_tmux_name(title)
                )
                > now
            )
        except Exception:  # noqa: BLE001 — detection must never break a pass
            limited = False
    # None means "could not measure", which must never be read as "nothing has
    # been committed" — that mistake produced a false "the agent finished without
    # changing anything" on sessions whose work was already committed and pushed.
    beyond = None
    base = ""
    live_branch = ""
    try:
        base = srv._session_base_branch(inst) or ""
        if base:
            beyond = srv._commits_beyond_base(wt, base)
    except Exception:  # noqa: BLE001
        beyond = None
    try:
        live_branch = srv._current_branch(wt) or ""
    except Exception:  # noqa: BLE001
        live_branch = ""
    # Sitting ON the base branch means there is no feature branch: pushing would
    # push the trunk and a PR has nothing to target. Compared case-sensitively
    # against the resolved base, and only when BOTH are known — an unknown base
    # must not be guessed into a refusal.
    on_base = bool(base and live_branch and base == live_branch)
    try:
        check = _wt_setup.check_summary(wt)
    except Exception:  # noqa: BLE001
        check = None
    # Whether this repo GATES the push on a verification run. The push route 409s
    # when a declared check has not passed, so the ladder has to know about the
    # gate up front rather than discovering it as a fatal error.
    try:
        check_required = bool(_wt_setup.load_config(wt).check_command)
    except Exception:  # noqa: BLE001
        check_required = False
    # CI verdict on the PR, only when a merge is actually the target — it is a
    # network round trip and every other rung is indifferent to it.
    # CI verdict + blockers on the PR, only when a merge is actually the target.
    # Reuses the same probe the UI's merge button reads, so the driver and the
    # button can never disagree about whether a merge would go through.
    pr_checks = ""
    merge_blockers: list = []
    try:
        rec = _autopilot.get(title) or {}
        if rec.get("depth") == "merge" and (stage.get("stage") or "") == "pr":
            branch = srv._current_branch(wt) or ""
            ms = srv._pr_merge_state(wt, branch) if branch else None
            if ms is None:
                pr_checks = "unknown"
            else:
                pr_checks = str(ms.get("checks") or "unknown")
                if not ms.get("can_merge"):
                    merge_blockers = list(ms.get("blockers") or ["cannot merge yet"])
    except Exception:  # noqa: BLE001
        pr_checks = "unknown"
    # A job the agent backgrounded and is waiting on: its turn ended, but the
    # job's completion will start another (see claude_background).
    try:
        background_waiting = activity == "idle" and any(
            j.get("state") == "waiting"
            for j in (srv._background_jobs(inst, title) or [])
        )
    except Exception:  # noqa: BLE001 — detection must never break a pass
        background_waiting = False
    return {
        "stage": stage.get("stage") or "",
        "failed_step": stage.get("failed_step") or "",
        "failed_hook": stage.get("failed_hook") or "",
        "dirty": srv._is_dirty(wt),
        # A handed-back failure records it, and its fix turn is judged by it.
        "head": srv._git_head_sha(wt),
        "beyond_base": beyond,
        "activity": activity,
        "limited": limited,
        "queue_pending": queue_pending,
        "background_waiting": background_waiting,
        "check": check,
        "check_required": check_required,
        "pr_checks": pr_checks,
        "merge_blockers": merge_blockers,
        "on_base_branch": on_base,
        "branch": live_branch,
        "has_origin": srv._has_origin(wt),
        "now": now,
    }


def _autopilot_observe(title: str):
    """Gather everything a decision needs, in a worker thread.

    Split from the decision + action so the blocking half (git, tmux, gh) never
    runs on the event loop while the action half can ``await`` the existing route
    coroutines directly. Returns ``(rec, inst, wt, snap)``, or None to skip this
    session on this pass.
    """
    srv = _server()
    rec = _autopilot.get(title)
    if rec is None:
        return
    if rec.get("state") != "running":
        # Finished or halted: the run has had its go, either way. It turns itself
        # OFF for that window rather than lingering armed — and the record stays so
        # the outcome (and a halt's reason) is still readable. Pressing the button
        # is how you start another one.
        return
    if not _autopilot.normalize_depth(rec.get("depth")) in _autopilot.DEPTHS:
        return
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        # Not adopted yet — the normal intake case, since arming happens before the
        # session exists. Say so rather than looking wedged.
        _autopilot_note(title, rec, "waiting for the workspace to be created")
        return
    # Every hold below is a WAIT, not a halt — but it must be VISIBLE. These used
    # to be bare `return`s, so a chain held by a paused session, a budget lock or a
    # failed worktree setup looked identical to one that had silently died.
    try:
        if not inst.Started():
            _autopilot_note(title, rec, "waiting for the session to start")
            return
        if inst.Status == session.Paused:
            _autopilot_note(title, rec, "session is paused — resume it to continue")
            return
    except Exception:  # noqa: BLE001
        return
    if srv._budget_locked(title):
        _autopilot_note(title, rec, "over the cost budget — raise it to continue")
        return
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        _autopilot_note(title, rec, "waiting for the workspace")
        return
    setup_st = _wt_setup.setup_status(wt)
    if setup_st and setup_st.get("state") == "running":
        _autopilot_note(title, rec, "workspace setup is running")
        return
    if setup_st and setup_st.get("state") == "failed":
        _autopilot_halt(title, "workspace setup failed — fix it and re-arm")
        return

    # ONE DRIVER PER CHAIN. Take (or refresh) the lease before deciding anything.
    # Two servers sharing this store — a dev instance on another port, say — both
    # ran the driver: each saw the other's boot id, treated it as a restart, and
    # reset the idle dwell every pass, so the 30s settle could never elapse and a
    # chain sat at "agent just went idle" indefinitely. Both would also have ACTED,
    # double-committing and double-pushing the same worktree. Taking the lease also
    # re-earns the dwell on a genuine handover (a restart, or a crashed owner), so
    # a chain cannot shortcut the "is the agent really done" wait.
    claimed, took_over = _autopilot.claim(title, srv._SERVER_BOOT_ID, now=time.time())
    if claimed is None:
        return  # another live server owns this chain
    rec = claimed
    if took_over and rec.get("boot") != srv._SERVER_BOOT_ID:
        rec = _autopilot.update(title, boot=srv._SERVER_BOOT_ID) or rec

    # Branch drift: the push/PR/merge routes all resolve the LIVE branch, so a
    # switch mid-run would retarget the chain at different work.
    live_branch = srv._current_branch(wt) or ""
    armed_branch = rec.get("branch") or ""
    acted = int(rec.get("commits") or 0) > 0 or rec.get("step") in (
        "commit",
        "push",
        "pr",
    )
    if armed_branch and not live_branch:
        # Detached HEAD (a rebase, a bisect, `checkout <sha>`). Pushing from here
        # would target something nobody asked for.
        _autopilot_note(
            title, rec, "workspace is on a detached HEAD — check out a branch"
        )
        return
    if armed_branch and live_branch and live_branch != armed_branch:
        if acted:
            _autopilot_halt(
                title,
                "the workspace switched from %s to %s mid-run"
                % (armed_branch, live_branch),
            )
            return
        # Nothing has been done on the armed branch yet, so this is not "drift" —
        # it is the agent creating its working branch, which is the NORMAL intake
        # sequence. Follow it instead of refusing to work.
        rec = (
            _autopilot.update(
                title,
                branch=live_branch,
                note="following the agent onto " + live_branch,
            )
            or rec
        )
    elif not armed_branch and live_branch:
        rec = _autopilot.update(title, branch=live_branch) or rec

    # The retry allowlist is re-read EVERY pass, never frozen at arm time: intake
    # arms hours before the commit step runs, and a hook id added in Settings must
    # take effect on the next pass rather than needing a disarm/re-arm.
    fresh_hooks = _precommit_retry_hooks()
    if list(rec.get("retryable") or []) != fresh_hooks:
        rec = _autopilot.update(title, retryable=fresh_hooks) or rec

    stage = srv._session_stage(inst)  # uncached on purpose
    return rec, inst, wt, _autopilot_snapshot(inst, title, wt, stage)


async def _autopilot_step(title: str) -> None:
    """One autopilot decision for a single session. Never raises.

    Async so the action half can ``await`` the existing route coroutines; every
    blocking probe happens inside :func:`_autopilot_observe`'s worker thread.
    """
    seen = await asyncio.to_thread(_autopilot_observe, title)
    if seen is None:
        return
    rec, inst, wt, snap = seen
    if getattr(inst, "PeerShare", ""):
        # Never ship a shared folder (the arming routes refuse it too).
        await asyncio.to_thread(
            _autopilot_halt, title, "autopilot is off for shared-folder sessions"
        )
        return
    now = snap["now"]
    action, detail = _autopilot.next_action(rec, snap)
    if detail.get("fixed"):
        # The agent's fix turn is over: the ladder is back in charge, and a
        # failure from here on is a new stop (a new hand-off, while any are left).
        rec = await asyncio.to_thread(_autopilot.update, title, fixing="") or rec

    if action == "wait":
        await asyncio.to_thread(_autopilot_wait, title, rec, snap, detail, now)
        return
    if action == "done":
        await asyncio.to_thread(_autopilot_draft_for_approval, title, wt, rec)
        await asyncio.to_thread(_autopilot_finish, title)
        return
    if action == "stop":
        if detail.get("unfixed"):
            await asyncio.to_thread(
                _autopilot_halt,
                title,
                detail.get("reason") or "stopped",
                _UNFIXED,
            )
            return
        hand_off = bool(detail.get("fix")) and _autopilot.can_hand_off(rec)
        told = await asyncio.to_thread(
            _autopilot_tell_agent, title, wt, rec, detail, hand_off
        )
        if told and hand_off:
            # Not a halt: the run waits for the fix and carries on from it.
            await asyncio.to_thread(
                _autopilot.hand_to_agent, title, detail, snap.get("head") or "", now
            )
            await asyncio.to_thread(_emit_autopilot, title)
            return
        await asyncio.to_thread(
            _autopilot_halt,
            title,
            detail.get("reason") or "stopped",
            _TOLD_AGENT if told else "",
        )
        return
    if now - float(rec.get("acted_at") or 0.0) < _AUTOPILOT_ACTION_COOLDOWN:
        return
    await _autopilot_act(title, wt, rec, snap, action, detail)


def _autopilot_note(title: str, rec: dict, note: str) -> None:
    """Record WHY a pass did nothing, write-on-change.

    The store's whole design is to avoid a write per 5s pass, so this only writes
    when the sentence actually changes — one write per phase transition. Without
    it every hold looked the same to the UI (and to the user) as a dead chain.
    """
    try:
        if (rec or {}).get("note") != note:
            _autopilot.update(title, note=note)
            _emit_autopilot_event(title)
    except Exception:  # noqa: BLE001
        pass


def _autopilot_wait(title, rec, snap, detail, now) -> None:
    """Book-keeping for a pass that decided to do nothing: keep the idle dwell
    honest, publish the reason, and halt if this step outlived its deadline."""
    limited = bool(snap.get("limited"))
    if detail.get("mark_idle") and rec.get("idle_since") is None:
        _autopilot.update(title, idle_since=now)
    elif (limited or snap["activity"] != "idle") and rec.get("idle_since") is not None:
        # A usage-limited session usually LOOKS idle (its turn ended, at the
        # CLI's own hook's word), so the dwell has to be dropped explicitly or it
        # keeps accruing through the outage — and the first pass after the window
        # reopens, before the resume nudge has landed, would find a satisfied
        # 30s settle and commit the half-finished work. The dwell is re-earned
        # after the limit lifts, which is the whole point of it.
        _autopilot.update(title, idle_since=None)
    # Remember that the agent was seen doing something: a clean tree only means
    # "finished with nothing to show" AFTER that, and means "not started yet"
    # before it. Throttled so a working agent costs at most one write per 30s.
    if str(snap.get("activity") or "") in ("working", "clarify"):
        worked = float(rec.get("worked_at") or 0.0)
        if now - worked > 30.0:
            _autopilot.update(title, worked_at=now)
    _autopilot_note(title, rec, detail.get("reason") or "")
    # STOP THE DEADLINE CLOCK WHILE A LIMIT HOLDS. A weekly cap can close the
    # window for days; every step deadline here is minutes-to-hours, so a run
    # that correctly waits out a limit would be halted for "no progress" by its
    # own patience. The stretch is credited back to step_since when the limit
    # lifts (two writes per episode, not one per 5s pass).
    was_limited = float(rec.get("limited_at") or 0.0)
    if limited and not was_limited:
        rec = _autopilot.update(title, limited_at=now) or rec
    elif not limited and was_limited:
        rec = (
            _autopilot.update(
                title,
                limited_at=0.0,
                step_since=float(rec.get("step_since") or now) + (now - was_limited),
            )
            or rec
        )
    step = rec.get("step") or "agent"
    # A run that has not acted yet is WAITING TO BE NEEDED, not making slow progress
    # through a step — so it gets the generous arm budget rather than the agent
    # step's. This is the "armed on a branch whose PR already exists" case: it may
    # legitimately sit idle for a long time before there is anything to carry.
    if not rec.get("step") and int(rec.get("commits") or 0) <= 0:
        deadline = _autopilot.ARM_WAIT_DEADLINE_S
    else:
        deadline = _AUTOPILOT_DEADLINES.get(step, _AUTOPILOT_DEADLINES["agent"])
    since = float(rec.get("step_since") or 0.0)
    if since and not limited and now - since > deadline:
        _autopilot_halt(
            title,
            "gave up waiting at %s after %d min — %s"
            % (step, deadline // 60, detail.get("reason") or "no progress"),
        )


def _autopilot_halt(title: str, reason: str, note: str = "") -> None:
    _autopilot.halt(title, reason, note)
    _emit_autopilot(title)


#: What the halted chip's tooltip adds once the agent has been sent its note.
_TOLD_AGENT = "MindFlock told the agent what failed and asked it to fix it."
#: ...and when a handed-back failure came back with nothing changed.
_UNFIXED = "The agent was told what failed but changed nothing."


def _autopilot_tell_agent(
    title: str, wt: str, rec: dict, detail: dict, resumes: bool = False
) -> bool:
    """A run that stopped on something the AGENT can fix (``detail["fix"]``: a
    failed pre-commit step or verification check, red CI, a conflicting PR)
    leaves that agent a short note saying what failed and to fix it — or to
    tell the user when it is a conflict that needs their call. Returns whether
    a note was posted. Never raises.

    The note rides the mailbox lane as a :data:`mailbox.NOTE_KIND`: typed once
    the agent is settled idle, held while a person is typing in the pane, never
    into a bare shell. Called just BEFORE the halt, which leaves the reason
    exactly as it was (team runs parse it) and carries :data:`_TOLD_AGENT` as
    its ``note`` when this returns True — one event, one toast. Off with
    ``repository.fasttrack_failure_notes``. A team run's member is skipped —
    its run already sends its own fix prompt. ``resumes``: the run will wait
    for the fix and carry on (a hand-off), and the note says so."""
    if not (detail or {}).get("fix"):
        return False
    try:
        from backend.config import settings as _settings

        if not _settings.load_settings().repository.fasttrack_failure_notes:
            return False
        if (rec or {}).get("source") == "run" or _run_member(title) is not None:
            return False
        cmd = ""
        if detail.get("fix") == "check" and wt:
            try:
                cmd = _wt_setup.load_config(wt).check_command or ""
            except Exception:  # noqa: BLE001 — the note just omits the command
                cmd = ""
        text = _autopilot.failure_note(detail, cmd, resumes=resumes)
        if not text:
            return False
        _mailbox.post(title, text, kind=_mailbox.NOTE_KIND)
        return True
    except Exception:  # noqa: BLE001 — a note is never worth breaking a pass
        return False


def _autopilot_draft_for_approval(title: str, wt: str, rec: dict) -> None:
    """A run about to park for "ask me first" BEFORE its commit gets the exact
    commit message now — written from the diff, as the commit itself would be —
    so the bell's approval card shows what will be committed, not "written from the diff
    when it commits". Unless the card's message is edited, ship-now commits
    exactly this text. A person's own message is never replaced; a failure
    leaves the card as it was (the commit still writes one)."""
    try:
        if (
            not rec.get("ask_first")
            or _autopilot.normalize_depth(rec.get("depth")) != "agent"
        ):
            return
        if _lanes.normalize_lane(rec.get("lane")) in ("", "leave"):
            return
        human = (
            str(rec.get("message") or "").strip()
            and not rec.get("message_auto")
            and not rec.get("message_written")
        )
        if human or not wt or not _server()._is_dirty(wt):
            return
        written = _autopilot_written_message(title, wt, rec)
        if written:
            _autopilot.update(
                title,
                message=written,
                message_auto=False,
                message_written=True,
                message_drafted=True,
            )
    except Exception:  # noqa: BLE001 — a draft is never worth failing a run
        pass


def _autopilot_finish(title: str) -> None:
    _autopilot.finish(title)
    _emit_autopilot(title)


async def _autopilot_act(title, wt, rec, snap, action, detail) -> None:
    """Perform one autopilot action by invoking the very code the buttons use.

    Push/PR/merge call the existing route coroutines rather than a second
    implementation of each, so gate order, verbatim error strings and the
    browser-handoff response shapes stay literally one implementation (the repo's
    own tests already drive these coroutines directly, so this is an established
    seam). Only the commit differs: it goes through ``_commit_into_shell`` so a
    SKIP list can be passed — a list that always comes from settings and never
    from a client.
    """
    srv = _server()
    now = snap["now"]
    fields = {"acted_at": now, "step_since": now}
    # Per-verb attempt cap. Without it a push the remote keeps refusing (protected
    # branch, non-fast-forward, an auth prompt) was re-typed every 15s forever: the
    # stage never moves, so next_action keeps saying "push", and acting re-stamps
    # step_since so the deadline can never fire either.
    issues = dict(rec.get("issues") or {})
    tries = int(issues.get(action) or 0)
    if tries >= _autopilot.MAX_ACTIONS_PER_VERB:
        await asyncio.to_thread(
            _autopilot_halt,
            title,
            "%s did not take after %d attempts — do it by hand"
            % (action.replace("_", " "), tries),
        )
        return
    issues[action] = tries + 1
    fields["issues"] = issues
    try:
        if action == "run_check":
            started = await asyncio.to_thread(_autopilot_run_check, title, wt)
            if not started:
                return
            fields["step"] = "check"
        elif action == "commit":
            done = await asyncio.to_thread(
                _autopilot_commit, title, wt, rec, detail, fields
            )
            if not done:
                return
        elif action == "push":
            resp = await srv.instance_push_branch(title)
            if resp.status_code >= 400:
                await asyncio.to_thread(_autopilot_halt, title, _resp_error(resp))
                return
            fields["step"] = "push"
        elif action == "make_pr":
            base = detail.get("base") or ""
            pr_args = {"base": base} if base else {}
            # A title / body written for this run (a team run's one PR, with a
            # section per piece) — otherwise both come from the commits.
            for key in ("title", "body"):
                if rec.get("pr_" + key):
                    pr_args[key] = rec["pr_" + key]
            resp = await srv.instance_make_pr(title, pr_args)
            if resp.status_code >= 400:
                await asyncio.to_thread(_autopilot_halt, title, _resp_error(resp))
                return
            body = _resp_json(resp)
            if body.get("ok") is False:
                # The branch is pushed but the PR needs a human click (no gh, no
                # token) — a stop with a link, not a failure to retry.
                await asyncio.to_thread(
                    _autopilot_halt,
                    title,
                    body.get("message") or "needs gh or a GitHub token to file the PR",
                )
                return
            fields["step"] = "pr"
            # Remember the PR so the client can open it exactly once, the same
            # courtesy the manual "Make PR" button does.
            if body.get("url"):
                fields["url"] = str(body["url"])
        elif action == "merge":
            resp = await srv.instance_merge_pr(title)
            if resp.status_code >= 400:
                msg = _resp_error(resp)
                low = msg.lower()
                # GitHub says "not mergeable" while required checks are still
                # queued. That is a WAIT — halting there raced the CI we are
                # deliberately waiting for. A red-zone refusal is never a wait,
                # even when a breached path happens to contain "pending".
                if not _resp_json(resp).get("red_zone_breaches") and any(
                    k in low
                    for k in (
                        "not mergeable",
                        "required status check",
                        "pending",
                        "waiting",
                        "in progress",
                    )
                ):
                    await asyncio.to_thread(
                        _autopilot_note, title, rec, "GitHub is not ready: " + msg
                    )
                    return
                await asyncio.to_thread(_autopilot_halt, title, msg)
                return
            body = _resp_json(resp)
            if body.get("ok") is False:
                await asyncio.to_thread(
                    _autopilot_halt, title, body.get("message") or "merge it on GitHub"
                )
                return
            _autopilot.update(title, step="merge", **fields)
            await asyncio.to_thread(_autopilot_finish, title)
            return
        else:
            return
    except Exception as err:  # noqa: BLE001 — a failed step halts, never crashes
        await asyncio.to_thread(_autopilot_halt, title, "%s failed: %s" % (action, err))
        return
    _autopilot.update(title, **fields)
    await asyncio.to_thread(_emit_autopilot, title)


def _pending_commit_message(wt: str) -> str:
    """The on-disk commit message, but ONLY when the last attempt failed.

    After a success there is nothing to retry, so a lingering message is stale and
    must not be adopted by the next thing that commits without one.
    """
    try:
        with open(os.path.join(wt, _server()._COMMIT_STATUS_FILE)) as fh:
            if fh.read().strip() in ("", "0"):
                return ""
    except OSError:
        return ""  # no attempt recorded — nothing is pending
    try:
        with open(os.path.join(wt, _server()._COMMIT_MSG_FILE)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _autopilot_default_message(inst, wt: str) -> str:
    """A commit subject for a fast-track press that carried none.

    Prefers the session's own identity, which for an intake session IS the item
    ("sc-1421-fix-login" / "issue-owner-repo-42"), so the commit reads as the work
    it is rather than as boilerplate.
    """
    try:
        title = str(getattr(inst, "Title", "") or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    if not title:
        # Called from the commit step, where the instance may not be in hand.
        try:
            title = str(_server()._current_branch(wt) or "").split("/")[-1]
        except Exception:  # noqa: BLE001
            title = ""
    branch = ""
    try:
        branch = _server()._current_branch(wt) or ""
    except Exception:  # noqa: BLE001
        branch = ""
    # Prefer the human name of the work the session came from (a ticket title, a
    # PR title) over the slug: "Add customer-submitted phone numbers to intake"
    # says what landed; "Work on shortcut-21039" says nothing.
    try:
        rec = _autopilot.get(title) or {}
        named = str(rec.get("message") or "").strip()
        if named:
            return named
    except Exception:  # noqa: BLE001
        pass
    subject = title or branch
    return ("Work on %s" % subject) if subject else "Work in progress"


def _autopilot_run_check(title: str, wt: str) -> bool:
    """Start the worktree's verification check. Returns whether one is now running.

    The push route soft-gates on this check, so the driver has to RUN it rather
    than discover the gate as a 409 and halt. Idempotent: an already-running check
    counts as started.
    """
    try:
        if _wt_setup.is_running(wt, "check"):
            return True
        cfg = _wt_setup.load_config(wt)
        if not cfg.check_command:
            return False
        return bool(_wt_setup.start_check(title, wt, cfg.check_command))
    except Exception:  # noqa: BLE001
        return False


def _autopilot_written_message(title: str, wt: str, rec: dict) -> str:
    """A model-written commit message for an autopilot commit, or ``""``.

    This is the fast-track half of the ✨ button: the same generator, at the one
    moment the diff is final. Never raises and never blocks the chain past its own
    timeout — a subject is not worth halting a run for, so every failure is ``""``
    and the caller falls back to the placeholder.
    """
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    # The intake item this run came from ("sc-1421-fix-login"), plus its NAME when
    # the run is an intake one — a ticket / PR / issue title is what the work was
    # asked to be, which is real context. The ⏩ button's own placeholder ("Work on
    # ft-session") is not, so rec["message"] is only borrowed for intake sources.
    hint = str(rec.get("item") or "")
    # A team run's task line is the same kind of context: what the work was
    # asked to be, written by a human.
    if rec.get("source") in ("tix", "pr", "iss", "run"):
        named = str(rec.get("message") or "").strip()
        if named and named != hint:
            hint = ("%s — %s" % (hint, named)) if hint else named
    return (
        _commit_message.suggest_or_none(
            wt,
            program=str(getattr(inst, "Program", "") or ""),
            timeout=_commit_message.TIMEOUT_AUTOPILOT,
            hint=hint,
            branch=srv._current_branch(wt) or "",
            fallback_program=srv.ENGINE.default_program(),
        )
        or ""
    )


def _autopilot_commit(title, wt, rec, detail, fields) -> bool:
    """Issue an autopilot commit (blocking). Returns whether to record success."""
    srv = _server()
    msg = rec.get("message") or ""
    if not msg:
        try:
            with open(os.path.join(wt, srv._COMMIT_MSG_FILE)) as fh:
                msg = fh.read().strip()
        except OSError:
            msg = ""
    # A placeholder — the arm route's "Work on ft-session", or the ticket / PR /
    # issue name an intake run carries — is worth replacing now that the diff
    # exists and is final: both describe the session or the request, not the
    # change that was actually made. A message a HUMAN typed is not touched, and
    # neither is the on-disk one a blocked commit left behind.
    #
    # On success the result is written back to the record so a pre-commit retry
    # commits the same sentence instead of paying for a second turn. A failure
    # leaves the flag up: retries are capped per verb, so at worst this costs one
    # bounded attempt each, and the run still commits under the placeholder.
    if not msg or rec.get("message_auto"):
        written = _autopilot_written_message(title, wt, rec)
        if written:
            msg = written
            fields["message"] = written
            fields["message_auto"] = False
            # Written from THIS diff: reused by a retry of this commit, never
            # carried into a later arm as a human's sentence.
            fields["message_written"] = True
    if not msg:
        # GENERATE one rather than halting. Arming on a CLEAN tree is the natural
        # way to use this — arm the session, let the agent work — and the route
        # records no message then, because there is nothing to describe yet. By the
        # time work exists the record still had none, so the run halted with "no
        # commit message to reuse" at the moment it was finally ready to commit.
        # An honest default subject beats refusing to commit the work.
        msg = _autopilot_default_message(srv.ENGINE.instances.get(title), wt)
    if not msg:
        _autopilot_halt(title, "no commit message to reuse")
        return False
    skip = [h for h in (detail.get("skip") or []) if h not in _autopilot.NEVER_SKIP]
    hook = detail.get("hook") or ""
    err = srv._commit_into_shell(title, wt, msg, ",".join(skip))
    if err is not None:
        _autopilot_halt(title, "could not start the commit: %s" % err)
        return False
    attempts = dict(rec.get("attempts") or {})
    if hook:
        attempts[hook] = int(attempts.get(hook) or 0) + 1
    fields.update(
        step="commit",
        attempts=attempts,
        commits=int(rec.get("commits") or 0) + 1,
        skipped=skip,
        idle_since=None,
    )
    if detail.get("skipping"):
        fields["reason"] = "skipped %s to get the commit through" % detail["skipping"]
    srv._forget_probes(title)
    _live_stage.watch(title, wt, "commit")
    return True


def _resp_error(resp) -> str:
    """The server's own error sentence out of a JSONResponse."""
    body = _resp_json(resp)
    return str(body.get("error") or "step failed")


def _resp_json(resp) -> dict:
    try:
        data = json.loads(resp.body)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _publish_autopilot(title: str) -> None:
    """Announce an autopilot change WITHOUT recomputing anything.

    For arming and disarming, which change no git state whatsoever — only a field
    in a small JSON file. The full :func:`_republish_session` was costing ~700ms
    on a cold cache (an ``ls-remote`` network round trip plus a ``gh`` call) and,
    because the routes call this on the event loop, it stalled every other request
    with it. Recording a target depth has no business paying for a PR lookup.

    So: emit the event, then patch JUST the ``autopilot`` block into the published
    row. That last part is what stops the next 4s poll — which serves the stored
    tick snapshot — from reverting the UI's optimistic toggle. Cost is a dict read
    and a short list scan.
    """
    try:
        _emit_autopilot_event(title)
        rows = _events.sessions_snapshot()
        for row in rows:
            if row.get("title") == title:
                row["autopilot"] = _autopilot_dto(title)
                row["lane"] = _row_lane(title)
                _events.patch_session_snapshot(title, row)
                return
    except Exception:  # noqa: BLE001
        pass


def _emit_autopilot_event(title: str) -> None:
    """The cheap half of :func:`_emit_autopilot`: the event only, no probes."""
    try:
        rec = _autopilot.get(title) or {}
        _events.BUS.emit(
            "session.autopilot_changed",
            session=title,
            new=rec.get("state") or "",
            data={
                "depth": rec.get("depth") or "",
                "step": rec.get("step") or "",
                "state": rec.get("state") or "",
                "reason": rec.get("reason") or "",
                "note": rec.get("note") or "",
                "url": rec.get("url") or "",
                "item": rec.get("item") or "",
                "skipped": list(rec.get("skipped") or []),
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _emit_autopilot(title: str) -> None:
    """Announce an autopilot change AND rebuild the session's published row.

    For the driver, after it has actually done something (a commit, push, PR or
    merge): the git-derived stage really did change, so the full recompute earns
    its cost. Blocking — the driver already calls it from a worker thread.

    Arm/disarm must use :func:`_publish_autopilot` instead; they change no git
    state and the routes that serve them run on the event loop.
    """
    _emit_autopilot_event(title)
    _server()._republish_session(title)


async def _autopilot_pass() -> None:
    """One pass over every armed session.

    Sessions are stepped one at a time rather than concurrently: the steps commit
    and push into a shared worktree, and serialising them is what keeps two chains
    on a copied session (which share one worktree) from racing.
    """
    titles = await asyncio.to_thread(_autopilot.all_titles)
    if not titles:
        return  # a flock with nothing armed costs one file read
    await asyncio.to_thread(_autopilot.prune, list(_server().ENGINE.instances.keys()))
    for title in titles:
        try:
            await _autopilot_step(title)
        except Exception:  # noqa: BLE001 — one bad chain can't stop the pass
            pass


async def _autopilot_loop() -> None:
    """Drive the autopilot forever (started by the lifespan).

    Work first, sleep after — so an armed chain resumes within one interval of a
    server restart rather than idling through the first sleep.
    """
    while True:
        try:
            await _autopilot_pass()
        except Exception:  # noqa: BLE001 — the pass must never die
            pass
        await asyncio.sleep(_AUTOPILOT_INTERVAL)
