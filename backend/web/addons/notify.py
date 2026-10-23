"""Notify addon: session-event notifications over two channels (roadmap B5).

The reference implementation of the *generic* extension path — manifest →
``core/slots.js`` imports the descriptor's ``module`` → the module registers
itself in ``window.mindflockAddons`` → ``init(ctx)`` subscribes to the client
event bus (``window.mindflock.events``). See docs/extensions.md.

What it does: notifies you when a session needs you (activity →
``clarify``), when its open PR leaves the review stage (merged / closed), when it
blows its cost budget, and a few opt-in transitions besides. One rule list
(:data:`NOTIFY_RULES`), two delivery channels:

* **Browser** — ``static/addons/notify.js`` applies the rules client-side, where
  the Notification API (and its permission prompt) lives. Only fires while a tab
  is open on a secure origin.
* **ntfy** (optional, off by default) — this module applies the same rules
  *server-side* on the event bus and pushes to the user's ntfy topic
  (:mod:`backend.web.core.ntfy`), so the alert reaches a phone with nothing open
  at all. That is the channel that makes "go check on session X" work while
  you're away from the machine.

The rules stay server-side data so the backend remains the one source of truth
for which transitions matter, and so both channels agree on what "notify me"
means — the per-rule switches in Settings → Notifications govern both.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.config import settings as settings_store
from backend.web.core import agent_io as _agent_io
from backend.web.core import aliases as _aliases
from backend.web.core import auth as _web_auth
from backend.web.core import events as _events
from backend.web.core import mobile_access, mobile_announce, ntfy, voice_actions

from .base import SECRET_MASK, Addon, AppContext, FrontendDescriptor

#: Event → notification rules both channels apply. ``old``/``new`` of ``None``
#: match any value; ``{session}`` in title/body is replaced with the envelope's
#: session title and ``{detail}`` with the envelope's ``data.detail`` (a
#: sentence the emitter wrote for humans; empty when the event has none). Each rule has a stable ``id``, a short ``label`` and a
#: one-line ``description`` for the settings UI (plain words, no placeholders —
#: ``title``/``body`` are the notification's own text), and a ``default_enabled`` flag: default-on rules are opt-out
#: (muted via settings.notifications.muted_rules); default-off rules are opt-in
#: (turned on via settings.notifications.enabled_rules) because they're noisy.
#:
#: ``priority``/``tags`` are ntfy-only presentation: priority 4 buzzes a phone
#: through most do-not-disturb setups, 2 lands silently, 3 is normal — so the
#: "come look at this" rules ring and the ambient ones don't. Tags are ntfy's
#: emoji shortcodes, kept to widely-supported names.
NOTIFY_RULES: List[dict] = [
    {
        # The rail's Offline state (sessionState.ts names this rule id for
        # its toast and badge gating): the agent process is gone and the chip
        # offers Restart.
        "id": "session_offline",
        "label": "A session stops unexpectedly",
        "description": "The agent closed. Restart it from the sidebar.",
        "event": "session.activity_changed",
        "old": None,
        "new": "offline",
        "title": "{session} is offline",
        "body": "The agent process is gone — Restart it from the rail.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["zzz"],
    },
    {
        "id": "needs_input",
        "label": "A session asks you a question",
        "description": "The agent is waiting for your answer.",
        "event": "session.activity_changed",
        "old": None,
        "new": "clarify",
        "title": "{session} needs you: Question",
        "body": "The agent is waiting on a clarification.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["question"],
    },
    {
        # Keyed on the PR lookup's own state, NOT on the stage leaving "pr".
        # The stage ladder drops off "pr" whenever the tree goes dirty or a
        # commit runs (agent_state._session_stage returns "agent"/"precommit"
        # before it ever consults the PR), so the old `stage_changed old="pr"`
        # rule fired "PR merged or closed" on every edit of a PR that was open
        # the whole time — then fired again as the session climbed back to "pr".
        # `old: "OPEN"` covers both real endings (MERGED and CLOSED).
        "id": "pr_closed",
        "label": "A pull request is merged or closed",
        "description": "The pull request is no longer open.",
        "event": "session.pr_state_changed",
        "old": "OPEN",
        "new": None,
        "title": "{session}: PR merged or closed",
        "body": "The pull request is no longer open.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["white_check_mark"],
    },
    {
        # The other side of a PR's life, and the one that is actually waiting on
        # a human: somebody approved it. Keyed on the REVIEW verdict rather than
        # on `mergeable_state`, which says "blocked" both for a missing review
        # and for a failing required check, and says nothing at all once the
        # approval lands. Emitted per real transition (agent_state.
        # _track_pr_review), so an approval that was already there when the
        # server started is never re-announced.
        "id": "pr_approved",
        "label": "A pull request is approved",
        "description": "A reviewer approved it. It's ready to merge.",
        "event": "session.pr_review_changed",
        "old": None,
        "new": "approved",
        "title": "{session}: PR approved",
        "body": "A reviewer approved the pull request — it is ready to merge.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["white_check_mark"],
    },
    {
        # Opt-in twin: the review came back asking for changes. Off by default
        # because the agent is usually still working in that session anyway, so
        # for most people it lands on a window they are already looking at.
        "id": "pr_changes_requested",
        "label": "A reviewer asks for changes",
        "description": "Someone requested changes on a session's pull request.",
        "event": "session.pr_review_changed",
        "old": None,
        "new": "changes_requested",
        "title": "{session}: changes requested",
        "body": "A reviewer asked for changes on the pull request.",
        "default_enabled": False,
        "priority": 3,
        "tags": ["memo"],
    },
    {
        # J5 — cost guardrail: emitted once per session when its estimated
        # cost crosses the configured per-session budget (data: {cost, budget}).
        "id": "budget_exceeded",
        "label": "A session goes over its cost budget",
        "description": "Set the budget in Settings → General.",
        "event": "session.budget_exceeded",
        "old": None,
        "new": None,
        "title": "{session} exceeded its cost budget",
        "body": "The session's estimated cost crossed its configured budget.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["moneybag"],
    },
    {
        # Roadmap D: the agent ran out of usage and is parked on its CLI's
        # limit screen. Actionable in the "stop waiting for this one" sense —
        # nothing will move until the provider's window resets, and MindFlock
        # picks it back up on its own when it does.
        "id": "usage_limit",
        "label": "A session runs out of usage",
        "description": "It waits until your plan's usage resets.",
        "event": "session.activity_changed",
        "old": None,
        "new": "limit",
        "title": "{session} ran out of usage",
        "body": "The agent hit its usage limit. MindFlock resumes it when the window resets.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["hourglass"],
    },
    {
        # The other end of the same story. Emitted once per reopening by the
        # drain-loop watcher (server._watch_one_limited), which only ever looks
        # at sessions that had run out — so this cannot fire on a window that
        # merely rolled over while everything was fine.
        "id": "usage_restored",
        "label": "Usage comes back",
        "description": "A session that ran out of usage can run again.",
        "event": "session.usage_restored",
        "old": None,
        "new": None,
        "title": "Usage is back",
        "body": "The provider's window reset — {session} can run again.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["arrows_counterclockwise"],
    },
    {
        # Opt-in: a session's work really is over — useful for babysitting a
        # long autonomous run.
        #
        # Keyed on ``session.turn_ended``, NOT on ``session.activity_changed``
        # with ``new == "idle"``. That was the same rule this used to be, and it
        # fired on a chip colour: at the end of every assistant turn (ten per
        # conversation), on a session that had merely booted to a bare prompt,
        # between two prompts of a draining queue, and on every re-opened window
        # — because the CLI's Stop hook says a turn ENDED and says nothing about
        # whether one ever began. ``turn_ended`` asserts the three things this
        # sentence has always claimed: the agent was observed working, it has
        # stayed idle since, and nothing is queued to wake it up.
        #
        # The id is unchanged on purpose: an existing opt-in in
        # settings.notifications.enabled_rules carries straight over and simply
        # gets quieter, with no migration.
        "id": "session_idle",
        "label": "A session finishes its work",
        "description": "The agent stopped and nothing else is queued.",
        "event": "session.turn_ended",
        "old": None,
        "new": None,
        "title": "{session} has finished",
        "body": "The agent finished its turn and has been idle since, with nothing queued.",
        # On by default (it is what a new user leaves sessions running FOR),
        # but it was opt-in until 2026-10: anyone who has saved a rule choice
        # keeps what they had — see ``was_opt_in`` in :func:`_rule_enabled`.
        "default_enabled": True,
        "was_opt_in": True,
        "priority": 2,
        "tags": ["zzz"],
    },
    {
        # Team runs (core.team_run_driver is the ONE emitter): an escalation
        # the RUN originated — stalled after two nudges, hooks failed twice, a
        # blocked report, a session gone after a restart, the budget spent.
        # Each fires once per (run, task, reason, incarnation). A dialog is
        # NOT re-announced here: needs_input already fired for that session.
        # The emitter writes the whole sentence into ``detail`` ("Q4
        # payments: PAY-421 hooks failed twice").
        "id": "run_needs_you",
        "label": "A team run needs you",
        "description": "A session in a team run is stuck or blocked.",
        "event": "run.needs_you",
        "old": None,
        "new": None,
        "title": "A team run needs you",
        "body": "{detail}",
        "default_enabled": True,
        "priority": 4,
        "tags": ["raising_hand"],
    },
    {
        # Once per group, when its last task is done (persisted, so a restart
        # never repeats it; a group that finished while the server was down
        # announces once after the boot quiet window).
        "id": "run_finished",
        "label": "A team run finishes",
        "description": "Every session in the team run is done.",
        "event": "run.finished",
        "old": None,
        "new": None,
        "title": "A team run finished",
        "body": "{detail}",
        "default_enabled": True,
        "priority": 3,
        "tags": ["checkered_flag"],
    },
    {
        # Opt-in: every member that reaches its lane. Its rail row and the
        # bell already show these; a push per PR is ambient, not actionable.
        "id": "run_task_shipped",
        "label": "A team run session ships",
        "description": "One session in a team run reached its goal, such as opening its pull request.",
        "event": "run.task_shipped",
        "old": None,
        "new": None,
        "title": "Shipped",
        "body": "{detail}",
        "default_enabled": False,
        "priority": 2,
        "tags": ["package"],
    },
    {
        # Opt-in (noisy): the commit hit the pre-commit lock — hooks are running
        # (stage -> "precommit"). See server._session_stage.
        "id": "precommit_running",
        "label": "Pre-commit hooks start",
        "description": "A commit is running your repo's pre-commit checks.",
        "event": "session.stage_changed",
        "old": None,
        "new": "precommit",
        "title": "{session}: pre-commit hooks running",
        "body": "A commit triggered the pre-commit hooks.",
        "default_enabled": False,
        "priority": 2,
        "tags": ["hourglass"],
    },
    {
        # Default-on (actionable, not noisy): a commit was blocked by the
        # pre-commit hooks and the tree is still dirty, so the session parks in
        # the "interrupt" stage (see server._session_stage). This is the failure
        # counterpart to precommit_running — it fires once per block, and the
        # session needs a re-commit to move on.
        "id": "precommit_failed",
        "label": "Pre-commit hooks block a commit",
        "description": "The commit didn't land. The session needs to commit again.",
        "event": "session.stage_changed",
        "old": None,
        "new": "interrupt",
        "title": "{session}: pre-commit hooks failed",
        "body": "A commit was blocked by the pre-commit hooks — re-commit to continue.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["x"],
    },
    {
        # Verify, opt-in: the headless one-shot finished WRITING a checklist.
        # ``session.test_plan_ready`` means "there are steps worth showing" —
        # the same narrow meaning the Verify dialog refetches on.
        #
        # Worth a rule because generation announces itself nowhere a human is:
        # a push kicks it off, it runs on a thread for up to
        # ``test_plans.TIMEOUT_GENERATE``, and the row says "writing…" until
        # somebody happens to be looking at it. Opt-in because a flock that
        # pushes all day writes one of these per session per branch, which is
        # ambient rather than actionable.
        "id": "verify_plan_ready",
        "label": "A checklist is ready",
        "description": "Verify finished writing a checklist for a push.",
        "event": "session.test_plan_ready",
        "old": None,
        "new": None,
        "title": "{session}: checklist ready",
        "body": "The agent finished writing the checklist for this work.",
        "default_enabled": False,
        "priority": 2,
        "tags": ["clipboard"],
    },
    {
        # The failure half of that same moment, and its own rule because a rule
        # matches exactly ONE event and ``test_plan_ready`` deliberately keeps
        # its success-only meaning (see ``server._generate_test_plan``).
        # Folded in, "tell me when the plan is written" would go silent in the
        # one case most worth telling a human about: the attempt that produced
        # no checklist at all.
        #
        # ON by default since round 4 (was opt-in): a push whose checklist
        # silently failed was the one Verify outcome nobody heard about. Anyone
        # who already saved a rule choice keeps it (``was_opt_in``).
        "id": "verify_plan_failed",
        "label": "A checklist couldn't be written",
        "description": "Open Verify to see why.",
        "event": "session.test_plan_failed",
        "old": None,
        "new": None,
        "title": "{session}: couldn't write the checklist",
        "body": "No checklist was written — open Verify for the reason.",
        "default_enabled": True,
        "was_opt_in": True,
        "priority": 3,
        "tags": ["warning"],
    },
    {
        # A checklist came DUE: the work shipped (merged, deploy wait over) and
        # its steps are ready to check — "your turn". On by default: it is the
        # moment the whole feature exists to deliver, and it lands minutes or
        # days after the push, usually while nobody is looking.
        #
        # ``ntfy: False`` — the phone push for this moment is
        # ``test_plans_driver._notify_test_plan_due``, which sends it at most
        # once per checklist and says how many steps are yours; this rule's
        # switch gates that push too, so one toggle governs both channels.
        "id": "verify_plan_due",
        "label": "A checklist is ready to check",
        "description": "The work shipped and its steps are ready to check.",
        "event": "session.test_plan_due",
        "old": None,
        "new": None,
        "title": "{session} shipped — ready to check",
        "body": "Its checklist is ready in Verify.",
        "default_enabled": True,
        "ntfy": False,
        "priority": 3,
        "tags": ["white_check_mark"],
    },
    {
        # An agent's check FOUND A FAILURE — the most valuable outcome Verify
        # has, and the one that must not wait for somebody to open the dialog.
        # The emitter stamps ``new = "failed"`` on ``test_plan_checked`` when
        # any step failed, so this matches only those; the opt-in rule below
        # still hears every finished run.
        "id": "verify_run_failed",
        "label": "A checklist fails",
        "description": "A step didn't do what was expected.",
        "event": "session.test_plan_checked",
        "old": None,
        "new": "failed",
        "title": "{session}: checklist failed",
        "body": "A step didn't do what was expected — open Verify to see which.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["x"],
    },
    {
        # Verify, opt-in: an agent finished WORKING a checklist — the far end of
        # a run. Starting one is loud (a toast, a row that changes, a terminal
        # that opens) and it lands its answers minutes later in a dialog the
        # user has almost certainly navigated away from.
        #
        # The envelope carries ``failed`` and ``needs_you``, which is what lets
        # the in-app toast say "8 passed, 3 need your eyes". A push cannot: the
        # templates fill ``{session}`` and ``{detail}``, and this event writes
        # no ``detail``. So the body says where the numbers are rather than
        # inventing them.
        "id": "verify_run_finished",
        "label": "An agent finishes a checklist",
        "description": "Open Verify to see what it found.",
        "event": "session.test_plan_checked",
        "old": None,
        "new": None,
        "title": "{session}: checklist run finished",
        "body": "The agent worked the checklist — open Verify for what it found.",
        "default_enabled": False,
        "priority": 3,
        "tags": ["ballot_box_with_check"],
    },
    {
        # …and the way a run ends having reported NOTHING: the agent window died,
        # or the two-hour deadline released it. Separate for the same reason as
        # ``verify_plan_failed`` — one rule, one event — and it earns the switch
        # more than most, because a released run reverts the plan to "not checked
        # yet", making a run that was started, billed and abandoned
        # indistinguishable from a button nobody ever pressed.
        "id": "verify_run_gave_up",
        "label": "A checklist run stops without answers",
        "description": "The checklist goes back to unchecked.",
        "event": "session.test_plan_gave_up",
        "old": None,
        "new": None,
        "title": "{session}: checklist run gave up",
        "body": "The run recorded no answers — the checklist is back to unchecked.",
        "default_enabled": False,
        "priority": 3,
        "tags": ["warning"],
    },
    {
        # Zones: the guard DID its job — the agent tried to edit a red-zone
        # path, or (green mode) a path outside its green scope, and was
        # refused (`data.kind` says which; `detail` names it). Opt-in: a block is the system working, and a
        # denied agent usually re-plans on its own. The monitor already dedupes
        # to once per (session, zone) per work cycle, so turning it on is one
        # push per zone per turn, not one per attempt.
        "id": "red_zone_blocked",
        "label": "An edit to an off-limits file is blocked",
        "description": "An agent tried to change a file in a zone you protected.",
        "event": "session.red_zone_blocked",
        "old": None,
        "new": None,
        "title": "{session}: zone guard held",
        "body": "MindFlock {detail}.",
        "default_enabled": False,
        "priority": 3,
        "tags": ["no_entry"],
    },
    {
        # …and the case the guard could NOT stop: a red-zone file — or, in
        # green mode, a file outside the scope — really changed
        # (a shell write past the heuristic, a detect-only CLI, an edit made
        # before the zone existed is excluded — that is seeded, not announced).
        # Default-on: this is the "go look before it gets pushed" moment.
        #
        # The body claims nothing about pushing on its own: the push gate
        # reads COMMITTED changes only and a git-ignored file never leaves
        # the machine, so the monitor's `detail` ends with what THIS breach
        # does ("pushing is blocked until it is reverted" / "committing it
        # would block pushes, PRs and merges" / "git-ignored, so never
        # pushed; restore it by hand"); `data.blocks_push` says the same.
        "id": "red_zone_breached",
        "label": "An off-limits file was changed",
        "description": "Look at it before it's pushed.",
        "event": "session.red_zone_breached",
        "old": None,
        "new": None,
        "title": "{session}: zone breached",
        "body": "The agent {detail}.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["warning"],
    },
    {
        # The protection itself was interfered with (guard file, hook config,
        # zone store). Default-on and loud: whatever did it, a human should know.
        "id": "red_zone_tampered",
        "label": "Zone protection was tampered with",
        "description": "Something changed the files that enforce your zones.",
        "event": "session.red_zone_tampered",
        "old": None,
        "new": None,
        "title": "{session}: red-zone guard tampered",
        "body": "{detail}.",
        "default_enabled": True,
        "priority": 4,
        "tags": ["rotating_light"],
    },
    {
        # A computer asks to join "Your devices" and waits for someone to
        # approve it on THIS one. Default-on and loud: nothing happens until a
        # human answers, and the request expires in 10 minutes. The body is the
        # emitter's "<host> · code 123 456" — the code is how the user checks
        # it's the computer in front of them (core.fleet.open_request).
        "id": "device_join",
        "label": "A computer asks to join your devices",
        "description": "Approve it on this computer within 10 minutes.",
        "event": "device.join_requested",
        "old": None,
        "new": None,
        "title": "Device wants to join",
        "body": "{detail}",
        "default_enabled": True,
        "priority": 4,
        "tags": ["computer"],
    },
    {
        # A template's SCHEDULED run couldn't start (no folder any more, the
        # repo moved, no free name). It fired with nobody watching — at 21:00
        # or overnight — so without a push the failure waits on a card in the
        # Templates dialog until someone happens to look. Default-on: only a
        # user with a schedule can ever get one.
        "id": "template_run_failed",
        "label": "A scheduled template run can't start",
        "description": "A template's scheduled run was refused. Its card in Templates says why.",
        "event": "template.run_failed",
        "old": None,
        "new": None,
        "title": "Scheduled run failed",
        "body": "{detail}",
        "default_enabled": True,
        "priority": 4,
        "tags": ["warning"],
    },
    {
        # A scheduled run skipped because the previous one hasn't finished.
        # Opt-in: a skip is the guard working (it stops a slow job stacking
        # sessions), and the card already says so.
        "id": "template_run_skipped",
        "label": "A scheduled template run is skipped",
        "description": "Its previous run hadn't finished, so this one didn't start.",
        "event": "template.run_skipped",
        "old": None,
        "new": None,
        "title": "Scheduled run skipped",
        "body": "{detail}",
        "default_enabled": False,
        "priority": 3,
        "tags": ["hourglass"],
    },
]

#: Rule fields the client never needs: opt-in/opt-out semantics (it gets the
#: resolved ``enabled``) and the ntfy-only presentation hints.
_INTERNAL_RULE_FIELDS = ("default_enabled", "was_opt_in", "priority", "tags", "ntfy")

#: At most one push per (session, RULE) per this many seconds — the key is the
#: rule id, not the event name, because three rules ride
#: ``session.activity_changed`` and an event-keyed window lets whichever fires
#: first swallow the others (a ``limit`` push eating a ``needs_input`` push,
#: both default-on). ``DEDUPE_MS`` in static/addons/notify.js keys the same way,
#: so both channels collapse a flapping transition identically.
#:
#: This is a FLAP collapser and nothing more. It is meaningless at turn cadence,
#: which is why "the agent has finished" is deduped by spending its evidence
#: instead — see ``server._note_turn_boundary``.
_DEDUPE_SECONDS = 5.0


def _unreadable_response() -> JSONResponse:
    """409 for a save refused because settings.json exists but can't be read
    (saving would replace it with defaults — and settings sync would spread
    "everything deleted" to every device)."""
    return JSONResponse({"error": settings_store.UNREADABLE_HINT}, status_code=409)


def _rule_enabled(rule: dict, muted: set, opted_in: set) -> bool:
    """Whether ``rule`` is active given the muted (opt-out) and enabled (opt-in)
    id sets. Default-on rules fire unless muted; default-off rules fire only when
    explicitly opted in.

    ``was_opt_in`` marks a rule that USED to be default-off. Its new default
    applies only to someone who never saved a rule choice (both lists empty):
    for anyone else, absence from ``enabled_rules`` was their answer — "off" —
    and a new default must not flip it. Its toggle keeps both lists in step
    (see ``set_rule``)."""
    rid = rule["id"]
    if rule.get("default_enabled", True):
        if rid in muted:
            return False
        if rule.get("was_opt_in") and (muted or opted_in) and rid not in opted_in:
            return False
        return True
    return rid in opted_in


def _enabled_rules() -> List[dict]:
    """The raw rules currently switched on (internal fields intact — this is the
    server-side dispatch view, not the client payload)."""
    try:
        notifs = settings_store.load_settings().notifications
        muted = set(notifs.muted_rules)
        opted_in = set(notifs.enabled_rules)
    except Exception:  # noqa: BLE001 — settings must never break dispatch
        muted, opted_in = set(), set()
    return [r for r in NOTIFY_RULES if _rule_enabled(r, muted, opted_in)]


def rule_enabled(rule_id: str) -> bool:
    """Whether one rule is switched on — for a sender outside this module that
    owns a rule's push (``ntfy: False``) and must still honour its switch."""
    return any(r["id"] == rule_id for r in _enabled_rules())


