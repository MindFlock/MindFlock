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
#: Consecutive-idle dwell before the FIRST commit of a chain. Longer than the
#: prompt queue's 12s settle on purpose: feeding a prompt to an agent that turns
#: out to be mid-thought is recoverable, committing and opening a PR for it is
#: not. Costs ~18s more latency and removes the whole "committed mid-thought"
#: class of failure.
IDLE_SETTLE_S = 30.0


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
        "source": "session",
        "item": "",
        "message": "",
        "base": "",
        "branch": "",
        "retryable": [],
        "attempts": {},
        "skipped": [],
        "commits": 0,
        "idle_since": None,
        "step_since": 0.0,
        "acted_at": 0.0,
        "boot": "",
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
    src = str(entry.get("source", "") or "session")
    e["source"] = src if src in ("session", "tix", "pr", "iss") else "session"
    e["item"] = str(entry.get("item", "") or "")
    e["message"] = str(entry.get("message", "") or "")
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
    sk = entry.get("skipped")
    e["skipped"] = [str(h) for h in sk][:16] if isinstance(sk, list) else []
    for key in ("commits",):
        try:
            e[key] = max(0, int(entry.get(key, 0) or 0))
        except (TypeError, ValueError):
            e[key] = 0
    idle = entry.get("idle_since")
    e["idle_since"] = float(idle) if isinstance(idle, (int, float)) else None
    for key in ("step_since", "acted_at", "started", "updated"):
        try:
            e[key] = float(entry.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            e[key] = 0.0
    e["boot"] = str(entry.get("boot", "") or "")
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
    if stage == "interrupt":
        return _interrupt_action(rec, snap)

    if stage == "provisioning":
        return "wait", {"reason": "workspace is still being provisioned"}

    if stage == "agent":
        return _agent_action(rec, snap, depth)

    if stage == "committed":
        if depth == "commit":  # defensive: reaches() already covered this
            return "done", {}
        if snap.get("has_origin") is False:
            return "stop", {"reason": "no origin remote — add one to push"}
        check = snap.get("check") or {}
        state = str(check.get("state") or "")
        if state == "failed":
            return "stop", {"reason": "checks failed — fix them and re-run"}
        if state == "running":
            return "wait", {"reason": "waiting for checks to finish"}
        return "push", {}

    if stage == "pushed":
        return "make_pr", {"base": rec.get("base") or ""}

    if stage == "pr":
        if depth != "merge":
            return "done", {}
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
    """
    if depth == "agent":
        # Target is "let the agent work": reaching idle IS completion.
        pass
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
        if int(snap.get("beyond_base") or 0) > 0:
            # Committed already but the stage still reads "agent": nothing to
            # commit, let the next pass see the real stage.
            return "wait", {"reason": "nothing uncommitted"}
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


def prune(live_titles) -> int:
    """Drop records for sessions that no longer exist.

    Mandatory, not housekeeping: titles are REUSED after a delete, so without
    this a recreated session would inherit the previous one's target and attempt
    counters. Finished/halted records are kept (the UI shows them) until their
    session goes away.
    """
    live = set(live_titles or [])
    with _LOCK:
        data = _load()
        dead = [t for t in data if t not in live]
        if not dead:
            return 0
        for t in dead:
            data.pop(t, None)
        _save(data)
    return len(dead)
