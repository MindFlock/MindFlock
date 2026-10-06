"""Team runs — "work on these things together", driven by the server.

A RUN is a batch of work the user started at once (a few ticket IDs, a few
typed lines): MindFlock starts one session per item, at most ``concurrency`` at
a time with the rest queued, carries each one along its ship LANE (leave /
commit / push / PR / merge, :mod:`backend.web.core.lanes`), and surfaces only
what needs a human. On screen there is no "run" noun — sessions say how far
they go, a batch is a group header, the bell holds what is waiting on you and
Customize → Outbox lists what is shipping — but the server keeps a record, because that is what
gives deterministic spawning, queueing, retries and restart safety.

This module is the record and the brain, and it is deliberately free of I/O
beyond its own files:

* the STORE — one JSON file per run under ``~/.mindflock/runs/`` (override
  ``$MINDFLOCK_RUNS_DIR``), written atomically under a module lock, plus a
  ``<id>.lease`` so two servers (desktop + CLI) never drive one run;
* :func:`parse_items` — a pasted goal → ticket refs and task lines,
  deterministic and previewable before anything starts;
* :func:`plan_actions` — PURE: (run, observation, now) → the actions that move
  every task along its state machine. The driver
  (:mod:`backend.web.core.team_run_driver`) observes, calls it, performs the
  side effects and applies the results with :func:`apply`.

A TARGET, NOT A SCRIPT (the autopilot's rule). The file holds facts and
intent — reserved titles, incarnations, attempt and nudge counts, PR urls,
terminal states — never a position in a chain. Every pass re-derives the live
state from observation, so first start and resume-after-restart are one code
path. The run never commits, pushes or opens a PR itself: it arms the
autopilot at the task's lane and reads its record.

Task states::

    queued ─start─▶ starting ─row seen (incarnation)─▶ working
    starting ─create failed─▶ queued after 30s·2^n (×2) ─▶ failed
    working ─dialog─▶ needs_you(prompt) ─answered─▶ working
    working ─idle, worked, no progress 10 min─▶ nudge (≤2) ─▶ needs_you(stuck)
    working ─report blocked|failed─▶ needs_you(blocked)
    working ─autopilot acting─▶ shipping ─lane reached─▶ shipped
    shipping ─autopilot halted─▶ fix prompt + re-arm (×2) ─▶ needs_you(ship_halted)
    any ─session deleted by the user─▶ cancelled      any ─Skip─▶ skipped

Terminal: shipped, integrated, failed, cancelled, skipped.

ONE PR FOR ALL (``grouping: "together"``) and SPLITS (``split: true``): the
group has a LEAD session whose branch every member merges into. Members are
the lead's workers, forked from its commit, and stop at a commit; the server
then merges them back one at a time (:mod:`backend.web.core.git_merge`),
hands a conflict to the lead, runs the check on the merged branch, and the
release — your click — ships the lead's branch as ONE PR whose body has a
section per member. A split's members come from the lead's PLAN, which the
server validates (disjoint paths, red zones, the cap) and you approve::

    planning ─lead proposes─▶ plan_ready ─you approve─▶ running
    member committed ─▶ integrating ─merged (ancestry)─▶ integrated
    integrating ─conflict─▶ the lead resolves (×2) | needs_you(conflict)
    all merged ─▶ checking ─ok─▶ release_ready ─you release─▶ releasing ─▶ done
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import random
import re
import shutil
import string
import tempfile
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from backend.config.config import GetConfigDir
from backend.config.home_guard import guard

__all__ = [
    "runs_dir",
    "load",
    "save",
    "edit",
    "create",
    "list_runs",
    "claim_lease",
    "release_leases",
    "title_index",
    "task_ask_first",
    "lead_gone",
    "local_origin_text",
    "finish_phrase",
    "archive_old",
    "parse_items",
    "match_ticket",
    "task_title",
    "name_suggestion",
    "run_brief",
    "counts",
    "summary_dto",
    "run_dto",
    "task_dto",
    "plan_actions",
    "apply",
    "summarize",
    "is_terminal",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
VERSION = 1
#: Driver pass interval (the autopilot's cadence).
RUN_INTERVAL_S = 5.0
#: Idle with work behind it and no progress this long before a nudge.
STUCK_AFTER_S = 600.0
MAX_NUDGES = 2
#: Fix-prompt-and-re-arm attempts after an autopilot halt.
MAX_SHIP_RETRIES = 2
#: Create retries after the first failure (30s, then 60s), then ``failed``.
MAX_CREATE_RETRIES = 2
CREATE_BACKOFF_S = 30.0
#: A started task whose row has not appeared (and no failure was reported)
#: after this long is treated as the restart table treats it.
START_GRACE_S = 180.0
#: A working task's row may vanish for this long (an adopt/reload hiccup)
#: before it reads as gone.
MISSING_GRACE_S = 60.0
#: Clock slack when matching a row's created_at against when WE started it.
INCARNATION_SLACK_S = 5.0
#: An unrefreshed driver lease is honoured this long (the autopilot's value).
LEASE_STALE_S = 45.0
EVENTS_MAX = 200
MAX_CONCURRENCY = 8
MAX_ITEMS = 50
#: Finished runs older than this move to ``runs/archive/``.
KEEP_FINISHED_S = 30 * 86400.0

TASK_STATES = (
    "queued",
    "starting",
    "working",
    "needs_you",
    "shipping",
    "integrating",
    "shipped",
    "integrated",
    "failed",
    "cancelled",
    "skipped",
)
TERMINAL = frozenset({"shipped", "integrated", "failed", "cancelled", "skipped"})
#: The states that hold a concurrency slot. ``needs_you`` holds one on purpose:
#: it keeps parallel prompts down while you are away.
ACTIVE = frozenset({"starting", "working", "needs_you", "shipping", "integrating"})
NEEDS_YOU_REASONS = (
    "prompt",
    "stuck",
    "blocked",
    "ship_halted",
    "conflict",
    "budget",
    "restart",
    "approve",
)
#: What the team-run routes can do, advertised as ``caps.team_runs`` on
#: ``/api/config`` so the UI offers only what the server takes (an older
#: server that does not say reads as "no"):
#: ``together`` = ``policy.grouping: "together"`` (one PR for the group);
#: ``split`` = ``split: true`` (one line into parallel pieces, plan, release).
CAPABILITIES = {"split": True, "together": True}
#: Escalations the RUN originates and therefore announces (once each). A
#: dialog is not one — ``needs_input`` already fires for that session — and an
#: approval is something you asked for, shown in the bell.
ANNOUNCED_REASONS = frozenset(
    {"stuck", "blocked", "ship_halted", "conflict", "budget", "restart"}
)
RUN_STATES = (
    "running",
    "planning",
    "plan_ready",
    "checking",
    "release_ready",
    "releasing",
    "done",
    "done_with_failures",
    "cancelled",
)
RUN_FINISHED = frozenset({"done", "done_with_failures", "cancelled"})
LANES = ("leave", "commit", "push", "pr", "merge")

#: The fixed, provider-agnostic nudge typed (through the prompt queue, with its
#: never-type-into-a-dialog guard) into a session that went quiet.
NUDGE_TEXT = (
    "If you're done, make sure tests pass and stop; if you're stuck, say "
    "what's blocking you in one line."
)
#: After the autopilot's commit was refused by a hook / check.
FIX_HOOK_TEXT = (
    "MindFlock's commit failed at `{hook}`: {tail}. Fix it, keep the change "
    "scoped to this task, then stop."
)
_LANE_CLAUSE = {
    "leave": "MindFlock will not commit it: leave the work in the tree for review.",
    "commit": "MindFlock then commits it with a message written from the diff.",
    "push": "MindFlock then commits it and pushes the branch.",
    "pr": "MindFlock then commits it, pushes the branch and opens a pull request.",
    "merge": (
        "MindFlock then commits it, opens a pull request and merges it once "
        "checks pass."
    ),
}
#: The server-owned brief every task's prompt ends with (≤600 chars).
RUN_BRIEF = (
    '---\nThis session is one of {n} in the MindFlock group "{name}". Do only '
    "the task above, in this workspace. When it is done and its tests pass, "
    "stop and say in a few lines what changed. {lane} Do not push, open pull "
    "requests or merge yourself, and do not start other sessions."
)

#: A one-for-all member's lane clause (it stops at a commit; the group ships
#: once, from the lead).
_TOGETHER_CLAUSE = (
    "Commit it on this branch when it is done and tested, then call "
    '{report}(status="done", summary=<what changed>, '
    'details="Tests: <command> — <result>") if you have it. MindFlock merges '
    "this branch with the group's other lines into one branch for a single PR."
)

# --- split / one-for-all briefs (server → agent; plain text, no CLI flags) --
#: Appended to a split lead's task (≤600 chars, see the test). The lead only
#: PROPOSES; the server validates, the user approves, the server starts.
LEAD_BRIEF = (
    '---\nMindFlock split: you lead group "{name}" (run {run}). Read the code, '
    "then call {propose}(run_id={run}, pieces=[{{title, "
    "prompt, paths}}], why) with 2-{max} pieces whose path globs don't overlap; "
    "each prompt must stand on its own. Commit any shared groundwork first: "
    "workers fork from your last commit. Don't spawn sessions or edit their "
    "files; once the user approves, MindFlock starts them and merges each "
    "back into your branch."
)
#: A one-for-all group's lead: an integration point, idle until a conflict.
INTEGRATOR_BRIEF = (
    'MindFlock group "{name}" (run {run}): {n} lines, one PR. You are its '
    "integration session. MindFlock starts the lines as your workers, merges "
    "each finished branch into yours and runs the checks. Do nothing until it "
    "asks you to resolve a merge conflict: then resolve it, commit, and call "
    "{integrated} if you have it. Don't edit files "
    "otherwise, spawn sessions, push or open pull requests. Reply OK."
)
#: Appended to every piece's prompt.
PIECE_BRIEF = (
    '---\nThis is one piece of the MindFlock split "{name}" (lead: {lead}). '
    "Change only files under: {paths} — this workspace is fenced to them "
    "(tests and lockfiles stay writable). When it is done and tested, commit "
    'it on this branch, then call {report}(status="done", '
    'summary=<what changed, 1-3 lines>, details="Tests: <command> — <result>"). '
    "Don't merge, push, open pull requests or start sessions: MindFlock merges "
    "this branch into {lead}'s."
)
#: The lead's conflict hand-off (a mailbox message from MindFlock).
CONFLICT_TEXT = (
    'MindFlock could not merge `{branch}` (piece "{piece}") into your branch: '
    "conflicts in {files}. Run `git merge --no-ff {branch}`, resolve them "
    "keeping both sides' intent, commit the merge, then call "
    '{integrated}(run_id="{run}", task_id="{task}", '
    "head_sha=<your new HEAD>). MindFlock also notices the merge by itself."
)
#: The check failed on the merged branch.
CHECK_FIX_TEXT = (
    "MindFlock ran `{command}` on your merged branch and it failed: {tail}. "
    "Make the suite pass with the smallest fix, commit it, then stop — "
    "MindFlock re-runs the check when you are done."
)
#: "Ask for a different split".
REPLAN_TEXT = (
    'The user asked for a different split of group "{name}". {note}Propose '
    'again with {propose}(run_id="{run}", …).'
)
#: How many times a conflict is handed to the lead before it asks you.
MAX_CONFLICT_HANDOFFS = 2
#: The lead went idle after a hand-off and stayed idle this long without the
#: piece being merged → that hand-off failed.
CONFLICT_IDLE_S = 90.0
#: A merge that keeps erroring (not a conflict) asks you after this many.
MAX_MERGE_ERRORS = 3
#: The lead's tree stays dirty (nothing can merge) this long → ask you.
MERGE_BLOCKED_S = 600.0
#: How long a member must sit idle, its work committed by itself, before the
#: run counts it done without its own "done" (a report, or a turn-end event
#: — which an armed autopilot record mutes).
DONE_QUIET_S = 60.0
#: Check failures handed back to the lead before it asks you.
MAX_CHECK_FIXES = 2
#: After a check-fix hand-off: the lead idle this long → re-run the check.
CHECK_FIX_IDLE_S = 60.0
MIN_PIECES = 2
#: How a split's pieces run, chosen on the plan card and recorded on the run
#: (``run["mode"]``, "" until the plan is approved):
#: ``worktrees`` — each piece in its own worktree, merged back into the
#: lead's branch (the default); ``same_folder`` — every piece an extra agent
#: in the LEAD's own folder, fenced to its paths, and MindFlock commits each
#: piece's paths itself when it is done (nothing to merge).
SPLIT_MODES = ("worktrees", "same_folder")
#: The trailer on a commit MindFlock made for a same-folder piece: the
#: record of which piece a commit is — after a restart, by the branch's
#: history alone (never by anyone's word).
SF_TRAILER = "MindFlock-Piece"
#: A same-folder piece's brief (instead of PIECE_BRIEF).
PIECE_SF_BRIEF = (
    '---\nThis is one piece of the MindFlock split "{name}" (lead: {lead}). '
    "Other pieces work in this same folder at the same time. Change only "
    "files under: {paths} — nothing else is yours, not even tests or "
    "lockfiles outside them. Don't run git add, commit, stash, reset or "
    "switch branches: MindFlock commits exactly your paths when you are "
    'done. When it is done and tested, call {report}(status="done", '
    'summary=<what changed, 1-3 lines>, details="Tests: <command> — '
    "<result>\"). Don't push, open pull requests or start sessions."
)
#: Added to the brief of a lead that sits on the trunk (an in-place session
#: on main, say): the pieces start from its last commit, which must not be a
#: commit the split put on the trunk.
LEAD_TRUNK_CLAUSE = (
    " You are on {branch}, the trunk: commit nothing here — put any shared "
    "groundwork into one of the pieces."
)

_LOCK = threading.RLock()
_INDEX_TTL_S = 1.0
_INDEX: Dict[str, object] = {"at": 0.0, "map": {}}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def runs_dir() -> str:
    """Where run files live: ``$MINDFLOCK_RUNS_DIR`` (tests point it at tmp —
    a hand-run server without it would write the user's real runs) or
    ``<config dir>/runs``."""
    env = os.environ.get("MINDFLOCK_RUNS_DIR")
    if env:
        return guard(env, "team-runs dir")
    return os.path.join(GetConfigDir(), "runs")


_ID_RE = re.compile(r"^r_[a-z0-9]{4,16}$")


def valid_id(run_id: str) -> bool:
    return bool(_ID_RE.match(str(run_id or "")))


def _path(run_id: str) -> str:
    return os.path.join(runs_dir(), run_id + ".json")


def _lease_path(run_id: str) -> str:
    return os.path.join(runs_dir(), run_id + ".lease")


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".run.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _new_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    while True:
        rid = "r_" + "".join(random.choice(alphabet) for _ in range(6))
        if not os.path.exists(_path(rid)):
            return rid


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _s(value) -> str:
    return str(value or "")


def _normalize_task(t) -> dict:
    """A task in canonical shape: the migration mechanism (no version bump for
    added fields) and the guard that a hand-mangled file cannot crash a pass."""
    t = t if isinstance(t, dict) else {}
    kind = _s(t.get("kind"))
    state = _s(t.get("state"))
    att = t.get("attempts") if isinstance(t.get("attempts"), dict) else {}
    lane = _s(t.get("lane"))
    report = t.get("report") if isinstance(t.get("report"), dict) else None
    return {
        "id": _s(t.get("id")),
        "kind": kind if kind in ("ticket", "task", "piece") else "task",
        "source": _s(t.get("source")),
        "ticket_id": _s(t.get("ticket_id")),
        "text": _s(t.get("text")),
        "paths": [str(p) for p in (t.get("paths") or []) if isinstance(p, str)],
        "title": _s(t.get("title")),
        "branch": _s(t.get("branch")),
        "repo_root": _s(t.get("repo_root")),
        "incarnation": _f(t.get("incarnation")),
        "state": state if state in TASK_STATES else "queued",
        "reason": _s(t.get("reason")),
        "detail": _s(t.get("detail")),
        "attempts": {
            "create": max(0, _i(att.get("create"))),
            "ship": max(0, _i(att.get("ship"))),
        },
        "nudges": max(0, _i(t.get("nudges"))),
        "lane": lane if lane in LANES else None,
        # "Ask me before it ships" chosen for THIS member (None = the group's):
        # a person's per-member choice must survive every re-arm.
        "ask_first": (
            bool(t.get("ask_first")) if t.get("ask_first") is not None else None
        ),
        "pr_url": _s(t.get("pr_url")),
        "merged_sha": _s(t.get("merged_sha")),
        "report": report,
        "started_at": _f(t.get("started_at")),
        "finished_at": _f(t.get("finished_at")),
        "cost_usd": _f(t.get("cost_usd")),
        # --- bookkeeping (facts, never a chain position) ---
        "adopted": bool(t.get("adopted")),
        "start_now": bool(t.get("start_now")),
        "retry_at": _f(t.get("retry_at")),
        "missing_since": _f(t.get("missing_since")),
        "worked": bool(t.get("worked")),
        "progress": _s(t.get("progress")),
        "progress_at": _f(t.get("progress_at")),
        "nudge_id": _s(t.get("nudge_id")),
        "nudge_at": _f(t.get("nudge_at")),
        "nudge_seen_at": _f(t.get("nudge_seen_at")),
        "report_seen": _f(t.get("report_seen")),
        "held": bool(t.get("held")),
        # A commit message a PERSON wrote (the bell approval's edit), kept
        # here while a pause holds the member (its autopilot record is
        # disarmed then) so the resume commits it as written.
        "message": _s(t.get("message"))[:5000],
        # "reserved" while this run holds the ticket's ingestion-ledger
        # reservation (handed back if it never starts), else "".
        "ledger": "reserved" if t.get("ledger") == "reserved" else "",
        # The title/branch the FIRST attempt used: a "retry fresh" numbers
        # from these (<base>-2, -3…), never from a title that merely ends in
        # digits.
        "base_title": _s(t.get("base_title")),
        "base_branch": _s(t.get("base_branch")),
        "flag": _s(t.get("flag")),
        # --- one-for-all / split (facts about the merge back) ---
        # The lead commit this task's branch was cut from, and the piece head
        # that was merged (both full shas).
        "base_sha": _s(t.get("base_sha")),
        "head_sha": _s(t.get("head_sha")),
        "commits": [str(c) for c in (t.get("commits") or []) if isinstance(c, str)][
            :50
        ],
        "conflict": _normalize_conflict(t.get("conflict")),
        "conflict_fixed": bool(t.get("conflict_fixed")),
        "tests": _s(t.get("tests"))[:300],
        "report_text": _s(t.get("report_text"))[:6000],
        "ready_at": _f(t.get("ready_at")),
        "merged_at": _f(t.get("merged_at")),
        "merge_errors": max(0, _i(t.get("merge_errors"))),
        "blocked_since": _f(t.get("blocked_since")),
        # Green zones ("only here") applied to the piece's worktree.
        "fenced": bool(t.get("fenced")),
        # Same folder: commits the piece made ITSELF (against its brief)
        # whose files are all its own — kept as its commits, never mixed.
        "self_commits": [
            str(c) for c in (t.get("self_commits") or []) if isinstance(c, str)
        ][:20],
    }


def _normalize_conflict(c) -> Optional[dict]:
    if not isinstance(c, dict):
        return None
    return {
        "files": [str(f) for f in (c.get("files") or []) if isinstance(f, str)][:50],
        "attempts": max(0, _i(c.get("attempts"))),
        "at": _f(c.get("at")),
    }


def _normalize_piece(p) -> Optional[dict]:
    if not isinstance(p, dict):
        return None
    paths = p.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    return {
        "title": _s(p.get("title")).strip()[:60],
        "prompt": _s(p.get("prompt")).strip()[:4000],
        "paths": [str(x).strip() for x in (paths or []) if isinstance(x, str)][:20],
    }


def _normalize_plan(d) -> Optional[dict]:
    if not isinstance(d, dict):
        return None
    state = _s(d.get("state"))
    pieces = [x for x in (_normalize_piece(p) for p in d.get("pieces") or []) if x]
    return {
        "state": state if state in ("proposed", "approved") else "proposed",
        "round": max(1, _i(d.get("round"), 1)),
        "pieces": pieces,
        "why": _s(d.get("why"))[:2000],
        "by": _s(d.get("by")) or "user",
        "proposed_at": _f(d.get("proposed_at")),
        "approved_at": _f(d.get("approved_at")),
        "base_sha": _s(d.get("base_sha")),
        "note": _s(d.get("note"))[:1000],
    }


def _normalize_lead(d) -> Optional[dict]:
    if not isinstance(d, dict) or not _s(d.get("title")):
        return None
    return {
        "title": _s(d.get("title")),
        "branch": _s(d.get("branch")),
        "base_branch": _s(d.get("base_branch")),
        "incarnation": _f(d.get("incarnation")),
        "adopted": bool(d.get("adopted")),
        "started_at": _f(d.get("started_at")),
        "missing_since": _f(d.get("missing_since")),
        "briefed": bool(d.get("briefed")),
        # A lead that works directly in its folder (in place), or sits on
        # its base/trunk branch: it can plan a split, but "separate
        # worktrees" gives the pieces a NEW lead of their own (run.origin).
        "in_place": bool(d.get("in_place")),
        "trunk": bool(d.get("trunk")),
    }


def _normalize_origin(d) -> Optional[dict]:
    """The session the user split when the run had to start its own lead
    (an in-place or trunk session, split into separate worktrees): it
    planned, and is never merged into, switched or pushed."""
    if not isinstance(d, dict) or not _s(d.get("title")):
        return None
    return {
        "title": _s(d.get("title")),
        "branch": _s(d.get("branch")),
        "head": _s(d.get("head")),
        "in_place": bool(d.get("in_place")),
        "trunk": bool(d.get("trunk")),
    }


def _normalize_sf(d) -> dict:
    """A same-folder split's facts: the commit the pieces started from, the
    paths already dirty then (never anyone's stray), foreign commits already
    judged, and the stray paths / commits standing now."""
    d = d if isinstance(d, dict) else {}

    def _strs(key, cap):
        return [str(x) for x in (d.get(key) or []) if isinstance(x, str)][:cap]

    return {
        "base": _s(d.get("base")),
        "baseline": _strs("baseline", 500),
        "seen_foreign": _strs("seen_foreign", 200),
        "stray": _strs("stray", 50),
        "stray_commits": _strs("stray_commits", 20),
        "stray_at": _f(d.get("stray_at")),
    }


CHECK_STATES = ("pending", "none", "running", "fixing", "ok", "failed", "skipped")


def _normalize_check(d) -> dict:
    d = d if isinstance(d, dict) else {}
    state = _s(d.get("state"))
    tests = d.get("tests")
    return {
        "state": state if state in CHECK_STATES else "pending",
        "command": _s(d.get("command"))[:500],
        "tests": int(tests) if isinstance(tests, int) and tests >= 0 else None,
        "summary": _s(d.get("summary"))[:600],
        "sha": _s(d.get("sha")),
        "attempts": max(0, _i(d.get("attempts"))),
        "started_at": _f(d.get("started_at")),
        "finished_at": _f(d.get("finished_at")),
        "fix_sent_at": _f(d.get("fix_sent_at")),
    }


RELEASE_STATES = ("none", "ready", "releasing", "done", "handoff", "failed")


def _normalize_release(d) -> dict:
    d = d if isinstance(d, dict) else {}
    state = _s(d.get("state"))
    return {
        "state": state if state in RELEASE_STATES else "none",
        "pr_url": _s(d.get("pr_url")),
        "compare_url": _s(d.get("compare_url")),
        "head_sha": _s(d.get("head_sha")),
        "title": _s(d.get("title"))[:256],
        "body": _s(d.get("body"))[:60000],
        "base": _s(d.get("base")),
        "branch": _s(d.get("branch")),
        "lane": _s(d.get("lane")) if _s(d.get("lane")) in LANES else "",
        "files": max(0, _i(d.get("files"))),
        "add": max(0, _i(d.get("add"))),
        "del": max(0, _i(d.get("del"))),
        "commits": max(0, _i(d.get("commits"))),
        "conflict_fixes": max(0, _i(d.get("conflict_fixes"))),
        # The lead's origin when it is a folder on this machine (a provisioned
        # workspace cloned from a checkout with no forge remote): the release
        # can only push there, and never claims a PR.
        "local_origin": _s(d.get("local_origin"))[:1024],
        "detail": _s(d.get("detail"))[:600],
        "at": _f(d.get("at")),
    }


def _normalize(d) -> dict:
    d = d if isinstance(d, dict) else {}
    pol = d.get("policy") if isinstance(d.get("policy"), dict) else {}
    lane = _s(pol.get("lane"))
    grouping = _s(pol.get("grouping"))
    release = _s(pol.get("release"))
    state = _s(d.get("state"))
    summ = d.get("summary") if isinstance(d.get("summary"), dict) else None
    events = [e for e in (d.get("events") or []) if isinstance(e, dict)]
    return {
        "v": VERSION,
        "id": _s(d.get("id")),
        "name": _s(d.get("name")) or "Group",
        "created_at": _f(d.get("created_at")),
        "created_by": _s(d.get("created_by")) or "user",
        "state": state if state in RUN_STATES else "running",
        "paused": bool(d.get("paused")),
        "pause_reason": _s(d.get("pause_reason")),
        "repo_root": _s(d.get("repo_root")),
        "program": _s(d.get("program")),
        "policy": {
            "lane": lane if lane in LANES else "pr",
            "ask_first": bool(pol.get("ask_first")),
            "grouping": grouping if grouping in ("each", "together") else "each",
            "release": release if release in ("auto", "ask") else "ask",
        },
        "concurrency": min(MAX_CONCURRENCY, max(1, _i(d.get("concurrency"), 3))),
        "budget_usd": max(0.0, _f(d.get("budget_usd"))),
        # A one-for-all group (and a split) merges every line into ONE
        # branch — the LEAD's — and ships that as one PR.
        "split": bool(d.get("split")),
        # A split's one line: what the lead is asked to cut into pieces.
        "goal": _s(d.get("goal"))[:4000],
        "lead": _normalize_lead(d.get("lead")),
        # How the pieces run ("" until the plan is approved; see SPLIT_MODES).
        "mode": _s(d.get("mode")) if _s(d.get("mode")) in SPLIT_MODES else "",
        "origin": _normalize_origin(d.get("origin")),
        "sf": _normalize_sf(d.get("sf")),
        "plan": _normalize_plan(d.get("plan")),
        "tasks": [_normalize_task(t) for t in (d.get("tasks") or [])],
        "check": _normalize_check(d.get("check")),
        "release": _normalize_release(d.get("release")),
        "events": events[-EVENTS_MAX:],
        "summary": summ,
        # --- bookkeeping ---
        "rev": max(0, _i(d.get("rev"))),
        "updated_at": _f(d.get("updated_at")),
        "finished_at": _f(d.get("finished_at")),
        "waiting_for_usage": bool(d.get("waiting_for_usage")),
        # (run, task, reason, incarnation) keys already announced — persisted,
        # so a restart never re-announces a standing escalation.
        "announced": [str(k) for k in (d.get("announced") or [])][-500:],
        "budget_announced": bool(d.get("budget_announced")),
    }


def load(run_id: str) -> Optional[dict]:
    """The normalized run, or None."""
    if not valid_id(run_id):
        return None
    with _LOCK:
        raw = _read_json(_path(run_id))
    return _normalize(raw) if raw is not None else None


def save(run: dict, now: Optional[float] = None) -> dict:
    """Persist ``run`` atomically (bumps ``rev``); returns the stored copy."""
    ts = float(now if now is not None else time.time())
    with _LOCK:
        out = _normalize(run)
        out["rev"] = int(run.get("rev") or 0) + 1
        out["updated_at"] = ts
        _write_json(_path(out["id"]), out)
        _INDEX["at"] = 0.0
    run["rev"] = out["rev"]
    run["updated_at"] = ts
    return copy.deepcopy(out)


_FLOCK = {"depth": 0, "fd": None}


@contextlib.contextmanager
def _store_lock():
    """The run store's lock ACROSS processes as well as threads: ``_LOCK``
    (in-process, reentrant) plus an ``fcntl.flock`` on ``<runs dir>/.lock``
    held for the outermost section. Two servers sharing one ``~/.mindflock``
    used to be able to both claim a lease, and a route on the non-driving
    server could lose an update (a lost pause turns every member into
    "leave")."""
    with _LOCK:
        if _FLOCK["depth"] == 0:
            fd = None
            try:
                import fcntl

                d = runs_dir()
                os.makedirs(d, exist_ok=True)
                fd = os.open(os.path.join(d, ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:  # noqa: BLE001 — no flock here: in-process only
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                fd = None
            _FLOCK["fd"] = fd
        _FLOCK["depth"] += 1
        try:
            yield
        finally:
            _FLOCK["depth"] -= 1
            if _FLOCK["depth"] == 0 and _FLOCK["fd"] is not None:
                try:
                    os.close(_FLOCK["fd"])  # releases the flock
                except OSError:
                    pass
                _FLOCK["fd"] = None


@contextlib.contextmanager
def edit(run_id: str):
    """Load-modify-save under the store lock: ``with edit(rid) as run:`` —
    ``run`` is None when there is no such run (nothing is saved then). The
    routes (worker threads) and the driver both write through this — in this
    process or another server's — so a route's change is never clobbered by a
    pass that loaded before it."""
    with _store_lock():
        run = load(run_id)
        before = _fingerprint(run) if run is not None else None
        yield run
        # Only a real change is written: ``rev`` is what a long-poll for
        # "change" waits on, so a pass that observed nothing new must not
        # move it (nor rewrite the file every 5s).
        if run is not None and _fingerprint(run) != before:
            save(run)


def _fingerprint(run: dict) -> str:
    body = {k: v for k, v in run.items() if k not in ("rev", "updated_at")}
    return json.dumps(body, sort_keys=True, default=str)


def create(fields: dict, now: Optional[float] = None) -> dict:
    """A new run from ``fields`` (normalized, with a fresh id), saved."""
    ts = float(now if now is not None else time.time())
    with _store_lock():
        run = _normalize(dict(fields, id=_new_id(), created_at=ts, rev=0))
        for n, t in enumerate(run["tasks"], 1):
            t["id"] = t["id"] or "t%d" % n
        return save(run, now=ts)


def remove(run_id: str) -> None:
    """Delete a run that never got going (its lead could not be created)."""
    if not valid_id(run_id):
        return
    with _store_lock():
        for path in (_path(run_id), _lease_path(run_id)):
            try:
                os.unlink(path)
            except OSError:
                pass
        _INDEX["at"] = 0.0


def list_runs(include_finished: bool = True) -> List[dict]:
    """Every run (newest first), normalized."""
    d = runs_dir()
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    with _LOCK:
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = _read_json(os.path.join(d, name))
            if raw is None:
                continue
            run = _normalize(raw)
            if not run["id"]:
                continue
            if not include_finished and run["state"] in RUN_FINISHED:
                continue
            out.append(run)
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, not ours to signal
    return True


def _host() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def _lease_live(held: dict, ts: float) -> bool:
    """Whether a lease someone ELSE holds still counts: refreshed within
    :data:`LEASE_STALE_S` by a process that is still alive. A server that
    restarted left its old process's lease behind; when that process is dead
    on this host the lease is stale at once — the new server's boot
    reconcile must not be refused for 45 s."""
    if ts - _f(held.get("owner_at")) > LEASE_STALE_S:
        return False
    pid = _i(held.get("pid"))
    host = _s(held.get("host"))
    if pid and host and host == _host() and pid != os.getpid():
        return _pid_alive(pid)
    return True


def claim_lease(run_id: str, owner: str, now: Optional[float] = None) -> bool:
    """Take or refresh the driver lease. False when ANOTHER live server holds
    it — then this server only reads the run. Read-then-write under the store
    lock, which spans processes, so two servers can never both win."""
    ts = float(now if now is not None else time.time())
    path = _lease_path(run_id)
    with _store_lock():
        held = _read_json(path) or {}
        other = _s(held.get("owner"))
        if other and other != owner and _lease_live(held, ts):
            return False
        _write_json(
            path, {"owner": owner, "owner_at": ts, "pid": os.getpid(), "host": _host()}
        )
    return True


def release_leases(owner: str) -> int:
    """Drop every lease ``owner`` holds (server shutdown), so a restarted
    server drives its runs — and reconciles them — on its first pass."""
    d = runs_dir()
    n = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    with _store_lock():
        for name in names:
            if not name.endswith(".lease"):
                continue
            path = os.path.join(d, name)
            held = _read_json(path) or {}
            if _s(held.get("owner")) == owner:
                try:
                    os.unlink(path)
                    n += 1
                except OSError:
                    pass
    return n


def lease_holder(run_id: str) -> str:
    held = _read_json(_lease_path(run_id)) or {}
    return _s(held.get("owner"))


def archive_old(now: Optional[float] = None) -> int:
    """Move finished runs older than :data:`KEEP_FINISHED_S` to ``archive/``."""
    ts = float(now if now is not None else time.time())
    moved = 0
    arch = os.path.join(runs_dir(), "archive")
    for run in list_runs():
        if run["state"] not in RUN_FINISHED:
            continue
        done_at = run["finished_at"] or run["updated_at"]
        if not done_at or ts - done_at < KEEP_FINISHED_S:
            continue
        with _LOCK:
            os.makedirs(arch, exist_ok=True)
            try:
                shutil.move(_path(run["id"]), os.path.join(arch, run["id"] + ".json"))
                moved += 1
            except OSError:
                continue
            try:
                os.unlink(_lease_path(run["id"]))
            except OSError:
                pass
    if moved:
        _INDEX["at"] = 0.0
    return moved


def title_index(now: Optional[float] = None) -> Dict[str, dict]:
    """``title -> {id, name, task, role, grouping}`` for every session a
    (non-archived) run owns — the row's ``run`` block. A skipped or cancelled
    task has let its session go and is not listed. Cached for a second and
    invalidated by every save in this process, because it is read once per row
    per snapshot."""
    ts = float(now if now is not None else time.time())
    with _LOCK:
        if ts - float(_INDEX["at"]) <= _INDEX_TTL_S:
            return dict(_INDEX["map"])  # type: ignore[arg-type]
    out: Dict[str, dict] = {}
    for run in list_runs():
        lead = run.get("lead")
        if lead and lead["title"] and run["state"] != "cancelled":
            out.setdefault(
                lead["title"],
                {
                    "id": run["id"],
                    "name": run["name"],
                    "task": "",
                    "role": "lead",
                    "grouping": "together",
                    "lane": run["policy"]["lane"],
                    "ask_first": run["policy"]["release"] == "ask",
                    "incarnation": float(lead.get("incarnation") or 0.0),
                },
            )
        for t in run["tasks"]:
            if not t["title"] or t["state"] in ("cancelled", "skipped", "queued"):
                continue
            out.setdefault(
                t["title"],
                {
                    "id": run["id"],
                    "name": run["name"],
                    "task": t["id"],
                    "role": "piece" if t["kind"] == "piece" else "task",
                    "grouping": run["policy"]["grouping"],
                    "lane": task_lane(run, t),
                    "ask_first": task_ask_first(run, t),
                    "incarnation": float(t["incarnation"] or 0.0),
                },
            )
    with _LOCK:
        _INDEX["map"] = out
        _INDEX["at"] = ts
    return dict(out)


def owner_of_title(title: str, exclude_run: str = "") -> Optional[Tuple[dict, dict]]:
    """``(run, task)`` for a run that holds ``title`` in a NON-terminal task
    (the duplicate-ownership guard), or None."""
    for run in list_runs(include_finished=False):
        if run["id"] == exclude_run:
            continue
        lead = run.get("lead")
        if lead and lead["title"] == title:
            # A group's lead belongs to it for as long as the group runs.
            return run, {"id": "", "title": title, "kind": "lead", "state": "working"}
        for t in run["tasks"]:
            if t["title"] == title and t["state"] not in TERMINAL:
                return run, t
    return None


def owner_of_branch(key: str, rows: Iterable[dict], exclude_run: str = ""):
    """``(run, task)`` owning the branch ``key`` (``repo::branch``) through any
    live row of a non-terminal task — duplicate windows share a branch, so
    ownership is a fact about the branch, not the title."""
    titles = {
        str(r.get("title") or "")
        for r in rows
        if isinstance(r, dict) and _branch_key(r) == key
    }
    for title in titles:
        hit = owner_of_title(title, exclude_run)
        if hit:
            return hit
    return None


def _branch_key(row: dict) -> str:
    repo = _s(row.get("repo")).strip()
    branch = _s(row.get("branch")).strip()
    return (repo + "::" + branch) if repo and branch else ""


# --------------------------------------------------------------------------- #
# Parsing a goal
# --------------------------------------------------------------------------- #
#: One token that names a ticket: a URL, ``PAY-412`` / ``sc-123`` /
#: ``ENG-9``, ``owner/repo#12``, or ``#12``.
_TICKET_TOKEN = re.compile(
    r"^(?:https?://\S+|[A-Za-z][A-Za-z0-9_]{0,19}-\d{1,9}|[\w.-]+/[\w.-]+#\d+|#\d+)$"
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+")


def parse_items(text: str) -> List[dict]:
    """A pasted goal → ``[{"kind": "ticket", "ref"} | {"kind": "task", "text"}]``.

    One thing per line. A line made ONLY of ticket-shaped tokens (separated by
    spaces, commas or semicolons) is that many tickets — ``PAY-412 PAY-415``
    is two; a line with any other word in it is one task line, so "Fix
    PAY-412 rounding" stays a sentence. Bullets and numbering are stripped,
    blank lines skipped, repeated refs dropped. Deterministic, so the preview
    shows exactly what Start will do."""
    items: List[dict] = []
    seen = set()
    for raw in str(text or "").splitlines():
        line = _BULLET.sub("", raw).strip()
        if not line:
            continue
        tokens = [t for t in re.split(r"[\s,;]+", line) if t]
        if tokens and all(_TICKET_TOKEN.match(t) for t in tokens):
            for tok in tokens:
                key = tok.lower().rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                items.append({"kind": "ticket", "ref": tok})
            continue
        key = "task:" + line.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append({"kind": "task", "text": line[:2000]})
        if len(items) >= MAX_ITEMS:
            break
    return items[:MAX_ITEMS]


def match_ticket(ref: str, rows: Iterable[dict]) -> Tuple[Optional[dict], str]:
    """Resolve a ticket ref against the Intake ticket listing →
    ``(row, "")``, ``(None, "")`` when not listed, or ``(None, error)`` when it
    is ambiguous across sources."""
    want = str(ref or "").strip().rstrip("/").lower()
    if not want:
        return None, ""
    hits = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        keys = {
            _s(t.get("slug")).lower(),
            _s(t.get("id")).lower(),
            _s(t.get("session")).lower(),
            _s(t.get("url")).rstrip("/").lower(),
        }
        keys.discard("")
        if want in keys:
            hits.append(t)
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        srcs = sorted({_s(t.get("source")) for t in hits})
        return None, "matches tickets on several sources (%s)" % ", ".join(srcs)
    return None, ""


_SLUG_STOP = frozenset({"a", "an", "the", "to", "of", "on", "in", "for", "and"})


def task_title(text: str, taken: Iterable[str] = ()) -> str:
    """A short, readable, unique session title for a task line: its first few
    meaningful words, slugged ("Per-user rate limit on /webhooks" →
    "per-user-rate-limit-webhooks"), suffixed -2, -3… against ``taken``."""
    taken = set(taken or ())
    words = [w for w in re.split(r"[^A-Za-z0-9-]+", str(text or "").lower()) if w]
    words = [w.strip("-") for w in words if w.strip("-")]
    meaningful = [w for w in words if w not in _SLUG_STOP] or words
    base = ""
    for w in meaningful:
        cand = (base + "-" + w) if base else w
        if len(cand) > 32:
            break
        base = cand
        if base.count("-") >= 5:
            break
    base = (base or "task")[:32].strip("-") or "task"
    title, n = base, 2
    while title in taken:
        title = "%s-%d" % (base, n)
        n += 1
    return title


def name_suggestion(items: List[dict]) -> str:
    """A group name to prefill: the tickets' shared project key ("PAY
    tickets"), else the first line's opening words, else "N items"."""
    tickets = [i for i in items if i.get("kind") == "ticket"]
    keys = {
        _s(i.get("ref") or i.get("id")).split("-")[0].upper()
        for i in tickets
        if "-" in _s(i.get("ref") or i.get("id"))
    }
    if tickets and len(tickets) == len(items) and len(keys) == 1:
        return "%s tickets" % keys.pop()
    first = next((i for i in items if i.get("kind") == "task"), None)
    if first is not None:
        words = _s(first.get("text")).split()
        name = " ".join(words[:4]).rstrip(" :;,.-—")
        if len(words) > 4:
            name += "…"
        if len(items) > 1:
            name += " + %d more" % (len(items) - 1)
        return name[:60]
    return "%d items" % len(items) if items else "Group"


def tools_for(provider: str = "") -> dict:
    """How this CLI spells the MindFlock tools a brief names (Claude lists
    them as ``mcp__mindflock__<tool>``, other CLIs by the bare name — the
    playbooks' one rule, so no brief hard-codes a CLI's convention)."""
    from backend.mcp import playbooks as _pb

    return {
        "propose": _pb.tool_name("propose_run_plan", provider),
        "report": _pb.tool_name("report_result", provider),
        "integrated": _pb.tool_name("report_integrated", provider),
    }


def run_brief(run: dict, provider: str = "") -> str:
    """The server-owned brief appended to every task's prompt (≤600 chars)."""
    lane = run["policy"]["lane"]
    clause = (
        _TOGETHER_CLAUSE.format(**tools_for(provider))
        if is_together(run)
        else _LANE_CLAUSE.get(lane, "")
    )
    return RUN_BRIEF.format(
        n=len(run["tasks"]),
        name=run["name"][:60],
        lane=clause,
    )


def lead_brief(run: dict, max_pieces: int, provider: str = "") -> str:
    """The split lead's brief (appended to its task). A lead on its trunk is
    also told to commit nothing there."""
    out = LEAD_BRIEF.format(
        name=run["name"][:60], run=run["id"], max=max_pieces, **tools_for(provider)
    )
    lead = run.get("lead") or {}
    if lead.get("trunk"):
        out += LEAD_TRUNK_CLAUSE.format(branch=lead.get("branch") or "the trunk")
    return out


def integrator_brief(run: dict, provider: str = "") -> str:
    """A one-for-all group's lead prompt."""
    return INTEGRATOR_BRIEF.format(
        name=run["name"][:60], run=run["id"], n=len(run["tasks"]), **tools_for(provider)
    )


def piece_brief(run: dict, paths: Iterable[str], provider: str = "") -> str:
    lead = (run.get("lead") or {}).get("title") or "the lead"
    shown = ", ".join("`%s`" % p for p in list(paths)[:8]) or "(none)"
    tmpl = PIECE_SF_BRIEF if same_folder(run) else PIECE_BRIEF
    return tmpl.format(
        name=run["name"][:60], lead=lead, paths=shown, **tools_for(provider)
    )


def same_folder(run: dict) -> bool:
    """A split whose pieces run in the lead's own folder (no merge)."""
    return bool(run.get("split")) and run.get("mode") == "same_folder"


def lead_base(title: str) -> str:
    """The name a split's pieces are titled after: the lead's, without the
    ``-lead`` (or a started lead's ``-split``) suffix MindFlock gave it."""
    for suffix in ("-lead", "-split"):
        if title.endswith(suffix) and len(title) > len(suffix):
            return title[: -len(suffix)]
    m = re.match(r"^(.+)-split-\d+$", title)
    return m.group(1) if m else title


def sf_trailer(run_id: str, task_id: str) -> str:
    """The trailer line on a same-folder piece's commit."""
    return "%s: %s/%s" % (SF_TRAILER, run_id, task_id)


# --------------------------------------------------------------------------- #
# Splits: the plan (validated, never trusted) and the one PR it ships as
# --------------------------------------------------------------------------- #
_GLOB_CHARS = re.compile(r"[*?\[]")


def _compile_paths(paths: Iterable[str]) -> Tuple[List[str], List[str]]:
    """``(compiled regexes, errors)`` for a piece's globs, with the red-zone
    matcher — the very one its green zones will be enforced with."""
    from backend.config import red_zones as _rz

    res, errs = [], []
    for p in paths:
        try:
            res.append(_rz.compile_pattern(p))
        except (ValueError, TypeError) as err:
            errs.append("%s: %s" % (p, err))
    return res, errs


def _hits(res: List[str], rel: str) -> bool:
    from backend.config import red_zones as _rz

    return any(_rz.matches(r, rel) for r in res)


def _glob_sample(glob: str) -> str:
    """A concrete path ``glob`` matches (each wildcard filled with ``x``)."""
    g = glob.strip().lstrip("/")
    g = re.sub(r"\[!?([^\]])[^\]]*\]", r"\1", g)
    g = g.replace("**", "x").replace("*", "x").replace("?", "x")
    return g.rstrip("/") or "x"


def _globs_overlap(ga: str, gb: str) -> bool:
    """Whether two path globs can match one path that does not exist yet
    (both globs: ``ls-files`` cannot tell). Conservative: a sample of either
    matching the other, or one covering a whole directory the other lives in."""
    from backend.config import red_zones as _rz

    try:
        ra, rb = _rz.compile_pattern(ga), _rz.compile_pattern(gb)
    except (ValueError, TypeError):
        return False
    if _rz.matches(rb, _glob_sample(ga)) or _rz.matches(ra, _glob_sample(gb)):
        return True
    for whole, other in ((ga, gb), (gb, ga)):
        w = whole.strip().lstrip("/")
        o = other.strip().lstrip("/")
        m = _GLOB_CHARS.search(w)
        head, rest = (w[: m.start()], w[m.start() :]) if m else (w, "")
        if rest in ("**", "**/*", "**/**") and o.startswith(head):
            return True
        # "**/*.py" against "tests/**": a path under the other's directory
        # with this one's tail ("tests/x.py") belongs to both.
        if o.startswith("**/"):
            cand = _glob_sample(head + o[3:])
            if _rz.matches(ra, cand) and _rz.matches(rb, cand):
                return True
    return False


def validate_plan(
    pieces, files: Iterable[str], red_res: Iterable[str], max_pieces: int
) -> Tuple[List[dict], List[dict]]:
    """``(normalized pieces, problems)`` for a proposed split. PURE.

    ``files`` are the lead worktree's tracked files (``git ls-files``),
    ``red_res`` the compiled red zones. A plan is refused (one problem per
    reason, ``{"piece", "error"}``) when:

    * it has fewer than :data:`MIN_PIECES` or more than ``max_pieces`` pieces;
    * a piece has no title, no prompt, no paths, a bad glob, or a title that
      repeats another's;
    * two pieces SHARE a file — over ``git ls-files`` matches, and over a
      not-yet-existing literal path one piece names and the other's globs
      cover (``overlaps <other> on <file>``);
    * every existing file a piece covers sits in a red zone (it could change
      nothing).

    A glob that matches no file yet is allowed: a piece may create files."""
    files = [f for f in files if f]
    red = list(red_res)
    out: List[dict] = []
    problems: List[dict] = []
    raw = pieces if isinstance(pieces, list) else []
    for p in raw:
        n = _normalize_piece(p)
        if n is not None:
            out.append(n)
    if len(out) < MIN_PIECES:
        problems.append(
            {"piece": "", "error": "propose at least %d pieces" % MIN_PIECES}
        )
    if len(out) > max_pieces:
        problems.append(
            {
                "piece": "",
                "error": "at most %d pieces (MINDFLOCK_MAX_CHILDREN)" % max_pieces,
            }
        )
    seen_titles: set = set()
    compiled: List[Tuple[dict, List[str]]] = []
    for n in out:
        name = n["title"] or "?"
        if not n["title"]:
            problems.append({"piece": name, "error": "a piece needs a title"})
        elif n["title"].lower() in seen_titles:
            problems.append({"piece": name, "error": "two pieces share this title"})
        seen_titles.add(n["title"].lower())
        if not n["prompt"]:
            problems.append({"piece": name, "error": "a piece needs a prompt"})
        if not n["paths"]:
            problems.append(
                {"piece": name, "error": "a piece needs the paths it may change"}
            )
            compiled.append((n, []))
            continue
        res, errs = _compile_paths(n["paths"])
        for e in errs:
            problems.append({"piece": name, "error": "bad path glob " + e})
        compiled.append((n, res))
    # Which existing files each piece covers.
    covers = []
    for n, res in compiled:
        covers.append({f for f in files if res and _hits(res, f)})
    for i, (a, res_a) in enumerate(compiled):
        for j in range(i + 1, len(compiled)):
            b, res_b = compiled[j]
            shared = sorted(covers[i] & covers[j])
            if not shared:
                # A literal path one piece names (it may not exist yet) that
                # the other's globs also cover is the same overlap.
                for lit_owner, other_res, lits in (
                    (a, res_b, a["paths"]),
                    (b, res_a, b["paths"]),
                ):
                    for lit in lits:
                        bare = lit.strip().lstrip("/").rstrip("/")
                        if bare and not _GLOB_CHARS.search(bare):
                            if other_res and _hits(other_res, bare):
                                shared = [bare]
                                break
                    if shared:
                        break
            if not shared:
                # Two GLOBS over files neither has created yet: the green zones
                # would fence both pieces onto the same new path.
                for ga in a["paths"]:
                    if not _GLOB_CHARS.search(ga):
                        continue
                    for gb in b["paths"]:
                        if _GLOB_CHARS.search(gb) and _globs_overlap(ga, gb):
                            shared = ["%s / %s" % (ga, gb)]
                            break
                    if shared:
                        break
            if shared:
                more = " (+%d more)" % (len(shared) - 1) if len(shared) > 1 else ""
                problems.append(
                    {
                        "piece": b["title"] or "?",
                        "error": "overlaps %s on %s%s"
                        % (a["title"] or "?", shared[0], more),
                    }
                )
    if red:
        for (n, _res), cov in zip(compiled, covers):
            if cov and all(_hits(red, f) for f in cov):
                problems.append(
                    {
                        "piece": n["title"] or "?",
                        "error": "every file it covers is in a red zone",
                    }
                )
    return out, problems


def _slug(text: str, n: int = 24) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]
    out = ""
    for w in words:
        cand = (out + "-" + w) if out else w
        if len(cand) > n:
            break
        out = cand
    return out or (words[0][:n] if words else "")


def piece_titles(base: str, pieces: List[dict], taken: Iterable[str]) -> List[str]:
    """Session titles for a plan's pieces: ``<base>-<piece slug>``, unique
    against ``taken`` (and each other)."""
    taken = set(taken or ())
    out = []
    for i, p in enumerate(pieces, 1):
        stem = "%s-%s" % (base, _slug(p.get("title")) or "piece%d" % i)
        title, k = stem, 2
        while title in taken:
            title = "%s-%d" % (stem, k)
            k += 1
        taken.add(title)
        out.append(title)
    return out


def tests_line(report_text: str) -> str:
    """The ``Tests: …`` line of a worker's report, or ``""``."""
    for line in str(report_text or "").splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?\**tests?\**\s*[:—-]\s*\**\s*(.+)$", line, re.I)
        if m:
            return m.group(1).strip()[:300]
    return ""


def tests_passed(log: str) -> Optional[int]:
    """The "N passed" count a test runner printed last in ``log``, or None."""
    hits = re.findall(r"(\d+) (?:passed|tests? passed|passing)", str(log or ""))
    return int(hits[-1]) if hits else None


def _subject_phrase(subject: str) -> str:
    """ "auth: rotate refresh tokens" → "rotate refresh tokens"."""
    s = re.sub(r"^[\w./-]{1,30}(\([^)]*\))?!?:\s+", "", str(subject or "").strip())
    return s[:1].lower() + s[1:] if s else ""


def member_label(run: dict, t: dict) -> str:
    """What a member is, in a few words: a piece's title, a ticket's id, a
    task line's start."""
    if t["kind"] == "piece":
        return _piece_title(run, t)
    if t["kind"] == "ticket":
        return t["ticket_id"] or t["title"]
    return (t["text"] or t["title"]).splitlines()[0][:80]


def _piece_title(run: dict, t: dict) -> str:
    lead = (run.get("lead") or {}).get("title") or ""
    base = lead_base(lead) if lead else ""
    title = t["title"]
    if base and title.startswith(base + "-"):
        return title[len(base) + 1 :]
    return title


def release_title(run: dict) -> str:
    """The one PR's title: the group's name, then what each merged member did
    (its first commit subject, scope prefix dropped; else its label)."""
    parts: List[str] = []
    for t in run["tasks"]:
        if t["state"] != "integrated":
            continue
        phrase = _subject_phrase(t["commits"][0]) if t["commits"] else ""
        phrase = _clip_words(phrase or member_label(run, t), 60)
        if phrase and phrase.lower() not in (p.lower() for p in parts):
            parts.append(phrase)
    # A name the dialog CUT ("Create notes/one.md, notes/two.md and…") is no
    # title: with the pieces to name, it is left out; otherwise it is trimmed
    # to whole words — never a dangling "and", never a literal "…".
    name = _s(run["name"]).strip()
    cut = name.endswith("…")
    head = _clip_words(_trim_dangling(name), 80)
    if cut and parts:
        head = ""
    title = head
    for i, p in enumerate(parts):
        more = len(parts) - i - 1
        sep = ": " if head and i == 0 else ", "
        cand = (title + sep + p) if title else (p[:1].upper() + p[1:])
        if len(cand) > 100 and title:
            title += " (+%d more)" % (len(parts) - i)
            break
        title = cand
        if more == 0:
            break
    return _clip_words(title, 120) or _clip_words(_trim_dangling(name), 120) or "Group"


_DANGLING = re.compile(
    r"(?:[\s,;:.\-—…]+|\s+(?:and|or|the|a|an|to|of|with|for|in|on))+$", re.I
)


def _trim_dangling(text: str) -> str:
    """``text`` without trailing punctuation, an ellipsis or a dangling
    conjunction ("…notes/two.md and" → "…notes/two.md")."""
    prev = None
    s = str(text or "").strip()
    while prev != s:
        prev = s
        s = _DANGLING.sub("", s).strip()
    return s


def _clip_words(text: str, n: int) -> str:
    """``text`` cut to at most ``n`` characters at a word boundary (no "…")."""
    s = str(text or "").strip()
    if len(s) <= n:
        return s
    return _trim_dangling(s[:n].rsplit(" ", 1)[0])


def release_body(run: dict, base: str, branch: str) -> str:
    """The one PR's body: a section per merged member — what changed (its
    report), its paths, its commits as written, the tests it ran — then the
    conflict fixes, the check on the merged branch, and the footer."""
    integrated = [t for t in run["tasks"] if t["state"] == "integrated"]
    lead = (run.get("lead") or {}).get("title") or "the lead"
    plan = run.get("plan") or {}
    lines: List[str] = []
    if same_folder(run):
        lines.append(
            "Split into %d pieces with separate paths by %s, worked on side by side "
            "in its folder; MindFlock committed each piece's paths on `%s` (one "
            "commit per piece)." % (len(integrated), lead, branch or "the branch")
        )
        if plan.get("why"):
            lines += ["", plan["why"].strip()]
    elif run.get("split"):
        lines.append(
            "Split into %d pieces with separate paths by %s; each was merged back "
            "into `%s` by MindFlock." % (len(integrated), lead, branch or "the branch")
        )
        if plan.get("why"):
            lines += ["", plan["why"].strip()]
    else:
        lines.append(
            "%d lines worked on in parallel and merged into `%s` by MindFlock."
            % (len(integrated), branch or "one branch")
        )
    for t in integrated:
        lines += ["", "## %s" % member_label(run, t)]
        what = (t.get("report_text") or "").strip()
        what = re.sub(r"(?im)^\s*details:\s*$", "", what)
        what = "\n".join(
            ln
            for ln in what.splitlines()
            if not re.match(r"(?i)^\s*tests?\s*[:—-]", ln)
        ).strip()
        if not what:
            what = (
                (t.get("text") or "").strip().splitlines()[0] if t.get("text") else ""
            )
        if what:
            lines.append(what[:3000])
        if t["kind"] == "piece" and t["paths"]:
            lines += ["", "**Paths:** " + ", ".join("`%s`" % p for p in t["paths"])]
        if t["commits"]:
            lines += ["", "**Commits:**"] + ["- %s" % c for c in t["commits"][:20]]
        lines += ["", "**Tests:** %s" % (t.get("tests") or "not reported")]
    fixed = [t for t in integrated if t["conflict_fixed"]]
    if fixed:
        lines += ["", "## Conflict fixes"]
        for t in fixed:
            files = ", ".join((t.get("conflict") or {}).get("files") or []) or "—"
            lines.append(
                "- %s: resolved by %s (%s)" % (member_label(run, t), lead, files)
            )
    chk = run.get("check") or {}
    lines += ["", "## Check"]
    if chk.get("state") == "ok":
        n = chk.get("tests")
        lines.append(
            "`%s` passed%s on the merged branch (%s)."
            % (
                chk.get("command") or "check",
                " (%d tests)" % n if isinstance(n, int) else "",
                (chk.get("sha") or "")[:7] or "HEAD",
            )
        )
    elif chk.get("state") == "none":
        lines.append("No `check_command` is configured for this repo.")
    else:
        lines.append("Not run.")
    lines += ["", "---", "Part of “%s” · MindFlock" % run["name"]]
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def is_terminal(task: dict) -> bool:
    return task.get("state") in TERMINAL


def is_together(run: dict) -> bool:
    """One PR for the whole group (a one-for-all batch, or a split): every
    member is committed, then merged into the LEAD's branch."""
    return run["policy"]["grouping"] == "together" or bool(run.get("split"))


def task_lane(run: dict, task: dict) -> str:
    """A member's lane. In a one-for-all group every member stops at a
    commit — the group's lane is carried by the lead, once, at release."""
    if is_together(run):
        return "commit"
    return task.get("lane") or run["policy"]["lane"]


def task_ask_first(run: dict, task: dict) -> bool:
    """Whether a member asks before it ships: its own choice when a person
    made one (``task["ask_first"]``), else the group's. A one-for-all member
    never asks per commit — "ask first" applies to the group's one release."""
    if is_together(run):
        return False
    own = task.get("ask_first")
    return bool(own) if own is not None else bool(run["policy"]["ask_first"])


def counts(run: dict) -> dict:
    tasks = run.get("tasks") or []
    return {
        "queued": sum(1 for t in tasks if t["state"] == "queued"),
        "active": sum(1 for t in tasks if t["state"] in ACTIVE),
        "needs_you": sum(1 for t in tasks if t["state"] == "needs_you"),
        "shipped": sum(1 for t in tasks if t["state"] in ("shipped", "integrated")),
        "failed": sum(1 for t in tasks if t["state"] == "failed"),
        "total": len(tasks),
    }


def run_cost(run: dict) -> float:
    return round(sum(float(t.get("cost_usd") or 0.0) for t in run["tasks"]), 4)


def summary_dto(run: dict) -> dict:
    """``RunSummary``: what a list, a header or an MCP reply needs."""
    return {
        "id": run["id"],
        "name": run["name"],
        "state": run["state"],
        "paused": run["paused"],
        "pause_reason": run["pause_reason"],
        "policy": dict(run["policy"]),
        "counts": counts(run),
        "cost_usd": run_cost(run),
        "created_at": run["created_at"],
    }


#: Task keys a RunDTO carries (the file's internal bookkeeping stays home).
_TASK_DTO_KEYS = (
    "id",
    "kind",
    "source",
    "ticket_id",
    "text",
    "paths",
    "title",
    "branch",
    "repo_root",
    "incarnation",
    "state",
    "reason",
    "attempts",
    "nudges",
    "lane",
    "pr_url",
    "merged_sha",
    "report",
    "started_at",
    "finished_at",
    "cost_usd",
    "head_sha",
    "commits",
    "conflict",
    "conflict_fixed",
    "tests",
    "merged_at",
)


def task_dto(task: dict, row_present: Optional[bool] = None) -> dict:
    out = {k: copy.deepcopy(task.get(k)) for k in _TASK_DTO_KEYS}
    # The one-line "why" behind needs_you / failed, for the bell.
    out["detail"] = task.get("detail") or ""
    out["retry_at"] = task.get("retry_at") or 0.0
    out["flag"] = task.get("flag") or ""
    if row_present is not None:
        out["row_present"] = bool(row_present)
    return out


def run_dto(run: dict, live_titles: Optional[Iterable[str]] = None) -> dict:
    """``RunDTO``: the file minus ``v`` and bookkeeping, plus ``counts`` and
    per-task ``row_present``."""
    live = set(live_titles or ())
    out = {
        k: copy.deepcopy(run[k])
        for k in (
            "id",
            "name",
            "created_at",
            "created_by",
            "state",
            "paused",
            "pause_reason",
            "goal",
            "repo_root",
            "program",
            "policy",
            "concurrency",
            "budget_usd",
            "split",
            "mode",
            "origin",
            "lead",
            "plan",
            "check",
            "release",
            "events",
            "summary",
        )
    }
    if out["lead"] is not None:
        out["lead"]["row_present"] = out["lead"]["title"] in live
        for k in ("missing_since", "briefed", "started_at"):
            out["lead"].pop(k, None)
    out["tasks"] = [
        task_dto(t, bool(t["title"]) and t["title"] in live) for t in run["tasks"]
    ]
    sf = run.get("sf") or {}
    out["stray"] = {
        "paths": list(sf.get("stray") or []),
        "commits": list(sf.get("stray_commits") or []),
    }
    out["counts"] = counts(run)
    out["cost_usd"] = run_cost(run)
    out["rev"] = run["rev"]
    out["waiting_for_usage"] = run["waiting_for_usage"]
    return out


def summarize(run: dict, now: float) -> dict:
    """The run's closing facts — PRs, failures, cost, duration — and the same
    as Markdown. Built from the record alone: no model call."""
    c = counts(run)
    prs = [
        {"task": t["id"], "title": t["title"], "pr_url": t["pr_url"], "flag": t["flag"]}
        for t in run["tasks"]
        if t["state"] in ("shipped", "integrated")
    ]
    failures = [
        {
            "task": t["id"],
            "title": t["title"] or _ref(t),
            "reason": t["detail"] or t["reason"],
        }
        for t in run["tasks"]
        if t["state"] == "failed"
    ]
    removed = [
        {
            "task": t["id"],
            "title": t["title"] or _ref(t),
            "state": t["state"],
            "detail": t["detail"] or "",
        }
        for t in run["tasks"]
        if t["state"] in ("cancelled", "skipped")
    ]
    started = min(
        [t["started_at"] for t in run["tasks"] if t["started_at"]]
        or [run["created_at"]]
    )
    duration = max(0.0, now - started)
    cost = run_cost(run)
    together = is_together(run)
    rel = run.get("release") or {}
    lines = [
        "## %s" % run["name"],
        "",
        "%d of %d %s · %d failed · $%.2f · %s"
        % (
            c["shipped"],
            c["total"],
            "merged back" if together else "shipped",
            c["failed"],
            cost,
            _duration(duration),
        ),
        "",
    ]
    if together:
        lead = (run.get("lead") or {}).get("branch") or (run.get("lead") or {}).get(
            "title", ""
        )
        if rel.get("pr_url"):
            lines.append("- ✓ One PR: %s" % rel["pr_url"])
        elif _s(rel.get("local_origin")) and rel.get("state") in ("handoff", "done"):
            lines.append(
                "- ⇡ pushed `%s` to `%s` — a folder on this machine, not a forge; "
                "no PR was opened" % (lead, rel["local_origin"])
            )
        elif rel.get("state") == "handoff":
            lines.append(
                "- ⇡ pushed `%s` — open the PR: %s" % (lead, rel["compare_url"])
                if rel.get("compare_url")
                else "- ⇡ pushed `%s` — no PR yet (no gh or GitHub token here): "
                "open it from the branch" % lead
            )
        elif c["shipped"]:
            lines.append("- everything is merged into `%s` (nothing pushed)" % lead)
        for t in run["tasks"]:
            if t["state"] == "integrated":
                lines.append(
                    "  - %s%s"
                    % (
                        member_label(run, t),
                        " (conflict fixed by the lead)" if t["conflict_fixed"] else "",
                    )
                )
        prs = (
            [{"task": "", "title": run["name"], "pr_url": rel["pr_url"], "flag": ""}]
            if rel.get("pr_url")
            else []
        )
    for p in ([] if together else prs):
        lines.append(
            "- ✓ %s%s%s"
            % (
                p["title"],
                " — " + p["pr_url"] if p["pr_url"] else "",
                " (checks failed)" if p["flag"] == "checks_failed" else "",
            )
        )
    for f in failures:
        lines.append("- ✗ %s — %s" % (f["title"], f["reason"] or "failed"))
    for r in removed:
        # The task's own why ("group cancelled — its session was kept") beats
        # the generic word: a cancelled group KEPT its running sessions, and
        # "removed by you" read as if they were gone.
        why = r["detail"] or (
            "removed by you" if r["state"] == "cancelled" else "skipped"
        )
        lines.append("- – %s (%s)" % (r["title"], why))
    return {
        "shipped": c["shipped"],
        "failed": c["failed"],
        "cost_usd": cost,
        "duration_s": round(duration, 1),
        "prs": prs,
        "failures": failures,
        "removed": removed,
        "text_md": "\n".join(lines).rstrip() + "\n",
        "announced": bool((run.get("summary") or {}).get("announced")),
    }


def _duration(secs: float) -> str:
    m = int(secs // 60)
    if m < 60:
        return "%dm" % max(1, m)
    return "%dh %02dm" % (m // 60, m % 60)


def _ref(task: dict) -> str:
    return task.get("ticket_id") or task.get("text", "")[:60] or task.get("id", "")


def ref_of(task: dict) -> str:
    """The human ref of a task (its ticket id, else the start of its line)."""
    return _ref(task)


# --------------------------------------------------------------------------- #
# The planner (pure)
# --------------------------------------------------------------------------- #
#: Halts no fix prompt can cure — they need a human, at once.
_ESCALATE_NOW = (
    "no origin remote",
    "make a branch",
    "needs gh or a github token",
    "merge it on github",
    "cannot merge",
    "switched from",
    "workspace setup failed",
    "did not take after",
    "red zone",
    "green zone",
    "rejected",
    "permission denied",
    "authentication",
    "over the cost budget",
    "index.lock",
)
_NOTHING_CHANGED = "finished without changing anything"
_GAVE_UP = "gave up waiting"
_CI_FAILED = "ci failed on the pr"


def classify_halt(reason: str) -> str:
    """What a halted autopilot's reason means for a run task:

    * ``"idle"`` — the agent finished with nothing to ship; re-arm and let the
      stuck detector (which needs proof of work and a dwell) judge it;
    * ``"stuck"`` — it waited hours for an agent that never worked;
    * ``"ci_failed"`` — the PR exists but CI is red: shipped, not merged;
    * ``"human"`` — something only a person can fix (escalate now);
    * ``"fixable"`` — a hook or check failed: hand the agent the failure."""
    low = str(reason or "").lower()
    if _NOTHING_CHANGED in low:
        return "idle"
    if _GAVE_UP in low:
        return "stuck"
    if _CI_FAILED in low:
        return "ci_failed"
    if any(k in low for k in _ESCALATE_NOW):
        return "human"
    return "fixable"


def _hook_of(reason: str) -> str:
    m = re.search(r"(?:failed at|failing at|failure:)\s+(.+)$", str(reason or ""))
    return (m.group(1).strip() if m else "") or "a hook"


def _depth_rank(depth: str) -> int:
    order = ("off", "agent", "commit", "push", "pr", "merge")
    return order.index(depth) if depth in order else -1


def _lane_depth(lane: str) -> str:
    return {"leave": "agent"}.get(lane, lane)


def _act(op: str, task: Optional[dict] = None, **kw) -> dict:
    out = {"op": op}
    if task is not None:
        out["task"] = task["id"]
    out.update(kw)
    return out


def _to(task: dict, state: str, reason: str = "", detail: str = "", **fields) -> dict:
    """A ``state`` action (pure transition)."""
    return _act("state", task, state=state, reason=reason, detail=detail, fields=fields)


def plan_actions(run: dict, obs: dict, now: float) -> List[dict]:
    """Every action this pass should take for ``run``. PURE.

    ``obs`` is the driver's observation::

        {"boot": bool,                       # the silent reconcile pass
         "limited_providers": [..],
         "cost": float,                      # sum of member costs
         "tasks": {task_id: {
             "present", "created_at", "pending", "status", "activity",
             "activity_since", "stage", "pr_url", "branch", "autopilot",
             "report", "limited", "worked", "progress", "queue_ids", "cost",
             "branch_exists", "provider", "deleted_at", "create_failed",
             "create_failed_at", "turn_ended_at"}}}

    Actions are dicts with ``op``: ``state`` (a pure transition, see
    :func:`apply`), ``start``, ``arm``, ``disarm``, ``nudge``, ``fix``,
    ``seen`` (bookkeeping fields), ``pause`` / ``finish`` (run level).
    """
    acts: List[dict] = []
    if run["state"] in RUN_FINISHED:
        return acts
    tobs = obs.get("tasks") or {}
    boot = bool(obs.get("boot"))
    together = is_together(run)
    if together:
        lead_acts = _plan_lead(run, obs.get("lead") or {}, now, boot)
        acts.extend(lead_acts)
        if any(a["op"] == "abort" for a in lead_acts):
            return acts
    sf = same_folder(run)
    if sf:
        acts.extend(_plan_sf_foreign(run, obs.get("sf") or {}, now))
    for t in run["tasks"]:
        if is_terminal(t):
            if sf and t["fenced"] and t["kind"] == "piece":
                # Its paths are committed (or it left): its fence in the
                # shared folder goes, so the folder is the lead's again.
                acts.append(_act("unfence", t))
            continue
        if together and t["state"] == "integrating":
            continue  # the merge queue's, below
        o = tobs.get(t["id"]) or {}
        acts.extend(_plan_task(run, t, o, now, boot))
        if (
            together
            and t["kind"] == "piece"
            and t["paths"]
            and not t["fenced"]
            and o.get("present")
            and t["state"] in ("starting", "working", "needs_you", "shipping")
        ):
            acts.append(_act("fence", t))

    # Budget: the group's spend reached its cap → pause and ask.
    budget = float(run.get("budget_usd") or 0.0)
    if budget > 0 and float(obs.get("cost") or 0.0) >= budget and not run["paused"]:
        acts.append(_act("pause", reason="budget"))

    acts.extend(_plan_starts(run, obs, now, acts))
    if together:
        acts.extend(_plan_integration(run, obs, now, acts))
        return acts

    # Finished: every task terminal (taking this pass's transitions into
    # account), and at least one exists.
    final = {t["id"]: t["state"] for t in run["tasks"]}
    for a in acts:
        if a["op"] == "state":
            final[a["task"]] = a["state"]
        elif a["op"] == "start":
            final[a["task"]] = "starting"
    if final and all(s in TERMINAL for s in final.values()):
        failed = any(s == "failed" for s in final.values())
        acts.append(_act("finish", state="done_with_failures" if failed else "done"))
    return acts


def _plan_starts(run: dict, obs: dict, now: float, acts: List[dict]) -> List[dict]:
    """Fill free slots from the queue, in order (``start_now`` first). Nothing
    starts in a pass that pauses the group (the budget crossed): a session
    started now would be armed and ship past the pause."""
    if run["paused"] or run["state"] != "running":
        return []
    if any(a["op"] == "pause" for a in acts):
        return []
    lo = obs.get("lead") or {}
    if is_together(run) and not (lo.get("ready") and lo.get("on_branch", True)):
        return []  # members fork from the group branch's commit: it must be there
    moving = {a["task"]: a["state"] for a in acts if a["op"] == "state"}
    # A transition this pass may set its own backoff (a create that failed).
    backoff = {
        a["task"]: float((a.get("fields") or {}).get("retry_at") or 0.0)
        for a in acts
        if a["op"] == "state"
    }
    active = 0
    for t in run["tasks"]:
        st = moving.get(t["id"], t["state"])
        if st in ACTIVE:
            active += 1
    limited = set(obs.get("limited_providers") or [])
    provider = _s(obs.get("provider"))
    out = []
    queued = [
        t
        for t in run["tasks"]
        if moving.get(t["id"], t["state"]) == "queued"
        and backoff.get(t["id"], float(t.get("retry_at") or 0.0)) <= now
    ]
    queued.sort(key=lambda t: (not t.get("start_now"),))
    for t in queued:
        if not t.get("start_now"):
            if active >= int(run["concurrency"]):
                continue
            # Waiting for usage to come back on this CLI: start nothing new
            # on it (the resume watcher brings the members back by itself).
            if provider and provider in limited:
                continue
        out.append(_act("start", t))
        active += 1
    return out


def _plan_task(run: dict, t: dict, o: dict, now: float, boot: bool) -> List[dict]:
    st = t["state"]
    acts: List[dict] = []
    if st == "queued":
        return acts

    # --- the user's act wins: a member they deleted is never re-created ----
    deleted_at = float(o.get("deleted_at") or 0.0)
    if (
        deleted_at
        and deleted_at >= float(t["started_at"] or 0.0) - 1.0
        and not o.get("present")
    ):
        return [_to(t, "cancelled", "", "removed by you", finished_at=now)]

    present = bool(o.get("present"))
    created = o.get("created_at")
    inc = float(t["incarnation"] or 0.0)
    if present and created is not None:
        if inc:
            mine = abs(float(created) - inc) <= 1.0
        else:
            mine = float(created) >= float(t["started_at"] or 0.0) - INCARNATION_SLACK_S
    else:
        mine = present and not inc  # no created_at on the row: trust the title

    # --- starting --------------------------------------------------------- #
    if st == "starting":
        if present and mine:
            fields = {"missing_since": 0.0}
            if not inc and created is not None:
                fields["incarnation"] = float(created)
            if o.get("branch"):
                fields["branch"] = o["branch"]
            return [_to(t, "working", **fields)]
        if present and not mine:
            stale = _stale_run_record(o, created)
            return ([_act("disarm", t)] if stale else []) + [
                _to(t, "needs_you", "restart", "the title is used by another session")
            ]
        failed_at = float(o.get("create_failed_at") or 0.0)
        if o.get("create_failed") and failed_at >= float(t["started_at"]) - 1.0:
            return [_create_failure(t, now, str(o["create_failed"]))]
        if o.get("pending"):
            return acts
        stale = now - float(t["started_at"] or now) >= START_GRACE_S
        if boot or stale:
            if o.get("branch_exists"):
                return [
                    _to(
                        t,
                        "needs_you",
                        "restart",
                        "the session did not come back after a restart; its branch "
                        "is still there — Re-create it on the existing branch",
                    )
                ]
            return [_create_failure(t, now, "the session never appeared")]
        return acts

    # --- a task with a session ---------------------------------------------- #
    if not present:
        if o.get("pending"):
            return acts
        missing = float(t["missing_since"] or 0.0)
        if not missing and not boot:
            return [_act("seen", t, fields={"missing_since": now})]
        if boot or now - missing >= MISSING_GRACE_S:
            if st == "needs_you" and t["reason"] == "restart":
                return acts
            detail = "its session is gone"
            if o.get("branch_exists"):
                detail += (
                    "; its branch is still there — Re-create it on the existing branch"
                )
            return [_to(t, "needs_you", "restart", detail)]
        return acts
    if not mine:
        # The title now belongs to an unrelated session. A record THIS run
        # armed before that session existed would drive it (records are keyed
        # by title): drop it. A record armed after it — that session's own
        # owner's choice — is never touched.
        stale = _stale_run_record(o, created)
        if st == "needs_you" and t["reason"] == "restart":
            return [_act("disarm", t)] if stale else acts
        return ([_act("disarm", t)] if stale else []) + [
            _to(t, "needs_you", "restart", "the title is used by another session")
        ]

    seen: dict = {}
    if t["missing_since"]:
        seen["missing_since"] = 0.0
    if o.get("worked") and not t["worked"]:
        seen["worked"] = True
    if o.get("branch") and o["branch"] != t["branch"]:
        seen["branch"] = o["branch"]
    if o.get("pr_url") and o["pr_url"] != t["pr_url"] and st != "shipped":
        seen["pr_url"] = o["pr_url"]
    cost = float(o.get("cost") or 0.0)
    if abs(cost - float(t["cost_usd"] or 0.0)) >= 0.005:
        seen["cost_usd"] = round(cost, 4)
    progress = _s(o.get("progress"))
    if progress and progress != t["progress"]:
        seen["progress"] = progress
        seen["progress_at"] = now
    # A queued nudge counts as DELIVERED once the queue no longer holds it.
    if t["nudge_id"] and not t["nudge_seen_at"]:
        if t["nudge_id"] not in (o.get("queue_ids") or []):
            seen["nudge_seen_at"] = now
    if seen:
        acts.append(_act("seen", t, fields=seen))
    progressed = "progress_at" in seen
    progress_at = seen.get("progress_at", t["progress_at"])
    nudge_seen = seen.get("nudge_seen_at", t["nudge_seen_at"])

    if run["paused"]:
        return acts  # held: observe only

    activity = _s(o.get("activity"))
    lane = task_lane(run, t)
    rec = o.get("autopilot") if isinstance(o.get("autopilot"), dict) else None
    report = o.get("report") if isinstance(o.get("report"), dict) else None
    fresh_report = report is not None and float(report.get("ts") or 0.0) > max(
        float(t["started_at"]), t["report_seen"]
    )

    # A report: blocked/failed asks for you (and stops shipping); done is
    # evidence for a leave-lane task and clears a blocked one.
    if fresh_report:
        status = _s(report.get("status")).lower()
        acts.append(
            _act(
                "seen", t, fields={"report_seen": float(report["ts"]), "report": report}
            )
        )
        if status in ("blocked", "failed"):
            acts.append(_act("disarm", t))
            acts.append(
                _to(
                    t, "needs_you", "blocked", _s(report.get("summary"))[:300] or status
                )
            )
            return acts
        if status == "done" and st == "needs_you" and t["reason"] == "blocked":
            acts.append(_act("arm", t))
            return acts + [_to(t, "working")]
        if status == "done" and lane == "leave":
            return acts + [_to(t, "shipped", finished_at=now)]

    # One for all: a member that committed its work itself (as its brief
    # asks) and stopped is DONE — the autopilot, armed at "commit", would
    # wait forever on a tree that is already committed. Done = a done report
    # or a turn that ended, on a clean tree with commits beyond its base.
    waiting_on_you = st == "needs_you" and t["reason"] not in ("stuck", "prompt")
    if same_folder(run) and t["kind"] == "piece":
        if not waiting_on_you:
            done = _sf_done(t, o, now, activity, report if fresh_report else None)
            if done:
                return acts + done
    elif is_together(run) and not waiting_on_you:
        rep = report if fresh_report else (t.get("report") or {})
        said_done = _s((rep or {}).get("status")).lower() == "done"
        ended = float(o.get("turn_ended_at") or 0.0) >= float(t["started_at"] or 0.0)
        # The server's own reading, needing no event: commits beyond its base
        # on a clean tree, and idle for DONE_QUIET_S. ``turn_ended`` is muted
        # while the member's commit-lane record is armed ("already at commit
        # — waiting for new work"), so relying on it alone left a worker that
        # committed but never reported "working" forever, then nudged and
        # escalated as "stalled" with its work sitting committed.
        committed = int(o.get("beyond_base") or 0) > 0 and o.get("clean") is True
        quiet = now - float(o.get("activity_since") or now) >= DONE_QUIET_S
        if activity == "idle" and (
            ((said_done or ended) and _s(o.get("stage")) == "committed")
            or (committed and (said_done or ended or quiet))
        ):
            return acts + [_act("disarm", t), _to(t, "integrating", ready_at=now)]

    # A dialog: it waits on YOU (the rail strip / the bell answer it; the run
    # never does). Not announced — needs_input already fired for it.
    if activity == "clarify":
        if st == "needs_you" and t["reason"] in ("prompt", "blocked", "restart"):
            return acts
        if st == "needs_you" and t["reason"] in ANNOUNCED_REASONS:
            return acts
        return acts + [_to(t, "needs_you", "prompt", "its agent is asking")]
    if st == "needs_you" and t["reason"] == "prompt":
        acts.append(_to(t, "working"))
        st = "working"

    if st == "needs_you" and t["reason"] in (
        "blocked",
        "restart",
        "conflict",
        "budget",
    ):
        return acts  # waits for Retry / Skip (or a done report, above)

    if same_folder(run) and t["kind"] == "piece":
        # No autopilot record drives a same-folder piece — MindFlock commits
        # its paths itself once it is done (above): only the stuck clock.
        if st == "needs_you" and t["reason"] == "stuck" and progressed:
            return acts + [_to(t, "working")]
        return acts + _plan_stuck(t, o, now, activity, progress_at, nudge_seen)

    # --- leave lane: done is the CLI's own "turn ended" (or a done report) --
    if lane == "leave" and rec is not None and not t["held"]:
        # Somebody armed a lane on it (the ⏩ button, the pane menu): the
        # user's act wins, and from the next pass it ships like any other.
        armed = _s(rec.get("lane")) or {"agent": "leave"}.get(
            _s(rec.get("depth")), _s(rec.get("depth"))
        )
        if armed in LANES and armed != "leave":
            acts.append(_act("seen", t, fields={"lane": armed}))
            acts.append(_act("log", t, text="lane changed to %s by you" % armed))
            return acts
    if lane == "leave":
        ended = float(o.get("turn_ended_at") or 0.0)
        if ended and ended >= float(t["started_at"] or 0.0):
            return acts + [_to(t, "shipped", finished_at=now)]
        if st == "needs_you" and t["reason"] == "stuck" and progressed:
            return acts + [_to(t, "working")]
        return acts + _plan_stuck(t, o, now, activity, progress_at, nudge_seen)

    # --- a shipping lane: read the autopilot's record ------------------------ #
    if rec is None:
        if t["held"]:
            return acts
        # Disarmed by someone other than the run (⏩ off, lane → leave): the
        # user's act wins — this task's lane is now "leave it".
        acts.append(_act("seen", t, fields={"lane": "leave"}))
        acts.append(_act("log", t, text="lane changed to leave it by you"))
        return acts
    ap_state = _s(rec.get("state"))
    step = _s(rec.get("step"))
    if ap_state == "running":
        acting = (
            step in ("commit", "check", "push", "pr", "merge")
            or int(rec.get("commits") or 0) > 0
        )
        if acting:
            return acts + ([_to(t, "shipping")] if st != "shipping" else [])
        if st == "needs_you" and t["reason"] == "stuck":
            # Escalated: only real progress (a diff, a commit) brings it back.
            return acts + ([_to(t, "working")] if progressed else [])
        if st in ("needs_you", "shipping"):
            # Re-armed — by Retry, by your approval (ship-now), by ⏩.
            acts.append(_to(t, "working"))
        # The agent carried the work to the rung itself (it committed, say)
        # and stopped: the record waits "already at commit — waiting for new
        # work" forever, because the AUTOPILOT did not act. For a run's member
        # that is the work done — settle the record at its rung, and the next
        # pass ships it (or, asking first, shows it for your go).
        held = _s(rec.get("depth"))
        if (
            activity == "idle"
            and (t["worked"] or o.get("worked"))
            and _autopilot_reaches(_s(o.get("stage")), held)
            and now - float(o.get("activity_since") or now) >= DONE_QUIET_S
        ):
            return acts + [_act("settle_rec", t)]
        return acts + _plan_stuck(t, o, now, activity, progress_at, nudge_seen)
    if ap_state == "done":
        if is_together(run):
            # Committed: into the merge queue (one at a time, into the lead).
            return acts + [_to(t, "integrating", ready_at=now)]
        if _approval(rec, lane):
            if st == "needs_you" and t["reason"] == "approve":
                return acts
            return acts + [_to(t, "needs_you", "approve", "waiting for your go")]
        url = _s(rec.get("url")) or _s(o.get("pr_url")) or t["pr_url"]
        return acts + [_to(t, "shipped", pr_url=url, finished_at=now)]
    if ap_state == "halted":
        reason = _s(rec.get("reason"))
        kind = classify_halt(reason)
        if kind == "idle":
            # Nothing to ship YET. Re-arm and let the stuck detector — which
            # needs proof of work and a dwell, never a CPU blip — decide.
            acts.append(_act("arm", t))
            if st != "working":
                acts.append(_to(t, "working"))
            return acts
        if kind == "stuck":
            return acts + [_to(t, "needs_you", "stuck", "the agent never got going")]
        if kind == "ci_failed":
            url = _s(rec.get("url")) or _s(o.get("pr_url")) or t["pr_url"]
            return acts + [
                _to(t, "shipped", pr_url=url, flag="checks_failed", finished_at=now)
            ]
        if st == "needs_you" and t["reason"] == "ship_halted":
            return acts
        if kind == "human":
            return acts + [_to(t, "needs_you", "ship_halted", reason)]
        if t["attempts"]["ship"] < MAX_SHIP_RETRIES:
            return acts + [_act("fix", t, hook=_hook_of(reason), reason=reason)]
        return acts + [
            _to(
                t,
                "needs_you",
                "ship_halted",
                (
                    "%s failed twice" % _hook_of(reason)
                    if "pre-commit" in reason.lower()
                    else reason
                ),
            )
        ]
    return acts


def _sf_done(
    t: dict, o: dict, now: float, activity: str, fresh: Optional[dict]
) -> List[dict]:
    """A same-folder piece that is DONE → into MindFlock's commit queue
    (``integrating``); ``[]`` while it is not. Done = its own "done" report,
    or a turn that ended / a quiet minute idle after it worked — either way
    idle, with changes under its paths (or commits of its own, attributed).
    A "done" with nothing under its paths is said, never committed empty."""
    if activity != "idle":
        return []
    rep = fresh if fresh is not None else (t.get("report") or {})
    said_done = _s((rep or {}).get("status")).lower() == "done"
    own = list(o.get("sf_own") or [])
    mine = list(t.get("self_commits") or [])
    idle_for = now - float(o.get("activity_since") or now)
    ended = float(o.get("turn_ended_at") or 0.0) >= float(t["started_at"] or 0.0)
    worked = bool(t["worked"] or o.get("worked"))
    quiet = idle_for >= DONE_QUIET_S and (ended or worked)
    if not (said_done or quiet):
        return []
    if own or mine:
        return [_to(t, "integrating", ready_at=now)]
    if said_done:
        return [
            _to(
                t,
                "needs_you",
                "blocked",
                "it said it was done, but nothing under its paths changed — "
                "Retry to ask it again, or Skip it",
            )
        ]
    return []


def _plan_sf_foreign(run: dict, so: dict, now: float) -> List[dict]:
    """Same folder: judge every commit on the lead's branch MindFlock did not
    make (no piece trailer) once — a piece committing against its brief.
    One piece's paths only → kept as that piece's commit; anything else
    (several pieces' files, files no piece owns) can't be split per piece
    → the pieces involved need you. Plus the stray paths: changes no piece
    owns, standing in the shared folder."""
    out: List[dict] = []
    sf = run.get("sf") or {}
    seen = set(sf.get("seen_foreign") or [])
    judged: List[str] = []
    stray_commits = list(sf.get("stray_commits") or [])
    by_id = {t["id"]: t for t in run["tasks"]}
    for c in so.get("foreign") or []:
        sha = _s(c.get("sha"))
        if not sha or sha in seen:
            continue
        judged.append(sha)
        pieces = [p for p in (c.get("pieces") or []) if p in by_id]
        outside = [str(f) for f in (c.get("outside") or [])]
        short = sha[:9]
        subject = _s(c.get("subject"))[:80]
        if len(pieces) == 1 and not outside:
            t = by_id[pieces[0]]
            out.append(
                _act(
                    "seen",
                    t,
                    fields={"self_commits": list(t["self_commits"]) + [sha]},
                )
            )
            out.append(
                _act(
                    "log",
                    t,
                    text="it committed by itself (%s %s) — only its own files, "
                    "kept as its commit" % (short, subject),
                )
            )
            continue
        if not pieces:
            stray_commits.append(sha)
            continue
        names = ", ".join(by_id[p]["title"] or p for p in pieces)
        why = (
            "a commit MindFlock didn't make (%s %s) holds the work of %s%s — it "
            "can't be split per piece: keep it as it is (Retry) or undo it, "
            "then Retry"
            % (
                short,
                subject,
                names,
                (
                    (" and files no piece owns (%s)" % ", ".join(outside[:3]))
                    if outside
                    else ""
                ),
            )
        )
        for p in pieces:
            t = by_id[p]
            if t["state"] in TERMINAL:
                continue
            out.append(_act("disarm", t))
            out.append(_to(t, "needs_you", "blocked", why))
    stray = sorted(str(x) for x in (so.get("stray") or []))[:50]
    fields: dict = {}
    if judged:
        fields["seen_foreign"] = list(sf.get("seen_foreign") or []) + judged
    if stray_commits != list(sf.get("stray_commits") or []):
        fields["stray_commits"] = stray_commits[-20:]
    if "stray" in so and stray != list(sf.get("stray") or []):
        fields["stray"] = stray
        fields["stray_at"] = now if stray else 0.0
    if fields:
        out.append(_act("run", sf=fields))
    return out


def _plan_sf_commits(run: dict, lo: dict, tobs: dict, now: float) -> List[dict]:
    """Same folder: the commit queue — MindFlock commits each done piece's
    paths, one piece per pass (one commit each), oldest first. A piece is
    committed once its trailer commit is on the lead's branch (history, not
    anyone's word)."""
    out: List[dict] = []
    remaining = []
    for t in run["tasks"]:
        if t["state"] != "integrating":
            continue
        o = tobs.get(t["id"]) or {}
        sha = _s(o.get("sf_commit"))
        own = list(o.get("sf_own") or [])
        if sha or (not own and t["self_commits"]):
            commits = list(o.get("commits") or []) or list(t["commits"])
            report_text = _s(o.get("report_text")) or t["report_text"]
            out.append(
                _to(
                    t,
                    "integrated",
                    merged_at=now,
                    finished_at=now,
                    merged_sha=sha or t["self_commits"][-1],
                    head_sha=sha or t["self_commits"][-1],
                    commits=commits,
                    report_text=report_text,
                    tests=tests_line(report_text) or t["tests"],
                    blocked_since=0.0,
                )
            )
        elif not own and o.get("sf_read"):
            out.append(
                _to(
                    t,
                    "needs_you",
                    "blocked",
                    "its changes are gone from the folder before MindFlock could "
                    "commit them — Retry to ask it again, or Skip it",
                )
            )
        else:
            remaining.append(t)
    if not remaining:
        return out
    remaining.sort(key=lambda t: (t["ready_at"] or 0.0, t["id"]))
    t = remaining[0]
    why = _sf_blocker(run, lo)
    if not why and lo.get("ready"):
        if t["blocked_since"]:
            out.append(_act("seen", t, fields={"blocked_since": 0.0}))
        out.append(_act("sf_commit", t))
        return out
    out.extend(_blocked(t, why, now))
    return out


def _sf_blocker(run: dict, lo: dict) -> str:
    """Why MindFlock cannot commit a piece into the lead's folder right now
    (``""`` = it can, or it is only a moment away)."""
    lead = run.get("lead") or {}
    title = lead.get("title") or "the lead"
    pinned = lead.get("branch") or ""
    if not lo.get("present"):
        if lo.get("pending"):
            return ""
        return "the group's lead %s is gone — bring it back or cancel the group" % (
            title
        )
    if lo.get("ready") and not lo.get("on_branch", True):
        return "%s is on %s, not the group's branch %s — check out %s, then Retry" % (
            title,
            _s(lo.get("live_branch")) or "a detached HEAD",
            pinned,
            pinned,
        )
    if lo.get("merging") or lo.get("operation"):
        return (
            "%s's folder is in the middle of a %s — finish or abort it, then Retry"
            % (
                title,
                _s(lo.get("operation")) or "merge",
            )
        )
    return ""


def _stale_run_record(o: dict, created) -> bool:
    """Whether the autopilot record on a title another session took is one a
    run armed BEFORE that session was created (so it is the run's, not the
    stranger's own)."""
    rec = o.get("autopilot") if isinstance(o.get("autopilot"), dict) else None
    if not rec or created is None or _s(rec.get("source")) not in ("run", "tix"):
        return False
    return float(rec.get("started") or 0.0) < float(created) - 1.0


def _autopilot_reaches(stage: str, depth: str) -> bool:
    from backend.web.core import autopilot as _ap

    return bool(stage) and _ap.reaches(stage, depth)


def _approval(rec: dict, lane: str) -> bool:
    if not rec.get("ask_first"):
        return False
    held = _s(rec.get("depth"))
    return _depth_rank(held) < _depth_rank(_lane_depth(lane))


def _plan_stuck(
    t: dict, o: dict, now: float, activity: str, progress_at: float, nudge_seen: float
) -> List[dict]:
    """Stuck detection, with provenance before anything is said:

    * the agent's work was CORROBORATED in this incarnation (``worked`` —
      ``agent_state.worked_at``, which a CPU-only blip never stamps);
    * it is idle now, and has been, with no new diff, commit, report or
      progress, for :data:`STUCK_AFTER_S` since the latest of those and of
      the last nudge's delivery;
    * then a nudge (at most :data:`MAX_NUDGES`, each counted once the queue
      has actually handed it over); only after both does it escalate.

    A usage limit is not idleness: the activity reads ``limit``, the clock
    stops, and it restarts when the agent moves again."""
    if not (t["worked"] or o.get("worked")):
        return []
    if activity != "idle" or o.get("limited"):
        return []
    if t["nudge_id"] and not nudge_seen:
        return []  # a nudge is still waiting to be delivered
    idle_since = float(o.get("activity_since") or 0.0)
    quiet_since = max(float(progress_at or 0.0), idle_since, float(nudge_seen or 0.0))
    if not quiet_since or now - quiet_since < STUCK_AFTER_S:
        return []
    if t["nudges"] < MAX_NUDGES:
        return [_act("nudge", t)]
    if t["state"] == "needs_you" and t["reason"] == "stuck":
        return []
    if int(o.get("beyond_base") or 0) > 0 or _s(o.get("stage")) in (
        "committed",
        "pushed",
        "pr_open",
    ):
        why = "stalled twice — it committed, but never said it was done"
    else:
        why = "stalled twice — no new diff or commit, no report"
    return [_to(t, "needs_you", "stuck", why)]


def _create_failure(t: dict, now: float, error: str) -> dict:
    n = t["attempts"]["create"] + 1
    attempts = dict(t["attempts"], create=n)
    if n > MAX_CREATE_RETRIES:
        return _to(
            t,
            "failed",
            "create",
            error[:300] or "the session could not be created",
            attempts=attempts,
            finished_at=now,
        )
    return _to(
        t,
        "queued",
        "",
        "retrying: " + (error[:200] or "create failed"),
        attempts=attempts,
        retry_at=now + CREATE_BACKOFF_S * (2 ** (n - 1)),
    )


# --------------------------------------------------------------------------- #
# One for all / split: the lead, the merge queue, the check, the release
# --------------------------------------------------------------------------- #
def after_check_state(run: dict) -> str:
    """Where a one-for-all group goes once its merged branch checks out: a
    lane that ships waits for the release (your click, or ``release: auto``);
    "leave it" / "commit" end there — the merged branch IS the result."""
    return (
        "release_ready" if run["policy"]["lane"] in ("push", "pr", "merge") else "done"
    )


def _plan_lead(run: dict, lo: dict, now: float, boot: bool) -> List[dict]:
    """The lead's own lifecycle: seen, gone, or never started."""
    lead = run.get("lead")
    if not lead:
        return []
    started = float(lead.get("started_at") or 0.0)
    deleted_at = float(lo.get("deleted_at") or 0.0)
    if deleted_at and deleted_at >= started - 1.0 and not lo.get("present"):
        return [
            _act(
                "abort",
                state="cancelled",
                detail="its lead %s was removed — the other sessions were kept"
                % lead["title"],
            )
        ]
    failed_at = float(lo.get("create_failed_at") or 0.0)
    if lo.get("create_failed") and not lo.get("present") and failed_at >= started - 1.0:
        return [
            _act(
                "abort",
                state="done_with_failures",
                detail="the lead could not be started: %s"
                % _s(lo.get("create_failed"))[:200],
            )
        ]
    fields: dict = {}
    if lo.get("present"):
        if lead.get("missing_since"):
            fields["missing_since"] = 0.0
        if not lead.get("incarnation") and lo.get("created_at"):
            fields["incarnation"] = float(lo["created_at"])
        # The group's branch is PINNED the first time it is seen: a lead that
        # later checks out something else (a detached HEAD, `checkout -b x`)
        # does not silently retarget the merges and the release — that is a
        # blocker the merge queue escalates (see _merge_blocker).
        if lo.get("branch") and not lead.get("branch"):
            fields["branch"] = lo["branch"]
    elif not lo.get("pending") and not lead.get("missing_since"):
        fields["missing_since"] = now
    return [_act("run", lead=fields)] if fields else []


def _lead_free(lo: dict) -> bool:
    """The lead may be merged into: there, not working (idle — or its CLI
    exited: a merge is plain git), a clean tracked tree, no merge or other
    git operation half-done, on the group's own branch, and no autopilot step
    of its own running."""
    return bool(
        lo.get("ready")
        and lo.get("activity") in ("idle", "offline")
        and lo.get("clean") is True
        and not lo.get("merging")
        and not lo.get("operation")
        and lo.get("on_branch", True)
        and not lo.get("shipping")
    )


def _merge_blocker(run: dict, lo: dict, handed: bool = False) -> str:
    """Why the merge queue cannot move without a person, or ``""`` when it is
    only waiting (the lead is busy, or a moment away from free). Each of these
    wedged the queue SILENTLY before: the caller escalates once one has held
    for :data:`MERGE_BLOCKED_S`."""
    lead = run.get("lead") or {}
    title = lead.get("title") or "the lead"
    pinned = lead.get("branch") or ""
    if not lo.get("present"):
        if lo.get("pending"):
            return ""
        return (
            "the group's lead %s is gone — %s keeps what was merged; cancel the "
            "group (its sessions are kept) or bring the lead back"
            % (title, ("its branch " + pinned) if pinned else "its branch")
        )
    if lo.get("ready") and not lo.get("on_branch", True):
        return "%s is on %s, not the group's branch %s — check out %s, then Retry" % (
            title,
            _s(lo.get("live_branch")) or "a detached HEAD",
            pinned,
            pinned,
        )
    if lo.get("operation"):
        return "%s is in the middle of a %s — finish or abort it, then Retry" % (
            title,
            _s(lo.get("operation")),
        )
    activity = _s(lo.get("activity"))
    if activity not in ("idle", "offline"):
        return ""  # working, or waiting on a prompt: it is the lead's turn
    if lo.get("merging"):
        return (
            "%s has a merge in progress (MERGE_HEAD) and stopped — commit it or "
            "abort it (git merge --abort), then Retry" % title
        )
    if lo.get("ready") and lo.get("clean") is False:
        return (
            "the lead's worktree has uncommitted changes — commit or discard "
            "them so MindFlock can merge this, then Retry"
        )
    if handed and activity == "offline":
        return (
            "%s's agent is not running, so nobody is resolving the conflict — "
            "merge it by hand (or restart the lead), then Retry" % title
        )
    return ""


def _went_idle_after(lo: dict, ts: float, settle: float, now: float) -> bool:
    """The lead worked after ``ts`` and has been idle ``settle`` seconds since:
    it took its turn on what it was handed."""
    since = float(lo.get("activity_since") or 0.0)
    return lo.get("activity") == "idle" and since >= ts + 5.0 and now - since >= settle


def _plan_integration(run: dict, obs: dict, now: float, acts: List[dict]) -> List[dict]:
    """The merge queue, the check on the merged branch and the release — the
    run-level half of a one-for-all group. One merge at a time, oldest ready
    first; a conflict holds the queue until the lead resolves it."""
    lo = obs.get("lead") or {}
    tobs = obs.get("tasks") or {}
    out: List[dict] = []
    state = run["state"]
    if run["paused"] or any(a["op"] == "pause" for a in acts):
        return out  # paused: nothing merges, checks or ships
    if state == "running":
        if same_folder(run):
            out.extend(_plan_sf_commits(run, lo, tobs, now))
        else:
            out.extend(_plan_merges(run, lo, tobs, now))
        moving = {a["task"]: a["state"] for a in acts + out if a["op"] == "state"}
        final = [moving.get(t["id"], t["state"]) for t in run["tasks"]]
        planned = not run.get("split") or (run.get("plan") or {}).get("state") == (
            "approved"
        )
        if final and planned and all(st in TERMINAL for st in final):
            if any(st == "integrated" for st in final):
                out.append(
                    _act(
                        "run",
                        state="checking",
                        check={"state": "pending", "attempts": 0, "summary": ""},
                    )
                )
                out.append(_act("check_start"))
            else:
                out.append(_act("finish", state="done_with_failures"))
        return out
    chk = run.get("check") or {}
    cst = chk.get("state")
    if state == "checking":
        lc = lo.get("check") if isinstance(lo.get("check"), dict) else None
        if cst == "pending":
            out.append(_act("check_start"))
        elif cst == "running":
            head = _s(lo.get("head"))
            done = lc is not None and lc.get("state") in ("ok", "failed")
            fresh = done and (not head or _s(lc.get("sha")) in ("", head))
            # The check ran on the commit it STARTED on (chk["sha"]); the
            # status is stamped with HEAD when it FINISHES. A lead that
            # committed in between (or after) would otherwise have an untested
            # commit credited with the pass — and a PR body saying so.
            started = _s(chk.get("sha"))
            stamp = _s(lc.get("sha")) if lc else ""
            if (
                done
                and started
                and ((head and head != started) or (stamp and stamp != started))
            ):
                out.append(
                    _act(
                        "run",
                        check={
                            "state": "pending",
                            "summary": "the branch moved during the check — "
                            "running it again",
                        },
                    )
                )
                out.append(_act("check_start"))
                return out
            if fresh and lc.get("state") == "ok":
                nxt = after_check_state(run)
                out.append(
                    _act(
                        "run",
                        state=nxt,
                        check={
                            "state": "ok",
                            "sha": _s(lc.get("sha")) or head,
                            "tests": lo.get("check_tests"),
                            "summary": "passed",
                            "finished_at": now,
                        },
                    )
                )
                if nxt == "done":
                    out.append(_act("finish", state=_finish_state(run)))
                else:
                    out.append(_act("release_prepare"))
            elif fresh:
                tail = _s(lo.get("check_tail"))[:400] or "see the check log"
                if int(chk.get("attempts") or 0) < MAX_CHECK_FIXES:
                    out.append(_act("check_fix", tail=tail))
                else:
                    out.append(
                        _act(
                            "run",
                            check={
                                "state": "failed",
                                "summary": tail,
                                "finished_at": now,
                            },
                        )
                    )
            elif (
                not lo.get("check_running")
                and lc is None
                and (now - float(chk.get("started_at") or now) > 60.0)
            ):
                out.append(_act("check_start"))  # the run was lost (a restart)
        elif cst == "fixing":
            if (
                _went_idle_after(
                    lo, float(chk.get("fix_sent_at") or 0.0), CHECK_FIX_IDLE_S, now
                )
                and lo.get("clean") is True
            ):
                out.append(_act("check_start"))
        elif cst in ("ok", "none", "skipped"):
            nxt = after_check_state(run)
            out.append(_act("run", state=nxt))
            if nxt == "done":
                out.append(_act("finish", state=_finish_state(run)))
        return out
    rel = run.get("release") or {}
    if state == "release_ready":
        if rel.get("state") not in ("ready", "failed"):
            out.append(_act("release_prepare"))
        elif (
            rel.get("state") == "ready"
            and run["policy"]["release"] == "auto"
            and run["policy"]["lane"] in ("push", "pr", "merge")
        ):
            out.append(_act("release", merge=run["policy"]["lane"] == "merge"))
        return out
    if state == "releasing":
        rec = lo.get("autopilot") if isinstance(lo.get("autopilot"), dict) else None
        if rec is None:
            out.append(
                _act(
                    "run",
                    state="release_ready",
                    release={
                        "state": "ready",
                        "detail": "the lead's lane was turned off — nothing was shipped",
                    },
                )
            )
        elif (
            rec.get("state") == "done"
            and _s(rel.get("local_origin"))
            and run["policy"]["lane"] in ("pr", "merge")
        ):
            # Pushed — into a folder on this machine. The PR the group asked
            # for cannot be opened from there: a hand-off that says so.
            out.append(_act("release_handoff", reason=local_origin_text(run)))
        elif rec.get("state") == "done":
            out.append(
                _act(
                    "run",
                    release={
                        "state": "done",
                        "pr_url": _s(rec.get("url")),
                        "head_sha": _s(lo.get("head")),
                        "detail": "",
                        "at": now,
                    },
                )
            )
            out.append(_act("finish", state=_finish_state(run)))
        elif rec.get("state") == "halted":
            reason = _s(rec.get("reason"))
            low = reason.lower()
            if "could not open the pull request" in low or "needs gh or" in low:
                out.append(_act("release_handoff", reason=reason))
            else:
                out.append(
                    _act(
                        "run",
                        state="release_ready",
                        release={"state": "failed", "detail": reason},
                    )
                )
        else:
            note = _s(rec.get("note"))
            if note and note != rel.get("detail"):
                out.append(_act("run", release={"detail": note}))
    return out


def local_origin_text(run: dict) -> str:
    """What a release whose lead pushes into a FOLDER did — the truth, not a
    PR: where the branch went, and that the PR is opened by hand on the
    forge after pushing it there."""
    rel = run.get("release") or {}
    branch = _s(rel.get("branch")) or _s((run.get("lead") or {}).get("branch"))
    return (
        "pushed %s to %s — a folder on this machine, not GitHub, so no PR was "
        "opened: push the branch to your forge and open the PR there"
        % (branch or "the branch", _s(rel.get("local_origin")))
    )


def lead_gone(run: dict, now: float) -> str:
    """The sentence for a one-for-all group whose LEAD has been gone (not
    removed by you — that cancels the group — but lost, e.g. across a
    restart) for :data:`MERGE_BLOCKED_S`, or ``""``. Nothing can start, merge
    or ship without it, so it is said instead of the group waiting silently."""
    lead = run.get("lead") or {}
    since = float(lead.get("missing_since") or 0.0)
    if not since or run["state"] in RUN_FINISHED or now - since < MERGE_BLOCKED_S:
        return ""
    return (
        "its lead %s is gone — cancel the group (its sessions and branches are "
        "kept) or bring the lead back" % (lead.get("title") or "")
    )


def _finish_phrase_sf(rel: dict, n: int) -> str:
    """:func:`finish_phrase` for a same-folder split (committed, not merged)."""
    pieces = "%d piece%s" % (n, "" if n == 1 else "s")
    if rel.get("state") == "done" and _s(rel.get("pr_url")):
        return "one PR opened (%s, one commit each)" % pieces
    if rel.get("state") == "handoff" and _s(rel.get("local_origin")):
        return "its branch was pushed to a folder on this machine; no PR was opened"
    if rel.get("state") == "handoff":
        return "its branch was pushed; the PR was not opened — open it from the branch"
    if rel.get("state") == "done":
        return "its branch was pushed (%s, one commit each)" % pieces
    return "%s committed on one branch, nothing pushed" % pieces


def finish_phrase(run: dict, shipped: int) -> str:
    """What a finished group did, in a few words — for its notification. A
    one-for-all group ships ONE branch (one PR at most), never "N PRs"."""
    if not is_together(run):
        lane = run["policy"]["lane"]
        noun = (
            ("PR" if shipped == 1 else "PRs") if lane in ("pr", "merge") else "shipped"
        )
        return "%d %s" % (shipped, noun)
    rel = run.get("release") or {}
    merged = sum(1 for t in run["tasks"] if t["state"] == "integrated")
    if same_folder(run):
        return _finish_phrase_sf(rel, merged)
    if rel.get("state") == "done" and _s(rel.get("pr_url")):
        return "one PR opened (%d merged into it)" % merged
    if rel.get("state") == "handoff" and _s(rel.get("local_origin")):
        return "its branch was pushed to a folder on this machine; no PR was opened"
    if rel.get("state") == "handoff":
        return "its branch was pushed; the PR was not opened — open it from the branch"
    if rel.get("state") == "done" and _s(rel.get("local_origin")):
        return (
            "its branch was pushed to a folder on this machine (%d merged into it)"
            % (merged)
        )
    if rel.get("state") == "done":
        return "its branch was pushed (%d merged into it)" % merged
    return "%d merged into one branch, nothing pushed" % merged


def _finish_state(run: dict) -> str:
    return (
        "done_with_failures"
        if any(t["state"] == "failed" for t in run["tasks"])
        else "done"
    )


def _plan_merges(run: dict, lo: dict, tobs: dict, now: float) -> List[dict]:
    out: List[dict] = []
    integ = [t for t in run["tasks"] if t["state"] == "integrating"]
    remaining = []
    for t in integ:
        o = tobs.get(t["id"]) or {}
        if o.get("merged"):
            # In the lead's history, by the server's merge or the lead's own:
            # verified by ancestry, never by anyone's word.
            report_text = _s(o.get("report_text")) or t["report_text"]
            out.append(
                _to(
                    t,
                    "integrated",
                    merged_at=now,
                    finished_at=now,
                    merged_sha=_s(lo.get("head")),
                    head_sha=_s(o.get("head")) or t["head_sha"],
                    commits=list(o.get("commits") or t["commits"]),
                    conflict_fixed=bool(t["conflict"]),
                    report_text=report_text,
                    tests=tests_line(report_text) or t["tests"],
                    blocked_since=0.0,
                )
            )
        else:
            remaining.append(t)
    if not remaining:
        return out
    remaining.sort(key=lambda t: (t["ready_at"] or 0.0, t["id"]))
    handed = [t for t in remaining if t["reason"] == "conflict" and t["conflict"]]
    if handed:
        t = handed[0]
        c = t["conflict"]
        if _lead_free(lo) and _went_idle_after(
            lo, float(c["at"] or 0.0), CONFLICT_IDLE_S, now
        ):
            if int(c["attempts"] or 0) < MAX_CONFLICT_HANDOFFS:
                out.append(_act("merge", t))  # conflicts again → hand-off 2
            else:
                out.append(
                    _to(
                        t,
                        "needs_you",
                        "conflict",
                        "the lead could not resolve the conflicts in %s — merge "
                        "%s by hand, then Retry"
                        % (
                            ", ".join(c["files"][:4]) or "it",
                            t["branch"] or t["title"],
                        ),
                    )
                )
        else:
            out.extend(_blocked(t, _merge_blocker(run, lo, handed=True), now))
        return out  # serialized: nothing else merges past a conflict
    t = remaining[0]
    if _lead_free(lo):
        if t["blocked_since"]:
            out.append(_act("seen", t, fields={"blocked_since": 0.0}))
        out.append(_act("merge", t))
        return out
    out.extend(_blocked(t, _merge_blocker(run, lo), now))
    return out


def _blocked(t: dict, why: str, now: float) -> List[dict]:
    """The merge queue's head is blocked by ``why`` (``""`` = not blocked):
    start the clock, and after :data:`MERGE_BLOCKED_S` hand it to you."""
    if not why:
        return (
            [_act("seen", t, fields={"blocked_since": 0.0})]
            if t["blocked_since"]
            else []
        )
    if not t["blocked_since"]:
        return [_act("seen", t, fields={"blocked_since": now})]
    if now - t["blocked_since"] >= MERGE_BLOCKED_S:
        if t["state"] == "needs_you" and t["reason"] == "conflict":
            return []
        return [_to(t, "needs_you", "conflict", why)]
    return []


# --------------------------------------------------------------------------- #
# Applying pure actions
# --------------------------------------------------------------------------- #
def task_by_id(run: dict, task_id: str) -> Optional[dict]:
    for t in run["tasks"]:
        if t["id"] == task_id:
            return t
    return None


def log_event(run: dict, now: float, kind: str, task: str = "", text: str = "") -> None:
    run["events"].append({"ts": now, "kind": kind, "task": task, "text": text})
    if len(run["events"]) > EVENTS_MAX:
        del run["events"][: len(run["events"]) - EVENTS_MAX]


_STATE_TEXT = {
    "working": "working",
    "shipping": "shipping",
    "shipped": "shipped",
    "failed": "failed",
    "cancelled": "removed by you",
    "skipped": "skipped",
    "queued": "queued",
    "needs_you": "needs you",
    "integrating": "waiting to merge",
    "integrated": "merged back",
}


_RUN_STATE_TEXT = {
    "planning": "waiting for the lead's plan",
    "plan_ready": "plan ready — waiting for your approval",
    "running": "running",
    "checking": "all merged — running the check",
    "release_ready": "ready to release",
    "releasing": "releasing",
}


def apply(run: dict, action: dict, now: float) -> Optional[Tuple[str, str]]:
    """Apply one PURE action (``state`` / ``seen`` / ``log`` / ``pause`` /
    ``finish``) to ``run`` in place. Returns ``(old_state, new_state)`` for a
    task state change, else None. Side-effect ops are ignored here."""
    op = action.get("op")
    if op == "run":
        old_state = run["state"]
        new_state = _s(action.get("state"))
        if new_state in RUN_STATES and new_state != old_state:
            run["state"] = new_state
            log_event(
                run, now, new_state, text=_RUN_STATE_TEXT.get(new_state, new_state)
            )
        for key, norm in (
            ("check", _normalize_check),
            ("release", _normalize_release),
        ):
            if isinstance(action.get(key), dict):
                run[key] = norm(dict(run.get(key) or {}, **action[key]))
        if isinstance(action.get("lead"), dict) and run.get("lead"):
            run["lead"] = _normalize_lead(dict(run["lead"], **action["lead"]))
        if isinstance(action.get("plan"), dict) and run.get("plan"):
            run["plan"] = _normalize_plan(dict(run["plan"], **action["plan"]))
        if isinstance(action.get("sf"), dict):
            run["sf"] = _normalize_sf(dict(run.get("sf") or {}, **action["sf"]))
        return None
    if op == "abort":
        detail = _s(action.get("detail"))
        for t in run["tasks"]:
            if t["state"] in TERMINAL:
                continue
            apply(run, _to(t, "cancelled", "", detail, finished_at=now), now)
        if run["state"] not in RUN_FINISHED:
            run["state"] = _s(action.get("state")) or "cancelled"
            run["paused"] = False
            run["finished_at"] = now
            # A cancel is the user's own act (they removed the lead): nothing
            # to announce — and "announced" is what lets the loop stop
            # stepping this run every pass for the next 30 days.
            run["summary"] = dict(
                summarize(run, now), announced=run["state"] == "cancelled"
            )
            log_event(run, now, run["state"], text=detail)
        return None
    if op == "pause":
        if not run["paused"]:
            run["paused"] = True
            run["pause_reason"] = _s(action.get("reason")) or "user"
            log_event(run, now, "paused", text=run["pause_reason"])
        return None
    if op == "finish":
        state = _s(action.get("state")) or "done"
        if run["state"] not in RUN_FINISHED:
            run["state"] = state
            run["finished_at"] = now
            prev = run.get("summary") or {}
            run["summary"] = dict(
                summarize(run, now), announced=bool(prev.get("announced"))
            )
            log_event(run, now, "finished", text=state)
        return None
    t = task_by_id(run, _s(action.get("task")))
    if t is None:
        return None
    if op == "seen":
        t.update(action.get("fields") or {})
        return None
    if op == "log":
        log_event(run, now, "note", t["id"], _s(action.get("text")))
        return None
    if op != "state":
        return None
    old = t["state"]
    new = _s(action.get("state"))
    if new not in TASK_STATES:
        return None
    t.update(action.get("fields") or {})
    old_reason = t["reason"]
    t["state"] = new
    t["reason"] = _s(action.get("reason"))
    t["detail"] = (
        _s(action.get("detail"))
        if new
        in ("needs_you", "failed", "queued", "cancelled", "skipped", "integrating")
        else ""
    )
    if new == "working" and old == "needs_you" and old_reason == "stuck":
        t["nudges"] = 0
        t["nudge_id"] = ""
        t["nudge_seen_at"] = 0.0
    if old != new or old_reason != t["reason"]:
        text = _STATE_TEXT.get(new, new)
        if new == "needs_you" and t["reason"]:
            text += " (%s)" % t["reason"]
        if t["detail"]:
            text += ": " + t["detail"][:200]
        log_event(run, now, new, t["id"], text)
        return old, new
    return None


def announce_key(run: dict, task: dict, reason: str) -> str:
    return "%s:%s:%s:%s" % (
        run["id"],
        task["id"],
        reason,
        int(task["incarnation"] or 0),
    )