def _rules_with_state() -> List[dict]:
    """NOTIFY_RULES each tagged with its current ``enabled`` flag from settings.

    Internal fields (:data:`_INTERNAL_RULE_FIELDS`) are NOT exposed to the
    client — the frontend only needs the resolved ``enabled``.
    """
    try:
        notifs = settings_store.load_settings().notifications
        muted = set(notifs.muted_rules)
        opted_in = set(notifs.enabled_rules)
    except Exception:  # noqa: BLE001 — settings must never break the config read
        muted, opted_in = set(), set()
    return [
        {
            **{k: v for k, v in rule.items() if k not in _INTERNAL_RULE_FIELDS},
            "enabled": _rule_enabled(rule, muted, opted_in),
        }
        for rule in NOTIFY_RULES
    ]


def _matches(rule: dict, envelope: dict) -> bool:
    """Whether ``envelope`` satisfies ``rule`` (``None`` constraint = any value).

    The server-side twin of ``ruleMatches`` in static/addons/notify.js — keep the
    two in step, they decide the same thing for their own channel.
    """
    if rule.get("event") != envelope.get("event"):
        return False
    if rule.get("old") is not None and rule["old"] != envelope.get("old"):
        return False
    if rule.get("new") is not None and rule["new"] != envelope.get("new"):
        return False
    return True


