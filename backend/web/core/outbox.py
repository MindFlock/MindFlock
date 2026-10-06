"""The Outbox: what is shipping, what shipped, and what is waiting on you.

Intake is what comes in, the Outbox is what goes out, Verify is what got
checked. One place answers "what's waiting on me / what shipped" for EVERY
session — a team run's members and the sessions you started on your own — so
juggling several workstreams costs a glance, not a tour of the rail.

PURE: :func:`build` turns the instance rows (the ``/api/instances`` listing,
with their ``autopilot`` / ``lane`` / ``run`` blocks), the run records, the
autopilot store and the Verify plans into the payload ``GET /api/outbox``
serves. Rows are de-duplicated on ``(repo, branch)``: duplicate windows share a
branch (``foo`` + ``foo-copy``), and one branch's work is one Outbox row, named
after the window that drives it.

Groups, in order:

* ``waiting`` — answer or approve; nothing else needs you. A session on a
  dialog (``prompt``), a lane parked by "ask me before it ships"
  (``approve``, with the commit message and diff stat to approve), and a run's
  escalations (``stuck`` / ``blocked`` / ``ship_halted`` / ``restart`` /
  ``budget`` / ``failed``) with the next action for each.
* ``shipping`` — MindFlock is doing these; no action.
* ``shipped`` — today's commits, pushes and PRs, with checks and the Verify
  checklist when there is one.
* ``queued`` — a run's tasks waiting for a free slot.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from backend.web.core import lanes as _lanes
from backend.web.core import team_runs as _runs

__all__ = ["build", "WAITING_ACTIONS"]

#: The buttons each kind of waiting item offers.
WAITING_ACTIONS = {
    "prompt": ["answer", "open"],
    "approve": ["ship", "diff", "edit_message"],
    "stuck": ["open", "retry", "skip"],
    "blocked": ["open", "retry", "skip"],
    "ship_halted": ["retry", "open", "skip"],
    "restart": ["retry", "retry_fresh", "skip"],
    "conflict": ["retry", "open", "skip"],
    "budget": ["raise_budget", "stop"],
    "failed": ["retry", "retry_fresh", "skip"],
    # A one-for-all group's own asks (their title is the group's lead).
    "plan": ["approve", "open"],
    "release": ["release", "open"],
    "check_failed": ["open", "retry_check"],
    "lead_gone": ["cancel_group"],
}
#: The step the autopilot is on, as the Outbox names it.
_STEP = {
    "commit": "commit",
    "check": "check",
    "push": "push",
    "pr": "make_pr",
    "merge": "merge",
}
_STEP_NOTE = {
    "commit": "committing",
    "check": "running checks",
    "push": "pushing",
    "make_pr": "opening PR",
    "merge": "merging",
}
_CHECKS = {"ok": "pass", "failed": "fail", "pending": "pending", "none": "none"}
#: Finished runs whose summary card stays in the Outbox.
SUMMARY_DAYS = 7


def _key(row: dict) -> str:
    return _lanes.branch_key(row) or "title::" + str(row.get("title") or "")


def _run_ref(run: Optional[dict], task: Optional[dict]) -> Optional[dict]:
    if not run:
        return None
    return {"id": run["id"], "name": run["name"], "task": task["id"] if task else None}


def _first_line(text: str) -> str:
    text = str(text or "").strip()
    return text.splitlines()[0][:200] if text else ""


def build(
    rows: Iterable[dict],
    runs: Iterable[dict],
    autopilot: Dict[str, dict],
    *,
    now: float,
    today_start: float,
    group: str = "all",
    verify: Optional[Callable[[dict], Optional[dict]]] = None,
) -> dict:
    """The ``GET /api/outbox`` payload (see the module docstring).

    ``group``: ``"all"``, a run id (that group's members and tasks only), or
    ``"own"`` (sessions in no group). ``verify(row)`` → ``{"id", "state"}`` of
    the checklist covering the row's branch, or None."""
    rows = [r for r in rows if isinstance(r, dict) and not r.get("device")]
    runs = list(runs)
    by_title: Dict[str, dict] = {}
    for r in rows:
        by_title.setdefault(str(r.get("title") or ""), r)
    member: Dict[str, tuple] = {}
    for run in runs:
        for t in run["tasks"]:
            if t["title"] and t["state"] not in ("cancelled", "skipped"):
                member.setdefault(t["title"], (run, t))

    def wanted(title: str, run: Optional[dict]) -> bool:
        if group in ("", "all"):
            return True
        if group == "own":
            return run is None and title not in member
        return bool(run) and run["id"] == group

    # One row per branch: prefer the window that drives it (holds the record).
    owners: Dict[str, dict] = {}
    for r in rows:
        key = _key(r)
        cur = owners.get(key)
        title = str(r.get("title") or "")
        if cur is None:
            owners[key] = r
            continue
        cur_t = str(cur.get("title") or "")
        if (title in autopilot and cur_t not in autopilot) or (
            title in member and cur_t not in member
        ):
            owners[key] = r
    keep = {id(r) for r in owners.values()}

    waiting: List[dict] = []
    shipping: List[dict] = []
    shipped: List[dict] = []
    queued: List[dict] = []
    listed_wait = set()
    listed_ship = set()

    for r in rows:
        if id(r) not in keep or r.get("pending"):
            continue
        title = str(r.get("title") or "")
        run, task = member.get(title, (None, None))
        if not wanted(title, run):
            continue
        key = _key(r)
        rec = autopilot.get(title)
        if str(r.get("activity") or "") == "clarify":
            waiting.append(
                {
                    "key": key,
                    "title": title,
                    "run": _run_ref(run, task),
                    "kind": "prompt",
                    "reason": "its agent is asking",
                    "since": float(r.get("activity_since") or 0.0),
                    "preview": None,
                    "actions": list(WAITING_ACTIONS["prompt"]),
                }
            )
            listed_wait.add(title)
            continue
        if rec and _lanes.awaiting_approval(rec):
            held = str(rec.get("depth") or "")
            lane = _lanes.normalize_lane(rec.get("lane"))
            ds = r.get("diff_stat") if isinstance(r.get("diff_stat"), dict) else {}
            if held == "agent" and isinstance(ds.get("uncommitted"), dict):
                # Approving a COMMIT: what will be committed is the working
                # tree's change, not everything since the fork point (which
                # also counts local base commits never pushed — "3 files" for a
                # one-file change in the live run).
                ds = ds["uncommitted"]
            # A placeholder (message_auto: the task line, a "Work on …") is
            # replaced at commit time by a message written from the diff, so
            # it is not previewed as THE message — only a person's is.
            # A model-written subject (message_written) described an earlier
            # commit's diff — it is not this work's message either. The FULL
            # message is previewed (a person's multi-line message keeps its
            # body when edited); the card shows its first line.
            said = (
                ""
                if rec.get("message_auto")
                or (rec.get("message_written") and not rec.get("message_drafted"))
                else str(rec.get("message") or "").strip()
            )
            waiting.append(
                {
                    "key": key,
                    "title": title,
                    "run": _run_ref(run, task),
                    "kind": "approve",
                    "step": "commit" if held == "agent" else "push",
                    "lane": lane,
                    "reason": "waiting for your go",
                    "since": float(rec.get("updated") or 0.0),
                    # This approval's identity (when the lane was armed): an
                    # edit made on one card never carries to the next.
                    "armed_at": float(rec.get("started") or 0.0),
                    "preview": {
                        "commit_message": said or None,
                        # Only a title someone SET is previewed: otherwise the
                        # PR is filled from the branch's commits when it opens,
                        # which is not knowable (nor promised) here.
                        "pr_title": (
                            str(rec.get("pr_title") or "") or None
                            if lane in ("pr", "merge")
                            else None
                        ),
                        "files": int(ds.get("files") or 0),
                        "add": int(ds.get("additions") or 0),
                        "del": int(ds.get("deletions") or 0),
                    },
                    "actions": list(WAITING_ACTIONS["approve"]),
                }
            )
            listed_wait.add(title)
            continue
        if rec and rec.get("state") == "running":
            step = _STEP.get(str(rec.get("step") or ""))
            if step:
                lane = _lanes.normalize_lane(rec.get("lane")) or _lanes.lane_of_depth(
                    rec.get("depth")
                )
                shipping.append(
                    {
                        "key": key,
                        "title": title,
                        "run": _run_ref(run, task),
                        "step": step,
                        "note": str(rec.get("note") or "") or _STEP_NOTE[step],
                        "lane": lane,
                    }
                )
                listed_ship.add(title)
                continue
        if (
            rec
            and rec.get("state") == "done"
            and float(rec.get("updated") or 0) >= (today_start)
            # A one-for-all member's commit is internal to the group's branch
            # — not something that shipped; the group ships once, as one PR.
            and not (run and _runs.is_together(run))
        ):
            if rec.get("step") or rec.get("url"):
                shipped.append(_shipped_row(r, rec, run, task, verify))
                listed_ship.add(title)

    for run in runs:
        for t in run["tasks"]:
            title = t["title"]
            if not wanted(title, run):
                continue
            if t["state"] == "queued":
                queued.append(
                    {
                        "run": {"id": run["id"], "name": run["name"], "task": t["id"]},
                        "ref": _runs.ref_of(t) if t["kind"] == "ticket" else None,
                        "text": t["text"],
                        "title": title or None,
                        "retry_at": t["retry_at"] or None,
                    }
                )
                continue
            row = by_title.get(title)
            key = _key(row) if row else "run::%s::%s" % (run["id"], t["id"])
            if t["state"] == "needs_you" and t["reason"] not in ("prompt", "approve"):
                if title in listed_wait:
                    continue
                waiting.append(
                    {
                        "key": key,
                        "title": title,
                        "run": _run_ref(run, t),
                        "kind": t["reason"],
                        "reason": t["detail"] or t["reason"],
                        "since": _since(run, t),
                        "preview": None,
                        "actions": list(WAITING_ACTIONS.get(t["reason"], ["open"])),
                    }
                )
                listed_wait.add(title)
            elif t["state"] == "failed" and run["state"] != "cancelled":
                if (
                    float(run.get("finished_at") or 0)
                    and now - float(run["finished_at"]) > 86400.0
                ):
                    continue
                waiting.append(
                    {
                        "key": key,
                        "title": title,
                        "run": _run_ref(run, t),
                        "kind": "failed",
                        "reason": t["detail"] or "it could not be started",
                        "since": t["finished_at"] or _since(run, t),
                        "preview": None,
                        "actions": list(WAITING_ACTIONS["failed"]),
                    }
                )
            elif t["state"] == "integrating" and title not in listed_ship:
                lead = (run.get("lead") or {}).get("title") or "the lead"
                shipping.append(
                    {
                        "key": key,
                        "title": title,
                        "run": _run_ref(run, t),
                        "step": "integrate",
                        "note": (
                            t["detail"]
                            if t["reason"] == "conflict"
                            else "merging back into %s" % lead
                        ),
                        "lane": "commit",
                    }
                )
                listed_ship.add(title)
            elif (
                t["state"] in ("shipped", "integrated")
                and title not in listed_ship
                and not _runs.is_together(run)
            ):
                if float(t["finished_at"] or 0.0) < today_start:
                    continue
                rec = autopilot.get(title) or {}
                shipped.append(
                    _shipped_row(row or {"title": title}, rec, run, t, verify)
                )
                listed_ship.add(title)
        ask = _run_ask(run, now) if wanted("", run) else None
        if ask is not None:
            waiting.append(ask)
        if run["paused"] and run["pause_reason"] == "budget" and wanted("", run):
            waiting.append(
                {
                    "key": "run::%s" % run["id"],
                    "title": "",
                    "run": _run_ref(run, None),
                    "kind": "budget",
                    "reason": "%s hit $%.2f" % (run["name"], run["budget_usd"]),
                    "since": float(run.get("updated_at") or 0.0),
                    "preview": None,
                    "actions": list(WAITING_ACTIONS["budget"]),
                }
            )

    summaries = []
    for run in runs:
        if run["state"] not in _runs.RUN_FINISHED or not run.get("summary"):
            continue
        if now - float(run.get("finished_at") or 0.0) > SUMMARY_DAYS * 86400.0:
            continue
        if group not in ("", "all") and group != run["id"]:
            continue
        summaries.append(
            {
                "run": run["id"],
                "name": run["name"],
                "state": run["state"],
                "finished_at": run["finished_at"],
                "text_md": run["summary"].get("text_md") or "",
            }
        )

    # Every item says what the work IS beside the session's name: a group
    # member's ticket title or task line (None for a session on its own).
    for item in waiting + shipping + shipped:
        _run_hit, task_hit = member.get(str(item.get("title") or ""), (None, None))
        item.setdefault("text", (task_hit or {}).get("text") or None)
    for item in shipped:
        ds = (by_title.get(item["title"]) or {}).get("diff_stat")
        item["files"] = int(ds.get("files") or 0) if isinstance(ds, dict) else None
    waiting.sort(key=lambda w: float(w.get("since") or 0.0))
    return {
        "counts": {
            "waiting": len(waiting),
            "shipping": len(shipping),
            "shipped": len(shipped),
            "queued": len(queued),
        },
        "groups": {
            "waiting": waiting,
            "shipping": shipping,
            "shipped": shipped,
            "queued": queued,
        },
        "summaries": summaries,
    }


def _run_ask(run: dict, now: float = 0.0) -> Optional[dict]:
    """A one-for-all group's own "waiting on you" item, or None: its lead's
    plan to approve, its one PR to open, a check that keeps failing — or a
    lead that has been gone long enough that nothing can move."""
    lead = (run.get("lead") or {}).get("title") or ""
    state = run["state"]
    base = {
        "key": "run::%s::%s" % (run["id"], state),
        "title": lead,
        "run": _run_ref(run, None),
        "since": float(run.get("updated_at") or 0.0),
    }
    gone = _runs.lead_gone(run, now) if now else ""
    if gone:
        return dict(
            base,
            kind="lead_gone",
            reason=gone,
            preview=None,
            actions=list(WAITING_ACTIONS["lead_gone"]),
        )
    if state == "plan_ready" and run.get("plan"):
        pieces = run["plan"]["pieces"]
        return dict(
            base,
            kind="plan",
            reason="%s proposed %d pieces — approve to start them"
            % (lead or "the lead", len(pieces)),
            preview={
                "pieces": [
                    {"title": p["title"], "paths": list(p["paths"])} for p in pieces
                ]
            },
            actions=list(WAITING_ACTIONS["plan"]),
        )
    rel = run.get("release") or {}
    if state == "release_ready" and rel.get("state") in ("ready", "failed"):
        merged = sum(1 for t in run["tasks"] if t["state"] == "integrated")
        reason = "%d merged into %s — one PR is ready to open" % (
            merged,
            rel.get("branch") or lead or "one branch",
        )
        if rel.get("state") == "failed" and rel.get("detail"):
            reason = "the release stopped: %s" % rel["detail"]
        return dict(
            base,
            kind="release",
            reason=reason,
            preview={
                "pr_title": rel.get("title") or None,
                "base": rel.get("base") or None,
                "branch": rel.get("branch") or None,
                "files": int(rel.get("files") or 0),
                "add": int(rel.get("add") or 0),
                "del": int(rel.get("del") or 0),
                "check": (run.get("check") or {}).get("state") or None,
                # The group's lane labels the buttons: "Open the PR" never
                # merges; a merge group's primary is "merge when checks pass".
                "lane": run["policy"]["lane"],
            },
            actions=list(WAITING_ACTIONS["release"]),
        )
    chk = run.get("check") or {}
    if state == "checking" and chk.get("state") == "failed":
        return dict(
            base,
            kind="check_failed",
            reason="the check failed on the merged branch: %s"
            % (chk.get("summary") or chk.get("command") or "see its log"),
            preview=None,
            actions=list(WAITING_ACTIONS["check_failed"]),
        )
    return None


def _since(run: dict, task: dict) -> float:
    for ev in reversed(run.get("events") or []):
        if ev.get("task") == task["id"]:
            return float(ev.get("ts") or 0.0)
    return float(task.get("started_at") or 0.0)


def _shipped_row(row, rec, run, task, verify) -> dict:
    ms = row.get("merge_state") if isinstance(row.get("merge_state"), dict) else None
    url = str((rec or {}).get("url") or "") or str(row.get("pr_url") or "")
    if task is not None and not url:
        url = task.get("pr_url") or ""
    if ms and ms.get("state"):
        pr_state = str(ms["state"]).lower()
    elif (rec or {}).get("step") == "merge":
        pr_state = "merged"
    else:
        pr_state = "open" if url else None
    checks = _CHECKS.get(str((ms or {}).get("checks") or ""))
    if task is not None and task.get("flag") == "checks_failed":
        checks = "fail"
    return {
        "key": _key(row),
        "title": str(row.get("title") or ""),
        "run": _run_ref(run, task),
        "pr_url": url or None,
        "pr_state": pr_state,
        "checks": checks,
        "commit_subject": _first_line((rec or {}).get("message")) or None,
        "lane": _lanes.normalize_lane((rec or {}).get("lane"))
        or _lanes.lane_of_depth((rec or {}).get("depth"))
        or None,
        "verify": verify(row) if (verify and row.get("branch")) else None,
    }
