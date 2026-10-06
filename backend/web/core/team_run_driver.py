"""The impure half of team runs: observe, act, announce — and the operations
the routes perform.

:mod:`backend.web.core.team_runs` holds the record and the pure planner. This
module is the loop that gives it effect, structured like the autopilot driver
in ``server.py``: a 5s pass (plus a wake-up when a route changed something),
one lease per run so two servers never drive the same one, every step wrapped
so one bad run cannot stop the pass.

* :func:`observe` — one snapshot of everything a decision needs: the engine's
  instances (presence and incarnation), the published ``/api/instances`` rows
  (activity, stage, cost — never a probe from here), the autopilot store, the
  prompt queues, the usage-limit state and the bus facts (a member the user
  deleted, a turn that ended).
* the side effects — starting a task (``ticket_start.launch`` /
  ``session_create.create_result``, then the lane armed through
  ``lanes.arm_session``), re-arming, holding, nudging through the prompt
  queue (whose never-type-into-a-dialog guard applies). Nothing here commits,
  pushes or opens a PR: the autopilot does, ONE driver per session.
* :func:`_announce` — the ONE emitter for ``run.*`` events, so every channel
  (ntfy, desktop, toasts, shell hooks, the bell) is fed at once and nothing is
  filtered per channel. Each escalation is announced once per
  ``(run, task, reason, incarnation)`` (the keys persist across restarts); a
  dialog is never re-announced (``needs_input`` already fired for it); the
  boot reconcile runs silent, and nothing is said inside the boot quiet window.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend.web.core import autopilot as _autopilot
from backend.web.core import events as _events
from backend.web.core import git_merge as _git_merge
from backend.web.core import lanes as _lanes
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import team_runs as _runs

__all__ = [
    "subscribe",
    "wake",
    "observe",
    "step_run",
    "run_pass",
    "reconcile",
    "run_loop",
    "RunError",
]


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


class RunError(Exception):
    """A run operation refused: ``status`` + the sentence the route says."""

    def __init__(self, message: str, status: int = 400, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- #
# Bus taps: facts the snapshot cannot tell us
# --------------------------------------------------------------------------- #
_EV_LOCK = threading.Lock()
#: title -> epoch the user removed it (DELETE, /close).
_DELETED: Dict[str, float] = {}
#: title -> epoch of its last ``session.turn_ended`` (the notification-grade
#: "the agent finished": observed work, a dwell, nothing queued).
_TURN_ENDED: Dict[str, float] = {}
_UNSUB = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WAKE: Optional[asyncio.Event] = None
_TAP_KEEP_S = 86400.0


def _on_event(env: dict) -> None:
    try:
        ev = env.get("event")
        title = str(env.get("session") or "")
        if not title:
            return
        if ev == "session.deleted":
            with _EV_LOCK:
                _DELETED[title] = float(env.get("ts") or time.time())
        elif ev == "session.turn_ended":
            with _EV_LOCK:
                _TURN_ENDED[title] = float(env.get("ts") or time.time())
    except Exception:  # noqa: BLE001 — a subscriber never breaks an emit
        pass


def subscribe() -> None:
    """Tap the bus (idempotent). The lifespan calls it before the loop."""
    global _UNSUB
    if _UNSUB is None:
        _UNSUB = _events.BUS.subscribe(_on_event)


def _prune_taps(now: float) -> None:
    with _EV_LOCK:
        for d in (_DELETED, _TURN_ENDED):
            for t in [t for t, ts in d.items() if now - ts > _TAP_KEEP_S]:
                d.pop(t, None)


def wake() -> None:
    """Ask the loop for a pass now (a route changed a run). Thread-safe; a
    no-op when the loop is not running (tests, a CLI)."""
    loop, ev = _LOOP, _WAKE
    if loop is None or ev is None:
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:  # loop closed
        pass


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
def _progress_of(row: dict) -> str:
    """What "the agent did something" means here: the diff it has produced
    (committed or not), the stage, the PR, its newest report."""
    ds = row.get("diff_stat") if isinstance(row.get("diff_stat"), dict) else {}
    rep = row.get("last_report") if isinstance(row.get("last_report"), dict) else {}
    if not row:
        return ""
    return "%s/%s/%s|%s|%s|%s" % (
        ds.get("files", ""),
        ds.get("additions", ""),
        ds.get("deletions", ""),
        row.get("stage") or "",
        row.get("pr_url") or "",
        rep.get("ts") or "",
    )


def _branch_exists(repo: str, branch: str) -> bool:
    if not repo or not branch or not os.path.isdir(repo):
        return False
    try:
        cp = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/heads/" + branch,
            ],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def _provider(program: str) -> str:
    try:
        from backend import providers

        return providers.resolve(program or "").name
    except Exception:  # noqa: BLE001
        return ""


def _program(run: dict) -> str:
    return run["program"] or _server().ENGINE.default_program()


def observe(run: dict, now: float, boot: bool = False) -> dict:
    """The observation :func:`team_runs.plan_actions` decides from. Blocking
    (file reads, at most a ``git rev-parse`` per vanished task) — call it in
    a worker thread."""
    srv = _server()
    rows = {}
    for r in _events.sessions_snapshot():
        if isinstance(r, dict) and r.get("title"):
            rows[r["title"]] = r
    try:
        pending = {r.get("title") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pending = set()
    instances = dict(srv.ENGINE.instances)
    records = _autopilot.snapshot()
    queues = _prompt_queue.snapshot()
    with srv._CREATE_FAILURES_LOCK:
        failures = {t: dict(v) for t, v in srv._CREATE_FAILURES.items()}
    with _EV_LOCK:
        deleted = dict(_DELETED)
        ended = dict(_TURN_ENDED)
    provider = _provider(_program(run))
    tasks: Dict[str, dict] = {}
    limited: set = set()
    cost = 0.0
    for t in run["tasks"]:
        if t["state"] in _runs.TERMINAL or t["state"] == "queued" or not t["title"]:
            cost += float(t.get("cost_usd") or 0.0)
            continue
        title = t["title"]
        inst = instances.get(title)
        row = rows.get(title) or {}
        o: dict = {
            "present": inst is not None,
            "created_at": srv._created_epoch(inst) if inst is not None else None,
            "pending": title in pending,
            "status": str(row.get("status") or ""),
            "activity": str(row.get("activity") or ""),
            "activity_since": float(row.get("activity_since") or 0.0),
            "stage": str(row.get("stage") or ""),
            "pr_url": str(row.get("pr_url") or ""),
            "branch": str(row.get("branch") or getattr(inst, "Branch", "") or ""),
            "autopilot": records.get(title),
            "report": row.get("last_report"),
            "cost": float(row.get("tokens_cost") or t.get("cost_usd") or 0.0),
            "progress": _progress_of(row),
            "queue_ids": [
                it.get("id") for it in (queues.get(title) or {}).get("items") or []
            ],
            "provider": provider,
            "deleted_at": deleted.get(title, 0.0),
            "turn_ended_at": ended.get(title, 0.0),
        }
        if inst is not None:
            try:
                o["worked"] = srv._agent_state.worked_at(title) is not None
            except Exception:  # noqa: BLE001
                o["worked"] = False
            if t["base_sha"] and _runs.is_together(run) and not _runs.same_folder(run):
                # Its own commits beyond the commit it was cut from, and a
                # clean tree: "done" needs no event (see the planner).
                try:
                    mwt = inst.GetWorktreePath() or ""
                except Exception:  # noqa: BLE001
                    mwt = ""
                if mwt:
                    o["beyond_base"] = len(
                        _git_merge.commit_subjects(mwt, t["base_sha"], "HEAD")
                    )
                    dirty = _git_merge.tracked_dirty(mwt)
                    o["clean"] = None if dirty is None else not dirty
            try:
                o["limited"] = o["activity"] == "limit" or (
                    srv._session_limited_until(title) > now
                )
            except Exception:  # noqa: BLE001
                o["limited"] = o["activity"] == "limit"
            if o["limited"]:
                limited.add(provider)
        fail = failures.get(title)
        if fail:
            o["create_failed"] = str(fail.get("error") or "create failed")
            o["create_failed_at"] = float(fail.get("ts") or 0.0)
        if inst is None and title not in pending:
            stale = now - float(t["started_at"] or now) >= _runs.START_GRACE_S
            if boot or stale or t["state"] != "starting":
                o["branch_exists"] = _branch_exists(_task_repo(run, t), t["branch"])
        cost += o["cost"]
        tasks[t["id"]] = o
    out = {
        "boot": boot,
        "limited_providers": sorted(limited),
        "provider": provider,
        "cost": round(cost, 4),
        "tasks": tasks,
    }
    if _runs.is_together(run) and run.get("lead"):
        lo = _observe_lead(run, now, rows, pending, instances, records, failures)
        out["lead"] = lo
        if _runs.same_folder(run):
            if lo.get("wt") and lo.get("ready"):
                out["sf"] = _observe_sf(run, lo["wt"], tasks)
                for t in run["tasks"]:
                    if t["state"] == "integrating" and t["id"] in tasks:
                        tasks[t["id"]].update(_observe_sf_report(run, t))
            return out
        for t in run["tasks"]:
            if t["state"] == "integrating" and t["id"] in tasks:
                tasks[t["id"]].update(_observe_merge(run, t, lo))
    return out


def _observe_sf_report(run: dict, t: dict) -> dict:
    """A same-folder piece's full report (for its commit body and the PR)."""
    try:
        from backend.web.core import mailbox as _mailbox

        msg = _mailbox.last_result(
            run["lead"]["title"], t["title"], since=float(t["started_at"] or 0.0) - 5.0
        )
        if msg:
            return {"report_text": str(msg.get("text") or "")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _observe_lead(run, now, rows, pending, instances, records, failures) -> dict:
    """The lead as the merge queue, the check and the release need it: there
    (and ours), idle, its tree clean, no merge half-done, its HEAD."""
    srv = _server()
    lead = run["lead"]
    title = lead["title"]
    inst = instances.get(title)
    row = rows.get(title) or {}
    rec = records.get(title)
    with _EV_LOCK:
        deleted = _DELETED.get(title, 0.0)
        ended = _TURN_ENDED.get(title, 0.0)
    lo: dict = {
        "present": inst is not None,
        "created_at": srv._created_epoch(inst) if inst is not None else None,
        "pending": title in pending,
        "activity": str(row.get("activity") or ""),
        "activity_since": float(row.get("activity_since") or 0.0),
        "branch": str(row.get("branch") or getattr(inst, "Branch", "") or ""),
        "autopilot": rec,
        "shipping": bool(rec and rec.get("state") == "running"),
        "deleted_at": deleted,
        "turn_ended_at": ended,
        "mcp_attached": row.get("mcp_attached"),
        "ready": False,
    }
    fail = failures.get(title)
    if fail:
        lo["create_failed"] = str(fail.get("error") or "create failed")
        lo["create_failed_at"] = float(fail.get("ts") or 0.0)
    if inst is None:
        return lo
    inc = float(lead.get("incarnation") or 0.0)
    created = lo["created_at"]
    mine = not inc or created is None or abs(float(created) - inc) <= 1.0
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    lo["wt"] = wt
    if not wt or not mine:
        return lo
    # A merge THIS server started and never finished (it died between the
    # conflict and the --abort) is unwound before anything reads the tree —
    # otherwise its MERGE_HEAD wedges the queue for good.
    _git_merge.recover_interrupted(wt)
    head = _git_merge.rev_parse(wt, "HEAD")
    lo["head"] = head
    _tidy_artifacts(wt)
    # A shared folder (same-folder split): an untracked file MindFlock's diff
    # view marked intent-to-add is nobody's change to wait on.
    dirty = _git_merge.tracked_dirty(wt, _runs.same_folder(run))
    lo["clean"] = None if dirty is None else not dirty
    lo["merging"] = _git_merge.merge_in_progress(wt)
    lo["operation"] = _git_merge.operation_in_progress(wt)
    lo["ready"] = bool(head)
    live = _git_merge.current_branch(wt)
    lo["live_branch"] = live
    if live:
        lo["branch"] = live
    # The group's branch is pinned (lead["branch"]): merges go into it and the
    # release ships it — a lead on any other branch, or detached, is blocked.
    pinned = lead.get("branch") or ""
    lo["on_branch"] = not pinned or live == pinned
    if run["state"] == "checking":
        wts = srv._wt_setup
        try:
            lo["check"] = wts.check_summary(wt)
            lo["check_running"] = wts.is_running(wt, "check")
            st = (lo["check"] or {}).get("state")
            if st in ("ok", "failed"):
                log = wts.log_tail(wt, wts.CHECK_LOG, 80)
                if st == "ok":
                    lo["check_tests"] = _runs.tests_passed(log)
                else:
                    tail = [ln for ln in log.splitlines() if ln.strip()][-6:]
                    lo["check_tail"] = " / ".join(tail)[-400:]
        except Exception:  # noqa: BLE001 — no check info reads as "not yet"
            lo["check"] = None
    return lo


def _observe_merge(run: dict, t: dict, lo: dict) -> dict:
    """An integrating member: its branch head, whether the lead already holds
    it (ancestry — the only proof the server takes), its own commits, and its
    worker's full report (for the PR body)."""
    wt = lo.get("wt") or ""
    out: dict = {}
    if not wt or not t["branch"]:
        return out
    head = _git_merge.rev_parse(wt, t["branch"]) or t["head_sha"]
    out["head"] = head
    if head and lo.get("ready") and lo.get("on_branch", True):
        # Ancestry of the GROUP's branch — never of whatever HEAD the lead
        # happens to have checked out (a detached HEAD would "verify" merges
        # that the release then ships without).
        pinned = (run.get("lead") or {}).get("branch") or "HEAD"
        out["merged"] = _git_merge.is_ancestor(wt, head, pinned) is True
    if head and t["base_sha"]:
        out["commits"] = _git_merge.commit_subjects(wt, t["base_sha"], head)
    try:
        from backend.web.core import mailbox as _mailbox

        msg = _mailbox.last_result(
            run["lead"]["title"], t["title"], since=float(t["started_at"] or 0.0) - 5.0
        )
        if msg:
            out["report_text"] = str(msg.get("text") or "")
    except Exception:  # noqa: BLE001 — the body falls back to the task line
        pass
    return out


# --------------------------------------------------------------------------- #
# Side effects
# --------------------------------------------------------------------------- #
def _human_message(rec: Optional[dict]) -> str:
    """The commit message a PERSON wrote on an autopilot record, or ``""`` —
    not a placeholder (``message_auto``), not a model's (``message_written``)."""
    rec = rec or {}
    msg = str(rec.get("message") or "").strip()
    if not msg or rec.get("message_auto") or rec.get("message_written"):
        return ""
    return msg


def _arm(run: dict, t: dict) -> None:
    """Arm (or re-arm) the task's lane — the autopilot's one record for it.
    Never for a same-folder piece: the autopilot commits EVERYTHING dirty in
    its folder, which is every piece's work — MindFlock commits each piece's
    own paths itself (:func:`_sf_commit`)."""
    if _runs.same_folder(run) and t["kind"] == "piece":
        return
    lane = _runs.task_lane(run, t)
    # A message a PERSON wrote (the Outbox approval's edited message, armed by
    # ship-now as message_auto=False) survives a re-arm — after a failed
    # commit's fix, a resume (the pause stashed it on the task), a retry.
    # Otherwise the task line is the placeholder, replaced at commit time by
    # one written from the diff.
    prev = _autopilot.get(t["title"]) if t["title"] else None
    human = _human_message(prev) or str(t.get("message") or "").strip()
    if human:
        message, auto = human, False
    else:
        message = (t["text"] or "").splitlines()[0][:200] if t["text"] else ""
        auto = True
    _lanes.arm_session(
        t["title"],
        lane,
        # A person's per-member choice (lane and "ask first", recorded on the
        # task by the /lane route) wins over the group's; a one-for-all
        # member's commit is internal to the group's branch — "ask me first"
        # applies to the one release, not to each commit.
        ask_first=_runs.task_ask_first(run, t),
        source="tix" if t["kind"] == "ticket" else "run",
        item=t["ticket_id"] or t["title"],
        message=message,
        message_auto=auto,
        require_workspace=False,
    )
    t["message"] = ""


#: Task states whose member is still the group's to ship: a resume re-arms
#: only these (a member already merging back, or merged, is never re-armed —
#: its lane would commit again on top of the integration).
_REARMABLE = frozenset({"starting", "working", "shipping", "needs_you"})


def _hold(run: dict) -> List[str]:
    """Hold every non-terminal member's autopilot (pause): disarm it and mark
    the task ``held`` so a resume re-arms exactly those. Agents are not
    interrupted — nothing new ships, that is all. A message a person wrote is
    kept on the task for the resume. A RELEASING lead is held too: its lane is
    disarmed and the group goes back to "ready to release" (release it again
    after the resume). Returns the held titles."""
    held = []
    for t in run["tasks"]:
        if t["state"] in _runs.TERMINAL or t["state"] == "queued" or not t["title"]:
            continue
        rec = _autopilot.get(t["title"])
        if rec is not None:
            human = _human_message(rec)
            if human:
                t["message"] = human
            _autopilot.disarm(t["title"])
        t["held"] = True
        held.append(t["title"])
    lead = (run.get("lead") or {}).get("title") or ""
    if run["state"] == "releasing" and lead:
        _autopilot.disarm(lead)
        now = time.time()
        _runs.apply(
            run,
            {
                "op": "run",
                "state": "release_ready",
                "release": {
                    "state": "ready",
                    "detail": "paused — release it again once the group is resumed",
                },
            },
            now,
        )
        held.append(lead)
    return held


def _unhold(run: dict) -> List[str]:
    rearmed = []
    for t in run["tasks"]:
        if not t.get("held"):
            continue
        t["held"] = False
        if t["state"] not in _REARMABLE or not t["title"]:
            continue
        if t["state"] == "needs_you" and t["reason"] in ("blocked", "restart"):
            continue
        if (
            _runs.is_together(run)
            and t["state"] == "needs_you"
            and t["reason"] in ("conflict",)
        ):
            continue
        try:
            _arm(run, t)
            rearmed.append(t["title"])
        except Exception:  # noqa: BLE001 — a lane that cannot arm stays held
            t["held"] = True
    return rearmed


def _taken_titles(repo: str = "") -> set:
    """Titles a new session may not take: live sessions, pending creates,
    every active run's members — and, with ``repo``, every title whose
    session BRANCH already exists there (a closed session keeps its branch,
    and a new session on that title would silently start on its old commits
    and ship them)."""
    srv = _server()
    taken = set(srv.ENGINE.instances)
    try:
        taken |= {r.get("title") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pass
    for run in _runs.list_runs(include_finished=False):
        taken |= {t["title"] for t in run["tasks"] if t["title"]}
    if repo:
        taken |= _branch_titles(repo)
    return taken


def _branch_titles(repo: str) -> set:
    """The session titles whose branch (``<branch prefix><title>``) exists in
    ``repo`` — one ``git for-each-ref``."""
    if not repo or not os.path.isdir(repo):
        return set()
    try:
        from backend.config import config as _config

        prefix = _config.LoadConfig().branch_prefix or ""
    except Exception:  # noqa: BLE001
        prefix = ""
    try:
        cp = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if cp.returncode != 0:
        return set()
    out = set()
    for line in (cp.stdout or b"").decode("utf-8", "replace").splitlines():
        b = line.strip()
        if b and b.startswith(prefix):
            out.add(b[len(prefix) :])
    return out


# --------------------------------------------------------------------------- #
# The ingestion ledger: a run's tickets are RESERVED (tagged "run:<id>") while
# queued, handed back on every path that drops a queued ticket, and marked
# started once a session exists — never someone else's marker, never history.
# --------------------------------------------------------------------------- #
def _ticket_slug(t: dict) -> str:
    """A ticket task's ledger key: its FIRST title (a fresh retry's
    ``<slug>-2`` is a session title, never a ticket)."""
    if t["kind"] != "ticket":
        return ""
    return t["base_title"] or t["title"]


def _holder(run: dict) -> str:
    return "run:" + run["id"]


def _reserve(run: dict, t: dict) -> bool:
    """Reserve ``t``'s ticket for this run (``t["ledger"]="reserved"``).
    False when it is in flight for someone else (the pipeline started it)."""
    slug = _ticket_slug(t)
    if not slug:
        return True
    try:
        ok = _server()._ticket_start.reserve(slug, _holder(run))
    except Exception:  # noqa: BLE001 — the ledger is best-effort
        return True
    if ok:
        t["ledger"] = "reserved"
    return ok


def _settle_ledger(run: dict) -> None:
    """Hand back every reservation this run holds for a ticket task that
    ended without ever starting (cancelled, skipped, failed to create, the
    group aborted)."""
    for t in run["tasks"]:
        if t.get("ledger") != "reserved" or t["state"] not in _runs.TERMINAL:
            continue
        slug = _ticket_slug(t)
        try:
            _server()._ticket_start.release_reservation(slug, _holder(run))
        except Exception:  # noqa: BLE001
            continue
        t["ledger"] = ""


async def _start(run_id: str, task_id: str, now: float) -> None:
    """Start one queued task: create its session, then arm its lane."""
    srv = _server()
    run = await asyncio.to_thread(_runs.load, run_id)
    if run is None:
        return
    t = _runs.task_by_id(run, task_id)
    if t is None or t["state"] != "queued":
        return
    title = t["title"]
    outcome: Tuple[str, dict] = ("failed", {"error": "not started"})
    started = time.time()
    base_sha = ""
    if _runs.is_together(run):
        outcome, base_sha = await _start_member(run, t)
    elif t["kind"] == "ticket":
        try:
            launched = await srv._ticket_start.launch(
                t["source"],
                t["ticket_id"],
                title=title or None,
                branch=t["branch"] or None,
                depth="off",  # the run arms its own lane, below
                agent=run["program"],
                extra_prompt=_runs.run_brief(run),
                run_id=run["id"],
            )
            outcome = ("started", {"title": launched.get("title")})
        except srv._ticket_start.LaunchError as err:
            inst = srv.ENGINE.instances.get(title) if title else None
            if err.status == 409 and inst is not None and _is_ours(t, inst):
                outcome = ("adopt", {})
            elif err.status == 409:
                # Someone ELSE is starting (or started) this ticket — the
                # pipeline, an Intake click. Never arm their session with this
                # group's lane: leave it out, with its own lane.
                outcome = ("elsewhere", {})
            else:
                outcome = ("failed", {"error": str(err.body.get("error") or err)})
        except Exception as err:  # noqa: BLE001
            outcome = ("failed", {"error": str(err)})
    else:
        prompt = (
            (t["text"] or "").strip()
            + "\n\n"
            + _runs.run_brief(run, _provider(_program(run)))
        )
        payload = {
            "title": title,
            "program": _program(run),
            "repo_path": t["repo_root"] or run["repo_root"],
            "prompt": prompt,
        }
        try:
            status, body = await srv._session_create.create_result(payload)
        except Exception as err:  # noqa: BLE001
            status, body = 500, {"error": str(err)}
        if status == 202:
            outcome = ("started", {"created_at": body.get("created_at")})
        elif status == 409 and "already exists" in str(body.get("error") or ""):
            outcome = ("rename", {})
        else:
            outcome = ("failed", {"error": str(body.get("error") or status)})

    kind, info = outcome

    def _record() -> None:
        with _runs.edit(run_id) as r:
            if r is None:
                return
            tk = _runs.task_by_id(r, task_id)
            if tk is None:
                return
            if tk["state"] != "queued":
                # Removed (skip / cancel) while its session was being created:
                # the session exists now, and nothing arms or drives it — say
                # so instead of leaving a session nobody knows about.
                if kind in ("started", "adopt") and tk["state"] in (
                    "cancelled",
                    "skipped",
                ):
                    _runs.log_event(
                        r,
                        now,
                        "note",
                        tk["id"],
                        "its session %s started as it was removed — kept, "
                        "and nothing ships it" % (info.get("title") or tk["title"]),
                    )
                return
            if kind == "elsewhere":
                _runs.apply(
                    r,
                    _runs._to(
                        tk,
                        "skipped",
                        "",
                        "started elsewhere meanwhile (Intake or ticket ingestion) — "
                        "left out of the group; it keeps its own lane",
                        finished_at=now,
                    ),
                    now,
                )
                _settle_ledger(r)
                return
            if kind == "rename":
                tk["title"] = _runs.task_title(
                    tk["text"],
                    _taken_titles("" if tk["kind"] == "ticket" else _task_repo(r, tk)),
                )
                _runs.log_event(r, now, "note", tk["id"], "renamed to " + tk["title"])
                return
            if kind == "failed":
                act = _runs._create_failure(tk, now, info.get("error") or "")
                _runs.apply(r, act, now)
                _settle_ledger(r)
                return
            # The launch (or the member start) turned the reservation into
            # its own in-flight marker: it is no longer the run's to hand back.
            tk["ledger"] = ""
            tk["started_at"] = started
            tk["start_now"] = False
            if base_sha:
                tk["base_sha"] = base_sha
            if info.get("title") and not tk["title"]:
                tk["title"] = str(info["title"])
            if kind == "adopt":
                inst = srv.ENGINE.instances.get(tk["title"])
                tk["incarnation"] = float(srv._created_epoch(inst) or 0.0)
                tk["adopted"] = True
                _runs.apply(
                    r,
                    _runs._to(tk, "working"),
                    now,
                )
                _runs.log_event(r, now, "adopted", tk["id"], "already running — added")
            else:
                if info.get("created_at"):
                    tk["incarnation"] = float(info["created_at"])
                _runs.apply(r, _runs._to(tk, "starting"), now)
            if r["paused"] or r["state"] in _runs.RUN_FINISHED:
                # Paused (or cancelled) while it was being created: its lane is
                # armed on resume, never now.
                tk["held"] = True
                return
            try:
                _arm(r, tk)
            except Exception as err:  # noqa: BLE001 — the session still runs
                _runs.log_event(r, now, "note", tk["id"], "could not arm: %s" % err)

    await asyncio.to_thread(_record)


async def _start_member(run: dict, t: dict) -> Tuple[Tuple[str, dict], str]:
    """Start one member of a one-for-all group (a line, a ticket, a piece) as
    a CHILD of the lead: a plain worktree of the lead's repository, cut from
    the lead's current commit, its diff measured against the lead's branch.
    Returns ``(outcome, base_sha)``."""
    srv = _server()
    lead = run["lead"] or {}
    inst = srv.ENGINE.instances.get(lead.get("title") or "")
    if inst is None:
        return ("failed", {"error": "the group's lead session is not there"}), ""
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    head = await asyncio.to_thread(_git_merge.rev_parse, wt, "HEAD") if wt else ""
    if not head:
        return ("failed", {"error": "the lead's worktree is not ready"}), ""
    # The group's pinned branch: a member forks from (and is measured
    # against) it, never from whatever the lead has checked out right now.
    live = await asyncio.to_thread(_git_merge.current_branch, wt)
    branch = lead.get("branch") or live
    if lead.get("branch") and live != lead["branch"]:
        return (
            "failed",
            {
                "error": "the lead is on %s, not the group's branch %s"
                % (live or "a detached HEAD", lead["branch"])
            },
        ), ""
    provider = _provider(_program(run))
    story = None
    if t["kind"] == "ticket":
        try:
            story = await asyncio.wait_for(
                srv._ticket_start.find_ticket(t["source"], t["ticket_id"]),
                _FIND_TIMEOUT_S,
            )
            prompt = srv._ticket_start.build_prompt(story)
        except Exception as err:  # noqa: BLE001
            return ("failed", {"error": "could not fetch the ticket: %s" % err}), ""
        prompt += "\n\n" + _runs.run_brief(run, provider)
    elif t["kind"] == "piece":
        prompt = (
            (t["text"] or "").strip()
            + "\n\n"
            + _runs.piece_brief(run, t["paths"], provider)
        )
    else:
        prompt = (t["text"] or "").strip() + "\n\n" + _runs.run_brief(run, provider)
    if _runs.same_folder(run):
        # Same folder: an extra agent IN the lead's folder (in place, like a
        # copy window) — no worktree, no branch of its own, nothing to merge.
        payload = {
            "title": t["title"],
            "program": _program(run),
            "repo_path": wt,
            "in_place": True,
            "prompt": prompt,
            "parent": lead["title"],
            "spawned": True,
        }
        try:
            status, body = await srv._session_create.create_result(payload)
        except Exception as err:  # noqa: BLE001
            status, body = 500, {"error": str(err)}
        if status == 202:
            return ("started", {"created_at": body.get("created_at")}), head
        if status == 409 and "already exists" in str(body.get("error") or ""):
            return ("rename", {}), ""
        return ("failed", {"error": str(body.get("error") or status)}), ""
    # A plain worktree of the repository HOLDING the lead's worktree — the
    # lead's ``Path`` is not it for a provisioned (ticket) lead, whose Path is
    # the server's cwd: there the lead's HEAD names no commit at all.
    repo = await asyncio.to_thread(_git_merge.repo_of, wt)
    payload = {
        "title": t["title"],
        "program": _program(run),
        "repo_path": repo or run["repo_root"] or getattr(inst, "Path", "") or "",
        "prompt": prompt,
        "parent": lead["title"],
        "spawned": True,
        "base_ref": head,
        "base_branch": branch,
    }
    try:
        status, body = await srv._session_create.create_result(payload)
    except Exception as err:  # noqa: BLE001
        status, body = 500, {"error": str(err)}
    if status == 202:
        if story is not None:
            # The same two ledger steps a ticket launch takes: in flight (the
            # run's reservation becomes it, in place), then "completed" — the
            # session exists, so ingestion never starts it again.
            try:
                await asyncio.to_thread(srv._ticket_start.record_started, story)
                await asyncio.to_thread(
                    lambda: srv._ticket_start.record_result(
                        story, branch=str(body.get("branch") or "") or None
                    )
                )
            except Exception:  # noqa: BLE001 — the ledger is best-effort
                pass
        return ("started", {"created_at": body.get("created_at")}), head
    if status == 409 and "already exists" in str(body.get("error") or ""):
        return ("rename", {}), ""
    return ("failed", {"error": str(body.get("error") or status)}), ""


def _red_res(wt: str) -> List[str]:
    """The compiled RED zones that apply to ``wt`` (repo + worktree scope)."""
    srv = _server()
    try:
        repo_id, _repo = srv._rz_repo(wt)
        zones = srv._red_zones.effective_zones(wt, repo_id)
    except Exception:  # noqa: BLE001 — no zones readable: none enforced here
        return []
    out = []
    for z in zones:
        if z.get("kind") == "green" or z.get("waived"):
            continue
        try:
            out.append(srv._red_zones.compile_pattern(str(z.get("pattern") or "")))
        except (ValueError, TypeError):
            continue
    return out


def _tidy_artifacts(wt: str) -> None:
    """Keep MindFlock's own scratch files (a check's status file, a launch
    script) out of the lead's index before a merge: an intent-to-add entry
    alone makes git refuse every merge ("not uptodate")."""
    try:
        from backend import workspace_setup as _ws

        _ws.exclude_artifacts(wt)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def _lead_wt(run: dict) -> Tuple[object, str]:
    """``(instance, worktree path)`` of the run's lead (``(None, "")``)."""
    srv = _server()
    lead = run.get("lead") or {}
    inst = srv.ENGINE.instances.get(lead.get("title") or "")
    if inst is None:
        return None, ""
    try:
        return inst, inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        return inst, ""


def _lead_repo(run: dict) -> str:
    """The repository a one-for-all group's members live in: the one holding
    the LEAD's worktree (:func:`git_merge.repo_of`) — they fork from its
    commit and merge back into its branch, so their branches must be in the
    same object store. ``run["repo_root"]`` only when the lead is not there
    (a group recorded before this was derived stored the lead's ``Path``,
    which for a ticket session is the server's cwd)."""
    _inst, wt = _lead_wt(run)
    return (_git_merge.repo_of(wt) if wt else "") or run["repo_root"]


def _task_repo(run: dict, t: dict) -> str:
    """Where ``t``'s session branch lives (for branch-taken probes)."""
    if _runs.is_together(run) and run.get("lead"):
        return _lead_repo(run)
    return t["repo_root"] or run["repo_root"]


async def _message_lead(run: dict, text: str) -> bool:
    """A mailbox message to the lead, from MindFlock — typed in when it is
    next idle (never into a dialog), and shown in its Thread."""
    srv = _server()
    title = (run.get("lead") or {}).get("title") or ""
    if not title or title not in srv.ENGINE.instances:
        return False
    try:
        resp = await srv.post_message(
            title, {"text": text[:7000], "from": "", "delivery": "auto"}
        )
    except Exception as err:  # noqa: BLE001
        _log_info("team_run %s: message to the lead failed: %s", run["id"], err)
        return False
    return getattr(resp, "status_code", 500) < 400


async def _fence(run_id: str, task_id: str, now: float) -> None:
    """Fence a piece to its paths: worktree green zones ("edit only here"),
    added through the very route the Map uses (the guard is synced before it
    returns). A worktree that is not ready yet is retried next pass."""
    srv = _server()
    run = await asyncio.to_thread(_runs.load, run_id)
    t = _runs.task_by_id(run, task_id) if run else None
    if t is None or t["fenced"] or not t["title"]:
        return
    added, problems = [], []
    if _runs.same_folder(run):
        # A folder several agents share: the worktree's green zones would
        # fence them ALL to the union of their paths. Each piece gets its
        # OWN fence instead (keyed by its tmux session, which the guard hook
        # resolves from its own pane at fire time), plus "MindFlock commits
        # for you" (git add / commit / stash / … refused).
        _inst, wt = _lead_wt(run)
        if not wt:
            return
        added, problems = await asyncio.to_thread(_sf_fence, run, t, wt)
    for pattern in [] if _runs.same_folder(run) else t["paths"]:
        resp = await srv.instance_red_zones_add(
            t["title"],
            {
                "pattern": pattern,
                "kind": "green",
                "name": "only here (%s)" % run["name"][:40],
                "note": "MindFlock split: this piece may change only these paths",
                # The fence lands just after the worker starts: anything it
                # already changed outside its paths is a breach, never quietly
                # exempted (the default for a person's own zone).
                "exempt": False,
            },
        )
        code = getattr(resp, "status_code", 500)
        body = bytes(getattr(resp, "body", b"") or b"")
        if code == 409 and b"workspace not ready" in body:
            return  # not yet — next pass
        if code >= 400:
            problems.append(
                "%s (%s)" % (pattern, body[:120].decode("utf-8", "replace"))
            )
        else:
            added.append(pattern)

    def _store() -> None:
        with _runs.edit(run_id) as r:
            tk = _runs.task_by_id(r, task_id) if r else None
            if tk is None:
                return
            tk["fenced"] = True
            text = "only here: " + (", ".join(added) or "—")
            if problems:
                text += "; not fenced: " + "; ".join(problems)
            _runs.log_event(r, now, "fenced", tk["id"], text)
            if not added and tk["state"] not in _runs.TERMINAL:
                # Not one path could be fenced: the piece would run unfenced —
                # free to edit what other pieces own. Stop it shipping and say so.
                _disarm(tk["title"])
                _runs.apply(
                    r,
                    _runs._to(
                        tk,
                        "needs_you",
                        "blocked",
                        "could not fence it to its paths (%s) — Retry or Skip"
                        % ("; ".join(problems)[:200] or "every path refused"),
                    ),
                    now,
                )

    await asyncio.to_thread(_store)


def _tmux_of(title: str) -> str:
    from backend.session.tmux import tmux as _tmux

    return _tmux.to_mindflock_tmux_name(title)


def _sf_fence(run: dict, t: dict, wt: str) -> Tuple[List[str], List[str]]:
    """Fence one same-folder piece to its paths (its session only) and
    re-write the folder's guard file before returning."""
    srv = _server()
    added, problems = srv._red_zones.set_session_fence(
        wt,
        _tmux_of(t["title"]),
        t["paths"],
        name="only here (%s)" % run["name"][:40],
        owner="run:" + run["id"],
        no_commit=True,
    )
    try:
        srv._red_zones.sync_guard(os.path.realpath(wt), lroot=wt)
    except Exception as err:  # noqa: BLE001
        problems.append("the guard could not be written (%s)" % err)
    return added, problems


def _unfence(run_id: str, task_id: str) -> None:
    """A same-folder piece is done (or gone): its fence in the shared folder
    goes, so the folder is the lead's again."""
    srv = _server()
    run = _runs.load(run_id)
    t = _runs.task_by_id(run, task_id) if run else None
    if t is None or not t["fenced"]:
        return
    _inst, wt = _lead_wt(run)
    if wt and t["title"]:
        srv._red_zones.drop_session_fence(wt, _tmux_of(t["title"]))
        try:
            srv._red_zones.sync_guard(os.path.realpath(wt), lroot=wt)
        except Exception:  # noqa: BLE001 — the monitor's tick re-syncs it
            pass
    with _runs.edit(run_id) as r:
        tk = _runs.task_by_id(r, task_id) if r else None
        if tk is not None:
            tk["fenced"] = False


def _unfence_all(run: dict) -> None:
    """Every fence a same-folder group set (its cancel, its end)."""
    srv = _server()
    _inst, wt = _lead_wt(run)
    if not wt:
        return
    if srv._red_zones.drop_session_fence(wt, owner="run:" + run["id"]):
        try:
            srv._red_zones.sync_guard(os.path.realpath(wt), lroot=wt)
        except Exception:  # noqa: BLE001
            pass


def _sf_settle_fences(run_id: str) -> None:
    """A finished same-folder group leaves no fence behind in the folder."""
    with _runs.edit(run_id) as r:
        if r is None:
            return
        _unfence_all(r)
        for t in r["tasks"]:
            t["fenced"] = False


#: One commit at a time per shared folder (``realpath`` → lock).
_SF_LOCKS: Dict[str, threading.Lock] = {}
_SF_LOCKS_GUARD = threading.Lock()


def _sf_lock(wt: str) -> threading.Lock:
    key = os.path.realpath(wt)
    with _SF_LOCKS_GUARD:
        lock = _SF_LOCKS.get(key)
        if lock is None:
            lock = _SF_LOCKS[key] = threading.Lock()
        return lock


def _sf_message(run: dict, t: dict, files: List[str]) -> str:
    """A same-folder piece's commit message, from its own report (or its
    line), naming its paths, with the trailer that makes it this piece's."""
    rep = t.get("report") or {}
    summary = str(rep.get("summary") or "").strip()
    label = _runs.member_label(run, t)
    first = summary.splitlines()[0].strip() if summary else ""
    if not first:
        first = (
            (t["text"] or label).strip().splitlines()[0]
            if (t["text"] or label)
            else label
        )
    subject = first if len(first) <= 72 else first[:71].rstrip() + "…"
    body = []
    if summary and summary != first:
        body.append(summary)
    tests = _runs.tests_line(t.get("report_text") or "") or str(
        rep.get("details") or ""
    )
    if tests and "Tests:" in tests:
        body.append(tests.strip()[:300])
    shown = ", ".join(files[:12]) + (
        " (+%d more)" % (len(files) - 12) if len(files) > 12 else ""
    )
    body.append("Piece %r of the split %r: %s" % (label, run["name"], shown))
    return (
        subject
        + "\n\n"
        + "\n\n".join(body)
        + "\n\n"
        + _runs.sf_trailer(run["id"], t["id"])
        + "\n"
    )


def _sf_commit(run_id: str, task_id: str, now: float) -> None:
    """Commit ONE same-folder piece: exactly its changed paths, nothing else
    (:func:`git_merge.commit_paths` — plumbing, the tree untouched), under
    the folder's lock. Verified on the next pass by its trailer commit."""
    run = _runs.load(run_id)
    t = _runs.task_by_id(run, task_id) if run else None
    if t is None or t["state"] != "integrating":
        return
    _inst, wt = _lead_wt(run)
    if not wt:
        return
    res_paths, _errs = _runs._compile_paths(t["paths"])
    with _sf_lock(wt):
        changed = _git_merge.changed_paths(wt)
        if changed is None:
            return
        mine = [f for f in changed if _runs._hits(res_paths, f)]
        if not mine:
            return  # the next pass decides (its own commits, or gone)
        res = _git_merge.commit_paths(
            wt,
            mine,
            _sf_message(run, t, mine),
            expect_head=_git_merge.rev_parse(wt, "HEAD"),
        )
    with _runs.edit(run_id) as r:
        tk = _runs.task_by_id(r, task_id) if r else None
        if tk is None or tk["state"] != "integrating":
            return
        label = _runs.member_label(r, tk)
        if res["result"] == "committed":
            tk["merge_errors"] = 0
            _runs.log_event(
                r,
                now,
                "merged",
                tk["id"],
                "committed %s (%d file%s) as %s"
                % (
                    label,
                    len(mine),
                    "" if len(mine) == 1 else "s",
                    str(res["sha"])[:9],
                ),
            )
            return
        if res["result"] in ("moved", "nothing"):
            return  # HEAD moved under it / nothing left: next pass
        tk["merge_errors"] += 1
        _runs.log_event(
            r, now, "note", tk["id"], "commit failed: %s" % res.get("error")
        )
        if tk["merge_errors"] >= _runs.MAX_MERGE_ERRORS:
            _runs.apply(
                r,
                _runs._to(
                    tk,
                    "needs_you",
                    "blocked",
                    "MindFlock could not commit it: %s — commit %s by hand, then "
                    "Retry" % (res.get("error") or "?", ", ".join(mine[:4])),
                ),
                now,
            )


def _observe_sf(run: dict, wt: str, tasks: Dict[str, dict]) -> dict:
    """A same-folder group's folder as the planner needs it: each piece's
    changed paths (``sf_own``), its trailer commit (``sf_commit``), commits
    MindFlock did not make (``foreign``, with the pieces whose paths they
    touch), and changes no piece owns (``stray``). Heals an index a commit
    left half-refreshed (a restart between the commit and the re-read)."""
    sf = run.get("sf") or {}
    base = sf.get("base") or ""
    _git_merge.heal_index(wt)
    changed = _git_merge.changed_paths(wt)
    commits = _git_merge.commits_since(wt, base) if base else []
    pieces = [t for t in run["tasks"] if t["kind"] == "piece"]
    res = {t["id"]: _runs._compile_paths(t["paths"])[0] for t in pieces}
    out: dict = {}
    trailer = _runs.SF_TRAILER + ": %s/" % run["id"]
    found: Dict[str, List[dict]] = {}
    foreign = []
    for c in commits or []:
        tid = ""
        for line in str(c.get("body") or "").splitlines():
            if line.startswith(trailer):
                tid = line[len(trailer) :].strip()
        if tid:
            found.setdefault(tid, []).append(c)
            continue
        files = list(c.get("files") or [])
        owners = [
            tid2 for tid2, r in res.items() if any(_runs._hits(r, f) for f in files)
        ]
        outside = [f for f in files if not any(_runs._hits(r, f) for r in res.values())]
        foreign.append(
            {
                "sha": c["sha"],
                "subject": c.get("subject") or "",
                "pieces": owners,
                "outside": outside,
            }
        )
    out["foreign"] = foreign
    if changed is not None:
        baseline = set(sf.get("baseline") or [])
        out["stray"] = [
            f
            for f in changed
            if f not in baseline
            and not f.startswith(".mindflock")
            and not any(_runs._hits(r, f) for r in res.values())
        ]
    for t in pieces:
        o = tasks.get(t["id"])
        if o is None:
            continue
        if changed is not None:
            o["sf_read"] = True
            o["sf_own"] = [f for f in changed if _runs._hits(res[t["id"]], f)]
        got = found.get(t["id"]) or []
        if got:
            o["sf_commit"] = got[-1]["sha"]
            o["commits"] = [c.get("subject") or "" for c in got]
        elif t["self_commits"]:
            by_sha = {c["sha"]: c for c in commits or []}
            o["commits"] = [
                by_sha[x].get("subject") or "" for x in t["self_commits"] if x in by_sha
            ]
    return out


async def _merge(run_id: str, task_id: str, now: float) -> None:
    """Merge one finished member into the lead's branch — the merge queue's
    one step. Clean: the next pass verifies it by ancestry and marks it merged
    back. Conflict: aborted and handed to the lead with the files — or, for a
    red-zone file the lead may not edit, straight to you."""
    run = await asyncio.to_thread(_runs.load, run_id)
    t = _runs.task_by_id(run, task_id) if run else None
    if t is None or t["state"] != "integrating":
        return
    _inst, wt = _lead_wt(run)
    if not wt or not t["branch"]:
        return
    lead_branch = (run.get("lead") or {}).get("branch") or "the lead"
    label = _runs.member_label(run, t)
    msg = "Merge %s (%s) into %s" % (label, t["branch"], lead_branch)
    await asyncio.to_thread(_tidy_artifacts, wt)
    res = await asyncio.to_thread(
        lambda: _git_merge.merge_into(
            wt,
            t["branch"],
            message=msg,
            expect_branch=(run.get("lead") or {}).get("branch") or "",
        )
    )
    head = await asyncio.to_thread(_git_merge.rev_parse, wt, t["branch"])
    red_files: List[str] = []
    if res["result"] == "conflict":
        red = await asyncio.to_thread(_red_res, wt)
        if red:
            red_files = [f for f in res["files"] if _runs._hits(red, f)]

    def _store() -> str:
        with _runs.edit(run_id) as r:
            tk = _runs.task_by_id(r, task_id) if r else None
            if tk is None or tk["state"] != "integrating":
                return ""
            if head:
                tk["head_sha"] = head
            result = res["result"]
            if result in ("clean", "up_to_date"):
                tk["merge_errors"] = 0
                _runs.log_event(
                    r,
                    now,
                    "merged",
                    tk["id"],
                    "merged %s into %s" % (label, lead_branch),
                )
                return ""
            if result == "conflict":
                prev = tk["conflict"] or {}
                files = list(res["files"])
                tk["conflict"] = {
                    "files": files,
                    "attempts": int(prev.get("attempts") or 0) + 1,
                    "at": now,
                }
                if red_files:
                    _runs.apply(
                        r,
                        _runs._to(
                            tk,
                            "needs_you",
                            "conflict",
                            "conflict in a red-zone file (%s) the lead may not edit "
                            "— merge %s by hand, then Retry"
                            % (", ".join(red_files[:3]), tk["branch"]),
                        ),
                        now,
                    )
                    return ""
                lead = (r.get("lead") or {}).get("title") or "the lead"
                _runs.apply(
                    r,
                    _runs._to(
                        tk,
                        "integrating",
                        "conflict",
                        "%s is resolving conflicts in %s"
                        % (lead, ", ".join(files[:4])),
                    ),
                    now,
                )
                return _runs.CONFLICT_TEXT.format(
                    branch=tk["branch"],
                    piece=label,
                    files=", ".join("`%s`" % f for f in files[:12]),
                    run=r["id"],
                    task=tk["id"],
                    **_runs.tools_for(_provider(_program(r))),
                )
            if result == "refused":
                return ""  # the lead's tree changed under us: next pass
            tk["merge_errors"] += 1
            _runs.log_event(
                r, now, "note", tk["id"], "merge failed: %s" % res.get("error")
            )
            if tk["merge_errors"] >= _runs.MAX_MERGE_ERRORS:
                _runs.apply(
                    r,
                    _runs._to(
                        tk,
                        "needs_you",
                        "conflict",
                        "MindFlock could not merge it: %s" % (res.get("error") or "?"),
                    ),
                    now,
                )
            return ""

    handoff = await asyncio.to_thread(_store)
    if handoff:
        await _message_lead(run, handoff)


def _check_start(run_id: str, now: float) -> None:
    """Run the repo's check (``check_command``) on the lead's merged branch;
    with none configured, say so and move on."""
    srv = _server()
    with _runs.edit(run_id) as r:
        if r is None or r["state"] != "checking":
            return
        _inst, wt = _lead_wt(r)
        if not wt:
            return
        try:
            cmd = srv._wt_setup.load_config(wt).check_command
        except Exception:  # noqa: BLE001
            cmd = ""
        if not cmd:
            nxt = _runs.after_check_state(r)
            _runs.apply(
                r,
                {
                    "op": "run",
                    "state": nxt,
                    "check": {"state": "none", "summary": "no check_command"},
                },
                now,
            )
            if nxt == "done":
                _runs.apply(r, {"op": "finish", "state": _runs._finish_state(r)}, now)
            return
        started = srv._wt_setup.start_check(r["lead"]["title"], wt, cmd)
        if not started and not srv._wt_setup.is_running(wt, "check"):
            _runs.log_event(r, now, "note", text="could not start the check")
            return
        _runs.apply(
            r,
            {
                "op": "run",
                "check": {
                    "state": "running",
                    "command": cmd,
                    "sha": _git_merge.rev_parse(wt, "HEAD"),
                    "started_at": now,
                    "tests": None,
                    "summary": "",
                },
            },
            now,
        )
        _runs.log_event(r, now, "check", text="running `%s` on the merged branch" % cmd)


async def _check_fix(run_id: str, tail: str, now: float) -> None:
    """The check failed on the merged branch: hand the failure to the lead."""
    run = await asyncio.to_thread(_runs.load, run_id)
    if run is None:
        return
    chk = run.get("check") or {}
    text = _runs.CHECK_FIX_TEXT.format(
        command=chk.get("command") or "the check", tail=tail or "see its log"
    )
    sent = await _message_lead(run, text)

    def _store() -> None:
        with _runs.edit(run_id) as r:
            if r is None:
                return
            c = r["check"]
            _runs.apply(
                r,
                {
                    "op": "run",
                    "check": {
                        "state": "fixing" if sent else "failed",
                        "attempts": int(c.get("attempts") or 0) + 1,
                        "fix_sent_at": now,
                        "summary": tail,
                    },
                },
                now,
            )
            _runs.log_event(
                r, now, "check", text="the check failed — handed to the lead"
            )

    await asyncio.to_thread(_store)


def _release_prepare(run_id: str, now: float) -> None:
    """Fill the release card: the one PR's title and body (built from the
    record — no model call), its base and branch, and the diff it carries."""
    srv = _server()
    with _runs.edit(run_id) as r:
        if r is None or r["state"] != "release_ready":
            return
        inst, wt = _lead_wt(r)
        if not wt:
            return
        try:
            base = srv._configured_pr_base() or srv._session_base_branch(inst) or ""
        except Exception:  # noqa: BLE001
            base = ""
        # The group's pinned branch — what the release ships.
        branch = r["lead"]["branch"] or _git_merge.current_branch(wt)
        base_ref = ""
        for cand in (base, ("origin/" + base) if base else ""):
            if cand and _git_merge.rev_parse(wt, cand):
                base_ref = cand
                break
        stat = _git_merge.diff_stat(wt, base_ref, "HEAD") if base_ref else {}
        # A member moved out of the group AFTER its work was merged is still
        # in this PR: the card and the body say so (they list what ships).
        view = dict(r, tasks=[_merged_view(wt, t) for t in r["tasks"]])
        commits = (
            len(_git_merge.commit_subjects(wt, base_ref, "HEAD", limit=500))
            if base_ref
            else 0
        )
        _runs.apply(
            r,
            {
                "op": "run",
                "release": {
                    "state": "ready",
                    "title": _runs.release_title(view),
                    "body": _runs.release_body(view, base, branch),
                    "base": base,
                    "branch": branch,
                    "lane": r["policy"]["lane"],
                    "files": stat.get("files", 0),
                    "add": stat.get("add", 0),
                    "del": stat.get("del", 0),
                    "commits": commits,
                    "conflict_fixes": sum(1 for t in r["tasks"] if t["conflict_fixed"]),
                    "head_sha": _git_merge.rev_parse(wt, "HEAD"),
                    "local_origin": _local_origin(wt),
                    "detail": "",
                },
            },
            now,
        )


def _local_origin(wt: str) -> str:
    """``origin`` of the lead's worktree when it is a FOLDER on this machine
    (``""`` otherwise). Provisioning clones from a local checkout, and a
    checkout with no forge remote of its own leaves the workspace's origin
    pointing at that folder: a push "succeeds" into it, and no PR can ever be
    opened from it. The release says so instead of promising a PR."""
    srv = _server()
    try:
        url = srv._github_pr.origin_url(wt) if wt else ""
    except Exception:  # noqa: BLE001
        return ""
    return url if url and srv._remote_url.is_local_path(url) else ""


def _merged_view(wt: str, t: dict) -> dict:
    """``t`` as the release card should list it: a SKIPPED member whose own
    commits are already in the group's branch reads as merged back."""
    if t["state"] != "skipped" or not t["branch"] or not t["base_sha"]:
        return t
    tip = _git_merge.rev_parse(wt, t["branch"])
    if not tip or _git_merge.is_ancestor(wt, tip, "HEAD") is not True:
        return t
    commits = t["commits"] or _git_merge.commit_subjects(wt, t["base_sha"], tip)
    if not commits:
        return t
    return dict(t, state="integrated", commits=commits)


def _release_handoff(run_id: str, reason: str, now: float) -> None:
    """The branch is pushed but no PR could be filed (no gh, no token): the
    group is done, and the release card links GitHub's prefilled compare
    page — the same handoff the Make PR button gives."""
    srv = _server()
    with _runs.edit(run_id) as r:
        if r is None or r["state"] != "releasing":
            return
        _inst, wt = _lead_wt(r)
        rel = r["release"]
        url = ""
        try:
            url = (
                srv._remote_url.compare_url(
                    srv._github_pr.origin_url(wt), rel["base"], rel["branch"]
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            url = ""
        _runs.apply(
            r,
            {
                "op": "run",
                "release": {
                    "state": "handoff",
                    "compare_url": url,
                    "detail": reason[:400],
                    "head_sha": _git_merge.rev_parse(wt, "HEAD") if wt else "",
                    "at": now,
                },
            },
            now,
        )
        _runs.apply(r, {"op": "finish", "state": _runs._finish_state(r)}, now)
        # The handoff is the EXPECTED end where no PR can be filed here: the
        # lead's record is finished, not left "halted" (a red ✗ on its pane
        # for a release that did exactly what it could).
        lead = (r.get("lead") or {}).get("title") or ""
        if lead and _autopilot.get(lead) is not None:
            _autopilot.update(
                lead,
                state="done",
                reason="",
                note=(
                    "pushed to %s (a folder on this machine) — no PR"
                    % rel["local_origin"]
                    if rel.get("local_origin")
                    else "pushed — open the PR from the branch"
                ),
            )


def _nudge(run_id: str, task_id: str, now: float) -> None:
    with _runs.edit(run_id) as r:
        if r is None:
            return
        t = _runs.task_by_id(r, task_id)
        if t is None or not t["title"]:
            return
        entry = _prompt_queue.enqueue(t["title"], _runs.NUDGE_TEXT)
        items = entry.get("items") or []
        t["nudges"] += 1
        t["nudge_id"] = items[-1]["id"] if items else ""
        t["nudge_at"] = now
        t["nudge_seen_at"] = 0.0
        _runs.log_event(
            r,
            now,
            "nudged",
            t["id"],
            "nudge %d of %d" % (t["nudges"], _runs.MAX_NUDGES),
        )


def _fix(run_id: str, task_id: str, hook: str, reason: str, now: float) -> None:
    with _runs.edit(run_id) as r:
        if r is None:
            return
        t = _runs.task_by_id(r, task_id)
        if t is None or not t["title"]:
            return
        tail = re.sub(r"\s+", " ", reason or "").strip()[:400] or "see the shell pane"
        _prompt_queue.enqueue(
            t["title"], _runs.FIX_HOOK_TEXT.format(hook=hook or "a hook", tail=tail)
        )
        t["attempts"]["ship"] += 1
        _runs.log_event(
            r,
            now,
            "fix",
            t["id"],
            "asked the agent to fix %s (%d of %d)"
            % (hook, t["attempts"]["ship"], _runs.MAX_SHIP_RETRIES),
        )
        if r["paused"] or r["state"] in _runs.RUN_FINISHED:
            t["held"] = True  # re-armed on resume, never while paused
            return
        _arm(r, t)


def _rearm(run_id: str, task_id: str) -> None:
    with _runs.edit(run_id) as r:
        if r is None:
            return
        t = _runs.task_by_id(r, task_id)
        if t is None or not t["title"] or t["state"] in _runs.TERMINAL:
            return
        if r["paused"] or r["state"] in _runs.RUN_FINISHED:
            t["held"] = True  # re-armed on resume, never while paused
            return
        _arm(r, t)


def _disarm(title: str) -> None:
    if title:
        _autopilot.disarm(title)


def _settle_rec(title: str) -> None:
    """The member's agent carried the work to its record's rung itself: the
    record is finished there (shipped — or, asking first, parked for your
    go), exactly as if the autopilot had done that last step."""
    rec = _autopilot.get(title) if title else None
    if rec is not None and rec.get("state") == "running":
        _autopilot.finish(title)


# --------------------------------------------------------------------------- #
# Announcing (the ONE emitter)
# --------------------------------------------------------------------------- #
def _emit(event: str, session: str = "", new=None, data: Optional[dict] = None) -> None:
    try:
        _events.BUS.emit(event, session=session, new=new, data=data or {})
    except Exception:  # noqa: BLE001
        pass


def _in_boot_quiet() -> bool:
    try:
        return bool(_server()._in_boot_quiet())
    except Exception:  # noqa: BLE001
        return False


def _standing_keys(run: Optional[dict]) -> set:
    """The announce keys of what a run ALREADY said or stood at before a pass
    — what a boot reconcile may seed silently. Anything the boot pass itself
    discovers (a session gone after the restart) is new, and is said."""
    if not run:
        return set()
    keys = set()
    for t in run["tasks"]:
        if t["state"] == "needs_you" and t["reason"] in _runs.ANNOUNCED_REASONS:
            keys.add(_runs.announce_key(run, t, t["reason"]))
        elif t["state"] in ("shipped", "integrated"):
            keys.add(_runs.announce_key(run, t, "shipped"))
    ask = _run_ask(run)
    if ask is not None:
        keys.add(ask[0])
    if run["paused"] and run["pause_reason"] == "budget":
        keys.add("budget")
    return keys


def _announce(
    run_id: str, boot: bool, changed: bool, standing: Optional[set] = None
) -> None:
    """Emit what this run has to say, once each. ``boot`` (the reconcile
    pass) seeds the keys of what was ALREADY standing (``standing``, read
    before the pass) silently; what the boot pass itself found is left for
    the next pass to say. Inside the boot quiet window nothing is said and
    nothing is seeded, so it is said once the window closes."""
    quiet = (not boot) and _in_boot_quiet()
    to_emit: List[Tuple[str, str, object, dict]] = []

    def _seed_only(key: str) -> bool:
        return standing is None or key in standing

    with _runs.edit(run_id) as run:
        if run is None:
            return
        seen = set(run["announced"])
        for t in run["tasks"]:
            if t["state"] == "needs_you" and t["reason"] in _runs.ANNOUNCED_REASONS:
                key = _runs.announce_key(run, t, t["reason"])
                if key in seen or quiet:
                    continue
                if boot:
                    if _seed_only(key):
                        seen.add(key)
                    continue
                seen.add(key)
                ref = _runs.ref_of(t) if t["kind"] == "ticket" else t["title"]
                text = t["detail"] or t["reason"]
                to_emit.append(
                    (
                        "run.needs_you",
                        t["title"],
                        t["reason"],
                        {
                            "run": run["id"],
                            "name": run["name"],
                            "task": t["id"],
                            "title": t["title"],
                            "ref": ref,
                            "reason": t["reason"],
                            "text": text,
                            "incarnation": t["incarnation"],
                            "key": key,
                            "detail": "%s: %s %s" % (run["name"], ref, text),
                        },
                    )
                )
            elif t["state"] in ("shipped", "integrated") and not _runs.is_together(run):
                key = _runs.announce_key(run, t, "shipped")
                if key in seen or quiet:
                    continue
                if boot:
                    if _seed_only(key):
                        seen.add(key)
                    continue
                seen.add(key)
                to_emit.append(
                    (
                        "run.task_shipped",
                        t["title"],
                        None,
                        {
                            "run": run["id"],
                            "name": run["name"],
                            "task": t["id"],
                            "title": t["title"],
                            "ref": (
                                _runs.ref_of(t) if t["kind"] == "ticket" else t["title"]
                            ),
                            "pr_url": t["pr_url"],
                            "incarnation": t["incarnation"],
                            "detail": "%s: %s shipped" % (run["name"], t["title"]),
                        },
                    )
                )
        ask = _run_ask(run)
        if ask is not None:
            key, reason, text = ask
            if key not in seen and not quiet and (not boot or _seed_only(key)):
                seen.add(key)
                if not boot:
                    lead = (run.get("lead") or {}).get("title") or ""
                    to_emit.append(
                        (
                            "run.needs_you",
                            lead,
                            reason,
                            {
                                "run": run["id"],
                                "name": run["name"],
                                "task": "",
                                "title": lead,
                                "ref": "",
                                "incarnation": 0.0,
                                "reason": reason,
                                "text": text,
                                "key": key,
                                "detail": "%s: %s" % (run["name"], text),
                            },
                        )
                    )
        if run["paused"] and run["pause_reason"] == "budget":
            if (
                not run["budget_announced"]
                and not quiet
                and (not boot or _seed_only("budget"))
            ):
                run["budget_announced"] = True
                if not boot:
                    text = "spent $%.2f of its $%.2f budget" % (
                        _runs.run_cost(run),
                        run["budget_usd"],
                    )
                    to_emit.append(
                        (
                            "run.needs_you",
                            "",
                            "budget",
                            {
                                "run": run["id"],
                                "name": run["name"],
                                "task": "",
                                "title": "",
                                "ref": "",
                                "incarnation": 0.0,
                                "reason": "budget",
                                "text": text,
                                # Each budget pause is its own ask (a second
                                # one after a raise must not be deduped).
                                "key": "%s:budget:%d" % (run["id"], int(time.time())),
                                "detail": "%s %s" % (run["name"], text),
                            },
                        )
                    )
        finished = run["state"] in ("done", "done_with_failures")
        summ = run.get("summary") or {}
        if finished and summ and not summ.get("announced") and not quiet and not boot:
            summ["announced"] = True
            run["summary"] = summ
            started = min(
                [t["started_at"] for t in run["tasks"] if t["started_at"]]
                or [run["created_at"]]
            )
            outcome = _runs.finish_phrase(run, int(summ.get("shipped", 0) or 0))
            to_emit.append(
                (
                    "run.finished",
                    "",
                    run["state"],
                    {
                        "run": run["id"],
                        "name": run["name"],
                        "shipped": summ.get("shipped", 0),
                        "failed": summ.get("failed", 0),
                        "cost_usd": summ.get("cost_usd", 0.0),
                        "duration_s": round(
                            float(run["finished_at"] or time.time()) - started, 1
                        ),
                        "grouping": run["policy"]["grouping"],
                        "outcome": outcome,
                        "detail": "%s finished — %s%s"
                        % (
                            run["name"],
                            outcome,
                            (
                                (", %d failed" % summ.get("failed", 0))
                                if summ.get("failed", 0)
                                else ""
                            ),
                        ),
                    },
                )
            )
            _log_info(
                "team_run finished %s shipped=%d failed=%d",
                run["id"],
                summ.get("shipped", 0),
                summ.get("failed", 0),
            )
        run["announced"] = sorted(seen)[-500:]
        state, counts = run["state"], _runs.counts(run)
    if boot:
        return
    if changed or to_emit:
        _emit("run.changed", data={"run": run_id, "state": state, "counts": counts})
    for event, session, new, data in to_emit:
        _emit(event, session=session, new=new, data=data)


def _run_ask(run: dict):
    """What a one-for-all group itself waits on you for — ``(key, reason,
    text)`` once per round, or None: a plan to approve, the one PR to open, a
    check that keeps failing on the merged branch."""
    lead = (run.get("lead") or {}).get("title") or "the lead"
    state = run["state"]
    gone = _runs.lead_gone(run, time.time())
    if gone:
        since = float((run.get("lead") or {}).get("missing_since") or 0.0)
        return ("%s:lead_gone:%d" % (run["id"], int(since)), "lead_gone", gone)
    if state == "plan_ready" and run.get("plan"):
        n = len(run["plan"]["pieces"])
        return (
            "%s:plan:%d" % (run["id"], run["plan"]["round"]),
            "plan",
            "%s proposed %d pieces — approve them" % (lead, n),
        )
    rel = run.get("release") or {}
    if (
        state == "release_ready"
        and rel.get("state") == "ready"
        and run["policy"]["release"] == "ask"
    ):
        return (
            "%s:release:%s" % (run["id"], (rel.get("head_sha") or "")[:12]),
            "release",
            "one PR is ready to open",
        )
    chk = run.get("check") or {}
    if state == "checking" and chk.get("state") == "failed":
        return (
            "%s:check:%d:%s"
            % (run["id"], chk.get("attempts") or 0, chk.get("sha", "")[:12]),
            "check_failed",
            "the check failed on the merged branch",
        )
    sf = run.get("sf") or {}
    stray = list(sf.get("stray") or [])
    stray_commits = list(sf.get("stray_commits") or [])
    if (
        _runs.same_folder(run)
        and (stray or stray_commits)
        and state not in (_runs.RUN_FINISHED)
    ):
        import hashlib

        key = hashlib.sha1(
            "\0".join(sorted(stray) + stray_commits).encode("utf-8", "replace")
        ).hexdigest()[:12]
        parts = []
        if stray:
            parts.append(
                "changes no piece owns in %s's folder: %s%s — commit or discard "
                "them yourself (no piece's commit takes them)"
                % (
                    lead,
                    ", ".join(stray[:4]),
                    " (+%d more)" % (len(stray) - 4) if len(stray) > 4 else "",
                )
            )
        if stray_commits:
            parts.append(
                "a commit no piece made is on the group's branch (%s) — it ships "
                "with the PR unless you undo it"
                % ", ".join(c[:9] for c in stray_commits[:3])
            )
        return ("%s:stray:%s" % (run["id"], key), "stray", "; ".join(parts))
    return None


def _log_info(fmt: str, *args) -> None:
    try:
        from backend import log

        if log.InfoLog is not None:
            log.InfoLog.Printf(fmt, *args)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #
_SIDE_EFFECTS = (
    "start",
    "settle_rec",
    "arm",
    "disarm",
    "nudge",
    "fix",
    "fence",
    "unfence",
    "merge",
    "sf_commit",
    "check_start",
    "check_fix",
    "release_prepare",
    "release",
    "release_handoff",
)


def _plan_and_apply(run_id: str, obs: dict, now: float) -> Tuple[List[dict], bool]:
    """Re-load (a route may have changed it since we observed), plan, apply
    the pure actions, save. Returns the side-effect actions and whether the
    run changed."""
    with _runs.edit(run_id) as run:
        if run is None:
            return [], False
        before = (
            run["state"],
            run["paused"],
            [(t["state"], t["reason"]) for t in run["tasks"]],
        )
        acts = _runs.plan_actions(run, obs, now)
        effects = []
        for a in acts:
            if a["op"] in _SIDE_EFFECTS:
                effects.append(a)
                continue
            if a["op"] == "abort":
                # The group stops: nothing it armed keeps shipping.
                for t in run["tasks"]:
                    if t["state"] not in _runs.TERMINAL and t["title"]:
                        _disarm(t["title"])
            _runs.apply(run, a, now)
            if a["op"] == "pause":
                _hold(run)
        run["waiting_for_usage"] = (
            bool(obs.get("limited_providers"))
            and any(t["state"] == "queued" for t in run["tasks"])
            and obs.get("provider") in (obs.get("limited_providers") or [])
        )
        after = (
            run["state"],
            run["paused"],
            [(t["state"], t["reason"]) for t in run["tasks"]],
        )
        # Arm/disarm effects carry their titles now (the task may move on).
        for e in effects:
            t = _runs.task_by_id(run, e.get("task", ""))
            e["title"] = t["title"] if t else ""
        # A ticket that ended without ever starting (the create kept failing,
        # the group aborted) hands its reservation back.
        _settle_ledger(run)
        return effects, before != after


async def step_run(run_id: str, boot: bool = False) -> None:
    """One decision pass over one run. Never raises."""
    srv = _server()
    try:
        now = time.time()
        run = await asyncio.to_thread(_runs.load, run_id)
        if run is None:
            return
        owner = srv._SERVER_BOOT_ID
        if not await asyncio.to_thread(_runs.claim_lease, run_id, owner, now):
            return  # another live server drives this run; this one only reads
        if run["state"] in _runs.RUN_FINISHED:
            if _runs.same_folder(run) and any(t["fenced"] for t in run["tasks"]):
                await asyncio.to_thread(_sf_settle_fences, run_id)
            await asyncio.to_thread(_announce, run_id, boot, False)
            return
        standing = _standing_keys(run) if boot else None
        obs = await asyncio.to_thread(observe, run, now, boot)
        effects, changed = await asyncio.to_thread(_plan_and_apply, run_id, obs, now)
        for e in effects:
            op = e["op"]
            try:
                if op == "start":
                    await _start(run_id, e["task"], now)
                elif op == "arm":
                    await asyncio.to_thread(_rearm, run_id, e["task"])
                elif op == "disarm":
                    await asyncio.to_thread(_disarm, e.get("title") or "")
                elif op == "settle_rec":
                    await asyncio.to_thread(_settle_rec, e.get("title") or "")
                elif op == "nudge":
                    await asyncio.to_thread(_nudge, run_id, e["task"], now)
                elif op == "fix":
                    await asyncio.to_thread(
                        _fix,
                        run_id,
                        e["task"],
                        e.get("hook") or "",
                        e.get("reason") or "",
                        now,
                    )
                elif op == "fence":
                    await _fence(run_id, e["task"], now)
                elif op == "merge":
                    await _merge(run_id, e["task"], now)
                    wake()  # verify it without waiting a whole interval
                elif op == "sf_commit":
                    await asyncio.to_thread(_sf_commit, run_id, e["task"], now)
                    wake()
                elif op == "unfence":
                    await asyncio.to_thread(_unfence, run_id, e["task"])
                elif op == "check_start":
                    await asyncio.to_thread(_check_start, run_id, now)
                elif op == "check_fix":
                    await _check_fix(run_id, e.get("tail") or "", now)
                elif op == "release_prepare":
                    await asyncio.to_thread(_release_prepare, run_id, now)
                elif op == "release":
                    await asyncio.to_thread(release, run_id, bool(e.get("merge")), True)
                elif op == "release_handoff":
                    await asyncio.to_thread(
                        _release_handoff, run_id, e.get("reason") or "", now
                    )
                changed = True
            except Exception as err:  # noqa: BLE001 — one effect never stops a pass
                _log_info("team_run %s: %s failed: %s", run_id, op, err)
        await asyncio.to_thread(_announce, run_id, boot, changed, standing)
    except Exception as err:  # noqa: BLE001
        _log_info("team_run %s pass failed: %s", run_id, err)


async def run_pass(boot: bool = False) -> None:
    """One pass over every run that still has something to do or say."""
    runs = await asyncio.to_thread(_runs.list_runs)
    for run in runs:
        finished = run["state"] in _runs.RUN_FINISHED
        if finished and (run.get("summary") or {}).get("announced", True):
            continue
        await step_run(run["id"], boot=boot)
    _prune_taps(time.time())


async def reconcile() -> None:
    """The boot pass: re-derive every run from what is really there (the
    restart table), silently — it seeds what has already been announced
    rather than re-announcing the standing state of everything."""
    await run_pass(boot=True)


_ARCHIVE_EVERY_S = 3600.0


async def run_loop() -> None:
    """Drive team runs forever (started by the lifespan): reconcile once,
    then a pass every :data:`team_runs.RUN_INTERVAL_S` or on :func:`wake`."""
    global _LOOP, _WAKE
    _LOOP = asyncio.get_running_loop()
    _WAKE = asyncio.Event()
    subscribe()
    try:
        await reconcile()
    except Exception:  # noqa: BLE001
        pass
    last_archive = 0.0
    while True:
        try:
            await run_pass()
            if time.time() - last_archive > _ARCHIVE_EVERY_S:
                last_archive = time.time()
                await asyncio.to_thread(_runs.archive_old)
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=_runs.RUN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _WAKE.clear()


# --------------------------------------------------------------------------- #
# Operations behind the /api/runs routes
# --------------------------------------------------------------------------- #
#: ref -> monotonic deadline: a ticket no source knows, remembered briefly so a
#: preview typed one keystroke at a time does not ask every tracker each time.
_UNRESOLVED: Dict[str, float] = {}
_UNRESOLVED_TTL_S = 60.0
_FIND_TIMEOUT_S = 15.0


async def _ticket_rows() -> List[dict]:
    """The Intake ticket listing (cached stale-while-revalidate, like the
    panel's), or [] when no source is configured / reachable."""
    srv = _server()
    try:
        data, _stale = await srv._cached_fanout(
            srv._ASSIGNED_TICKETS_CACHE, srv._ticket_start.list_assigned_tickets
        )
    except Exception:  # noqa: BLE001 — unconfigured / offline: nothing listed
        return []
    return [dict(t) for t in (data or {}).get("tickets") or [] if isinstance(t, dict)]


def _from_row(row: dict) -> dict:
    return {
        "source": str(row.get("source") or ""),
        "id": str(row.get("id") or ""),
        "slug": str(row.get("slug") or ""),
        "title": str(row.get("session") or row.get("slug") or ""),
        "name": str(row.get("name") or ""),
        "url": str(row.get("url") or ""),
        "repo_url": str(row.get("repo_url") or ""),
        "branch": str(row.get("branch") or ""),
    }


def _from_story(source: str, story) -> dict:
    ts = _server()._ticket_start
    return {
        "source": source,
        "id": str(getattr(story, "id", "") or ""),
        "slug": str(getattr(story, "slug", "") or ""),
        "title": ts.session_title(story),
        "name": str(getattr(story, "name", "") or ""),
        "url": str(getattr(story, "app_url", "") or ""),
        "repo_url": str(getattr(story, "repo_url", "") or ""),
        "branch": ts.branch_for(story),
    }


def _configured_sources() -> List[Tuple[str, str]]:
    try:
        cfg = _server()._ticket_start._load_config()
        return [
            (str(s.id or s.provider), str(s.provider or ""))
            for s in (cfg.ticketing_sources or [])
        ]
    except Exception:  # noqa: BLE001
        return []


async def resolve_ticket(
    ref: str, rows: List[dict], source: str = "", ticket_id: str = ""
) -> dict:
    """A ticket ref (or an exact ``source`` + ``id``) → its facts, or
    ``{"error": ...}``. The Intake listing first (cheap, and it names the
    session exactly as a start would); then, for a ticket the listing does not
    carry (assigned to someone else, say), each configured source in turn."""
    ts = _server()._ticket_start
    if source and ticket_id:
        for row in rows:
            if str(row.get("source")) == source and str(row.get("id")) == ticket_id:
                return _from_row(row)
        try:
            story = await asyncio.wait_for(
                ts.find_ticket(source, ticket_id), _FIND_TIMEOUT_S
            )
            return _from_story(source, story)
        except Exception as err:  # noqa: BLE001
            return {"error": str(err) or "not found in %s" % source}
    row, err = _runs.match_ticket(ref, rows)
    if row is not None:
        return _from_row(row)
    if err:
        return {"error": err}
    key = ref.strip().lower()
    if _UNRESOLVED.get(key, 0.0) > time.monotonic():
        return {"error": "not found in any source"}
    m = re.fullmatch(r"sc-(\d+)", key)
    for src_key, provider in _configured_sources():
        tid = m.group(1) if (m and provider == "shortcut") else ref.strip()
        try:
            story = await asyncio.wait_for(
                ts.find_ticket(src_key, tid), _FIND_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — not in this source; try the next
            continue
        if story is not None:
            return _from_story(src_key, story)
    _UNRESOLVED[key] = time.monotonic() + _UNRESOLVED_TTL_S
    return {"error": "not found in any source"}


def _repo_label(path_or_url: str) -> str:
    base = str(path_or_url or "").rstrip("/").split("/")[-1].split(":")[-1]
    return base[:-4] if base.endswith(".git") else base


def _repo_labels(repo_root: str) -> set:
    """Every name ``repo_root`` answers to, lowercased: its folder, its
    ``origin``, and — for MindFlock's ``_base_<repo>`` clone, which is where a
    ticket lead's group lives — the clone source it records and the folder
    name without the prefix. A ticket names its repository by URL; the group's
    repository is a folder, and either spelling has to match."""
    from backend.session import provisioned as _prov

    out = {_repo_label(repo_root)}
    try:
        out.add(_repo_label(_server()._github_pr.origin_url(repo_root)))
    except Exception:  # noqa: BLE001
        pass
    try:
        out.add(_repo_label(_prov.clone_source_of(repo_root)))
    except Exception:  # noqa: BLE001
        pass
    name = os.path.basename(os.path.normpath(repo_root or ""))
    if _prov.is_base_repo_dirname(name):
        out.add(name[len(_prov.BASE_REPO_PREFIX) :])
    return {x.lower() for x in out if x}


def _live_titles() -> set:
    srv = _server()
    live = set(srv.ENGINE.instances)
    try:
        live |= {r.get("title") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pass
    return live


async def preview(text: str, repo_path: str = "", program: str = "") -> dict:
    """``POST /api/runs/preview``: parse and resolve, no side effects."""
    srv = _server()
    items = _runs.parse_items(text)
    rows = await _ticket_rows() if any(i["kind"] == "ticket" for i in items) else []
    live = _live_titles()
    reserved: set = set()
    out: List[dict] = []
    warnings: List[str] = []
    repo = _repo_label(repo_path)
    taken_here = _taken_titles(
        os.path.abspath(os.path.expanduser(repo_path)) if repo_path else ""
    )
    for it in items:
        if it["kind"] == "task":
            hint = _runs.task_title(it["text"], live | reserved | taken_here)
            reserved.add(hint)
            out.append(
                {"kind": "task", "text": it["text"], "repo": repo, "title_hint": hint}
            )
            continue
        r = await resolve_ticket(it["ref"], rows)
        if r.get("error"):
            out.append(
                {
                    "kind": "ticket",
                    "source": None,
                    "id": None,
                    "ref": it["ref"],
                    "title": None,
                    "repo": None,
                    "title_hint": "",
                    "has_session": False,
                    "error": r["error"],
                }
            )
            continue
        has = r["title"] in live
        out.append(
            {
                "kind": "ticket",
                "source": r["source"],
                "id": r["id"],
                "ref": it["ref"],
                "title": r["name"],
                "repo": _repo_label(r["repo_url"]) or repo,
                "title_hint": r["title"],
                "has_session": has,
                "error": None,
            }
        )
        owned = _runs.owner_of_title(r["title"])
        if owned:
            warnings.append(
                "%s is already in group %s — it will be left out"
                % (it["ref"], owned[0]["name"])
            )
        elif has:
            warnings.append(
                "%s already has a session (%s) — it will be added to the group, "
                "not restarted" % (it["ref"], r["title"])
            )
    return {
        "items": out,
        "name_suggestion": _runs.name_suggestion(items),
        # THE default (Settings; Off when unset) — the New dialog seeds a
        # list from /api/config's fasttrack_default, the same value.
        "lane_default": _lanes.normalize_lane(srv._fasttrack_default()) or "leave",
        "warnings": warnings,
    }


def _bool(payload: dict, key: str, default: bool = False) -> bool:
    v = payload.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise RunError("%s must be a boolean" % key)
    return v


def _validate_program(program: str) -> str:
    program = str(program or "").strip()
    if not program:
        return ""
    from backend import providers

    known = {p.name for p in providers.all_providers() if p.name != "generic"}
    if program not in known:
        raise RunError(
            "unknown agent %r — pick one of: %s" % (program, ", ".join(sorted(known)))
        )
    return program


async def _build_tasks(
    items: List[dict],
    run_policy_lane: str,
    repo_root: str,
    exclude_run: str = "",
    together: bool = False,
) -> Tuple[List[dict], List[dict], List[str]]:
    """Request items → ``(tasks, adopted_rows, warnings)``. Tickets are
    resolved now so their titles are reserved (and the ledger can be told);
    a live session for one is ADOPTED, not restarted; one another group owns
    is left out with a warning.

    ``together`` (one for all): every line runs as a fresh worktree of
    ``repo_root`` forked from the lead, so a ticket of another repository is
    refused (one PR is one repository) and a ticket that already has a
    session is left out (its branch was not cut from the lead)."""
    srv = _server()
    # The listing names each ticket's session exactly as a start would, and it
    # is cached — so even an exact source + id is looked up there first.
    want_rows = any(isinstance(i, dict) and i.get("kind") == "ticket" for i in items)
    rows = await _ticket_rows() if want_rows else []
    taken = _taken_titles()
    # A task line's session must not land on a branch a closed session left
    # behind (it would start on — and ship — those old commits).
    branch_taken = _branch_titles(repo_root) if repo_root else set()
    ours = _repo_labels(repo_root) if together and repo_root else set()
    tasks: List[dict] = []
    adopted: List[dict] = []
    warnings: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            raise RunError(
                "each item is {kind: ticket, source, id} or {kind: task, text}"
            )
        kind = str(it.get("kind") or "")
        if kind == "task":
            text = str(it.get("text") or "").strip()
            if not text:
                raise RunError("a task item needs text")
            if not repo_root:
                raise RunError("repo_path is required for task lines")
            title = _runs.task_title(text, taken | branch_taken)
            taken.add(title)
            tasks.append(
                {
                    "kind": "task",
                    "text": text[:4000],
                    "title": title,
                    "repo_root": repo_root,
                }
            )
            continue
        if kind != "ticket":
            raise RunError("unknown item kind %r" % kind)
        source = str(it.get("source") or "").strip()
        tid = str(it.get("id") or "").strip()
        ref = str(it.get("ref") or tid or "").strip()
        if not ref and not (source and tid):
            raise RunError("a ticket item needs source and id (or ref)")
        r = await resolve_ticket(ref, rows, source, tid)
        if r.get("error"):
            # Never silently turned into a task: it fails visibly at start,
            # after the usual retries, with the tracker's own reason.
            tasks.append(
                {
                    "kind": "ticket",
                    "source": source,
                    "ticket_id": tid or ref,
                    "text": ref,
                    "title": "",
                    "detail": r["error"],
                }
            )
            continue
        title = r["title"]
        if together:
            theirs = _repo_label(r.get("repo_url") or "")
            if theirs and theirs.lower() not in ours:
                raise RunError(
                    "one-for-all needs a single repository — %s is in %s, not %s"
                    % (ref or tid, theirs, _repo_label(repo_root))
                )
        owned = _runs.owner_of_title(title, exclude_run)
        if owned:
            warnings.append(
                "%s is already in group %s — left out" % (ref or tid, owned[0]["name"])
            )
            continue
        if together and title in srv.ENGINE.instances:
            warnings.append(
                "%s already has a session (%s) — one-for-all starts its lines "
                "fresh off the lead, so it was left out" % (ref or tid, title)
            )
            continue
        task = {
            "kind": "ticket",
            "source": r["source"],
            "ticket_id": r["id"],
            "text": r["name"] or ref,
            "title": title,
            "branch": r["branch"],
            "repo_root": "",
        }
        inst = srv.ENGINE.instances.get(title)
        if inst is not None:
            task.update(
                state="working",
                adopted=True,
                incarnation=float(srv._created_epoch(inst) or 0.0),
                started_at=time.time(),
            )
            adopted.append(task)
            warnings.append(
                "%s already has a session (%s) — it was added to the group, not "
                "restarted" % (ref or tid, title)
            )
        elif title in taken:
            warnings.append("%s is listed twice — kept once" % (ref or tid))
            continue
        else:
            # In flight in the ingestion ledger with no session yet: the
            # pipeline (or another group) is starting it right now. Starting
            # it here too would put two sessions on one ticket's branch.
            try:
                holder = srv._ticket_start.ledger_holder(title)
            except Exception:  # noqa: BLE001 — the ledger is best-effort
                holder = None
            if holder is not None and holder != ("run:" + exclude_run):
                warnings.append(
                    "%s is already being started (%s) — left out"
                    % (
                        ref or tid,
                        "by another group" if holder else "by ticket ingestion",
                    )
                )
                continue
        taken.add(title)
        tasks.append(task)
    return tasks, adopted, warnings


async def create_run(payload: dict) -> Tuple[dict, List[str]]:
    """``POST /api/runs`` → ``(RunDTO, warnings)``. Raises :class:`RunError`."""
    srv = _server()
    payload = payload or {}
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise RunError("nothing to do")
    if len(items) > _runs.MAX_ITEMS:
        raise RunError("at most %d items in one group" % _runs.MAX_ITEMS)
    pol = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    raw_lane = pol.get("lane")
    # No lane given (the MCP's start_team_run without one): THE default from
    # Settings, which is Off ("leave") when the user never set it.
    defaulted = raw_lane in (None, "")
    lane = (
        _lanes.normalize_lane(srv._fasttrack_default()) or "leave"
        if defaulted
        else _lanes.normalize_lane(raw_lane)
    )
    if not lane:
        raise RunError("unknown lane")
    ask_first = _bool(pol, "ask_first")
    grouping = str(pol.get("grouping") or "each")
    if grouping not in ("each", "together"):
        raise RunError("grouping must be each or together")
    split = _bool(payload, "split")
    if grouping == "together" and not _runs.CAPABILITIES["together"]:
        raise RunError("one-for-all (one PR for the whole group) isn't available")
    if split and not _runs.CAPABILITIES["split"]:
        raise RunError("splitting a line into parallel pieces isn't available")
    if split:
        grouping = "together"
    together = grouping == "together"
    release = str(pol.get("release") or "ask")
    if release not in ("auto", "ask"):
        raise RunError("release must be auto or ask")
    if together:
        # One PR for all IS "commit every line into the group's branch": a
        # "leave it" group would commit unasked, so it is refused rather than
        # silently committing. "Ask me first" means the group's one outward
        # step — the release — asks (members' commits are internal).
        if lane == "leave" and defaulted:
            # A DEFAULTED Off can't be one-for-all's lane (it would commit
            # unasked): the nearest one that keeps everything on this
            # machine, exactly as the New dialog shows it.
            lane = "commit"
        if lane == "leave":
            raise RunError(
                "one PR for all commits each line into the group's branch — "
                "choose commit (nothing leaves this machine) or further"
            )
        if ask_first:
            release, ask_first = "ask", False
    try:
        concurrency = int(payload.get("concurrency") or 3)
    except (TypeError, ValueError):
        raise RunError("concurrency must be a number from 1 to 8") from None
    if not 1 <= concurrency <= _runs.MAX_CONCURRENCY:
        raise RunError("concurrency must be a number from 1 to 8")
    try:
        budget = float(payload.get("budget_usd") or 0.0)
    except (TypeError, ValueError):
        raise RunError("budget_usd must be a number") from None
    if budget < 0:
        raise RunError("budget_usd must be a number")
    program = _validate_program(payload.get("program"))
    repo_root = str(payload.get("repo_path") or "").strip()
    if repo_root:
        repo_root = os.path.abspath(os.path.expanduser(repo_root))
        if not os.path.isdir(repo_root) or not srv._is_git_repo(repo_root):
            raise RunError("repo_path must be a git repository: %s" % repo_root)
    created_by = str(payload.get("created_by") or "user")
    if created_by != "user":
        who = created_by[len("agent:") :] if created_by.startswith("agent:") else ""
        created_by = ("agent:" + who) if who in srv.ENGINE.instances else "user"
    goal = ""
    lead_title = str(payload.get("lead") or "").strip()
    lead_inst = None
    if split:
        one = items[0] if len(items) == 1 and isinstance(items[0], dict) else {}
        goal = str(one.get("text") or "").strip() if one.get("kind") == "task" else ""
        if not goal:
            raise RunError("split needs exactly one line")
    elif lead_title:
        raise RunError("lead is only for a split")
    lead_fit = None
    if lead_title:
        lead_inst, lead_repo = await asyncio.to_thread(_lead_candidate, lead_title)
        lead_fit = await asyncio.to_thread(_lead_fit, lead_inst)
        # The lead's repository wins: its pieces are worktrees of the repo its
        # commits live in, whatever folder the request named.
        repo_root = lead_repo
        program = program or str(getattr(lead_inst, "Program", "") or "")
    if together and not repo_root:
        raise RunError("one-for-all needs a single repository")
    if split:
        reason = srv._mcp_unattachable(program or srv.ENGINE.default_program())
        if reason is not None:
            raise RunError("this CLI doesn't get the MindFlock tools (%s)" % reason)
    max_kids = _max_pieces()
    if together and not split and len(items) > max_kids:
        raise RunError(
            "one-for-all takes at most %d lines (MINDFLOCK_MAX_CHILDREN) — every "
            "line is a worker of the group's lead" % max_kids
        )
    if split:
        tasks, adopted, warnings = [], [], []
    else:
        tasks, adopted, warnings = await _build_tasks(
            items, lane, repo_root, together=together
        )
        if not tasks:
            raise RunError("nothing to do")
    name = str(payload.get("name") or "").strip()[:80] or _runs.name_suggestion(
        [
            {"kind": t["kind"], "ref": t.get("ticket_id"), "text": t.get("text")}
            for t in tasks
        ]
        or [{"kind": "task", "text": goal}]
    )
    fields = {
        "name": name,
        "created_by": created_by,
        "state": "planning" if split else "running",
        "split": split,
        "goal": goal,
        "repo_root": repo_root,
        "program": program,
        "policy": {
            "lane": lane,
            "ask_first": ask_first,
            "grouping": grouping,
            "release": release,
        },
        "concurrency": concurrency,
        "budget_usd": budget,
        "tasks": tasks,
    }

    def _store() -> dict:
        now = time.time()
        run = _runs.create(fields, now=now)
        with _runs.edit(run["id"]) as r:
            _runs.log_event(
                r, now, "started", text="started with %d items" % len(r["tasks"])
            )
            for t in r["tasks"]:
                if t["state"] == "working" and t["adopted"]:
                    try:
                        _arm(r, t)
                    except Exception as err:  # noqa: BLE001
                        _runs.log_event(
                            r, now, "note", t["id"], "could not arm: %s" % err
                        )
                elif t["kind"] == "ticket" and t["title"] and not _reserve(r, t):
                    _runs.apply(
                        r,
                        _runs._to(
                            t,
                            "skipped",
                            "",
                            "already being started elsewhere — left out",
                            finished_at=now,
                        ),
                        now,
                    )
            run = r
        return _runs.load(run["id"])

    run = await asyncio.to_thread(_store)
    if together:
        try:
            run = await _start_lead(run, lead_inst, lead_fit)
        except RunError:
            # No group after all: hand back every ticket it reserved, or the
            # ledger keeps them "in flight" with no session and no run.
            def _undo() -> None:
                with _runs.edit(run["id"]) as r:
                    if r is not None:
                        for t in r["tasks"]:
                            if t["state"] not in _runs.TERMINAL:
                                t["state"] = "cancelled"
                        _settle_ledger(r)
                _runs.remove(run["id"])

            await asyncio.to_thread(_undo)
            raise
    _emit(
        "run.changed",
        data={"run": run["id"], "state": run["state"], "counts": _runs.counts(run)},
    )
    wake()
    return _runs.run_dto(run, srv.ENGINE.instances), warnings


def _max_pieces() -> int:
    from backend.web.core import lineage as _lineage

    return _lineage.limit(_lineage.MAX_CHILDREN_ENV, _lineage.DEFAULT_MAX_CHILDREN)


def _lead_fit(inst) -> dict:
    """Whether ``inst`` can take a split's merges ITSELF: its own worktree
    (not in place) on its own branch (not its base, not a trunk, not
    detached). ``{"in_place", "trunk", "branch", "wt"}`` — read live."""
    srv = _server()
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    branch = (srv._current_branch(wt) if wt else "") or ""
    if not branch and not getattr(inst, "InPlace", False):
        branch = getattr(inst, "Branch", "") or ""
    base = ""
    try:
        base = srv._session_base_branch(inst) or ""
    except Exception:  # noqa: BLE001
        pass
    trunk = not branch or branch == base or branch.lower() in _autopilot.TRUNK_BRANCHES
    return {
        "in_place": bool(getattr(inst, "InPlace", False)),
        "trunk": bool(trunk),
        "branch": branch,
        "base": base,
        "wt": wt,
    }


def _lead_candidate(title: str):
    """An existing session that may lead a split → ``(instance, repo path)``.
    Any session in a git folder: one with its own worktree on its own branch
    takes the merges itself; one that works directly in its folder (in place)
    or sits on its base/trunk branch plans the split, and the plan card then
    offers separate worktrees (MindFlock starts a NEW lead from its last
    commit — it is never merged into) or its own folder (no merge)."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        raise RunError("instance not found: %s" % title, 404)
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        raise RunError("workspace not ready", 409)
    owned = _runs.owner_of_title(title)
    if owned:
        raise RunError("%s is already in group %s" % (title, owned[0]["name"]), 409)
    # The repository its pieces fork from is the one holding its worktree —
    # never its ``Path``: a ticket session's is "." (the server's cwd), and
    # its worktree hangs off MindFlock's base clone (or is its own clone).
    repo = _git_merge.repo_of(wt)
    if not repo:
        raise RunError("could not read %s's repository" % title, 409)
    return inst, repo


async def _start_lead(run: dict, existing=None, fit: Optional[dict] = None) -> dict:
    """Give a one-for-all group (or a split) its LEAD: the session whose
    branch every member merges into and whose agent resolves conflicts. A
    split's lead proposes the plan. ``existing`` — an adopted live session
    (Split… on a session) — is briefed through its prompt queue instead of
    being created. Raises :class:`RunError` (the route answers it)."""
    srv = _server()
    now = time.time()
    provider = _provider(_program(run))
    if existing is not None:
        title = str(existing.Title)
        fit = fit or await asyncio.to_thread(_lead_fit, existing)
        branch = fit.get("branch") or ""
        if not branch and not fit.get("in_place"):
            branch = getattr(existing, "Branch", "") or ""
        own = not (fit.get("in_place") or fit.get("trunk"))
        # The adopted session's OWN lane stops here: the group ships it once,
        # at the release. Left armed, it would push (and PR) the groundwork
        # before any plan is approved — and a running record keeps the lead
        # "busy" forever, so no piece could ever merge. A session that only
        # PLANS (in place, or on its trunk) keeps its lane until you pick
        # "in this folder" — split into separate worktrees, it is untouched.
        disarmed = await asyncio.to_thread(_autopilot.disarm, title) if own else False
        brief_run = dict(
            run, lead={"trunk": bool(fit.get("trunk")), "branch": branch or ""}
        )
        brief = "Split this task into parallel pieces for MindFlock:\n\n%s\n\n%s" % (
            run["goal"],
            _runs.lead_brief(brief_run, _max_pieces(), provider),
        )
        await asyncio.to_thread(_prompt_queue.enqueue, title, brief)
        lead = {
            "title": title,
            "branch": branch,
            "incarnation": float(srv._created_epoch(existing) or 0.0),
            "adopted": True,
            "started_at": now,
            "briefed": True,
            "in_place": bool(fit.get("in_place")),
            "trunk": bool(fit.get("trunk")),
        }
        note = (
            "%s's own lane was turned off — the group ships it once, at the "
            "release" % title
            if disarmed
            else ""
        )
    else:
        note = ""
        # The work's own words (stop words dropped), whole words only, short
        # enough that "<base>-<piece>" still reads on the rail.
        base = _runs.task_title(run["goal"] or run["name"], ())
        while len(base) > 26 and "-" in base:
            base = base.rsplit("-", 1)[0]
        base = base or "group"
        taken = _taken_titles(run["repo_root"])
        title = base + "-lead"
        n = 2
        while title in taken:
            title = "%s-lead-%d" % (base, n)
            n += 1
        if run.get("split"):
            prompt = (
                run["goal"] + "\n\n" + _runs.lead_brief(run, _max_pieces(), provider)
            )
        else:
            prompt = _runs.integrator_brief(run, provider)
        payload = {
            "title": title,
            "program": _program(run),
            "repo_path": run["repo_root"],
            "prompt": prompt,
        }
        try:
            status, body = await srv._session_create.create_result(payload)
        except Exception as err:  # noqa: BLE001
            status, body = 500, {"error": str(err)}
        if status != 202:
            raise RunError(
                "the group's lead could not be started: %s"
                % (body.get("error") or status),
                status if 400 <= status < 500 else 500,
            )
        lead = {
            "title": title,
            "branch": str(body.get("branch") or ""),
            "incarnation": float(body.get("created_at") or 0.0),
            "adopted": False,
            "started_at": now,
            "briefed": True,
        }

    def _store() -> dict:
        with _runs.edit(run["id"]) as r:
            r["lead"] = lead
            _runs.log_event(
                r,
                now,
                "lead",
                text=(
                    "%s leads the split"
                    if r.get("split")
                    else "%s integrates the group"
                )
                % lead["title"],
            )
            if note:
                _runs.log_event(r, now, "note", text=note)
            out = r
        return out

    return await asyncio.to_thread(_store)


def _need(run_id: str) -> dict:
    run = _runs.load(run_id)
    if run is None:
        raise RunError("no such group: %s" % run_id, 404)
    return run


def _need_task(run: dict, task_id: str) -> dict:
    t = _runs.task_by_id(run, task_id)
    if t is None:
        raise RunError("no such task: %s" % task_id, 404)
    return t


def _changed(run: dict) -> None:
    _emit(
        "run.changed",
        data={"run": run["id"], "state": run["state"], "counts": _runs.counts(run)},
    )
    wake()


def pause(run_id: str, reason: str = "user") -> dict:
    now = time.time()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] in _runs.RUN_FINISHED:
            raise RunError("this group has finished", 409)
        if not run["paused"]:
            run["paused"] = True
            run["pause_reason"] = (
                reason if reason in ("user", "budget", "limit") else "user"
            )
            _hold(run)
            _runs.log_event(run, now, "paused", text=run["pause_reason"])
        out = run
    _changed(out)
    return _runs.summary_dto(out)


def resume(run_id: str, budget_usd=None) -> dict:
    now = time.time()
    if budget_usd is not None:
        try:
            budget_usd = float(budget_usd)
        except (TypeError, ValueError):
            raise RunError("budget_usd must be a number") from None
        if budget_usd < 0:
            raise RunError("budget_usd must be a number")
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] in _runs.RUN_FINISHED:
            raise RunError("this group has finished", 409)
        if budget_usd is not None:
            run["budget_usd"] = budget_usd
        if run["paused"]:
            run["paused"] = False
            run["pause_reason"] = ""
            run["budget_announced"] = False
            for t in run["tasks"]:
                if t["state"] == "needs_you" and t["reason"] == "budget":
                    t["state"], t["reason"], t["detail"] = "working", "", ""
            _unhold(run)
            _runs.log_event(run, now, "resumed")
        out = run
    _changed(out)
    return _runs.summary_dto(out)


def cancel(run_id: str) -> Tuple[dict, List[str]]:
    """Stop starting new work and stop shipping. Sessions and branches stay;
    queued tickets are handed back to ingestion."""
    srv = _server()
    now = time.time()
    kept: List[str] = []
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] == "cancelled":
            out = run
        else:
            for t in run["tasks"]:
                if t["state"] in _runs.TERMINAL:
                    continue
                if t["state"] == "queued":
                    _runs.apply(
                        run,
                        _runs._to(
                            t,
                            "cancelled",
                            "",
                            "removed when the group was cancelled",
                            finished_at=now,
                        ),
                        now,
                    )
                    continue
                if t["title"]:
                    _disarm(t["title"])
                    if t["title"] in srv.ENGINE.instances:
                        kept.append(t["title"])
                _runs.apply(
                    run,
                    _runs._to(
                        t,
                        "cancelled",
                        "",
                        "group cancelled — its session was kept",
                        finished_at=now,
                    ),
                    now,
                )
            if run["state"] == "releasing" and run.get("lead"):
                _disarm(run["lead"]["title"])
            if _runs.same_folder(run):
                _unfence_all(run)
                for t in run["tasks"]:
                    t["fenced"] = False
            if run.get("lead") and run["lead"]["title"] in srv.ENGINE.instances:
                kept.append(run["lead"]["title"])
            run["state"] = "cancelled"
            run["paused"] = False
            run["finished_at"] = now
            run["summary"] = dict(_runs.summarize(run, now), announced=True)
            _runs.log_event(run, now, "cancelled")
            _settle_ledger(run)
            out = run
    _changed(out)
    return _runs.summary_dto(out), kept


async def add_tasks(run_id: str, items) -> dict:
    srv = _server()
    run = _need(run_id)
    if run["state"] == "cancelled":
        raise RunError("this group was cancelled", 409)
    if not isinstance(items, list) or not items:
        raise RunError("nothing to do")
    together = _runs.is_together(run)
    if together and run["state"] in ("planning", "plan_ready"):
        # The approval REPLACES the task list with the plan's pieces: a line
        # added now would be dropped without a word.
        raise RunError(
            "this group's plan is not approved yet — add lines once it is "
            "(or ask the lead to include them in its plan)",
            409,
        )
    if together and run["state"] == "releasing":
        raise RunError("this group is being released — add lines once it is done", 409)
    if together:
        live = sum(1 for t in run["tasks"] if t["state"] not in _runs.TERMINAL)
        if live + len(items) > _max_pieces():
            raise RunError(
                "one-for-all takes at most %d lines at once "
                "(MINDFLOCK_MAX_CHILDREN) — every line is a worker of the "
                "group's lead" % _max_pieces(),
                409,
            )
    # A one-for-all line forks off the LEAD: same repository, never an
    # existing session adopted from elsewhere (its branch was not cut from
    # the lead, and merging it would drag an unrelated base into the PR).
    repo = (
        await asyncio.to_thread(_lead_repo, run)
        if together and run.get("lead")
        else run["repo_root"]
    )
    tasks, adopted, warnings = await _build_tasks(
        items,
        run["policy"]["lane"],
        repo,
        exclude_run=run_id,
        together=together,
    )
    now = time.time()

    def _store() -> dict:
        with _runs.edit(run_id) as r:
            if r is None:
                raise RunError("no such group: %s" % run_id, 404)
            have = {t["title"] for t in r["tasks"] if t["title"]}
            n = len(r["tasks"])
            for raw in tasks:
                if raw.get("title") and raw["title"] in have:
                    continue
                n += 1
                t = _runs._normalize_task(dict(raw, id="t%d" % n))
                while _runs.task_by_id(r, t["id"]):
                    n += 1
                    t["id"] = "t%d" % n
                r["tasks"].append(t)
                if t["adopted"]:
                    try:
                        _arm(r, t)
                    except Exception:  # noqa: BLE001
                        pass
                elif t["kind"] == "ticket" and t["title"] and not _reserve(r, t):
                    _runs.apply(
                        r,
                        _runs._to(
                            t,
                            "skipped",
                            "",
                            "already being started elsewhere — left out",
                            finished_at=now,
                        ),
                        now,
                    )
            if r["state"] in ("done", "done_with_failures"):
                r["state"] = "running"
                r["finished_at"] = 0.0
                r["summary"] = None
            if _runs.is_together(r) and r["state"] in ("checking", "release_ready"):
                # The new lines merge into the group's branch first: back to
                # running, and the check and the release card are redone on
                # the branch that has them (the release never ships without).
                _runs.apply(
                    r,
                    {
                        "op": "run",
                        "state": "running",
                        "check": {"state": "pending", "attempts": 0, "summary": ""},
                        "release": {"state": "none", "detail": ""},
                    },
                    now,
                )
            _runs.log_event(r, now, "added", text="%d more" % len(tasks))
            return r

    run = await asyncio.to_thread(_store)
    _changed(run)
    dto = _runs.run_dto(run, srv.ENGINE.instances)
    if warnings:
        dto["warnings"] = warnings
    return dto


def _reopen(run: dict) -> None:
    if run["state"] in ("done", "done_with_failures"):
        run["state"] = "running"
        run["finished_at"] = 0.0
        run["summary"] = None


def retry(run_id: str, task_id: str, fresh: bool = False) -> dict:
    srv = _server()
    now = time.time()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] == "cancelled":
            raise RunError("this group was cancelled", 409)
        t = _need_task(run, task_id)
        if t["state"] not in ("failed", "needs_you"):
            raise RunError(
                "only a failed task, or one waiting on you, can be retried", 409
            )
        if (
            not fresh
            and _runs.is_together(run)
            and t["state"] == "needs_you"
            and t["reason"] == "conflict"
        ):
            # Back into the merge queue (you merged it by hand, or cleared
            # what blocked it): the next pass re-checks ancestry first.
            t.update(conflict=None, merge_errors=0, blocked_since=0.0)
            _runs.apply(run, _runs._to(t, "integrating", ready_at=now), now)
            _runs.log_event(run, now, "retry", t["id"], "back in the merge queue")
        else:
            _retry_task(run, t, fresh, now)
        _reopen(run)
        out, task = run, dict(t)
    _changed(out)
    return _runs.task_dto(task, task["title"] in srv.ENGINE.instances)


def _is_ours(t: dict, inst) -> bool:
    """Whether the live session titled ``t["title"]`` is THIS task's — its
    incarnation (creation time), not merely its name. A title can be reused
    by an unrelated session; that one is never armed, disarmed or driven."""
    created = _server()._created_epoch(inst)
    inc = float(t["incarnation"] or 0.0)
    started = float(t["started_at"] or 0.0)
    if not inc and not started:
        return False  # this task never started a session: any namesake is not it
    if created is None:
        return not inc
    if inc:
        return abs(float(created) - inc) <= 1.0
    return float(created) >= started - _runs.INCARNATION_SLACK_S


def _retry_task(run: dict, t: dict, fresh: bool, now: float) -> None:
    srv = _server()
    inst = srv.ENGINE.instances.get(t["title"]) if t["title"] else None
    live = inst is not None
    ours = live and _is_ours(t, inst)
    if live and not ours and not fresh:
        raise RunError(
            "%s is another session now (its title was reused) — Retry fresh "
            "starts this task under a new title and leaves that session alone"
            % t["title"],
            409,
        )
    repo = _task_repo(run, t)
    if fresh:
        # A new title on a new branch; the old session and branch stay — but
        # the old session stops SHIPPING: left armed, its agent's later diff
        # would be committed, pushed and PR'd next to the new one's.
        if ours:
            _disarm(t["title"])
        base = t["base_title"] or t["title"] or _runs.task_title(t["text"])
        t["base_title"] = base
        if t["kind"] == "ticket" and (t["base_branch"] or t["branch"]):
            t["base_branch"] = t["base_branch"] or t["branch"]
        taken = _taken_titles("" if t["kind"] == "ticket" else repo)
        n = 2
        while "%s-%d" % (base, n) in taken:
            n += 1
        title = "%s-%d" % (base, n)
        t["branch"] = (
            "%s-%d" % (t["base_branch"], n)
            if t["kind"] == "ticket" and t["base_branch"]
            else ""
        )
        t["title"] = title
        t.update(incarnation=0.0, started_at=0.0, adopted=False, pr_url="")
        t.update(fenced=False, conflict=None, merge_errors=0, base_sha="")
        live = False
    elif not live and _runs.is_together(run) and t["branch"]:
        # A vanished one-for-all member: what it COMMITTED is on its branch.
        # Merge that back (never re-create around it — a fresh fork would
        # leave those commits out of the group's one PR).
        _inst, wt = _lead_wt(run)
        tip = _git_merge.rev_parse(wt, t["branch"]) if wt else ""
        commits = (
            _git_merge.commit_subjects(wt, t["base_sha"], tip)
            if tip and t["base_sha"]
            else []
        )
        if commits:
            t.update(held=False, finished_at=0.0, blocked_since=0.0, conflict=None)
            t.update(merge_errors=0, head_sha=tip, commits=commits)
            _runs.apply(
                run,
                _runs._to(
                    t,
                    "integrating",
                    "",
                    "its session is gone; merging the %d commit%s on its branch"
                    % (len(commits), "" if len(commits) == 1 else "s"),
                    ready_at=now,
                ),
                now,
            )
            _runs.log_event(run, now, "retry", t["id"], "merging its branch back")
            return
    if not live:
        # Re-created: a new worktree needs its fence again, and a new base.
        t.update(fenced=False)
    t["attempts"] = {"create": 0, "ship": 0}
    t.update(
        nudges=0,
        nudge_id="",
        nudge_at=0.0,
        nudge_seen_at=0.0,
        retry_at=0.0,
        missing_since=0.0,
        progress_at=now,
        held=False,
        flag="",
        finished_at=0.0,
    )
    if live and not fresh:
        try:
            _arm(run, t)
        except Exception as err:  # noqa: BLE001
            raise RunError("could not re-arm its lane: %s" % err, 409) from None
        _runs.apply(run, _runs._to(t, "working"), now)
    else:
        if t["kind"] == "ticket" and not _reserve(run, t):
            raise RunError(
                "%s is already being started elsewhere (ticket ingestion) — "
                "it was not queued again" % _ticket_slug(t),
                409,
            )
        _runs.apply(run, _runs._to(t, "queued"), now)
    _runs.log_event(run, now, "retry", t["id"], "fresh" if fresh else "")


def start_now(run_id: str, task_id: str) -> dict:
    srv = _server()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        t = _need_task(run, task_id)
        if t["state"] != "queued":
            raise RunError("only a queued task can be started now", 409)
        if run["paused"]:
            raise RunError("this group is paused — resume it first", 409)
        t["start_now"] = True
        t["retry_at"] = 0.0
        task = dict(t)
        out = run
    _changed(out)
    return _runs.task_dto(task, task["title"] in srv.ENGINE.instances)


def skip(run_id: str, task_id: str) -> dict:
    """A queued task is removed; a running one is DETACHED — its session stays
    and its lane is unchanged."""
    srv = _server()
    now = time.time()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        t = _need_task(run, task_id)
        if t["state"] in _runs.TERMINAL:
            raise RunError("this task is already %s" % t["state"], 409)
        detail = (
            "removed" if t["state"] == "queued" else "detached — its session was kept"
        )
        _runs.apply(run, _runs._to(t, "skipped", "", detail, finished_at=now), now)
        _settle_ledger(run)
        task, out = dict(t), run
    _changed(out)
    return _runs.task_dto(task, task["title"] in srv.ENGINE.instances)


def adopt(run_id: str, title: str) -> dict:
    """Add a live session to the group (it is not restarted; the group's lane
    is armed on it). Refuses a BRANCH some group already owns — a copy window
    of a member is the same work."""
    srv = _server()
    title = str(title or "").strip()
    now = time.time()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        raise RunError("instance not found: %s" % title, 404)
    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    row = next((r for r in rows if r.get("title") == title), None) or {
        "title": title,
        "repo": srv._repo_name(inst),
        "branch": getattr(inst, "Branch", "") or "",
    }
    key = _lanes.branch_key(row)
    owned = _runs.owner_of_title(title) or (
        _runs.owner_of_branch(key, rows) if key else None
    )
    if owned:
        raise RunError("branch already in group %s" % owned[0]["name"], 409)
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] == "cancelled":
            raise RunError("this group was cancelled", 409)
        n = len(run["tasks"]) + 1
        while _runs.task_by_id(run, "t%d" % n):
            n += 1
        t = _runs._normalize_task(
            {
                "id": "t%d" % n,
                "kind": "task",
                "text": title,
                "title": title,
                "branch": row.get("branch") or "",
                "repo_root": run["repo_root"],
                "state": "working",
                "incarnation": float(srv._created_epoch(inst) or 0.0),
                "started_at": now,
                "adopted": True,
            }
        )
        run["tasks"].append(t)
        _reopen(run)
        try:
            _arm(run, t)
        except Exception as err:  # noqa: BLE001
            _runs.log_event(run, now, "note", t["id"], "could not arm: %s" % err)
        _runs.log_event(run, now, "adopted", t["id"], "added " + title)
        task, out = dict(t), run
    _changed(out)
    return _runs.task_dto(task, True)


# --------------------------------------------------------------------------- #
# Splits: the plan, the approval, the merge-back report, the check, the release
# --------------------------------------------------------------------------- #
def user_lane(
    title: str, lane: str = "", ask_first: Optional[bool] = None, ship: bool = False
) -> Tuple[bool, Optional[bool]]:
    """A PERSON sets ``title``'s lane (``/lane``, ⏩) or ships it now: what its
    group allows. Returns ``(held, ask_first)`` — ``held`` when the group is
    paused (the choice is recorded on the task and armed on resume; nothing is
    armed now), ``ask_first`` the member's effective "ask first" when the
    caller passed None (keep what it was). A session no group owns →
    ``(False, ask_first)``.

    Refuses (:class:`RunError` 409) what only the group may do: a lead ships
    once, through the group's release; a one-for-all member's work merges into
    the group's one PR; Ship now on a paused group's member."""
    owned = _runs.owner_of_title(title)
    if not owned:
        return False, ask_first
    run, t = owned
    if t.get("kind") == "lead":
        raise RunError(
            "%s leads group %s — it ships once, through the group's release, "
            "not a lane of its own" % (title, run["name"]),
            409,
        )
    if _runs.is_together(run):
        raise RunError(
            "%s is part of group %s — its work merges into the group's one PR. "
            "Move it out of the group to give it a lane of its own"
            % (title, run["name"]),
            409,
        )
    if ship:
        if run["paused"]:
            raise RunError(
                "group %s is paused — resume it before shipping its members"
                % run["name"],
                409,
            )
        return False, ask_first
    now = time.time()
    lane = _lanes.normalize_lane(lane) or _runs.task_lane(run, t)
    with _runs.edit(run["id"]) as r:
        tk = _runs.task_by_id(r, t["id"]) if r else None
        if tk is None:
            return False, ask_first
        if ask_first is None:
            ask_first = _runs.task_ask_first(r, tk)
        ask_first = bool(ask_first) and lane != "leave"
        if tk["lane"] != lane or tk["ask_first"] != ask_first:
            tk["lane"] = lane
            tk["ask_first"] = ask_first
            _runs.log_event(
                r,
                now,
                "note",
                tk["id"],
                "lane set to %s%s by you" % (lane, ", ask first" if ask_first else ""),
            )
        held = bool(r["paused"])
        if held:
            tk["held"] = True
    if not held:
        wake()
    return held, ask_first


def _need_split(run: dict) -> None:
    if not run.get("split"):
        raise RunError("this group is not a split", 409)


def _check_sender(run: dict, sender, what: str) -> str:
    """A route the LEAD calls through its MCP tools names itself in ``from``;
    any other session is refused. No ``from`` is the user (the UI)."""
    sender = str(sender or "").strip()
    lead = (run.get("lead") or {}).get("title") or ""
    if sender and sender != lead:
        raise RunError(
            "only the group's lead (%s) can %s" % (lead or "none", what), 409
        )
    return sender


def propose_plan(run_id: str, payload: dict) -> dict:
    """``POST /api/runs/{id}/plan``: validate a split's pieces and store them
    for your approval. Raises :class:`RunError` (422 with ``problems``)."""
    payload = payload or {}
    run = _need(run_id)
    _need_split(run)
    if run["state"] not in ("planning", "plan_ready"):
        raise RunError(
            "this group's plan was already approved (%s)" % run["state"], 409
        )
    sender = _check_sender(run, payload.get("from"), "propose its plan")
    _inst, wt = _lead_wt(run)
    root = wt or run["repo_root"]
    files = _git_merge.tracked_files(root) if root else []
    red = _red_res(root) if root else []
    pieces, problems = _runs.validate_plan(
        payload.get("pieces"), files, red, _max_pieces()
    )
    if problems:
        raise RunError(
            "the plan has %d problem%s — %s"
            % (
                len(problems),
                "" if len(problems) == 1 else "s",
                "; ".join(
                    ("%s: %s" % (p["piece"], p["error"])) if p["piece"] else p["error"]
                    for p in problems[:6]
                ),
            ),
            422,
            plan=None,
            problems=problems,
        )
    now = time.time()
    with _runs.edit(run_id) as r:
        if r is None:
            raise RunError("no such group: %s" % run_id, 404)
        if r["state"] not in ("planning", "plan_ready"):
            raise RunError("this group's plan was already approved", 409)
        prev = r.get("plan") or {}
        r["plan"] = {
            "state": "proposed",
            "round": int(prev.get("round") or 0) + 1,
            "pieces": pieces,
            "why": str(payload.get("why") or "")[:2000],
            "by": ("agent:" + sender) if sender else "user",
            "proposed_at": now,
        }
        _runs.apply(r, {"op": "run", "state": "plan_ready"}, now)
        _runs.log_event(
            r,
            now,
            "plan",
            text="%s proposed %d pieces" % (sender or "you", len(pieces)),
        )
        out = r
    _changed(out)
    return {"plan": _runs.run_dto(out)["plan"], "problems": []}


def _mode(mode) -> str:
    mode = str(mode or "").strip() or "worktrees"
    if mode not in _runs.SPLIT_MODES:
        raise RunError("mode must be one of: %s" % ", ".join(_runs.SPLIT_MODES), 400)
    return mode


async def approve(run_id: str, mode: str = "") -> dict:
    """``POST /api/runs/{id}/plan/approve {mode}`` — the plan card's one
    click. ``worktrees`` (the default): every piece in its own worktree,
    merged back into the lead's branch; a lead that works directly in its
    folder (or sits on its trunk) first gets a NEW lead of its own, started
    from its last commit (:func:`_start_own_lead`). ``same_folder``: every
    piece an extra agent in the lead's own folder, no merge."""
    mode = _mode(mode)
    run = await asyncio.to_thread(_need, run_id)
    _need_split(run)
    plan = run.get("plan")
    if run["state"] != "plan_ready" or not plan or plan["state"] != "proposed":
        raise RunError("this group has no plan to approve (%s)" % run["state"], 409)
    if mode == "worktrees" and not run.get("origin"):
        inst, _wt = _lead_wt(run)
        if inst is None:
            raise RunError("the lead session is not there", 409)
        fit = await asyncio.to_thread(_lead_fit, inst)
        if fit["in_place"] or fit["trunk"]:
            await _start_own_lead(run_id, fit)
    return await asyncio.to_thread(approve_plan, run_id, mode)


def _own_lead_title(origin: str, repo: str) -> str:
    """``<origin>-split``, numbered past every title (and session branch)
    already taken."""
    taken = _taken_titles(repo)
    base = (origin or "split")[:40].rstrip("-") or "split"
    title = base + "-split"
    n = 2
    while title in taken:
        title = "%s-split-%d" % (base, n)
        n += 1
    return title


async def _start_own_lead(run_id: str, fit: dict) -> None:
    """Separate worktrees for a split whose lead works directly in its folder
    (or sits on its trunk): MindFlock starts a NEW lead — a plain worktree of
    the same repository on a fresh branch, cut from the original session's
    last commit — and the group runs on it. The original session, its
    checkout, branch, index and uncommitted files are never touched: never
    merged into, switched or pushed. Its uncommitted changes would not be in
    the split, so they are refused here (commit them first)."""
    srv = _server()
    run = await asyncio.to_thread(_need, run_id)
    origin = (run.get("lead") or {}).get("title") or ""
    wt = fit.get("wt") or ""
    if not origin or not wt:
        raise RunError("the lead session is not there", 409)
    await asyncio.to_thread(_tidy_artifacts, wt)
    # Untracked files (also ones MindFlock's diff view marked intent-to-add)
    # are the folder owner's scratch: not in the split either, but nothing to
    # refuse over — the event log names them.
    dirty = await asyncio.to_thread(_git_merge.tracked_dirty, wt, True)
    if dirty:
        raise RunError(
            "%s has uncommitted changes — they would not be in the split (the "
            "pieces start from its last commit): commit them first" % origin,
            409,
            code="origin_dirty",
        )
    head = await asyncio.to_thread(_git_merge.rev_parse, wt, "HEAD")
    repo = await asyncio.to_thread(_git_merge.repo_of, wt)
    if not head or not repo:
        raise RunError("could not read %s's last commit" % origin, 409)
    untracked = await asyncio.to_thread(_git_merge.changed_paths, wt) or []
    title = await asyncio.to_thread(_own_lead_title, origin, repo)
    base_branch = fit.get("base") or fit.get("branch") or ""
    payload = {
        "title": title,
        "program": _program(run),
        "repo_path": repo,
        "prompt": _runs.integrator_brief(run, _provider(_program(run))),
        "base_ref": head,
        "base_branch": base_branch,
    }
    try:
        status, body = await srv._session_create.create_result(payload)
    except Exception as err:  # noqa: BLE001
        status, body = 500, {"error": str(err)}
    if status != 202:
        raise RunError(
            "MindFlock could not start a lead for the pieces: %s"
            % (body.get("error") or status),
            status if 400 <= status < 500 else 500,
        )
    now = time.time()

    def _store() -> None:
        with _runs.edit(run_id) as r:
            if r is None:
                return
            if untracked:
                _runs.log_event(
                    r,
                    now,
                    "note",
                    text="%d untracked file%s in %s %s not in the split (%s)"
                    % (
                        len(untracked),
                        "" if len(untracked) == 1 else "s",
                        origin,
                        "is" if len(untracked) == 1 else "are",
                        ", ".join(untracked[:4]),
                    ),
                )
            r["origin"] = {
                "title": origin,
                "branch": fit.get("branch") or "",
                "head": head,
                "in_place": bool(fit.get("in_place")),
                "trunk": bool(fit.get("trunk")),
            }
            r["repo_root"] = repo
            r["lead"] = {
                "title": title,
                "branch": str(body.get("branch") or ""),
                "base_branch": base_branch,
                "incarnation": float(body.get("created_at") or 0.0),
                "adopted": False,
                "started_at": now,
                "briefed": True,
            }
            _runs.log_event(
                r,
                now,
                "lead",
                text="%s %s — MindFlock started %s from its last commit to take "
                "the pieces; %s is left as it is"
                % (
                    origin,
                    (
                        "works directly in its folder"
                        if fit.get("in_place")
                        else "is on %s" % (fit.get("branch") or "no branch")
                    ),
                    title,
                    origin,
                ),
            )

    await asyncio.to_thread(_store)


def approve_plan(run_id: str, mode: str = "worktrees") -> dict:
    """Approve a split's plan: create the pieces (children of the lead,
    fenced to their paths) and start them all — each in its own worktree
    forked from the lead's HEAD (``worktrees``), or as extra agents in the
    lead's own folder (``same_folder``)."""
    srv = _server()
    now = time.time()
    mode = _mode(mode)
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        _need_split(run)
        plan = run.get("plan")
        if run["state"] != "plan_ready" or not plan or plan["state"] != "proposed":
            raise RunError("this group has no plan to approve (%s)" % run["state"], 409)
        inst, wt = _lead_wt(run)
        origin = run.get("origin")
        sf_fields = None
        if mode == "worktrees" and origin and origin.get("head"):
            # A lead MindFlock started for the pieces, from the original
            # session's last commit (its worktree may still be provisioning).
            head = origin["head"]
            repo = run["repo_root"]
        else:
            if inst is None or not wt:
                raise RunError("the lead session is not there", 409)
            fit = _lead_fit(inst)
            title = run["lead"]["title"]
            if mode == "worktrees" and (fit["in_place"] or fit["trunk"]):
                raise RunError(
                    "%s %s — approve it from the plan card (MindFlock starts a "
                    "lead of its own for the pieces)"
                    % (
                        title,
                        (
                            "works directly in its folder"
                            if fit["in_place"]
                            else "is on " + (fit["branch"] or "no branch")
                        ),
                    ),
                    409,
                    code="needs_lead",
                )
            if mode == "same_folder" and fit["trunk"]:
                raise RunError(
                    "%s is on %s — the pieces would commit onto it. Start a branch "
                    "here first, or run them in separate worktrees"
                    % (title, fit["branch"] or "no branch"),
                    409,
                    code="trunk",
                    branch=fit["branch"],
                )
            _tidy_artifacts(wt)
            if _git_merge.tracked_dirty(wt, mode == "same_folder"):
                raise RunError(
                    "the lead has uncommitted changes — commit its groundwork first "
                    + (
                        "(MindFlock commits each piece's paths in this folder: "
                        "they would mix)"
                        if mode == "same_folder"
                        else "(workers fork from its last commit)"
                    ),
                    409,
                    code="lead_dirty",
                )
            head = _git_merge.rev_parse(wt, "HEAD")
            if mode == "same_folder":
                # The pieces work IN the lead's folder: their "repository"
                # is that folder (in-place sessions), and the group's branch
                # is the one checked out there now.
                repo = wt
                run["lead"]["branch"] = fit["branch"]
                changed = _git_merge.changed_paths(wt) or []
                sf_fields = {
                    "base": head,
                    "baseline": changed[:500],
                    "seen_foreign": [],
                    "stray": [],
                    "stray_commits": [],
                }
                # Now the lead: its own lane stops (the group ships it once).
                _autopilot.disarm(title)
            else:
                # The pieces' branches are cut in the repository holding the
                # lead's worktree (a ticket lead's is MindFlock's base clone).
                repo = _git_merge.repo_of(wt) or run["repo_root"]
        run["repo_root"] = repo if mode == "worktrees" else run["repo_root"]
        lead = run["lead"]["title"]
        base = _runs.lead_base(lead)
        titles = _runs.piece_titles(
            base,
            plan["pieces"],
            _taken_titles(repo if mode == "worktrees" else ""),
        )
        tasks = []
        for i, (p, title) in enumerate(zip(plan["pieces"], titles), 1):
            tasks.append(
                _runs._normalize_task(
                    {
                        "id": "t%d" % i,
                        "kind": "piece",
                        "text": p["prompt"],
                        "paths": p["paths"],
                        "title": title,
                        "repo_root": repo,
                        "lane": "commit",
                    }
                )
            )
        run["tasks"] = tasks
        run["mode"] = mode
        if sf_fields is not None:
            run["sf"] = _runs._normalize_sf(sf_fields)
        run["concurrency"] = max(1, min(_runs.MAX_CONCURRENCY, len(tasks)))
        _runs.apply(
            run,
            {
                "op": "run",
                "state": "running",
                "plan": {"state": "approved", "approved_at": now, "base_sha": head},
            },
            now,
        )
        _runs.log_event(
            run,
            now,
            "approved",
            text="you started %d workers %s"
            % (
                len(tasks),
                (
                    "in %s's folder (no merge)" % lead
                    if mode == "same_folder"
                    else "in separate worktrees"
                ),
            ),
        )
        out = run
    _changed(out)
    return _runs.run_dto(out, srv.ENGINE.instances)


def lead_branch(run_id: str) -> dict:
    """``POST /api/runs/{id}/lead/branch`` — "Start a branch here first": the
    lead's folder is on its trunk, and "in this folder" would commit the
    pieces onto it. Only on your click: ``git switch -c <prefix><lead>-split``
    in that folder (its uncommitted changes come along), and the group's
    branch is the new one."""
    srv = _server()
    now = time.time()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        _need_split(run)
        if run["state"] not in ("planning", "plan_ready"):
            raise RunError("the pieces already started (%s)" % run["state"], 409)
        inst, wt = _lead_wt(run)
        if inst is None or not wt:
            raise RunError("the lead session is not there", 409)
        fit = _lead_fit(inst)
        title = run["lead"]["title"]
        if not fit["trunk"]:
            raise RunError(
                "%s is already on its own branch (%s)" % (title, fit["branch"]), 409
            )
        if _git_merge.operation_in_progress(wt) or _git_merge.merge_in_progress(wt):
            raise RunError(
                "%s's folder is in the middle of a git operation" % title, 409
            )
        existing = set()
        try:
            cp = subprocess.run(
                [
                    "git",
                    "-C",
                    wt,
                    "for-each-ref",
                    "--format=%(refname:short)",
                    "refs/heads",
                ],
                capture_output=True,
                timeout=20,
            )
            existing = set((cp.stdout or b"").decode("utf-8", "replace").split())
        except (OSError, subprocess.SubprocessError):
            pass
        stem = srv._session_branch_name(title + "-split") or (title + "-split")
        branch, n = stem, 2
        while branch in existing:
            branch = "%s-%d" % (stem, n)
            n += 1
        err = _git_merge.switch_new_branch(wt, branch)
        if err:
            raise RunError("could not start a branch: %s" % err, 409)
        run["lead"]["branch"] = branch
        run["lead"]["trunk"] = False
        _runs.log_event(
            run,
            now,
            "lead",
            text="you started branch %s in %s's folder (from %s)"
            % (branch, title, fit["branch"] or "a detached HEAD"),
        )
        out = run
    _changed(out)
    return _runs.run_dto(out, srv.ENGINE.instances)


async def reject_plan(run_id: str, note: str = "") -> dict:
    """ "Ask for a different split": back to planning, and the lead is told."""
    srv = _server()
    now = time.time()
    note = str(note or "").strip()[:800]

    def _store() -> dict:
        with _runs.edit(run_id) as run:
            if run is None:
                raise RunError("no such group: %s" % run_id, 404)
            _need_split(run)
            if run["state"] != "plan_ready":
                raise RunError("there is no proposed plan to send back", 409)
            _runs.apply(
                run, {"op": "run", "state": "planning", "plan": {"note": note}}, now
            )
            _runs.log_event(run, now, "plan", text="you asked for a different split")
            return run

    run = await asyncio.to_thread(_store)
    text = _runs.REPLAN_TEXT.format(
        name=run["name"],
        run=run["id"],
        note=(note.rstrip(".") + ". ") if note else "",
        **_runs.tools_for(_provider(_program(run))),
    )
    await _message_lead(run, text)
    _changed(run)
    return _runs.run_dto(run, srv.ENGINE.instances)


def report_integrated(run_id: str, task_id: str, head_sha: str, sender="") -> dict:
    """The lead says it merged a member by hand (a conflict it resolved).
    The server takes nothing on trust: the member's branch head must be in
    the reported commit, and that commit in the lead's HEAD. ``verified:
    false`` leaves the member in the merge queue."""
    now = time.time()
    run = _need(run_id)
    if not _runs.is_together(run):
        raise RunError("this group has no lead to merge into", 409)
    if not str(sender or "").strip():
        # The lead's MCP tool always names itself; a person merging by hand
        # uses Retry, which re-checks ancestry the same way.
        raise RunError(
            "only the group's lead reports a merge — after merging by hand, "
            "use Retry",
            409,
        )
    _check_sender(run, sender, "report a merge")
    t = _need_task(run, task_id)
    if t["state"] == "integrated":
        return {"ok": True, "verified": True}
    waiting = t["state"] == "integrating" or (
        t["state"] == "needs_you" and t["reason"] == "conflict"
    )
    if not waiting:
        # A member still working (or blocked, or asking) has not finished:
        # a mistaken task id must never mark it merged back.
        raise RunError(
            "this task is not waiting to be merged (%s%s)"
            % (t["state"], (": " + t["reason"]) if t["reason"] else ""),
            409,
        )
    _inst, wt = _lead_wt(run)
    if not wt:
        raise RunError("the lead session is not there", 409)
    piece = _git_merge.rev_parse(wt, t["branch"]) if t["branch"] else ""
    piece = piece or t["head_sha"]
    if (
        piece
        and t["base_sha"]
        and not _git_merge.commit_subjects(wt, t["base_sha"], piece)
    ):
        raise RunError(
            "%s has no commits of its own yet — there is nothing to merge"
            % (t["title"] or task_id),
            409,
        )
    reported = _git_merge.rev_parse(wt, str(head_sha or "").strip() or "HEAD")
    verified = bool(
        piece
        and reported
        and _git_merge.is_ancestor(wt, piece, reported) is True
        and _git_merge.is_ancestor(wt, reported, "HEAD") is True
    )
    if verified:
        commits = (
            _git_merge.commit_subjects(wt, t["base_sha"], piece)
            if t["base_sha"]
            else []
        )
        with _runs.edit(run_id) as r:
            tk = _runs.task_by_id(r, task_id) if r else None
            if tk is not None and tk["state"] in ("integrating", "needs_you"):
                _runs.apply(
                    r,
                    _runs._to(
                        tk,
                        "integrated",
                        merged_at=now,
                        finished_at=now,
                        merged_sha=_git_merge.rev_parse(wt, "HEAD"),
                        head_sha=piece,
                        commits=commits or tk["commits"],
                        conflict_fixed=True,
                    ),
                    now,
                )
                _runs.log_event(r, now, "merged", tk["id"], "the lead merged it")
            out = r
        if out is not None:
            _changed(out)
    else:
        wake()
    return {"ok": True, "verified": verified}


def recheck(run_id: str) -> dict:
    """Run the check on the merged branch again (after you or the lead fixed
    what failed)."""
    srv = _server()
    now = time.time()
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        if run["state"] != "checking":
            raise RunError("this group is not checking (%s)" % run["state"], 409)
        _runs.apply(
            run,
            {"op": "run", "check": {"state": "pending", "attempts": 0, "summary": ""}},
            now,
        )
        _runs.log_event(run, now, "check", text="you asked for the check again")
        out = run
    _changed(out)
    return _runs.run_dto(out, srv.ENGINE.instances)


def release_lane(run: dict, merge_when_green: bool = False) -> str:
    """The lane a release arms on the lead. "Open the PR" never merges: only
    the explicit "merge when checks pass" (or a ``merge`` group released
    automatically, which the planner asks for with ``merge_when_green``) does.
    A ``push`` group pushes its branch and opens nothing."""
    if merge_when_green:
        return "merge"
    return "push" if run["policy"]["lane"] == "push" else "pr"


#: The lead's activity values that make a release take half-finished work.
_RELEASE_BLOCKED = {
    "working": "is mid-turn",
    "clarify": "is waiting on a prompt",
    "limit": "stopped at its usage limit",
}


def _release_refusal(run: dict, now: float) -> Tuple[str, bool]:
    """Why the release cannot ship what the card and the check showed RIGHT
    NOW — ``("", False)`` when it can. The second value says the lead's branch
    MOVED since it was checked: the caller re-checks it (never ships a commit
    nobody tested, under a PR body that says the check passed)."""
    lead = run.get("lead") or {}
    title = lead.get("title") or ""
    _inst, wt = _lead_wt(run)
    if not wt:
        return "the group's lead session is not there", False
    row = next(
        (
            r
            for r in _events.sessions_snapshot()
            if isinstance(r, dict) and r.get("title") == title
        ),
        {},
    )
    why = _RELEASE_BLOCKED.get(str(row.get("activity") or ""))
    if why:
        return "%s %s — release once it is idle" % (title, why), False
    _tidy_artifacts(wt)
    if _git_merge.merge_in_progress(wt):
        return "%s has a merge in progress — finish or abort it first" % title, False
    op = _git_merge.operation_in_progress(wt)
    if op:
        return "%s is in the middle of a %s — finish it first" % (title, op), False
    if _runs.same_folder(run):
        # The release arms the lead's lane, whose commit step takes EVERY
        # change in the folder — untracked files too. In a shared folder
        # (often the person's own checkout) that is never the pieces' work:
        # the release waits until the folder holds nothing but the branch.
        left = _git_merge.changed_paths(wt)
        if left is None:
            return "could not read %s's folder" % title, False
        if left:
            return (
                "%s's folder has changes no piece made (%s%s) — the release would "
                "commit them into the PR: commit them elsewhere, discard them or "
                "ignore them (.gitignore), then release"
                % (
                    title,
                    ", ".join(left[:4]),
                    " +%d more" % (len(left) - 4) if len(left) > 4 else "",
                ),
                False,
            )
    dirty = _git_merge.tracked_dirty(wt)
    if dirty is None:
        return "could not read %s's worktree" % title, False
    if dirty:
        return (
            "%s has uncommitted changes the check never saw — commit or discard "
            "them, then release" % title,
            False,
        )
    pinned = lead.get("branch") or ""
    live = _git_merge.current_branch(wt)
    if pinned and live != pinned:
        return (
            "%s is on %s, not the group's branch %s — check it out again, then "
            "release" % (title, live or "a detached HEAD", pinned),
            False,
        )
    head = _git_merge.rev_parse(wt, "HEAD")
    rel = run.get("release") or {}
    chk = run.get("check") or {}
    shown = _runs._s(rel.get("head_sha"))
    checked = _runs._s(chk.get("sha")) if chk.get("state") == "ok" else ""
    if not head or head != shown or (checked and checked != head):
        return (
            "%s's branch moved since it was checked — MindFlock is checking it "
            "again; release once the card is ready" % title,
            True,
        )
    return "", False


def release(run_id: str, merge_when_green: bool = False, auto: bool = False) -> dict:
    """``POST /api/runs/{id}/release``: ship the group's ONE branch — arm the
    lead's lane (a PR, or merge once checks pass) with the server-built PR
    title and body. Only from ``release_ready``. It is the one outward step a
    one-for-all group takes, and it is yours (or ``release: auto``).

    It ships exactly what the card and the check showed: refused while the
    lead is mid-turn, has uncommitted or half-merged work, or sits on another
    branch; when its HEAD moved since the check, the group goes back to
    checking (and the card is rebuilt) instead."""
    now = time.time()
    refusal = ""
    with _runs.edit(run_id) as run:
        if run is None:
            raise RunError("no such group: %s" % run_id, 404)
        rel = run["release"]
        if run["state"] != "release_ready" or rel["state"] not in ("ready", "failed"):
            raise RunError(
                "this group is not ready to release (%s)" % run["state"], 409
            )
        if run["paused"]:
            raise RunError("this group is paused — resume it first", 409)
        refusal, moved = _release_refusal(run, now)
        if moved:
            _runs.apply(
                run,
                {
                    "op": "run",
                    "state": "checking",
                    "check": {"state": "pending", "attempts": 0, "summary": ""},
                    "release": {"state": "none", "detail": ""},
                },
                now,
            )
            _runs.log_event(
                run, now, "check", text="the branch moved since it was checked"
            )
        if not refusal:
            lane = release_lane(run, merge_when_green)
            # Asked live: a lead whose origin is a folder on this machine can
            # only push there — no PR can be opened, so none is attempted
            # (the release ends in a hand-off that names the folder).
            _inst, wt = _lead_wt(run)
            local = _local_origin(wt)
            if local and lane != "push":
                lane = "push"
            lead = (run.get("lead") or {}).get("title") or ""
            try:
                _lanes.ship_now(lead, lane)
            except _lanes.LaneError as err:
                raise RunError(err.message, err.status) from None
            _autopilot.update(lead, pr_title=rel["title"], pr_body=rel["body"])
            _runs.apply(
                run,
                {
                    "op": "run",
                    "state": "releasing",
                    "release": {
                        "state": "releasing",
                        "lane": lane,
                        "local_origin": local,
                        "detail": "",
                        "at": now,
                    },
                },
                now,
            )
            _runs.log_event(
                run,
                now,
                "release",
                text="%s → %s"
                % ("released automatically" if auto else "you released it", lane),
            )
        out = run
    _changed(out)
    if refusal:
        raise RunError(refusal, 409)
    return _runs.summary_dto(out)


def get_dto(run_id: str) -> dict:
    run = _need(run_id)
    return _runs.run_dto(run, _server().ENGINE.instances)


async def wait_run(run_id: str, until: str, rev: int, wait_s: float) -> dict:
    """Long-poll one run for ``until``: ``needs_you`` (something waits on you),
    ``done`` (finished or cancelled), ``change`` (any newer ``rev``). Returns
    ``{"run": RunDTO, "reason": until|"timeout"}``."""
    deadline = time.monotonic() + max(0.0, min(float(wait_s), 60.0))
    while True:
        run = await asyncio.to_thread(_need, run_id)
        reason = ""
        if until == "change" and run["rev"] > rev:
            reason = "change"
        elif until == "needs_you" and (
            _runs.counts(run)["needs_you"]
            or (run["paused"] and run["pause_reason"] == "budget")
            or _run_ask(run) is not None
        ):
            reason = "needs_you"
        elif until in ("done", "needs_you") and run["state"] in _runs.RUN_FINISHED:
            reason = "done"
        if reason or time.monotonic() >= deadline:
            return {
                "run": _runs.run_dto(run, _server().ENGINE.instances),
                "reason": reason or "timeout",
            }
        await asyncio.sleep(1.0)


def outbox(group: str = "all") -> dict:
    """``GET /api/outbox`` (see :mod:`backend.web.core.outbox`)."""
    import datetime as _dt

    from backend.web.core import outbox as _outbox

    srv = _server()
    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    try:
        rows += srv._pending_rows()
    except Exception:  # noqa: BLE001
        pass
    now = time.time()
    today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def _verify(row: dict):
        try:
            pid = srv._test_plans.owner_for_branch(
                str(row.get("title") or ""),
                str(row.get("path") or ""),
                str(row.get("branch") or ""),
            )
        except Exception:  # noqa: BLE001
            return None
        return {"id": pid} if pid else None

    return _outbox.build(
        rows,
        _runs.list_runs(),
        _autopilot.snapshot(),
        now=now,
        today_start=today.timestamp(),
        group=group or "all",
        verify=_verify,
    )