def _session_branch(title: str) -> str:
    """The session's branch, off the ``/api/instances`` snapshot the bus keeps.

    Only naming needs it, so an empty answer is not a failure: a session the
    poll has not seen yet (or one on a device this node only proxies) simply
    gets named by its title, which is what happened before this looked at all.
    """
    try:
        for inst in _events.sessions_snapshot():
            if isinstance(inst, dict) and inst.get("title") == title:
                return str(inst.get("branch") or "")
    except Exception:  # noqa: BLE001 — naming must never break a push
        pass
    return ""


def _fill(template: str, envelope: dict) -> str:
    """``{session}`` → the session's DISPLAY name (the JS ``fill``'s twin).

    The envelope carries the raw title; the user reads the name in the rail. Two
    things stand between them and both belong here, or the push describes a
    session that does not appear to exist:

    * **The rename**, which the SPA syncs into ``core.aliases``.
    * **The pipeline label** for the sessions nobody renamed. A ticket session is
      titled by its provider slug — "shortcut-21431" — and the sidebar shows
      "(tix) sitecheck-social-stop-producing/shortcut-21431", built from the
      branch. Naming the push by the slug alone made the reader match a
      notification against a rail that agrees with it nowhere.

    The raw title remains the last resort, which is also the right answer for a
    hand-made session: that IS its name.

    ``{detail}`` → ``data.detail``, the human sentence an emitter wrote for
    exactly this purpose (the red-zone events: "blocked 3 edits to
    config.toml"); empty when the event carries none."""
    title = str(envelope.get("session") or "")
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    out = str(template or "")
    if "{session}" in out:
        out = out.replace(
            "{session}",
            _aliases.display_name(title, _session_branch(title)) or "session",
        )
    return out.replace("{detail}", str(data.get("detail") or ""))


