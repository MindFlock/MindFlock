"""Autopilot — carry a session as far down agent → commit → push → PR → merge
as its owner chose, and stop there.

ONE MECHANISM, TWO ENTRY POINTS. The fast-track button on a live session and the
"how far should this go" option on an ingested ticket/PR/issue are the same
thing: a target rung recorded against a session title, plus a background driver
that keeps nudging that session toward it. Nothing about the chain lives in the
browser — an intake-triggered run begins before the agent has written a line and
can last hours, so a tab close (or a page reload, or a server restart) must not
end it.

WHY A TARGET AND NOT A SCRIPT. Every step is fire-and-forget: the commit and push
endpoints type a shell one-liner into the session's interactive tmux and return
before anything has happened, with no exit code and no completion callback. So
the driver cannot execute a sequence — it can only observe the git-derived stage
and decide what the next action is. That makes the whole policy a pure function
of (target, observed state), which is why :func:`next_action` takes a snapshot
dict and returns a verb: resume-after-restart, first start, and steady-state are
all the same code path, and none of them need bookkeeping about where the chain
"was".

THE PERSISTED PART IS DELIBERATELY TINY. The chain's POSITION is never stored,
because the stage is recomputed from real git and GitHub on every pass — after a
restart the driver simply re-derives where it is. Only the things reality cannot
tell us are kept: the target depth, the bounded attempt counters, which hooks
have been skipped, and a halt reason a human needs to read.

STATE LIVES IN ITS OWN FILE (``~/.mindflock/autopilot.json``), not in the
engine's ``state.json``: this is ancillary per-session data, ``Engine.save()``
only persists sessions that have Started (which would lose the depth for exactly
the intake case that needs it — a session still provisioning), and a mutable
step/attempt record would otherwise put a state.json write on every pass.

Keyed by SESSION TITLE, which is what makes the intake half work with no IPC: the
title an ingested item produces is deterministic and identical whether the
pipeline child process or a forced start creates it, so an item can be armed
before its session exists. Titles are reused after a delete, so :func:`prune`
against the live session set is mandatory.

Thread-safety follows :mod:`backend.web.core.prompt_queue`: sync routes run in
the worker threadpool, the driver runs on the event loop, and the ingestion
pipeline is a separate OS process — so every accessor takes a module lock and
re-reads/writes the (tiny) file under it, and every write is atomic.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend.config.config import GetConfigDir

__all__ = [
    "autopilot_path",
    "DEPTH_ORDER",
    "DEPTHS",
    "SOURCE_DEPTHS",
    "NEVER_SKIP",
    "DEPTH_LABELS",
    "normalize_depth",
    "reaches",
    "stage_achieved",
    "next_action",
    "arm",
    "disarm",
    "get",
    "update",
    "halt",
    "finish",
    "all_titles",
    "prune",
    "snapshot",
    "dto",
    "claim",
]

_FileName = "autopilot.json"
_LOCK = threading.Lock()

# --- The ladder --------------------------------------------------------------
# Rungs are TARGETS (imperative); the session stages they are compared against
# are past-tense facts ("committed", "pushed"). Keeping the two vocabularies
# separate — and mapping between them in exactly one place, `_STAGE_ACHIEVED` —
# is what stops "am I there yet?" from turning into a pile of special cases.
DEPTH_ORDER = ("off", "agent", "commit", "push", "pr", "merge")
#: Depths a caller may arm. "off" is the absence of a run, not a target.
DEPTHS = DEPTH_ORDER[1:]
#: Depths a whole intake SOURCE may default to. Merge is deliberately absent:
#: a per-source default applies to every future item with no human in the loop,
#: and merging is the one step in the ladder that cannot be undone. Merge stays
#: available per item.
SOURCE_DEPTHS = tuple(d for d in DEPTHS if d != "merge")

DEPTH_LABELS = {
    "off": "Off",
    "agent": "Agent only",
    "commit": "Commit",
    "push": "Push",
    "pr": "Open PR",
    "merge": "Merge",
}

#: Which rung a given session stage PROVES has been completed. Stages that mean
#: "still working" (provisioning/agent/precommit/interrupt) prove nothing.
_STAGE_ACHIEVED = {
    "committed": "commit",
    "pushed": "push",
    "pr": "pr",
}

#: Hooks whose failure must never be skipped, whatever the settings say. A test
#: hook failing is the signal the owner explicitly wants to keep stopping the
#: run; a secret scanner failing means a credential is about to be committed.
#: Enforced here rather than only in the UI so a hand-edited settings file, an
#: addon, or a future caller cannot route around it.
NEVER_SKIP = frozenset(
    {
        "detect-secrets",
        "detect-private-key",
        "gitleaks",
        "trufflehog",
        "run-tests",
        "run_tests",
        "pytest",
        "tests",
    }
)

# Bounded attempts. A commit is retried at most twice per offending hook (one
# plain retry, then one with the hook skipped), and a run makes at most this many
# commit attempts overall however the retries are distributed.
MAX_COMMIT_ATTEMPTS = 4
#: Cap on how many times the driver will issue the SAME verb. A push the remote
#: keeps refusing must stop being re-typed; the stage never moves, so nothing else
#: would ever end that loop.
MAX_ACTIONS_PER_VERB = 3
#: Consecutive-idle dwell before the FIRST commit of a chain. Longer than the
#: prompt queue's 12s settle on purpose: feeding a prompt to an agent that turns
#: out to be mid-thought is recoverable, committing and opening a PR for it is
#: not. Costs ~18s more latency and removes the whole "committed mid-thought"
#: class of failure.
IDLE_SETTLE_S = 30.0
#: How long a record survives with no matching session before :func:`prune`
#: drops it. Must comfortably exceed a cold clone plus worktree provisioning,
#: because intake arms BEFORE the session exists. 30 minutes.
ARM_GRACE_S = 1800.0
#: How long the "waiting for the agent to start" state may last before the run
#: gives up. Generous — you may arm a ticket and let it sit — but bounded, so a
#: session that never gets an agent does not stay armed forever. 2 hours.
ARM_WAIT_DEADLINE_S = 7200.0
#: How long an unrefreshed driver lease is honoured before another server may take
#: over. The driver passes every 5s, so this is many missed passes — long enough
#: that a slow pass never loses the lease, short enough that a crashed server does
#: not strand a chain.
LEASE_STALE_S = 45.0
#: Branch names autopilot will never push or PR from, whatever the configured base
#: resolves to. Defence in depth: the base-branch guard relies on
#: `_session_base_branch`, and if that resolves to "" (a detached probe, a repo with
#: no upstream, an exception swallowed upstream) the comparison silently passes and
#: the trunk gets pushed. A remote whose ruleset is bypass-silent then accepts it
#: and merely NOTES that a PR was required — which is exactly what happened, twice.
TRUNK_BRANCHES = frozenset({"main", "master", "trunk", "develop", "development"})


def autopilot_path() -> str:
    """Path to the autopilot store.

    Honors ``$MINDFLOCK_AUTOPILOT_FILE`` (tests point it at a tmp file, and
    without that an ad-hoc TestClient run would write the user's real store);
    otherwise ``<config dir>/autopilot.json``.
    """
    env = os.environ.get("MINDFLOCK_AUTOPILOT_FILE")
    if env:
        return env
    return os.path.join(GetConfigDir(), _FileName)


# On-disk shape::
#   {"<title>": {"depth": "pr", "state": "running"|"halted"|"done",
#                "step": "agent", "reason": "", "source": "session"|"tix"|"pr"|"iss",
#                "item": "sc-123", "message": "commit subject",
#                "base": "", "branch": "", "retryable": ["gitnexus-index"],
#                "attempts": {"<hook>": 1}, "commits": 0, "skipped": [],
#                "idle_since": null, "step_since": 0.0, "acted_at": 0.0,
#                "boot": "", "started": 0.0, "updated": 0.0}}
def _load() -> dict:
    try:
        with open(autopilot_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = autopilot_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".ap.", suffix=".tmp"
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


def _blank() -> dict:
    return {
        "depth": "",
        "state": "running",
        "step": "",
        "reason": "",
        # The server's own sentence for what this pass is waiting on ("waiting for
        # checks to finish", "prompt queue still has work"). next_action already
        # produces one for every wait; without a home for it the UI had to guess.
        "note": "",
        "source": "session",
        "item": "",
        "message": "",
        # The PR this run opened, so the UI can bring it up exactly once — the same
        # courtesy the manual "Make PR" button does.
        "url": "",
        "base": "",
        "branch": "",
        "retryable": [],
        "attempts": {},
        "skipped": [],
        "commits": 0,
        # Per-verb action counter, e.g. {"push": 2} — the backstop for a step that
        # keeps being attempted because the observed stage never changes.
        "issues": {},
        "idle_since": None,
        # When this run last SAW the agent working. Until it is set, a clean tree
        # means "not started yet", not "finished with nothing to show".
        "worked_at": 0.0,
        "step_since": 0.0,
        "acted_at": 0.0,
        "boot": "",
        # WHICH SERVER IS DRIVING THIS CHAIN, and when it last said so. Two servers
        # sharing one store (a dev instance on another port, say) both ran the
        # driver: they saw each other's boot id, each treated it as a restart, and
        # reset the idle dwell every pass — so the 30s settle could never elapse and
        # a chain sat at "agent just went idle" forever. Both would also have ACTED,
        # double-committing and double-pushing. One lease, one driver.
        "owner": "",
        "owner_at": 0.0,
        "started": 0.0,
        "updated": 0.0,
    }


def normalize_depth(value) -> str:
    """Coerce anything to a valid rung name, or ``""`` for "not a depth".

    ``""`` and ``"off"`` both mean "no run"; the caller decides which of the two
    it wants to store.
    """
    d = str(value or "").strip().lower()
    if d in ("", "off"):
        return "off" if d == "off" else ""
    return d if d in DEPTHS else ""


def _normalize(entry) -> dict:
    """Coerce a stored entry (any shape) into the canonical dict.

    This IS the migration mechanism — there is no version key. A record written
    by an older build simply gains the new fields at their defaults, and a
    hand-mangled one cannot crash a pass.
    """
    e = _blank()
    if not isinstance(entry, dict):
        return e
    e["depth"] = normalize_depth(entry.get("depth"))
    state = str(entry.get("state", "") or "running")
    e["state"] = state if state in ("running", "halted", "done") else "running"
    e["step"] = str(entry.get("step", "") or "")
    e["reason"] = str(entry.get("reason", "") or "")
    e["note"] = str(entry.get("note", "") or "")
    src = str(entry.get("source", "") or "session")
    e["source"] = src if src in ("session", "tix", "pr", "iss") else "session"
    e["item"] = str(entry.get("item", "") or "")
    e["message"] = str(entry.get("message", "") or "")
    e["url"] = str(entry.get("url", "") or "")
    e["base"] = str(entry.get("base", "") or "")
    e["branch"] = str(entry.get("branch", "") or "")
    raw = entry.get("retryable")
    e["retryable"] = [str(h) for h in raw][:16] if isinstance(raw, list) else []
    att = entry.get("attempts")
    if isinstance(att, dict):
        out: Dict[str, int] = {}
        for k, v in list(att.items())[:32]:
            try:
                out[str(k)] = max(0, int(v))
            except (TypeError, ValueError):
                continue
        e["attempts"] = out
    iss = entry.get("issues")
    if isinstance(iss, dict):
        out2: Dict[str, int] = {}
        for k, v in list(iss.items())[:32]:
            try:
                out2[str(k)] = max(0, int(v))
            except (TypeError, ValueError):
                continue
        e["issues"] = out2
    sk = entry.get("skipped")
    e["skipped"] = [str(h) for h in sk][:16] if isinstance(sk, list) else []
    for key in ("commits",):
        try:
            e[key] = max(0, int(entry.get(key, 0) or 0))
        except (TypeError, ValueError):
            e[key] = 0
    idle = entry.get("idle_since")
    e["idle_since"] = float(idle) if isinstance(idle, (int, float)) else None
    for key in (
        "step_since",
        "acted_at",
        "started",
        "updated",
        "worked_at",
        "owner_at",
    ):
        try:
            e[key] = float(entry.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            e[key] = 0.0
    e["boot"] = str(entry.get("boot", "") or "")
    e["owner"] = str(entry.get("owner", "") or "")
    return e


# --- Pure ladder helpers -----------------------------------------------------
def stage_achieved(stage: str) -> str:
    """The highest rung a given session ``stage`` proves is complete."""
    return _STAGE_ACHIEVED.get(str(stage or ""), "")


def reaches(stage: str, depth: str) -> bool:
    """Whether ``stage`` already satisfies the target ``depth``.

    ``merge`` is never satisfied by a stage: the server has no "merged" stage (a
    merged PR moves the stage OFF "pr"), so the merge rung completes when the
    merge call returns ok, not by observation.
    """
    depth = normalize_depth(depth)
    if depth in ("", "off", "merge"):
        return False
    got = stage_achieved(stage)
    if not got:
        return False
    return DEPTH_ORDER.index(got) >= DEPTH_ORDER.index(depth)


def next_action(rec: dict, snap: dict) -> Tuple[str, dict]:
    """Decide the ONE thing to do next for a session. Pure — no I/O.

    ``rec`` is a normalized store record; ``snap`` is an observation::

        {"stage", "failed_step", "failed_hook", "dirty", "beyond_base",
         "activity", "limited", "queue_pending", "check", "has_origin", "now"}

    Returns ``(action, detail)`` where action is one of ``wait`` (nothing to do
    yet), ``commit``, ``push``, ``make_pr``, ``merge``, ``done`` (target
    reached), or ``stop`` (halt and tell the human). ``detail`` carries
    ``reason`` for stop/wait and ``skip`` for commit.

    Keeping this pure is the point: the whole ladder-and-retry policy is
    table-testable with no git, no tmux and no network, and the impure driver
    becomes a dispatch loop with nothing to reason about.
    """
    depth = normalize_depth(rec.get("depth"))
    if depth in ("", "off"):
        return "stop", {"reason": "no target depth"}
    if rec.get("state") != "running":
        return "done" if rec.get("state") == "done" else "stop", {
            "reason": rec.get("reason") or ""
        }

    stage = str(snap.get("stage") or "")

    # A commit is in flight (the lock is held): the shell owns this session.
    if stage == "precommit":
        return "wait", {"reason": "pre-commit hooks are running"}

    # Target already met?
    if reaches(stage, depth):
        return "done", {}

    # A blocked commit: retry, skip, or halt.
    # A target of "agent only" must never run git commit — dispatch it before the
    # interrupt branch, which would otherwise re-commit on an allowlisted hook.
    if depth == "agent":
        return _agent_action(rec, snap, depth)

    if stage == "interrupt":
        return _interrupt_action(rec, snap)

    if stage == "provisioning":
        return "wait", {"reason": "workspace is still being provisioned"}

    if stage == "agent":
        return _agent_action(rec, snap, depth)

    if stage == "committed":
        if depth == "commit":  # defensive: reaches() already covered this
            return "done", {}
        # NEVER push the base branch. A session sitting on main/master has no
        # feature branch, so "push" means pushing the trunk itself and "open a PR"
        # is meaningless — there is nothing to open one against. This is not
        # hypothetical: a run on `main` pushed 20 files straight to origin/main and
        # the remote reported "Bypassed rule violations … Changes must be made
        # through a pull request". Committing is fine and already happened; going
        # further needs a branch, so stop and say exactly that.
        live = str(snap.get("branch") or "")
        if snap.get("on_base_branch") or live.lower() in TRUNK_BRANCHES:
            return "stop", {
                "reason": "committed, but this session is on %s — make a branch to push or PR"
                % (live or "the base branch")
            }
        if snap.get("has_origin") is False:
            return "stop", {"reason": "no origin remote — add one to push"}
        check = snap.get("check")
        state = str((check or {}).get("state") or "")
        if state == "failed":
            return "stop", {"reason": "checks failed — fix them and re-run"}
        if state == "running":
            return "wait", {"reason": "waiting for checks to finish"}
        # The push route SOFT-GATES on the verification check: in a repo that
        # declares a `check_command` it answers 409 "checks haven't passed for this
        # commit" whenever the result is missing, not-ok, or stale. Discovering
        # that by pushing turned into a permanent halt, so ask for the check here
        # instead. Never force past it — the gate is the owner's "if tests fail,
        # stop", and it is the one thing that must keep working.
        if snap.get("check_required"):
            if check is None or check.get("stale"):
                return "run_check", {"reason": "starting the verification check"}
            if state != "ok":
                return "wait", {"reason": "waiting for checks to finish"}
        return "push", {}

    if stage == "pushed":
        return "make_pr", {"base": rec.get("base") or ""}

    if stage == "pr":
        if depth != "merge":
            return "done", {}
        # Merge is the one irreversible rung, so CI gates it. "unknown" (no token,
        # no remote, API fault) is treated as pending and eventually times out —
        # never as permission to merge. "none" means this repo reports no checks at
        # all, which is a legitimate green light rather than something to wait for.
        ci = str(snap.get("pr_checks") or "unknown")
        if ci == "failed":
            return "stop", {"reason": "CI failed on the PR — not merging"}
        if ci in ("pending", "unknown"):
            return "wait", {"reason": "waiting for CI to pass before merging"}
        return "merge", {}

    return "wait", {"reason": "stage %s" % (stage or "unknown")}


def _interrupt_action(rec: dict, snap: dict) -> Tuple[str, dict]:
    """The retry policy for a pre-commit failure.

    A hook that failed WITHOUT changing any files has already defeated the shell
    one-liner's own retry loop (which only re-commits when an auto-fixer left
    unstaged changes). Some such failures are nonetheless spurious — an
    index-rebuilding hook with a corrupted index is the motivating case: it will
    fail identically forever and blocks the commit permanently, while its output
    has no bearing on whether the code is correct.

    So an ALLOWLISTED hook gets one plain retry (which clears a genuinely
    transient failure) and then one attempt with the hook bypassed via ``SKIP=``.
    Anything else — and every hook in :data:`NEVER_SKIP`, whatever the settings
    say — halts the run with the hook named, which is how a failing test keeps
    stopping the chain.
    """
    hook = str(snap.get("failed_hook") or "")
    step = str(snap.get("failed_step") or "")
    named = step or hook or "a hook"
    retryable = [h for h in (rec.get("retryable") or []) if h not in NEVER_SKIP]

    # THIS RUN HAS NOT COMMITTED YET. The "interrupt" it is looking at therefore
    # belongs to an EARLIER attempt — very often a manual commit whose
    # .mindflock_commit_status is still on disk — so halting here would abort the
    # press within seconds, blaming a failure the user pressed the button to get
    # past. Spend our own first attempt instead: that is byte-for-byte what the
    # manual "Re-commit" button does (re-stage the hooks' auto-fixes, same
    # message), and the policy below then governs everything after it.
    if int(rec.get("commits") or 0) == 0:
        return "commit", {"skip": list(rec.get("skipped") or [])}

    if not hook:
        # No parseable hook id (a raw git hook, or the pane scrolled away).
        # Guessing from the display name is not possible — pre-commit's `name:`
        # is free text and does not map back to an id.
        return "stop", {"reason": "pre-commit failed at " + named}
    if hook in NEVER_SKIP or hook not in retryable:
        return "stop", {"reason": "pre-commit failed at " + named}
    if int(rec.get("commits") or 0) >= MAX_COMMIT_ATTEMPTS:
        return "stop", {"reason": "too many commit attempts; last failure: " + named}

    tries = int((rec.get("attempts") or {}).get(hook) or 0)
    if tries == 0:
        return "commit", {"skip": list(rec.get("skipped") or []), "hook": hook}
    if tries == 1:
        skip = list(rec.get("skipped") or [])
        if hook not in skip:
            skip.append(hook)
        return "commit", {"skip": skip, "hook": hook, "skipping": hook}
    return "stop", {"reason": "pre-commit kept failing at " + named}


def _agent_action(rec: dict, snap: dict, depth: str) -> Tuple[str, dict]:
    """The "is the agent done?" gate.

    There is no signal for "the agent finished its task" — only for "a turn
    ended", and even that is produced by a stop-hook, an exit marker for ANY exit
    code, or a quiet pane. A crashed CLI and a completed task look identical
    here. So this gate stacks every cheap corroborating condition it can:

    * the activity probe says ``idle`` (never merely "not working");
    * it has said so continuously for :data:`IDLE_SETTLE_S`;
    * the account is not usage-limited (a limit can end a turn mid-task);
    * the session's prompt queue has nothing pending — that is what composes the
      two features instead of racing them: queue the follow-up turns you want and
      autopilot waits for the queue to drain before it commits;
    * the tree is actually dirty, so an agent that died before writing anything
      halts loudly instead of committing an empty tree.

    CRUCIALLY, a clean tree is only a FAILURE once this run has actually seen the
    agent do something (``worked_at``) or produce a commit. Arming is normally the
    FIRST thing you do — before the agent has written a line, or between turns, or
    while it is still starting up — and ``_agent_activity`` reports "idle" 8s after
    a static pane and instantly when a Stop hook fires. Halting then said "the
    agent finished without changing anything" about an agent that had not begun,
    roughly 35 seconds after every press. That was the single worst bug in the
    feature. Now it waits, bounded by the arm deadline.
    """
    activity = str(snap.get("activity") or "")
    if snap.get("limited"):
        return "wait", {"reason": "usage limit — waiting for the window to reopen"}
    if activity != "idle":
        return "wait", {"reason": "agent is %s" % (activity or "busy")}
    if snap.get("queue_pending"):
        return "wait", {"reason": "prompt queue still has work"}
    idle_since = rec.get("idle_since")
    now = float(snap.get("now") or 0.0)
    if idle_since is None:
        return "wait", {"reason": "agent just went idle", "mark_idle": True}
    if now - float(idle_since) < IDLE_SETTLE_S:
        return "wait", {"reason": "confirming the agent is done"}
    if depth == "agent":
        return "done", {}
    if not snap.get("dirty"):
        beyond = snap.get("beyond_base")
        if beyond is None:
            # Could not MEASURE how far ahead we are (no base branch resolved, or
            # the count failed). That is not evidence of "nothing happened", so it
            # must never be read as one — wait and re-measure.
            return "wait", {"reason": "working out what has already been committed"}
        if int(beyond) > 0:
            # Work is already committed; the stage just still reads "agent".
            return "done", {}
        # HAS THIS RUN ALREADY DONE SOMETHING? If so, the sentence below is a lie
        # and must never be reached. A session on its BASE branch lands here
        # permanently — `_session_stage` collapses to "agent" whenever
        # `origin/<base>..HEAD` is 0, which it always is once the base branch has
        # been pushed — so a run that had committed AND pushed reported "the agent
        # finished without changing anything", with commits=1 and step="push"
        # recorded right next to it.
        if int(rec.get("commits") or 0) > 0 or rec.get("step"):
            live = str(snap.get("branch") or "")
            if snap.get("on_base_branch") or live.lower() in TRUNK_BRANCHES:
                return "stop", {
                    "reason": "committed on %s — make a branch to push or PR"
                    % (live or "the base branch")
                }
            return "done", {}
        if not rec.get("worked_at"):
            # Armed before the agent got going. Wait for it, bounded by the arm
            # deadline — see the docstring.
            return "wait", {"reason": "waiting for the agent to start"}
        return "stop", {"reason": "the agent finished without changing anything"}
    return "commit", {"skip": list(rec.get("skipped") or [])}


# --- Store accessors ---------------------------------------------------------
def get(title: str) -> Optional[dict]:
    """The normalized record for ``title``, or None."""
    if not title:
        return None
    with _LOCK:
        entry = _load().get(title)
    return _normalize(entry) if entry is not None else None


def claim(title: str, owner: str, now: Optional[float] = None):
    """Take or refresh the driver lease for ``title``.

    Returns ``(rec, took_over)`` when this ``owner`` may drive the chain, or
    ``(None, False)`` when a DIFFERENT server holds a live lease. ``took_over`` is
    True when the lease changed hands (a restart, or a crashed owner), which is the
    signal to re-earn the idle dwell rather than trusting a stale one.

    Read-modify-write under the module lock, so the last writer wins within a
    process; across processes the staleness window is what keeps exactly one
    driver acting.
    """
    if not title or not owner:
        return None, False
    ts = float(now if now is not None else time.time())
    with _LOCK:
        data = _load()
        if title not in data:
            return None, False
        rec = _normalize(data[title])
        held = rec.get("owner") or ""
        fresh = ts - float(rec.get("owner_at") or 0.0) <= LEASE_STALE_S
        if held and held != owner and fresh:
            return None, False  # another live server is driving this one
        took = held != owner
        rec["owner"] = owner
        rec["owner_at"] = ts
        if took:
            # A new driver must not inherit a dwell it never observed.
            rec["idle_since"] = None
        rec["updated"] = ts
        data[title] = rec
        _save(data)
    return dict(rec), took


def dto(title: str):
    """The compact block a session's ``/api/instances`` row carries, or None.

    Lives here rather than in ``server.py`` because TWO callers need it and one of
    them (``core.pending``, which renders provisioning rows) cannot import the
    server without a cycle. A provisioning row that omitted this showed the
    fast-track toggle OFF for the whole clone window — exactly when you would want
    to change your mind — because intake arms BEFORE the session exists.
    """
    rec = get(title)
    if rec is None:
        return None
    return {
        "depth": rec.get("depth") or "",
        "state": rec.get("state") or "",
        "step": rec.get("step") or "",
        "reason": rec.get("reason") or "",
        "note": rec.get("note") or "",
        "url": rec.get("url") or "",
        "source": rec.get("source") or "session",
        "item": rec.get("item") or "",
        "skipped": list(rec.get("skipped") or []),
    }


def snapshot() -> dict:
    """Every record, normalized (for the DTO / diagnostics)."""
    with _LOCK:
        data = _load()
    return {t: _normalize(e) for t, e in data.items()}


def all_titles() -> List[str]:
    """Titles with a stored record."""
    with _LOCK:
        return list(_load().keys())


def arm(
    title: str,
    depth: str,
    *,
    source: str = "session",
    item: str = "",
    message: str = "",
    base: str = "",
    branch: str = "",
    retryable: Optional[List[str]] = None,
    boot: str = "",
    now: Optional[float] = None,
) -> Optional[dict]:
    """Record a target depth for ``title``, replacing any previous run.

    Returns the stored record, or None when ``depth`` is not a real rung (which
    is how "off" disarms). Safe to call for a title that has no session yet —
    that is exactly what the intake path does, so the target survives a
    provisioning crash or a restart mid-launch.
    """
    if not title:
        return None
    d = normalize_depth(depth)
    if d in ("", "off"):
        disarm(title)
        return None
    ts = float(now if now is not None else time.time())
    rec = _blank()
    rec.update(
        {
            "depth": d,
            "state": "running",
            "step": "",
            "source": (
                source if source in ("session", "tix", "pr", "iss") else "session"
            ),
            "item": str(item or ""),
            "message": str(message or ""),
            "base": str(base or ""),
            "branch": str(branch or ""),
            "retryable": [
                str(h) for h in (retryable or []) if str(h) not in NEVER_SKIP
            ][:16],
            "boot": str(boot or ""),
            "started": ts,
            "step_since": ts,
            "updated": ts,
        }
    )
    with _LOCK:
        data = _load()
        data[title] = rec
        _save(data)
    return dict(rec)


def update(title: str, **fields) -> Optional[dict]:
    """Merge ``fields`` into a record (read-modify-write under the lock)."""
    if not title:
        return None
    with _LOCK:
        data = _load()
        if title not in data:
            return None
        rec = _normalize(data[title])
        rec.update(fields)
        rec = _normalize(rec)
        rec["updated"] = time.time()
        data[title] = rec
        _save(data)
    return dict(rec)


def halt(title: str, reason: str) -> Optional[dict]:
    """Stop a run and record why, so the UI can say what needs a human.

    A silently stopped chain is the one failure mode that destroys trust in the
    feature, so the reason is required and always surfaced.
    """
    return update(title, state="halted", reason=str(reason or "stopped"))


def finish(title: str) -> Optional[dict]:
    """Mark a run complete (its target rung was reached)."""
    return update(title, state="done", reason="")


def disarm(title: str) -> bool:
    """Forget a run entirely. Returns whether there was one."""
    if not title:
        return False
    with _LOCK:
        data = _load()
        if title not in data:
            return False
        data.pop(title, None)
        _save(data)
    return True


def prune(live_titles, now: Optional[float] = None) -> int:
    """Drop records for sessions that no longer exist AND are past the arm grace.

    Mandatory, not housekeeping: titles are REUSED after a delete, so without
    this a recreated session would inherit the previous one's target and attempt
    counters. Finished/halted records are kept (the UI shows them) until their
    session goes away.

    THE GRACE WINDOW IS LOAD-BEARING, not politeness. Every intake entry point
    arms BEFORE its session exists — that is the whole point of keying the store
    by title (a forced start arms, then clones; the ingestion pipeline arms from a
    separate OS process and its session only reaches this engine after a save and
    an adopt). A prune that required the title to be live right now deleted every
    such record within one 5s pass, which silently disabled the entire intake half
    of the feature: you picked "take this to a PR", and nothing ever happened,
    with no halt reason because there was no record left to hold one.

    So a record younger than :data:`ARM_GRACE_S` is never dropped for being
    unknown — a cold clone plus provisioning can legitimately take that long. The
    reuse hazard is covered where it actually arises: the session-delete path
    disarms explicitly.
    """
    live = set(live_titles or [])
    ts = float(now if now is not None else time.time())
    with _LOCK:
        data = _load()
        dead = []
        for t, raw in data.items():
            if t in live:
                continue
            started = 0.0
            if isinstance(raw, dict):
                try:
                    started = float(raw.get("started") or 0.0)
                except (TypeError, ValueError):
                    started = 0.0
            if ts - started > ARM_GRACE_S:
                dead.append(t)
        if not dead:
            return 0
        for t in dead:
            data.pop(t, None)
        _save(data)
    return len(dead)