# --------------------------------------------------------------------------- #
# Pocket mode: the agent's words in the push, and one-tap dialog buttons
# --------------------------------------------------------------------------- #
#: The two pushes that carry the agent's own words when
#: ``notifications.ntfy_agent_words`` is on, and whose click URL is the phone
#: page's VOICE deep link (``/m?s=<title>&voice=1``): a finished turn (its
#: spoken ``Say:`` line rides in the event) and a dialog (its question is read
#: off the screen at fire time). While the phone is locked nothing on the web
#: can speak, so the push body itself is what iOS Announce Notifications (or
#: any read-aloud app) says through the earbuds.
_SPOKEN_RULES = frozenset({"session_idle", "needs_input"})

#: Ceiling on a dialog push's body: the question plus the command it asks
#: about. ntfy truncates the iOS push itself at 100 characters and its app
#: restores the full text, so long is fine; unbounded is not.
MAX_DIALOG_BODY = 300

#: ntfy shows at most this many buttons.
MAX_ACTIONS = 3


def _agent_words_on() -> bool:
    """``notifications.ntfy_agent_words`` — off keeps every body generic
    (the words would travel through the ntfy server)."""
    try:
        return bool(settings_store.load_settings().notifications.ntfy_agent_words)
    except Exception:  # noqa: BLE001 — an unreadable store says nothing
        return False


def _read_dialog(title: str) -> Optional[dict]:
    """The dialog on ``title``'s visible screen, as ``GET /dialog`` serves it
    (``{id, parsed, question, command, options: [{key, label, kind}]}``) —
    only when its provider PARSED one; ``None`` for no session, no live
    pane, a capture failure, or an unparsed screen. Never raises: this runs
    inside the event dispatch, where a push is never worth an exception."""
    try:
        from backend.web import server as _server  # lazy: the addon loads first

        inst = _server.ENGINE.instances.get(title)
        if inst is None:
            return None
        name = _server._live_session_name(_server.tmux.to_mindflock_tmux_name(title))
        if name is None:
            return None
        body, err = _agent_io.current_dialog(name, getattr(inst, "Program", "") or "")
        if err is not None or not isinstance(body, dict) or not body.get("parsed"):
            return None
        return body
    except Exception:  # noqa: BLE001
        return None


def dialog_body(dialog: Optional[dict]) -> str:
    """The spoken body of a dialog push: the parsed question, then `` — `` and
    the command it asks about when there is one, capped at
    :data:`MAX_DIALOG_BODY`. ``""`` when there is nothing to say."""
    if not isinstance(dialog, dict):
        return ""
    question = " ".join(str(dialog.get("question") or "").split())
    command = " ".join(str(dialog.get("command") or "").split())
    if not question:
        return ""
    text = question + (" — " + command if command else "")
    if len(text) > MAX_DIALOG_BODY:
        text = text[: MAX_DIALOG_BODY - 1].rstrip() + "…"
    return text


def push_body(rule: dict, envelope: dict, dialog: Optional[dict]) -> "tuple[str, bool]":
    """``(body, spoken)`` for one push. ``spoken`` means the body IS the
    agent's words — a finished turn's ``data.say``, a dialog's question — so
    the caller leaves the click URL out of the text (a read-aloud app would
    spell it out letter by letter). Anything else gets the rule's own
    template, exactly as before."""
    generic = _fill(rule.get("body", ""), envelope)
    if rule.get("id") not in _SPOKEN_RULES or not _agent_words_on():
        return generic, False
    if rule["id"] == "session_idle":
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        say = " ".join(str(data.get("say") or "").split())
        return (say, True) if say else (generic, False)
    text = dialog_body(dialog)
    return (text, True) if text else (generic, False)


def dialog_actions(
    base: str, click: str, title: str, dialog: Optional[dict]
) -> List[dict]:
    """The buttons of a dialog push, ntfy's action shape, at most
    :data:`MAX_ACTIONS`: **Yes** and (when the dialog offers one) **No** as
    ``http`` POSTs to ``<base>/api/notify/act/<token>``, then **Open** as a
    ``view`` of the click URL. ``[]`` unless the dialog parsed with a ``yes``
    option — a free-text question has no one-tap answer.

    Each button is a single-use token bound to this dialog's id and key
    (:mod:`backend.web.core.voice_actions`) — nothing standing travels to the
    ntfy server or sits on a lock screen, and ``always`` is never offered: a
    standing permission must not be one tap away. No ``base`` (no tailnet,
    or the shared link — see ``mobile_announce.act_base``) means no http
    buttons at all; Open alone still helps."""
    if not isinstance(dialog, dict) or not dialog.get("parsed"):
        return []
    options = [o for o in (dialog.get("options") or []) if isinstance(o, dict)]
    yes = next((o for o in options if o.get("kind") == "yes"), None)
    if yes is None:
        return []
    no = next((o for o in options if o.get("kind") == "no"), None)
    actions: List[dict] = []
    dialog_id = str(dialog.get("id") or "")
    if base and dialog_id:
        for option, label in ((yes, "Yes"), (no, "No")):
            if option is None:
                continue
            key = str(option.get("key") or "")
            if key not in _agent_io.ANSWER_KEYS:
                continue
            token = voice_actions.issue(title, dialog_id, key)
            url, _ = ntfy.strip_token_param("%s/api/notify/act/%s" % (base, token))
            actions.append(
                {
                    "action": "http",
                    "label": label,
                    "url": url,
                    "method": "POST",
                    "clear": True,
                }
            )
    if click:
        url, _ = ntfy.strip_token_param(click)
        actions.append({"action": "view", "label": "Open", "url": url})
    return actions[:MAX_ACTIONS]


class NotifyAddon(Addon):
    id = "notify"
    label = "Notifications"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()
        self._unsubscribe = None
        # (session, event) -> monotonic ts of the last push, for _DEDUPE_SECONDS.
        self._last_push: dict = {}
        self._push_lock = threading.Lock()

    # --- ntfy channel (server-side dispatch) ------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        """Subscribe the ntfy channel to the event bus.

        Always subscribed, cheaply: the callback's first act is to check whether
        ntfy is configured at all, so an unconfigured install pays one settings
        read per event. Subscribing unconditionally means turning ntfy on in
        Settings takes effect immediately instead of on the next restart.
        """
        try:
            ntfy.set_loop(asyncio.get_running_loop())
        except RuntimeError:  # no loop: publish_soon falls back to a thread
            pass
        if self._unsubscribe is None and ctx is not None:
            self._unsubscribe = ctx.subscribe("*", self._on_event)

    async def on_shutdown(self, ctx: AppContext) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            finally:
                self._unsubscribe = None

    def _on_event(self, envelope: dict) -> None:
        """Push a matching event to ntfy. Runs on the emitting thread — so it
        stays tiny and hands the HTTP call off to the loop.

        Unlike the browser channel there is no replay to guard against (the bus
        replays only to reconnecting websockets) and no first-poll burst after a
        restart (``server._emit_state_changes`` seeds on first sighting without
        emitting), so a restart doesn't re-push old news.
        """
        try:
            cfg = ntfy.load()
            if not cfg.active:
                return
            data = envelope.get("data") or {}
            if envelope.get("event") == "device.join_requested" and data.get("via"):
                # A copy of a request waiting on another member (shown here
                # so it can be approved here): that member already pushed it.
                return
            for rule in _enabled_rules():
                # ``ntfy: False``: another sender owns this moment's phone push
                # (see the rule), so the generic dispatch stays out of it.
                if rule.get("ntfy") is False or not _matches(rule, envelope):
                    continue
                key = (str(envelope.get("session") or ""), str(rule["id"]))
                now = time.monotonic()
                with self._push_lock:
                    if now - self._last_push.get(key, 0.0) < _DEDUPE_SECONDS:
                        continue
                    self._last_push[key] = now
                session = str(envelope.get("session") or "")
                # Tap the notification -> the mobile view, already on the
                # session it is about (the voice deep link for the two pushes
                # that carry words). Cached, never probed here: this runs on
                # the emitting thread. Empty when there's no tailnet — and then
                # publish() falls back to the user's own configured click URL.
                # (Two call shapes on purpose: every other push keeps the
                # one-argument call it always made.)
                click = (
                    mobile_announce.click_for(session, voice=True)
                    if rule["id"] in _SPOKEN_RULES
                    else mobile_announce.click_for(session)
                )
                if click and rule["id"] == "device_join" and data.get("id"):
                    # Straight to the phone's Approve card for this request.
                    click += "#approve=" + str(data["id"])
                # A dialog push reads the dialog off the screen now (the same
                # parse GET /dialog serves): its question is the body, its
                # yes/no options become one-tap buttons. The emitter already
                # captured this screen to decide "clarify"; one more capture
                # per push is what the route costs too.
                dialog = _read_dialog(session) if rule["id"] == "needs_input" else None
                message, spoken = push_body(rule, envelope, dialog)
                actions = (
                    dialog_actions(mobile_announce.act_base(), click, session, dialog)
                    if dialog is not None
                    else []
                )
                if click and not spoken:
                    # Also in the text: a notification you have to go find the
                    # server for is a notification that made you get up. The
                    # click action covers most clients; the line covers the rest
                    # (and a phone that shows the push without acting on it).
                    # Not under the agent's words: that body is read aloud, and
                    # the click action (plus Open) carries the URL anyway.
                    message += "\n" + click
                ntfy.publish_soon(
                    cfg,
                    title=_fill(rule.get("title", ""), envelope),
                    message=message,
                    priority=rule.get("priority"),
                    tags=rule.get("tags"),
                    click=click or None,
                    actions=actions or None,
                )
        except Exception as err:  # noqa: BLE001 — a push never breaks an emit
            ntfy.log_error(
                "ntfy dispatch for %s failed: %s", envelope.get("event"), err
            )

    # --- routes ----------------------------------------------------------- #
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/notify")

        @router.get("/config")
        def get_config() -> JSONResponse:
            """The notification rules the frontend subscribes with, each tagged
            with its current ``enabled`` state so the client skips muted ones."""
            return JSONResponse({"rules": _rules_with_state()})

        @router.post("/rules/{rule_id}")
        def set_rule(
            rule_id: str,
            payload: dict,
            allowed: bool = Depends(_web_auth.configure_allowed),
        ) -> JSONResponse:
            """Turn one rule on/off (for BOTH channels). Default-on rules persist
            as an opt-out (``muted_rules``); default-off rules persist as an
            opt-in (``enabled_rules``). Unknown ids are rejected. Owner only
            (``auth.may_configure``): the rules are synced, and muting
            ``device_join`` everywhere would hide a stranger's join request."""
            if not allowed:
                return _web_auth.configure_refused()
            rule = next((r for r in NOTIFY_RULES if r["id"] == rule_id), None)
            if rule is None:
                return JSONResponse(
                    {"error": f"unknown rule: {rule_id}"}, status_code=404
                )
            enabled = bool((payload or {}).get("enabled", True))
            notifs = settings_store.load_settings().notifications
            muted = list(notifs.muted_rules)
            opted_in = list(notifs.enabled_rules)
            if rule.get("default_enabled", True):
                # Opt-out: on = absent from muted, off = present in muted.
                if enabled:
                    muted = [m for m in muted if m != rule_id]
                elif rule_id not in muted:
                    muted.append(rule_id)
                # A once-opt-in rule also records "on" as an opt-in, which is
                # what keeps it on once any other choice has been saved.
                if rule.get("was_opt_in"):
                    opted_in = [e for e in opted_in if e != rule_id]
                    if enabled:
                        opted_in.append(rule_id)
            else:
                # Opt-in: on = present in enabled, off = absent from enabled.
                if enabled and rule_id not in opted_in:
                    opted_in.append(rule_id)
                elif not enabled:
                    opted_in = [e for e in opted_in if e != rule_id]
            try:
                settings_store.update_settings(
                    notifications={"muted_rules": muted, "enabled_rules": opted_in}
                )
            except settings_store.SettingsUnreadable:
                return _unreadable_response()
            return JSONResponse({"rules": _rules_with_state()})

        @router.get("/ntfy")
        def get_ntfy() -> JSONResponse:
            """The ntfy channel's state for Settings → Notifications.

            Never returns the token — only ``has_token``. Includes a scannable
            QR of the subscribe URL (the fastest path from "configured here" to
            "subscribed on the phone") and, when no topic is set yet, a random
            one to offer.
            """
            return JSONResponse(_ntfy_view())

        @router.post("/ntfy")
        def set_ntfy(
            payload: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            """Save the ntfy channel config; returns the same view as ``GET``.

            Only the keys present are touched. The token follows the store's
            secret convention (empty / the mask sentinel = keep the saved one),
            with one extra safety: retargeting the channel at a *different host*
            without supplying a new token drops the old one rather than shipping
            server A's credential to server B.

            That convention has no way to say "I want no token at all", which
            matters here more than for the app's other secrets: an ntfy token is
            *optional* (public topics need none), and a wrong one is worse than
            none — ntfy rejects a bad credential with 401 instead of ignoring
            it, so a stray token breaks a push that would otherwise work.
            ``{"clear_token": true}`` is that escape hatch, and it wins over any
            ``token`` in the same payload: it is the destructive intent, and the
            only way to send both is to mean it.

            Owner only (``auth.may_configure``): the channel is synced, and
            a server/topic of someone else's would get every device's pushes.
            """
            if not allowed:
                return _web_auth.configure_refused()
            payload = payload or {}
            stored = settings_store.load_settings().notifications
            patch: dict = {}
            note = ""

            server = stored.ntfy_server
            if "server" in payload:
                server = ntfy.normalize_server(str(payload.get("server") or ""))
                patch["ntfy_server"] = server
            topic = stored.ntfy_topic
            if "topic" in payload:
                topic = str(payload.get("topic") or "").strip()
                patch["ntfy_topic"] = topic
            if "click_url" in payload:
                click, stripped = ntfy.strip_token_param(
                    str(payload.get("click_url") or "")
                )
                patch["ntfy_click_url"] = click
                if stripped:
                    note = (
                        "The access token was removed from the tap-to-open URL — "
                        "it would have been stored on the ntfy server."
                    )
            if "enabled" in payload:
                patch["ntfy_enabled"] = bool(payload.get("enabled"))
            if "agent_words" in payload:
                # The agent's words in push bodies (pocket mode). Synced like
                # the rest of the channel; the toggle sits in the same fold.
                patch["ntfy_agent_words"] = bool(payload.get("agent_words"))

            # Validate whenever the channel is (or is becoming) on, and whenever
            # a topic is present at all — but not "pick a topic" for someone who
            # is clearing the field on their way to switching the channel off.
            will_be_on = bool(patch.get("ntfy_enabled", stored.ntfy_enabled))
            if will_be_on or topic:
                problem = ntfy.validate(server, topic)
                if problem:
                    return JSONResponse({"error": problem}, status_code=400)

            token = str(payload.get("token") or "").strip()
            if payload.get("clear_token"):
                patch["ntfy_token"] = ""  # "" clears (see update_settings)
                if stored.ntfy_token:
                    note = (
                        (note + " ") if note else ""
                    ) + "The saved access token was cleared."
            elif token and token != SECRET_MASK:
                patch["ntfy_token"] = token
            elif stored.ntfy_token and not ntfy.same_host(
                stored.ntfy_server or ntfy.DEFAULT_SERVER, server
            ):
                patch["ntfy_token"] = ""  # "" clears (see update_settings)
                note = (
                    ((note + " ") if note else "")
                    + "The saved access token was cleared — it belonged to the previous ntfy server."
                )

            if patch:
                try:
                    settings_store.update_settings(notifications=patch)
                except settings_store.SettingsUnreadable:
                    return _unreadable_response()
            # Channel just came on: the first thing worth pushing to a phone
            # that has never heard from this machine is how to reach it.
            if patch.get("ntfy_enabled") and not stored.ntfy_enabled:
                mobile_announce.announce_soon(mobile_announce.REASON_NTFY)
            view = _ntfy_view()
            if note:
                view["note"] = note
            return JSONResponse(view)

        @router.post("/ntfy/test")
        async def test_ntfy(payload: Optional[dict] = None) -> JSONResponse:
            """Send one test push and report the verdict.

            Takes its config from the request body when supplied, so the user can
            verify a topic *before* saving it (same shape as the ticketing
            connection test). Exempt from the rate cap: it is an explicit,
            hand-driven action.
            """
            payload = payload or {}
            stored = settings_store.load_settings().notifications
            server = ntfy.normalize_server(
                str(payload.get("server") or stored.ntfy_server or "")
            )
            topic = str(payload.get("topic") or stored.ntfy_topic or "").strip()
            token = str(payload.get("token") or "").strip()
            if not token or token == SECRET_MASK:
                token = stored.ntfy_token
            problem = ntfy.validate(server, topic)
            if problem:
                return JSONResponse({"ok": False, "error": problem})
            click, _ = ntfy.strip_token_param(
                str(payload.get("click_url") or stored.ntfy_click_url or "")
            )
            cfg = ntfy.NtfyConfig(
                enabled=True,
                server=server,
                topic=topic,
                token=token,
                click_url=click,
            )
            # The test push is the first one anyone sees, so it shows what the
            # real ones will: the phone URL, tappable. Probed here (an async
            # route can afford it) rather than read from the cache — this is
            # also where a first-run install *fills* that cache.
            phone_url, _live = await asyncio.to_thread(mobile_access.tailnet_url)
            mobile_announce.remember_url(phone_url)
            message = (
                "Push notifications are working — this is where session "
                "alerts will land."
            )
            if phone_url:
                message += "\n" + phone_url
            ok, error = await ntfy.publish(
                cfg,
                title="MindFlock is connected",
                message=message,
                priority=3,
                tags=["bell"],
                click=phone_url or None,
                budgeted=False,
            )
            return JSONResponse({"ok": ok, "error": error})

        @router.post("/act/{token}")
        async def act(token: str) -> JSONResponse:
            """A dialog push's Yes / No button, pressed on the phone.

            ``token`` is the single-use capability the push carried
            (:mod:`backend.web.core.voice_actions`): it stands for one
            ``(session, dialog_id, key)`` and nothing else. Consuming it, this
            does exactly what ``POST /api/instances/{title}/answer`` does with
            ``{"keys": [key], "dialog_id": …, "by": "user"}`` — the same
            helper, so the dialog-id pin, the per-session answer lock, the
            budget lock and the "just answered" guard all apply — and answers
            the same way: 200 ``{"ok": true, "activity_before"}``, the answer
            route's own 409s passed through (``dialog_changed`` when the
            prompt on screen is no longer the one the button was made for,
            ``dialog_answered``, not waiting, over budget), 502 when tmux
            refused the key. 404 ``{error}`` for a token that is unknown,
            already used or older than ten minutes — the same answer for all
            three, on purpose.

            Public (no bearer token — the ntfy app on the phone holds none):
            ``auth._PUBLIC_VOICE_ACT`` opens POST on exactly this path shape.
            """
            grant = voice_actions.consume(token)
            if grant is None:
                return JSONResponse(
                    {"error": "that button has expired — open MindFlock to answer"},
                    status_code=404,
                )
            from backend.web import server as _server  # lazy: the addon loads first

            title = grant["title"]
            inst, err = _server._inst_or_404(title)
            if err is not None:
                return err
            text, keys, perr = _agent_io.parse_answer({"keys": [grant["key"]]})
            if perr:
                return JSONResponse({"error": perr}, status_code=400)
            dialog_id, by, perr = _agent_io.parse_answer_meta(
                {"dialog_id": grant["dialog_id"], "by": "user"}
            )
            if perr:
                return JSONResponse({"error": perr}, status_code=400)
            if await asyncio.to_thread(_server._budget_locked, title):
                return JSONResponse(
                    {
                        "error": "session is over budget — raise the budget to send",
                        "budget_locked": True,
                    },
                    status_code=409,
                )
            body, status = await asyncio.to_thread(
                _server._answer_dialog, inst, title, text, keys, dialog_id, by
            )
            return JSONResponse(body, status_code=status)

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- frontend --------------------------------------------------------- #
    def frontend(self) -> List[FrontendDescriptor]:
        return [
            FrontendDescriptor(
                id="notify",
                label="Notifications",
                where="settings",
                module="/addons/notify.js",
                api_base="/api/notify",
                order=40,
                # where="settings": no sidebar bar. slots.js still imports the
                # module (it keys on `module`, not `where`); the module has no
                # visible surface of its own — it subscribes to the event bus
                # and exposes enable/disable/state. The on/off toggle lives in
                # the bell dropdown and Settings → Notifications.
            )
        ]


def _ntfy_view() -> dict:
    """The ntfy channel as the settings screen needs it — never the token.

    ``suggested_topic`` is always a fresh random name: it seeds the field's
    placeholder when nothing is set AND backs the Generate button, so the client
    never has to invent a topic with its own weaker randomness.
    """
    cfg = ntfy.load()
    return {
        "enabled": cfg.enabled,
        "server": cfg.server,
        "server_default": ntfy.DEFAULT_SERVER,
        "topic": cfg.topic,
        "has_token": bool(cfg.token),
        "click_url": cfg.click_url,
        # Pocket mode: the agent's words (a finished turn's spoken line, a
        # dialog's question) in push bodies.
        "agent_words": _agent_words_on(),
        "configured": cfg.configured,
        "active": cfg.active,
        "public_server": cfg.is_public_server,
        "subscribe_url": cfg.subscribe_url,
        "last": ntfy.last_result(),
        "suggested_topic": ntfy.random_topic(),
        # Same renderer as the Settings → Your phone QR; None when segno is absent.
        "qr_svg": mobile_access.qr_svg(cfg.subscribe_url) if cfg.topic else None,
    }


def ntfy_status() -> dict:
    """The ntfy channel's state for Settings → Connections — never the token
    or the topic: ``{enabled, configured, active, server, last}`` where
    ``last`` is :func:`ntfy.last_result` (``{}`` before the first push)."""
    cfg = ntfy.load()
    return {
        "enabled": bool(cfg.enabled),
        "configured": bool(cfg.configured),
        "active": bool(cfg.active),
        # Never userinfo a person typed into the server URL.
        "server": str(cfg.host or "").rsplit("@", 1)[-1],
        "last": ntfy.last_result(),
    }
