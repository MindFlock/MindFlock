"""The impure half of Flights: observe, act, announce — and the operations the
routes perform (docs/flights.md).

:mod:`backend.web.core.flights` holds the stores and the pure planner. This
module is the loop that gives it effect, shaped like the team-run driver
(``core.team_run_driver``): a 5 s pass plus a :func:`wake` when a route or a
bus tap changed something, one lease per flight so two servers never drive the
same one, every flight wrapped so one bad flight cannot stop the pass.

* :func:`observe` — what the current step's done test needs, and nothing more:
  the published ``/api/instances`` row (never a probe from here), the prompt
  queue, the autopilot record (ship), the checklist (verify), the PR's review
  comments (reviews, throttled to one fetch a minute), its checks (throttled),
  a running command's job.
* the executors (:func:`_perform`) — one per step kind. Nothing here commits,
  pushes or opens a PR: a ship step arms the session's lane with
  ``source="flight"`` and the autopilot carries it out, ONE driver per session.
  Prompts go through the prompt queue, whose drain respects dialogs and idle.
* :func:`_announce` — the ONE emitter of ``flight.*`` events. ``needs_you`` and
  ``finished`` are said once per (step, round) / finish, the keys persisted in
  the flight record so a restart never repeats them.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import hashlib
import os
import subprocess
import threading
import time
import traceback
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from backend import compat
from backend.web.core import autopilot as _autopilot
from backend.web.core import events as _events
from backend.web.core import flight_kinds as _kinds
from backend.web.core import flight_when as _when
from backend.web.core import flights as _flights
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core.flight_kinds import StepFailed as _StepFailed

__all__ = [
    "subscribe",
    "wake",
    "observe",
    "step_flight",
    "run_pass",
    "run_loop",
    "start",
    "ground",
    "attach",
    "operate",
    "change_steps",
    "group_steps",
    "on_session_created",
    "on_session_removed",
]


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def _log(fmt: str, *args, level: str = "error") -> None:
    """Log at ``level``: "error" for what nobody handled (a pass that blew
    up, an executor's unexpected exception), "warning" for a failure the
    route handled (an unreadable fact, a refused post), "info" for a routine
    outcome."""
    try:
        from backend import log

        logger = {
            "info": log.InfoLog,
            "warning": log.WarningLog,
        }.get(level, log.ErrorLog)
        if logger is not None:
            logger.Printf(fmt, *args)
    except Exception:  # noqa: BLE001
        pass


def _warn(fmt: str, *args) -> None:
    _log(fmt, *args, level="warning")


# --------------------------------------------------------------------------- #
# Bus taps
# --------------------------------------------------------------------------- #
_EV_LOCK = threading.Lock()
#: title -> epoch of its last ``session.turn_ended`` (a wake-up and one more
#: piece of "the agent worked" evidence, never the only proof of "done").
_TURN_ENDED: Dict[str, float] = {}
_UNSUB = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WAKE: Optional[asyncio.Event] = None
_TAP_KEEP_S = 86400.0
#: Events that may have moved a flight's current step.
WAKE_EVENTS = frozenset(
    {
        "session.turn_ended",
        "session.autopilot_changed",
        "session.deleted",
        "session.pushed",
        "session.test_plan_checked",
        "session.test_plan_settled",
        "session.pr_review_changed",
        "session.created",
    }
)


def _on_event(env: dict) -> None:
    try:
        ev = env.get("event")
        if ev not in WAKE_EVENTS:
            return
        title = str(env.get("session") or "")
        if ev == "session.turn_ended" and title:
            with _EV_LOCK:
                _TURN_ENDED[title] = float(env.get("ts") or time.time())
        if ev == "session.created" and title:
            on_session_created(title)
        wake()
    except Exception:  # noqa: BLE001 — a subscriber never breaks an emit
        pass


def subscribe() -> None:
    """Tap the bus (idempotent). The lifespan calls it before the loop."""
    global _UNSUB
    if _UNSUB is None:
        _UNSUB = _events.BUS.subscribe(_on_event)


def _prune_taps(now: float) -> None:
    with _EV_LOCK:
        for t in [t for t, ts in _TURN_ENDED.items() if now - ts > _TAP_KEEP_S]:
            _TURN_ENDED.pop(t, None)


def wake() -> None:
    """Ask the loop for a pass now. Thread-safe; a no-op with no loop."""
    loop, ev = _LOOP, _WAKE
    if loop is None or ev is None:
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:  # loop closed
        pass


# --------------------------------------------------------------------------- #
# Announcing (the ONE emitter)
# --------------------------------------------------------------------------- #
def _emit(event: str, flight: dict, extra: Optional[dict] = None) -> None:
    data = _flights.event_data(flight)
    if extra:
        data.update(extra)
    try:
        _events.BUS.emit(
            event, session=flight.get("session") or "", new=flight["state"], data=data
        )
    except Exception:  # noqa: BLE001
        pass


def _view_key(f: Optional[dict]) -> tuple:
    if not f:
        return ()
    step = _flights.step_view(f)
    st = step.get("state")
    if step.get("parallel"):
        # At a parallel group: every member's state (round 20).
        st = tuple(m["state"] for m in step["parallel"])
    return (f["state"], f["cursor"], st, f["reason"], f["session"])


def _announce(before: Optional[dict], after: Optional[dict]) -> None:
    """Say what changed between two versions of a flight: ``flight.changed``
    on any visible change, ``flight.needs_you`` / ``flight.finished`` once."""
    if not after:
        return
    if _view_key(before) != _view_key(after):
        _emit("flight.changed", after)
    keys: List[str] = []
    step = _flights.step_view(after)
    if after["state"] == "waiting_you" and step:
        cur = _flights.waiting_step(after)
        sid = cur["id"] if cur is not None else step["id"]
        ss = after["step_states"][sid]
        # Keyed on when the stop STARTED: Retry / Jump / Loop start it afresh
        # (``since``) but reset ``rounds``, so a rounds key never rang again
        # for a stop that parked a second time.
        key = "%s:%d:you" % (sid, int(float(ss.get("since") or 0.0)))
        quiet = (
            cur is not None
            and cur["kind"] == "you"
            and not cur["config"].get("notify", True)
        )
        if key not in after["announced"]:
            keys.append(key)
            if not quiet:
                text, items = _flights.you_text(after)
                _emit("flight.needs_you", after, {"text": text, "items": items})
    if after["state"] in ("done", "halted", "cancelled"):
        key = "finished:%s:%d" % (
            after["state"],
            int(after["finished"] or after["updated"]),
        )
        if key not in after["announced"]:
            keys.append(key)
            _emit("flight.finished", after)
            _take_down_previews(after)
    if keys:

        def _mark(f: dict) -> None:
            f["announced"] = (f["announced"] + keys)[-20:]

        _flights.mutate(after["id"], _mark)


def _take_down_previews(flight: dict) -> None:
    """A finished (or removed) flight takes its previews down: every done
    preview stop's ``stop_command`` (docs/flights.md "Round 15"), on a
    thread — it blocks and is best effort."""
    if not any(s.get("kind") == "preview" for s in flight.get("steps") or []):
        return
    from backend.web.core.flight_kinds import preview as _preview

    threading.Thread(
        target=_preview.stop_all,
        args=(copy.deepcopy(flight),),
        name="mf-flight-preview-stop-%s" % flight["id"],
        daemon=True,
    ).start()


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
#: (flight id, step id) -> {"running", "exit", "output"}: command jobs. Lost on
#: restart, on purpose — the planner then runs the command again.
_JOBS: Dict[Tuple[str, str], dict] = {}
_JOBS_LOCK = threading.Lock()
#: flight id -> {"at", "ids", "comments", "pr"}: the last review-comment fetch.
_REVIEWS: Dict[str, dict] = {}
_REVIEWS_EVERY_S = 60.0
#: flight id -> {"at", "verdict", "failing"}: the last checks read.
_CHECKS: Dict[str, dict] = {}
_CHECKS_EVERY_S = 30.0


def _row(title: str) -> dict:
    for r in _events.sessions_snapshot():
        if isinstance(r, dict) and r.get("title") == title:
            return r
    return {}


def _session_obs(flight: dict, now: float, agent: bool) -> dict:
    """The session facts every step reads (blocking; a worker thread)."""
    srv = _server()
    title = flight["session"]
    if not title:
        # A free chain: no session, its folder is where it runs.
        return {"present": False, "folder": str(flight.get("repo_root") or "")}
    inst = srv.ENGINE.instances.get(title)
    row = _row(title)
    try:
        pending = any(r.get("title") == title for r in srv._pending_rows())
    except Exception:  # noqa: BLE001
        pending = False
    status = str(row.get("status") or "")
    if not status and inst is not None:
        status = str(getattr(inst.Status, "name", inst.Status) or "").lower()
    folder = str(row.get("folder") or "")
    if not folder and inst is not None:
        try:
            folder = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            folder = ""
    activity = str(row.get("activity") or "")
    out = {
        "present": inst is not None,
        "pending": pending,
        "status": status,
        "sleeping": bool(row.get("sleeping")),
        "activity": activity,
        "branch": str(row.get("branch") or getattr(inst, "Branch", "") or ""),
        "pr_url": str(row.get("pr_url") or ""),
        "folder": folder,
        "path": str(row.get("path") or getattr(inst, "Path", "") or ""),
    }
    if inst is None or not agent:
        return out
    try:
        q = _prompt_queue.get_state(title)
        out["queue_pending"] = bool(
            q.get("items") and (q.get("enabled") or q.get("held"))
        )
    except Exception:  # noqa: BLE001
        out["queue_pending"] = False
    limited = activity == "limit"
    if not limited and activity == "idle":
        try:
            limited = srv._session_limited_until(title) > now
        except Exception:  # noqa: BLE001
            limited = False
    out["limited"] = limited
    try:
        out["background_waiting"] = activity == "idle" and any(
            j.get("state") == "waiting"
            for j in (srv._background_jobs(inst, title) or [])
        )
    except Exception:  # noqa: BLE001
        out["background_waiting"] = False
    try:
        out["worked_at"] = float(srv._agent_state.worked_at(title) or 0.0)
    except Exception:  # noqa: BLE001
        out["worked_at"] = 0.0
    with _EV_LOCK:
        out["turn_ended_at"] = _TURN_ENDED.get(title, 0.0)
    return out


def _ship_git_obs(title: str) -> dict:
    """``{stage, pr_url, ahead}``: the session's git stage — the one the lane
    climbs by (``agent_state._session_stage``) — and its commits ahead of the
    base, for a Ship stop the agent may already have done (blocking; ``{}``
    when unreadable)."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return {}
    try:
        st = srv._session_stage(inst) or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {"stage": str(st.get("stage") or ""), "pr_url": str(st.get("pr_url") or "")}
    if out["stage"] in _flights.STAGE_RUNG:
        try:
            wt = inst.GetWorktreePath()
            out["ahead"] = int(
                srv._commits_beyond_base(wt, srv._session_base_branch(inst)) or 0
            )
        except Exception:  # noqa: BLE001 — the receipt just omits the count
            pass
    return out


def _repo_root(title: str) -> str:
    inst = _server().ENGINE.instances.get(title)
    if inst is None:
        return ""
    try:
        root = inst.GetGitWorktree().GetRepoPath() or ""
    except Exception:  # noqa: BLE001 — raises before Start finished
        root = ""
    if not root:
        try:
            root = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            root = ""
    return root


def _verify_cfg(flight: dict) -> dict:
    """The current verify stop's config (``{}`` when the cursor is on none)."""
    step = _flights._current(flight) if flight.get("steps") else None
    if step is None or step.get("kind") != "verify":
        return {}
    return step.get("config") or {}


def _verify_env(flight: dict, where: str) -> Tuple[Optional[dict], bool]:
    """``(env, missing)`` for a verify stop's ``where`` (blocking): the
    repo's environment of that name, or ``(None, False)`` for "branch" or
    when the environments could not be read, ``(None, True)`` when the repo
    has no environment of that name."""
    from backend.web.core import test_plans as _tp

    if not where or where == _flights.VERIFY_WHERE_BRANCH:
        return None, False
    repo = flight.get("repo_root") or _repo_root(flight["session"])
    try:
        env = _tp.environment(repo, where)
    except Exception as err:  # noqa: BLE001 — unknown is not "no such env"
        _warn("flight %s: environments unavailable: %v", flight.get("id"), err)
        return None, False
    if env is None:
        # The implicit "live" falls back to the checklist's own live mark.
        return None, where != "live"
    return dict(env), False


def _verify_walk(cfg: Optional[dict]) -> str:
    """The run ``walk`` a Verify stop asks for: who "agent" ("Agent only")
    walks ALL steps — the agent attempts a person's too, and only what it
    truly cannot do (``needs you:`` / blocked) parks on the person; "both"
    and "you" keep "agent" (the person's steps stay theirs)."""
    return "all" if str((cfg or {}).get("who") or "both") == "agent" else "agent"


def _verify_obs(flight: dict, ss: dict, sess: dict) -> dict:
    """The behaviors' facts for a verify step (blocking): the checklist, plus
    ``env`` (the stop's environment, None for "branch") and, for ``ready``
    "on_branch", ``env_reached_at`` (0 until the plan's commit is on the
    environment's branch)."""
    from backend.web.core import test_plans as _tp

    cfg = _verify_cfg(flight)
    where = str(cfg.get("where") or _flights.VERIFY_WHERE_BRANCH)
    env, missing = _verify_env(flight, where) if cfg else (None, False)
    if missing:
        return {"env_missing": where}
    title = flight["session"]
    pid = str(ss["detail"].get("plan") or "")
    if not pid:
        repo = flight["repo_root"] or _repo_root(title)
        branch = sess.get("branch") or flight["branch"]
        if not branch:
            return {}
        pid = _tp.owner_for_branch(title, repo, branch)
    if not pid:
        return {}  # no check yet: the stop makes one (_ensure_checklist)
    plan = _tp.get(pid)
    if plan is None:
        # The stop's check is gone (deleted, or its record lost): say so —
        # an empty observation read as "getting the check ready" for an hour.
        return {"plan_gone": pid}
    reached = 0.0
    if env is not None and cfg.get("ready") == "on_branch":
        try:
            reached = float(_tp.env_reached_at(pid, env) or 0.0)
        except Exception as err:  # noqa: BLE001 — not reached (yet)
            _warn("flight %s: env reach unknown: %v", flight.get("id"), err)
            reached = 0.0
    run = plan["runs"][-1] if plan.get("runs") else None
    results = (run or {}).get("results") or {}
    try:
        progress = _tp.run_progress(plan) if run else {}
    except Exception:  # noqa: BLE001 — progress is a nicety, never a failure
        progress = {}
    checker: dict = {}
    run_session = str(plan.get("run_session") or "")
    in_session = bool((run or {}).get("in_session")) and bool(run_session)
    if plan.get("state") == "running" and run_session:
        try:
            facts = _session_obs({"session": run_session}, time.time(), True)
            checker = {
                "activity": facts.get("activity") or "",
                "background_waiting": bool(facts.get("background_waiting")),
                "present": bool(facts.get("present")),
            }
        except Exception:  # noqa: BLE001
            checker = {}
        if in_session and progress:
            progress = _walk_clock(pid, run_session, progress, checker)
    elif pid:
        _WALK_QUEUED_AT.pop(pid, None)
    # The steps the AGENT walks: its own, or — for an "Agent only" stop, whose
    # run walks "all" — every step, so a plan of person's steps still starts
    # the walker and the stop is done only once each one is settled.
    left = _agent_left(plan)
    walks = {s["id"] for s in _tp.agent_steps(plan, walk=_verify_walk(cfg))}
    open_agent = [
        s["text"]
        for s in plan["steps"]
        if s["id"] in walks and not (results.get(s["id"]) or {}).get("result")
    ]
    return {
        "plan": pid,
        "branch": plan.get("branch") or "",
        "state": plan.get("state") or "",
        "error": plan.get("error") or "",
        "steps": [s["id"] for s in plan["steps"]],
        "answered": sum(
            1 for s in plan["steps"] if (results.get(s["id"]) or {}).get("result")
        ),
        "settled": _tp.is_settled(plan),
        "open_human": [s["text"] for s in _tp.open_human_steps(plan)],
        "open_agent": open_agent,
        # Each failed step with what was seen ("— saw: …"), so the halted
        # stop and the agent's fix round both know what went wrong.
        "failed": [_failed_line(f) for f in _tp.failed_steps(plan)],
        # When the newest of those failures was answered: older than the
        # stop's attempt (a Retry, the agent's fix) = check again first.
        "failed_at": max(
            [
                float((results.get(s["id"]) or {}).get("at") or 0.0)
                for s in plan["steps"]
                if (results.get(s["id"]) or {}).get("result") == "fail"
            ]
            or [0.0]
        ),
        "live": plan.get("state") in ("due", "done") or bool(plan.get("live_at")),
        "live_branch": plan.get("live_branch") or "",
        "env": env,
        "env_reached_at": reached,
        # What keeps the stop from being silently stuck (flights._verify_guard
        # / _judge_verify): the newest run's progress, when it was opened,
        # the checker session's own state, and the steps the agent handed to
        # the person ("needs you: …" vs plain blocked).
        "progress": progress,
        "latest_run_at": float(run.get("at") or 0.0) if run else 0.0,
        "checker": checker,
        # The run is walked by the session that wrote the work (the run
        # route's in-session path): which one, for "Walking in <session>".
        "in_session": in_session,
        "walk_session": run_session if in_session else "",
        "needs_you": progress.get("needs_you") or [],
        "blocked_steps": progress.get("blocked") or [],
        "run_budget_s": float(getattr(_tp, "RUN_BUDGET_MIN", 30)) * 60.0,
        # SELF-VERIFY's verdict on the newest run (test_plans.OUTCOMES) and
        # what a person is asked to check before confirming a needs_you.
        "outcome": str((run or {}).get("outcome") or ""),
        "outcome_note": str((run or {}).get("outcome_note") or ""),
        "confirm_items": [
            _confirm_line(s, results.get(s["id"]) or {}, left.get(s["id"]))
            for s in _tp.confirm_steps(plan)
        ],
    }


#: How much of a step's evidence note a one-line face carries.
_EVIDENCE_FACE = 200


def _one_line(text, limit: int = _EVIDENCE_FACE) -> str:
    """``text`` folded onto one line, clipped to ``limit`` with "…"."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _failed_line(f: dict) -> str:
    """A failed step for the stop's reason: its text, and what was seen."""
    text = str((f.get("step") or {}).get("text") or "")
    saw = _one_line(f.get("note"))
    return "%s — saw: %s" % (text, saw) if saw else text


def _agent_left(plan: dict) -> Dict[str, str]:
    """``{step id: why}`` for the steps the agent left to the person, from
    ``test_plans.verify_status(plan)["human_steps"][i]["why"]`` ("agent
    couldn't: <note>") — ``{}`` when that is not there to read."""
    from backend.web.core import test_plans as _tp

    status_of = getattr(_tp, "verify_status", None)
    if status_of is None:
        return {}
    try:
        steps = (status_of(plan) or {}).get("human_steps") or []
    except Exception:  # noqa: BLE001 — the result's own note is the fallback
        return {}
    return {
        str(h.get("id") or ""): _one_line(h.get("why"))
        for h in steps
        if isinstance(h, dict) and h.get("why")
    }


def _confirm_line(step: dict, entry: dict, left: Optional[str] = None) -> str:
    """One thing a person is asked to check before confirming: the step, what
    to look for, and — for a step the agent could not do — why not
    (``left``: Verify's own "agent couldn't: …" for it; else the result's
    ``why``, else its note)."""
    line = step["text"]
    if step.get("expect"):
        line = "%s — look for: %s" % (line, step["expect"])
    if left and not entry.get("why"):
        # One wording on every surface: "the agent couldn't: …".
        if left.startswith("agent couldn't"):
            left = "the " + left
        return "%s — %s" % (line, left)
    if entry.get("result") == "blocked" and str(entry.get("by") or "") != "human":
        why = _one_line(entry.get("why") or entry.get("note"))
        if why:
            line = "%s — the agent couldn't: %s" % (line, why)
    return line


async def _pr_of(sess: dict, flight: dict):
    """The session's PR as an ingestion ``PullRequest`` + its repo slug, or
    ``(None, "")``."""
    from backend.ticket_ingestion.models import PullRequest
    from backend.web.core import github_pr as _gh

    wt, branch = sess.get("folder") or "", sess.get("branch") or ""
    if not wt or not branch:
        return None, ""
    ref = await asyncio.to_thread(_gh.repo_ref, wt)
    found = await _gh.find_pr(wt, branch)
    if ref is None or not found or not found.get("number"):
        return None, ""
    head = ""
    try:
        head = await asyncio.to_thread(_server()._git_head_sha, wt) or ""
    except Exception:  # noqa: BLE001
        head = ""
    pr = PullRequest(
        number=int(found["number"]),
        head_ref=branch,
        head_sha=head,
        base_ref=str(found.get("base") or ""),
        title=flight["subject"].get("title") or branch,
        url=str(found.get("url") or sess.get("pr_url") or ""),
        author="",
        created_at=_dt.datetime.now(_dt.timezone.utc),
        repo=ref.slug,
    )
    return pr, ref.slug


def _is_bot(author: str) -> bool:
    a = str(author or "").lower()
    return a.endswith("[bot]") or "coderabbit" in a


def _is_reviewer(author: str, who: str) -> bool:
    """``author`` is the ``require_reviewer`` named ``who`` — a
    case-insensitive substring, as :func:`_is_bot` matches ("coderabbit"
    matches ``coderabbitai`` and ``coderabbitai[bot]``)."""
    who = str(who or "").strip().lower()
    return bool(who) and who in str(author or "").lower()


async def _reviewer_posted(pr, who: str) -> bool:
    """Has ``who`` posted on ``pr`` at all: a review in any state (an
    approval, a comment-only review, "changes requested" — every inline
    comment belongs to one) or a conversation comment (where a bot such as
    CodeRabbit posts its summary)? Raises when GitHub can't be asked."""
    import aiohttp

    from backend.ticket_ingestion.github_auth import resolve_token
    from backend.web.core import github_pr as _gh

    token = await resolve_token(_gh._github_config())
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    api = "https://api.github.com/repos/%s" % pr.repo
    urls = (
        "%s/pulls/%d/reviews" % (api, pr.number),
        "%s/issues/%d/comments" % (api, pr.number),
    )
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for url in urls:
            async with http.get(
                url, params={"per_page": "100"}, headers=headers
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError("%s returned %d" % (url, resp.status))
                data = await resp.json()
            for row in data if isinstance(data, list) else []:
                user = (row.get("user") or {}) if isinstance(row, dict) else {}
                if _is_reviewer(str(user.get("login") or ""), who):
                    return True
    return False


async def _reviews_obs(flight: dict, step: dict, sess: dict, now: float) -> dict:
    """``{"ids"}`` of the PR's actionable comments (None = could not ask),
    fetched at most once a minute per flight. With ``require_reviewer`` (and
    until the stop has seen it post), also ``reviewer``: whether it has
    posted on the PR (None = could not ask)."""
    from backend.ticket_ingestion import pr_comments as _prc
    from backend.web.core import github_pr as _gh

    who = str(step["config"].get("require_reviewer") or "")
    d = ((flight.get("step_states") or {}).get(step["id"]) or {}).get("detail") or {}
    ask = bool(who) and not d.get("reviewer_seen")
    cache = _REVIEWS.get(flight["id"])
    if cache and now - cache["at"] < _REVIEWS_EVERY_S:
        out = {"ids": cache["ids"]}
        if ask:
            out["reviewer"] = cache.get("reviewer")
        return out
    ids = None
    comments: Dict[str, object] = {}
    pr = None
    posted: Optional[bool] = None
    try:
        pr, _slug = await _pr_of(sess, flight)
        if pr is not None:
            got = await _prc.fetch_actionable_comments(pr, _gh._github_config())
            for c in got:
                if ask and _is_reviewer(c.author, who):
                    posted = True
                if not step["config"].get("include_bots", True) and _is_bot(c.author):
                    continue
                comments[str(c.id)] = c
            ids = list(comments)
    except Exception as err:  # noqa: BLE001 — "could not ask" is not "no comments"
        _warn("flight %s: review comments unavailable: %v", flight["id"], err)
        ids = None
    if ask and pr is not None and posted is None:
        try:
            posted = await _reviewer_posted(pr, who)
        except Exception as err:  # noqa: BLE001 — unknown, not "never posted"
            _log("flight %s: %s's reviews unavailable: %v", flight["id"], who, err)
            posted = None
    _REVIEWS[flight["id"]] = {
        "at": now,
        "ids": ids,
        "comments": comments,
        "pr": pr,
        "reviewer": posted,
    }
    out = {"ids": ids}
    if ask:
        out["reviewer"] = posted
    return out


async def _checks_obs(flight: dict, sess: dict, now: float) -> dict:
    from backend.web.core import github_pr as _gh

    cache = _CHECKS.get(flight["id"])
    if cache and now - cache["at"] < _CHECKS_EVERY_S:
        return cache
    wt, branch = sess.get("folder") or "", sess.get("branch") or ""
    verdict, failing, why = "unknown", [], ""
    if not wt:
        why = "the session has no folder"
    elif not branch:
        why = "the session is not on a branch"
    else:
        try:
            verdict, why = await _gh.pr_checks_why(wt, branch)
            if verdict == "failed":
                failing = await _failing_checks(wt, branch)
        except Exception as err:  # noqa: BLE001 — unknown, saying why
            verdict, why = "unknown", "couldn't ask GitHub: %s" % _error_line(err)
    out = {"at": now, "verdict": verdict, "failing": failing}
    if verdict == "unknown":
        # flights._judge_checks fails the stop with this after a while of
        # unknown, instead of "could not read the checks yet" for good.
        out["why"] = why or "GitHub gave no answer"
    _CHECKS[flight["id"]] = out
    return out


async def _failing_checks(wt: str, branch: str) -> List[str]:
    """Names of the failing check runs on ``branch`` (best effort)."""
    from backend.web.core import github_pr as _gh

    ref = await asyncio.to_thread(_gh.repo_ref, wt)
    token = await _gh.api_token()
    if ref is None or not token:
        return []
    status, data = await _gh._request(
        "GET",
        "/repos/{}/commits/{}/check-runs".format(ref.slug, branch),
        token=token,
        params={"per_page": "100"},
    )
    if status != 200 or not isinstance(data, dict):
        return []
    bad = ("failure", "timed_out", "cancelled", "action_required", "startup_failure")
    return [
        str(r.get("name") or "")
        for r in data.get("check_runs") or []
        if isinstance(r, dict) and str(r.get("conclusion") or "") in bad
    ][:12]


# --- ``when``: the facts a pending step's condition reads ---------------------
#: (flight id, HEAD sha + working-tree fingerprint) -> the diff facts.
_WHEN_DIFF: Dict[Tuple[str, str], dict] = {}
_WHEN_DIFF_KEEP = 64
#: flight id -> {at, labels}: the PR / issue labels, asked at most once a minute.
_WHEN_LABELS: Dict[str, dict] = {}
_WHEN_LABELS_EVERY_S = 60.0
_UNTRACKED_MAX = 200


def _git_out(folder: str, *args: str, timeout: float = 30.0) -> str:
    """Stripped stdout of ``git -C folder …``, "" on any failure."""
    try:
        cp = subprocess.run(
            ["git", "-C", folder, "--no-pager", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def _ref_ok(folder: str, ref: str) -> bool:
    return bool(ref) and bool(
        _git_out(folder, "rev-parse", "--verify", "-q", ref + "^{commit}")
    )


def diff_base(flight: dict, folder: str) -> str:
    """The ref a flight's diff is measured against: the session's base branch
    when the row knows one (the server's ``_session_base_branch``: recorded at
    creation, else ``origin/HEAD``), else ``origin/HEAD``'s target, falling
    back to main / master — the local branch first, its remote-tracking twin
    second. "" when nothing verifies (no diff facts then)."""
    if not folder or not os.path.isdir(folder):
        return ""
    srv = _server()
    inst = None
    try:
        inst = srv.ENGINE.instances.get(flight.get("session") or "")
    except Exception:  # noqa: BLE001
        inst = None
    base = ""
    if inst is not None:
        try:
            base = str(srv._session_base_branch(inst) or "")
        except Exception:  # noqa: BLE001 — an older server, a fake
            base = str(getattr(inst, "BaseBranch", "") or "")
    if not base:
        head = _git_out(
            folder, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"
        )
        base = head[len("origin/") :] if head.startswith("origin/") else head
    cands: List[str] = []
    for name in ([base] if base else []) + ["main", "master"]:
        if name.startswith("origin/"):
            cands += [name, name[len("origin/") :]]
        else:
            cands += [name, "origin/" + name]
    for ref in cands:
        if _ref_ok(folder, ref):
            return ref
    return ""


def when_diff(flight: dict, folder: str) -> dict:
    """``{files, added, removed, base, head}`` of the session's work against
    :func:`diff_base` — the working tree included, so a condition before the
    first commit still sees the agent's edits (plus untracked files, counted
    by their lines). Blocking; cached per (flight, HEAD sha + a fingerprint
    of ``git status``) so a pass costs one ``git status`` once it is warm."""
    empty = {"files": [], "added": 0, "removed": 0, "base": "", "head": ""}
    if not folder or not os.path.isdir(folder):
        return dict(empty)
    head = _git_out(folder, "rev-parse", "HEAD")
    if not head:
        return dict(empty)
    status = _git_out(folder, "status", "--porcelain", "--untracked-files=all")
    key = (
        flight["id"],
        head + ":" + hashlib.sha1(status.encode("utf-8")).hexdigest()[:12],
    )
    hit = _WHEN_DIFF.get(key)
    if hit is not None:
        return dict(hit, files=list(hit["files"]))
    base = diff_base(flight, folder)
    files: List[str] = []
    added = removed = 0
    if base:
        since = _git_out(folder, "merge-base", base, "HEAD") or base
        for line in _git_out(folder, "diff", "--numstat", since).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files.append("\t".join(parts[2:]))
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
        for name in _git_out(
            folder, "ls-files", "--others", "--exclude-standard"
        ).splitlines()[:_UNTRACKED_MAX]:
            if not name or name in files:
                continue
            files.append(name)
            try:
                with open(os.path.join(folder, name), "rb") as fh:
                    added += fh.read(1 << 20).count(b"\n")
            except OSError:
                pass
    rec = {
        "files": files,
        "added": added,
        "removed": removed,
        "base": base,
        "head": head,
    }
    if len(_WHEN_DIFF) >= _WHEN_DIFF_KEEP:
        for k in [k for k in _WHEN_DIFF if k[0] == flight["id"]] or [
            next(iter(_WHEN_DIFF))
        ]:
            _WHEN_DIFF.pop(k, None)
    _WHEN_DIFF[key] = rec
    return dict(rec, files=list(files))


def diff_stat_text(diff: dict, limit: int = 4000) -> str:
    """``when_diff``'s facts as the lines a model is shown: ``path (+a -r)``
    per file, the totals last."""
    lines = ["%s" % f for f in diff.get("files") or []]
    text = "\n".join(lines)[:limit]
    return "%s\n%d files, +%d -%d" % (
        text or "(no changed files)",
        len(diff.get("files") or []),
        int(diff.get("added") or 0),
        int(diff.get("removed") or 0),
    )


async def _when_labels(flight: dict, sess: dict, now: float) -> List[str]:
    """The labels on the flight's PR (found from the branch) or on its
    subject issue / PR; ``[]`` without one, a token or an answer."""
    from backend.web.core import github_pr as _gh

    cache = _WHEN_LABELS.get(flight["id"])
    if cache and now - cache["at"] < _WHEN_LABELS_EVERY_S:
        return list(cache["labels"])
    labels: List[str] = []
    try:
        number, slug = 0, ""
        subj = flight.get("subject") or {}
        folder = sess.get("folder") or ""
        if subj.get("kind") in ("pr", "issue") and str(subj.get("id") or "").isdigit():
            number = int(subj["id"])
            ref = await asyncio.to_thread(_gh.repo_ref, folder) if folder else None
            slug = ref.slug if ref is not None else ""
        if not (number and slug):
            pr, slug = await _pr_of(sess, flight)
            number = int(pr.number) if pr is not None else 0
        token = await _gh.api_token() if number and slug else ""
        if token:
            status, data = await _gh._request(
                "GET",
                "/repos/{}/issues/{}/labels".format(slug, number),
                token=token,
                params={"per_page": "100"},
            )
            if status == 200 and isinstance(data, list):
                labels = [
                    str(d.get("name"))
                    for d in data
                    if isinstance(d, dict) and d.get("name")
                ]
    except Exception as err:  # noqa: BLE001 — no labels, the condition says so
        _warn("flight %s: labels unavailable: %v", flight["id"], err)
        labels = []
    _WHEN_LABELS[flight["id"]] = {"at": now, "labels": labels}
    return list(labels)


def when_judges(flight: dict, step: dict) -> Tuple[Dict[str, str], Optional[str]]:
    """``({sid: value} of every done judge stop, the nearest earlier one's
    value or None)`` for ``step``."""
    judges: Dict[str, str] = {}
    last: Optional[str] = None
    for s in flight.get("steps") or []:
        if s.get("kind") != "judge":
            continue
        ss = (flight.get("step_states") or {}).get(s["id"]) or {}
        value = (ss.get("detail") or {}).get("value")
        if ss.get("state") == "done" and value not in (None, ""):
            judges[s["id"]] = str(value)
    for s in flight.get("steps") or []:
        if s["id"] == step["id"]:
            break
        if s["id"] in judges:
            last = judges[s["id"]]
    return judges, last


async def when_facts(flight: dict, step: dict, sess: dict, now: float) -> dict:
    """``obs["facts"]`` for a pending step with a ``when``: only the groups
    its condition reads (:func:`flight_when.needs`) — ``diff`` (git in the
    session's folder), ``labels`` (GitHub), ``clock`` (the server's local
    time), ``branch``, ``judge`` (the flight's done judge stops). A group
    that cannot be gathered reads as empty: the condition decides, the
    flight never wedges on it."""
    try:
        cond = _when.parse(step.get("when") or "")
    except _when.WhenError:
        return {}
    need = _when.needs(cond)
    facts: dict = {}
    if "clock" in need:
        t = time.localtime(now)
        facts["hour"] = t.tm_hour
        facts["weekday"] = t.tm_wday
    if "branch" in need:
        facts["branch"] = sess.get("branch") or flight.get("branch") or ""
    if "diff" in need:
        try:
            d = await asyncio.to_thread(when_diff, flight, sess.get("folder") or "")
        except Exception as err:  # noqa: BLE001
            _warn("flight %s: diff facts unavailable: %v", flight["id"], err)
            d = {"files": [], "added": 0, "removed": 0}
        facts["files"] = d["files"]
        facts["added"] = d["added"]
        facts["removed"] = d["removed"]
    if "labels" in need:
        facts["labels"] = await _when_labels(flight, sess, now)
    if "judge" in need:
        facts["judges"], facts["judge_last"] = when_judges(flight, step)
    return facts


async def observe(flight: dict, now: float) -> dict:
    """The observation :func:`flights.next_action` decides from."""
    step = _flights._current(flight)
    if step is None:
        return {"now": now}
    kind = step["kind"]
    ss = flight["step_states"][step["id"]]
    plug = _kinds.get(kind)
    agent = (
        kind in ("agent", "reviews")
        or ss["detail"].get("phase") == "fix"
        or bool(ss["detail"].get("after_turn"))
        or bool(plug is not None and plug.types_to_agent)
    )
    sess = await asyncio.to_thread(_session_obs, flight, now, agent)
    obs: dict = {"now": now, "session": sess}
    if kind == "ship" and ss["state"] in ("pending", "running") and sess.get("present"):
        # What git says is already done (the agent may have shipped itself).
        obs["git"] = await asyncio.to_thread(_ship_git_obs, flight["session"])
    if ss["state"] in ("pending", "running"):
        await _guard_obs(flight, sess, now, obs)
    if ss["state"] == "pending" and step.get("when"):
        obs["facts"] = await when_facts(flight, step, sess, now)
    if ss["state"] not in ("running", "waiting_you"):
        return obs
    if kind == "ship" or (kind == "reviews" and ss["detail"].get("phase") == "push"):
        obs["autopilot"] = await asyncio.to_thread(_autopilot.get, flight["session"])
    if kind == "verify":
        try:
            obs["verify"] = await asyncio.to_thread(_verify_obs, flight, ss, sess)
        except Exception as err:  # noqa: BLE001 — said, never "getting ready"
            _warn("flight %s: the check is unreadable: %v", flight["id"], err)
            obs["verify"] = {"error": "couldn't read the check: %s" % _one_line(err)}
    if (
        kind == "reviews"
        and sess.get("pr_url")
        and (ss["detail"].get("phase") or "watch") == "watch"
    ):
        obs["reviews"] = await _reviews_obs(flight, step, sess, now)
    if kind == "checks":
        obs["checks"] = await _checks_obs(flight, sess, now)
    if kind == "command":
        with _JOBS_LOCK:
            job = _JOBS.get((flight["id"], step["id"]))
            obs["command"] = dict(job) if job else None
    if kind == "join" or (kind == "agent" and step["config"].get("join", True)):
        try:
            obs["children"] = await asyncio.to_thread(_children_obs, flight, step)
            sess["messages_pending"] = await asyncio.to_thread(
                _messages_pending, flight["session"]
            )
        except Exception as err:  # noqa: BLE001 — unknown, never "no workers"
            _warn("flight %s: workers unreadable: %v", flight["id"], err)
            obs["children"] = None
    if kind in _LAUNCHED_WORKER and ss["state"] == "running":
        try:
            obs["launch"] = await asyncio.to_thread(_launch_obs, ss)
        except Exception as err:  # noqa: BLE001 — unknown: the kind judges alone
            _warn("flight %s: launch queue unreadable: %v", flight["id"], err)
    if plug is not None and plug.observe is not None:
        try:
            obs[kind] = await plug.observe(flight, step, ss, sess, now)
        except _StepFailed as err:
            obs[kind] = {"failed": str(err), "detail": dict(err.detail or {})}
        except Exception as err:  # noqa: BLE001 — unknown, the judge waits
            _warn("flight %s: %s unobservable: %v", flight["id"], kind, err)
    return obs


#: Kinds whose stop waits on a worker it spawned (``spawn_worker``, the
#: worker's title first in ``detail.children``) → what a person calls it.
_LAUNCHED_WORKER = {"review": "the reviewer"}


def _launch_obs(ss: dict) -> Optional[dict]:
    """Whether the worker a stop spawned ever started (blocking): ``{"title",
    "waiting": True, "why", "since"}`` while the launch gate still holds it
    (memory), ``{"title", "failed": <error>}`` when its queued launch failed
    or its background create failed after the 202 (the server's create
    failure record, newer than the stop's start), else None (it started, or
    nothing is known)."""
    from backend.web.core import launch_gate as _lg

    kids = (ss.get("detail") or {}).get("children") or []
    if not kids:
        return None
    title = str(kids[0])
    rec = _lg.waiting(title)
    if rec is not None:
        return {
            "title": title,
            "waiting": True,
            "why": str(rec.get("reason") or "") or "memory is low",
            "since": float(rec.get("since") or 0.0),
        }
    if title in _server().ENGINE.instances:
        return None
    done = _lg.last_outcome(title)
    if done and not done.get("started") and done.get("error"):
        return {"title": title, "failed": str(done["error"])}
    why = _create_failure(title, float(ss.get("since") or 0.0))
    if why:
        return {"title": title, "failed": why}
    return None


#: What a worker that vanished before it ever started is said to have done
#: when nothing recorded why.
GONE_BEFORE_START = "it was gone before it started"


def _never_started(step: dict, obs: dict, action: dict) -> bool:
    """Whether the stop's judge failed it over a worker that is gone without
    ever having started (its join entry never left ``loading``) and left no
    report: a create that died in the background, nothing it did."""
    if action.get("action") != "fail":
        return False
    o = obs.get(step["kind"])
    entry = (o.get("entry") if isinstance(o, dict) else None) or {}
    return (
        entry.get("state") == "gone"
        and not entry.get("started")
        and not entry.get("report")
        and not o.get("report")
    )


def _launch_verdict(flight: dict, obs: dict, action: dict, now: float) -> dict:
    """``action`` for a stop whose spawned worker never started: while the
    launch gate holds it, a wait saying why and for how long — a park on the
    person past :data:`MEMORY_PARK_S`. A launch or background create that
    failed (or a worker gone before it ever started) is never "did not
    finish" handed to the agent: the stop spawns ONE fresh worker (a new
    title: ``_free_worker_title`` skips one whose create just failed), and
    if that one cannot start either it halts for the person with "couldn't
    start the reviewer: <why>" — no fix round spent, nothing typed to the
    agent. Any other stop, or a worker that started, keeps ``action``."""
    step = _flights._current(flight)
    if step is None or step["kind"] not in _LAUNCHED_WORKER:
        return action
    ss = flight["step_states"][step["id"]]
    if ss["state"] != "running" or ss["detail"].get("phase") == "fix":
        return action
    launch = obs.get("launch")
    if not isinstance(launch, dict):
        if not _never_started(step, obs, action):
            return action
        launch = {"failed": GONE_BEFORE_START}
    what = _LAUNCHED_WORKER[step["kind"]]
    if launch.get("failed"):
        why = "couldn't start %s: %s" % (what, launch["failed"])
        if not ss["detail"].get("respawned"):
            # Once, automatically: a stale title or a passing hiccup.
            return {
                "action": "run",
                "note": why + " — starting another",
                "detail": {"respawned": why},
            }
        # The stop's own on_fail (skip still skips), with the real reason —
        # no "(after N fix rounds)": the agent's rounds are not why.
        out = dict(_flights.fail_action(flight, why, obs), reason=why)
        # Memory or a refused create is nothing the agent can fix.
        return dict(out, then="halt") if out.get("then") == "agent" else out
    if not launch.get("waiting") or action.get("action") not in ("wait", "fail"):
        return action
    since = float(launch.get("since") or 0.0) or float(ss.get("since") or now)
    return _memory_wait(what, str(launch.get("why") or ""), since, now, {})


async def _guard_obs(flight: dict, sess: dict, now: float, obs: dict) -> None:
    """``obs["guard"][sid]``: what each done guard step before the cursor
    (``Kind.guard``, a budget watching the flight) observes this pass, for
    :func:`flights.next_action` to ask its guard."""
    for gstep in flight["steps"][: flight["cursor"]]:
        gplug = _kinds.get(gstep["kind"])
        gss = flight["step_states"][gstep["id"]]
        if gplug is None or gplug.guard is None or gplug.observe is None:
            continue
        if gss["state"] != "done":
            continue
        guards = obs.setdefault("guard", {})
        try:
            guards[gstep["id"]] = await gplug.observe(flight, gstep, gss, sess, now)
        except _StepFailed as err:
            guards[gstep["id"]] = {"failed": str(err), "detail": dict(err.detail or {})}
        except Exception as err:  # noqa: BLE001 — unknown, the guard holds its fire
            _warn("flight %s: %s unobservable: %v", flight["id"], gstep["kind"], err)


def _messages_pending(title: str) -> bool:
    """Whether a message (a worker's report) still waits to be typed into
    ``title``."""
    from backend.web.core import mailbox as _mailbox

    return bool(title) and _mailbox.next_pending(title) is not None


def _epoch(inst) -> float:
    created = getattr(inst, "CreatedAt", None)
    try:
        return float(created.timestamp()) if created is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _children_obs(flight: dict, step: dict) -> List[dict]:
    """The facts a join judges each worker by (blocking): every live child of
    the flight's session (hidden helpers left out), or — a join of ``spawned``
    — the workers the split before it made, a deleted one as not present."""
    from backend.mcp import tools as _mcp_tools
    from backend.web.core import hidden_sessions as _hidden
    from backend.web.core import lineage as _lineage
    from backend.web.core import thread as _thread

    srv = _server()
    title = flight["session"]
    if not title:
        return []
    insts = srv.ENGINE.instances
    rows = {
        str(r.get("title") or ""): r
        for r in _events.sessions_snapshot()
        if isinstance(r, dict)
    }
    if step["kind"] == "join" and step["config"].get("of") == "spawned":
        titles = _flights.spawned_titles(flight)
    else:
        titles = _lineage.children_of(insts, title)
    out: List[dict] = []
    for t in titles:
        inst = insts.get(t)
        if inst is None:
            out.append({"title": t, "present": False})
            continue
        row = rows.get(t) or {}
        if _hidden.hidden_by(t) or row.get("hidden_by"):
            continue  # a Verify helper or another feature's own session
        status = str(row.get("status") or "")
        if not row:
            status = "loading"  # not published yet: it is starting
        queue = row.get("queue") if isinstance(row.get("queue"), dict) else {}
        order = row.get("order") if isinstance(row.get("order"), dict) else {}
        provider = str(row.get("provider") or "") or _mcp_tools._provider_of_program(
            str(row.get("program") or getattr(inst, "Program", "") or "")
        )
        try:
            report = _thread.last_report(insts, t)
        except Exception:  # noqa: BLE001
            report = None
        out.append(
            {
                "title": t,
                "present": True,
                "status": status,
                "activity": str(row.get("activity") or ""),
                "activity_since": row.get("activity_since") or 0.0,
                "sleeping": bool(row.get("sleeping")),
                "held": order.get("state") == "held",
                "queue_pending": int(queue.get("pending") or 0) > 0,
                "background_waiting": _mcp_tools.background_waiting(
                    row.get("background")
                ),
                "messages_pending": _messages_pending(t),
                "created": _epoch(inst),
                "settle_s": _flights.child_settle_s(provider),
                "report": report,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
def _deliver(title: str, text: str) -> None:
    """A prompt to a live session through its queue (the drain respects dialogs
    and idle), waking a sleeping agent first."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        raise _StepFailed("the session is gone")
    try:
        from backend.web.core import sleep as _sleep

        _woke, err = _sleep.ensure_awake(inst, title, "a flight step", srv)
        if err:
            raise _StepFailed(str(err))
    except _StepFailed:
        raise
    except Exception:  # noqa: BLE001 — waking is best effort; the drain retries
        pass
    try:
        _prompt_queue.enqueue(title, text)
        _prompt_queue.set_flags(title, enabled=True, held=False)
    except ValueError as err:
        raise _StepFailed(str(err))


def _session_payload(plan_session: dict, repo_path: str, prompt: str) -> dict:
    payload = {"repo_path": repo_path, "prompt": prompt}
    if plan_session.get("program"):
        payload["program"] = plan_session["program"]
    if plan_session.get("model"):
        payload["profile_model"] = plan_session["model"]
    if plan_session.get("in_place"):
        payload["in_place"] = True
    return payload


#: What a client names "no session — the default folder" by (``attach
#: {title}``); ``attach {folder}`` names another folder.
DEFAULT_SESSION = "@default"


#: MindFlock's own default folder (the owner: "a folder like mindflock
#: pastes, created for this specific purpose"): where a chain runs when no
#: session is picked. A git repo so a session made there can commit.
DEFAULT_PLACE = "~/.mindflock/default"


def _ensure_default_place() -> str:
    """``~/.mindflock/default``, made (``git init`` + one empty commit) the
    first time; never touched after."""
    import os
    import subprocess

    folder = os.path.expanduser(DEFAULT_PLACE)
    os.makedirs(folder, exist_ok=True)
    if not os.path.isdir(os.path.join(folder, ".git")):
        readme = os.path.join(folder, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write(
                    "# MindFlock's default place\n\n"
                    "Routes run here when no session is picked.\n"
                )
        git = [
            "git",
            "-c",
            "user.name=MindFlock",
            "-c",
            "user.email=mindflock@localhost",
        ]
        try:
            subprocess.run(["git", "init", "-q", folder], check=True, timeout=30)
            subprocess.run(
                git + ["-C", folder, "add", "README.md"], check=True, timeout=30
            )
            subprocess.run(
                git + ["-C", folder, "commit", "-q", "-m", "MindFlock default place"],
                check=True,
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            pass  # a plain folder still works for agents
    return folder


def _default_folder() -> str:
    """Where a chain with no session picked runs: the ``general.default_folder``
    setting when it names a folder, else MindFlock's own place
    (:data:`DEFAULT_PLACE`, made on first use)."""
    import os

    from backend.config.settings import load_settings

    try:
        folder = str(load_settings().general.default_folder or "").strip()
    except Exception:  # noqa: BLE001
        folder = ""
    if folder and os.path.isdir(os.path.expanduser(folder)):
        return os.path.expanduser(folder)
    return _ensure_default_place()


def needs_agent(flight: dict) -> bool:
    """Whether a chain types into an agent: an agent / reviews stop, a verify
    the agent takes part in, a plug-in that types to the agent, or a Loop that
    repeats the session's prompt or goes back to the session's agent
    (``back_to: "agent"``). A perch, a wait, a notify… need none."""
    from backend.web.core import flight_kinds as _kinds

    steps = flight.get("steps") or []
    for i, st in enumerate(steps):
        k = st.get("kind")
        cfg = st.get("config") or {}
        if k in _flights.AGENT_KINDS:
            return True
        if k == "verify" and str(cfg.get("who") or "both") != "you":
            return True
        plug = _kinds.get(k)
        if plug is not None and getattr(plug, "types_to_agent", False):
            return True
        if k == "loop" and _flights.loops_to_agent(st):
            return True
        if (
            k == "loop"
            and not str(cfg.get("back_to") or "")
            and all(x.get("kind") == "loop" for x in steps[:i])
        ):
            return True
    return False


def needs_session(flight: dict) -> bool:
    """Whether a chain needs a session at all: an agent (:func:`needs_agent`)
    or a stop that works on a session (a ship lane, a verify, checks, a
    split…). A chain of perches, waits, notifies, commands and Loops runs on
    its own in a folder — like an n8n workflow, nothing is started for it."""
    if needs_agent(flight):
        return True
    for st in flight.get("steps") or []:
        k = st.get("kind")
        if k in _flights.SESSION_KINDS and k not in _flights.FOLDER_KINDS:
            return True
    return False


async def _create_session(payload: dict) -> str:
    """Create a flight's session → its title. An automatic start, so it asks
    the launch gate (core.launch_gate): when memory is short the create waits
    in its queue and the title is a pending row until then — the flight reads
    it as a session still provisioning, as it does an intake start."""
    from backend.web.core import session_create as _session_create

    status, body = await _session_create.create_result(payload, auto="flight")
    if status >= 400:
        raise _flights.FlightError(
            str(body.get("error") or "could not create the session"), status
        )
    title = str(body.get("title") or "")
    if not title:
        raise _flights.FlightError("could not create the session", 500)
    return title


async def _run_agent(flight: dict, step: dict, action: dict, now: float) -> dict:
    text = _flights.fill_prompt(step["config"].get("prompt") or "", flight)
    if step["config"].get("fresh") and not action.get("retry"):
        plan = _flights.get_plan(flight["plan_id"]) or {}
        repo = flight["repo_root"] or (plan.get("session") or {}).get("repo_path") or ""
        if not repo:
            raise _StepFailed("no repo to start a new session in")
        payload = _session_payload(plan.get("session") or {}, repo, text)
        # "Switch pilot": the stop's own CLI / model over the plan's.
        if step["config"].get("program"):
            payload["program"] = step["config"]["program"]
        if step["config"].get("model"):
            payload["profile_model"] = step["config"]["model"]
        try:
            title = await _create_session(payload)
        except _flights.FlightError as err:
            raise _StepFailed(err.error)
        return dict(
            action, session=title, detail={"fresh": True, "no_prompt": not text}
        )
    if not text:
        # Nothing to say: the step is "let the current turn settle".
        return dict(action, detail={"no_prompt": True})
    await asyncio.to_thread(_deliver, flight["session"], text)
    return dict(action, detail={"sent": True})


def _arm(flight: dict, lane: str, ask_first: bool = False, message: str = "") -> dict:
    """Arm the session's lane for a ship step (blocking). Raises
    :class:`_StepFailed` with the same refusals the ``/fast-track`` route
    gives (no git, a peer session, a shared folder, a copy window)."""
    from backend.web.core import lanes as _lanes

    srv = _server()
    title = flight["session"]
    if not srv.git_available():
        raise _StepFailed("git is not installed")
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        raise _StepFailed("the session is gone")
    try:
        if srv._peer_guard.refusal(inst, "autopilot") is not None:
            raise _StepFailed("a peer's session can't ship by itself from here")
    except _StepFailed:
        raise
    except Exception:  # noqa: BLE001
        pass
    refused = srv._lane_refusal(title, lane)
    if refused is not None:
        import json as _json

        try:
            msg = _json.loads(refused.body).get("error") or "the lane was refused"
        except Exception:  # noqa: BLE001
            msg = "the lane was refused"
        raise _StepFailed(msg)
    try:
        rec = _lanes.arm_session(
            title,
            lane,
            ask_first=ask_first,
            message=message or flight["subject"].get("title") or "",
            source="flight",
            item=flight["id"],
            require_workspace=False,
        )
    except _lanes.LaneError as err:
        raise _StepFailed(err.message)
    try:
        srv._publish_autopilot(title)
    except Exception:  # noqa: BLE001
        pass
    return {"armed_at": float((rec or {}).get("started") or 0.0)}


def _ensure_checklist(flight: dict) -> dict:
    """Find or make the session's plan for its SELF-VERIFY (blocking) →
    ``{"plan": id}``. Nothing is generated: the agent writes its own steps
    once the check starts (the judge starts it next). An environment the repo
    does not have fails the stop, and so does a session with no plan to be
    had (no workspace, not on a branch)."""
    srv = _server()
    title = flight["session"]
    cfg = _verify_cfg(flight)
    where = str(cfg.get("where") or _flights.VERIFY_WHERE_BRANCH)
    if _verify_env(flight, where)[1]:
        raise _StepFailed("no environment named %s in this repo" % where)
    try:
        pid, why = srv._ensure_verify_plan(title)
    except Exception as err:  # noqa: BLE001
        raise _StepFailed("could not set up the check: %s" % err)
    if not pid:
        raise _StepFailed("could not set up the check: %s" % why)
    return {"plan": pid}


#: plan id → when its in-session walk was last seen still waiting on the
#: session's prompt queue (``_walk_clock``). In memory: a restart falls back
#: to the run's own start, which only makes the clock stricter.
_WALK_QUEUED_AT: Dict[str, float] = {}


def _walk_clock(pid: str, title: str, progress: dict, checker: dict) -> dict:
    """``progress`` for an in-session walk, with its clock started when the
    session's agent PICKED THE WALK UP — not when it was queued.

    A walk handed to a session mid-turn waits on its prompt queue until the
    turn ends; that wait is the person's own work, not a stalled check, so
    while the walk is queued the stall/budget clock reads "just started"
    (and the session ``queued``), and afterwards it runs from the last
    moment it was seen queued."""
    from backend.web.core import test_plans_driver as _tpd

    now = time.time()
    if _tpd.walk_is_queued(title):
        _WALK_QUEUED_AT[pid] = now
        checker["activity"] = "queued"
        return dict(progress, started_at=now, progress_at=now, queued=True)
    since = float(_WALK_QUEUED_AT.get(pid) or 0.0)
    started = float(progress.get("started_at") or 0.0)
    if since <= started:
        return progress
    out = dict(progress, started_at=since)
    if float(progress.get("progress_at") or 0.0) < since:
        out["progress_at"] = since
    return out


async def _start_verify_run(
    plan_id: str,
    where: str = "branch",
    notes: str = "",
    walk: str = "agent",
    confirm: bool = False,
) -> dict:
    """``POST /api/test-plans/{id}/run`` in process (its body is the route):
    checked on ``where`` (this branch or an environment), with ``notes``,
    walking ``walk`` ("agent" = the agent's steps; "all" = a who:"agent" stop,
    where the walker attempts the person's steps too) →
    ``{"started": bool, "reason": str, "detail": str}``.

    THE ANSWER IS READ, never assumed. Every 409 used to count as "already
    running", so a check the memory guard refused read as started and the stop
    waited forever on a run that did not exist (sc-25811, 2026-11-09). Now:
    202 or ``busy`` (a run IS in progress) → started; ``memory`` → not
    started, the stop waits and retries with the reason on screen; anything
    else → :class:`_StepFailed` with the server's sentence."""
    import json as _json

    srv = _server()
    resp = await srv.run_test_plan(
        plan_id,
        None,
        {
            "environment": where or "branch",
            "notes": notes or "",
            "walk": walk or "agent",
            **({"confirm": True} if confirm else {}),
        },
    )
    try:
        body = _json.loads(resp.body)
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code < 400:
        # IN THE SESSION when it is still open: the walk was handed to the
        # flight's own session as a prompt — nothing created, no memory gate.
        return {
            "started": True,
            "reason": "",
            "detail": "",
            "in_session": bool(body.get("in_session")),
            "session": str(body.get("session") or ""),
            "queued": bool(body.get("queued")),
        }
    reason = str(body.get("reason") or "")
    if resp.status_code == 409 and (reason == "busy" or body.get("session")):
        return {"started": True, "reason": "busy", "detail": ""}
    if reason == "memory":
        return {
            "started": False,
            "reason": "memory",
            "detail": str(body.get("detail") or body.get("error") or "memory is low"),
        }
    raise _StepFailed(
        "couldn't start Verify: %s"
        % (str(body.get("error") or "") or "the server answered %d" % resp.status_code)
    )


#: How long a stop waits on memory to start what it needs (Verify's check, a
#: reviewer) before it asks the person. Real flights waited 13–44 min on
#: "memory is low" with nobody asked; past this they park on you.
MEMORY_PARK_S = 5 * 60.0
#: What works on a stop parked on memory (Done there is "retry" too).
MEMORY_ITEMS = [
    "Free some memory (close a session or an app), then press Retry — or "
    "Skip this stop",
]


def _waited_word(seconds: float) -> str:
    """How long something has waited, in whole minutes once past one."""
    s = max(0.0, float(seconds))
    return _flights.duration_word(s if s < 60 else s - s % 60)


def _memory_wait(what: str, why: str, since: float, now: float, detail: dict) -> dict:
    """The verdict for a stop whose ``what`` ("Verify", "the reviewer") could
    not start because memory is short: a wait that says why and for how long,
    or — past :data:`MEMORY_PARK_S` — a park on the person (``ask``
    "memory": Retry / Skip / Done = retry)."""
    why = str(why or "").strip() or "memory is low"
    waited = now - since
    if waited >= MEMORY_PARK_S:
        return {
            "action": "you",
            "note": "couldn't start %s: %s (waited %s)"
            % (what, why, _waited_word(waited)),
            "detail": dict(detail, ask="memory", items=list(MEMORY_ITEMS)),
        }
    note = "waiting for memory to start %s: %s" % (what, why)
    if waited >= 60:
        note += " · waited %s" % _waited_word(waited)
    return {"action": "wait", "note": note, "detail": detail}


def _start_command(flight: dict, step: dict, folder: str) -> None:
    run_shell(
        flight,
        step,
        step["config"]["command"],
        step["config"]["timeout_minutes"] * 60.0,
        folder,
    )


def job_of(flight: dict, step: dict) -> Optional[dict]:
    """The shell job :func:`run_shell` started for this step — ``{running,
    exit, output}`` (``output`` is the last 2000 chars, ``exit`` -1 on a
    timeout) — or None when none was started this server run."""
    with _JOBS_LOCK:
        job = _JOBS.get((flight["id"], step["id"]))
        return dict(job) if job else None


def worktree_of(flight: dict) -> str:
    """The flight's session's working folder (its worktree), "" when the
    session is gone."""
    return str(_session_obs(flight, time.time(), False).get("folder") or "")


def run_shell(
    flight: dict,
    step: dict,
    cmd: str,
    timeout_s: float,
    folder: str,
    env_extra: Optional[dict] = None,
    keep: int = 2000,
) -> None:
    """Run ``cmd`` with ``/bin/sh -c`` in ``folder`` on a thread, as the
    ``command`` kind does; :func:`job_of` reads the job and the loop is woken
    when it ends. One job per (flight, step): a second call replaces the
    record (the earlier process runs on to its end). ``keep`` is how much of
    the output's tail the job keeps."""
    key = (flight["id"], step["id"])
    timeout = float(timeout_s)
    job = {"running": True, "exit": None, "output": ""}
    with _JOBS_LOCK:
        _JOBS[key] = job
    env = dict(os.environ)
    env["MINDFLOCK_FLIGHT"] = flight["id"]
    env["MINDFLOCK_SESSION_TITLE"] = flight["session"]
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    def _work() -> None:
        try:
            cp = subprocess.run(
                [compat.posix_shell(), "-c", cmd],
                cwd=folder or None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            out = cp.stdout.decode("utf-8", "replace")
            code = cp.returncode
        except subprocess.TimeoutExpired as err:
            raw = err.stdout or b""
            out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            out += "\n(timed out after %d min)" % int(timeout // 60)
            code = -1
        except OSError as err:
            out, code = "could not run it: %s" % err, -1
        with _JOBS_LOCK:
            job.update(running=False, exit=code, output=out[-keep:])
        wake()

    threading.Thread(
        target=_work, name="mf-flight-cmd-%s" % flight["id"], daemon=True
    ).start()


async def _run_ticket(flight: dict, step: dict) -> str:
    """Move / comment on the flight's ticket. ``"skipped"`` when its source is
    not configured here."""
    from backend.ticket_ingestion.config import config_for_launch
    from backend.ticket_ingestion.providers import get_provider

    subj = flight["subject"]
    try:
        src = config_for_launch(None).source_for(subj.get("source") or "")
    except Exception:  # noqa: BLE001
        src = None
    if src is None:
        return "skipped"
    provider = get_provider(src)
    want = str(step["config"].get("state") or "").strip()
    try:
        if want:
            state_id = await _resolve_state(provider, want)
            await provider.set_state(str(subj["id"]), state_id)
        if step["config"].get("comment"):
            await provider.add_comment(
                str(subj["id"]), _flights.fill_prompt(step["config"]["comment"], flight)
            )
    except _StepFailed:
        raise
    except Exception as err:  # noqa: BLE001
        raise _StepFailed("the tracker refused: %s" % err)
    return "done"


async def _resolve_state(provider, want: str) -> str:
    """The provider's state ID for what a person typed: an id as is, else a
    name (or unqualified label) matched case-insensitively. ``set_state`` takes
    an ID on every provider (Shortcut ``int()``s it), so a name passed through
    used to halt the flight on its last step."""
    states = [s for s in (await provider.list_states() or []) if isinstance(s, dict)]
    for s in states:
        if str(s.get("id") or "") == want:
            return want
    low = want.casefold()
    for s in states:
        if low in (
            str(s.get("name") or "").strip().casefold(),
            str(s.get("label") or "").strip().casefold(),
        ):
            return str(s.get("id") or "")
    names = ", ".join(str(s.get("name") or s.get("id") or "") for s in states[:12])
    raise _StepFailed(
        "the tracker has no state %r%s"
        % (want, (" (it has: %s)" % names) if names else " (it lists no states)")
    )


def _run_notify(flight: dict, step: dict) -> dict:
    """A notify stop (blocking) → what really happened: ``{"delivered":
    "phone" | "bell", "why"}``. The bell always gets the row (the
    ``flight.notified`` event the bell lists); the phone gets it only when
    phone notifications (ntfy) are on, and the push is awaited, so "sent to
    your phone" is said only when the phone's server took it."""
    from backend.web.core import ntfy as _ntfy

    text = _flights.fill_prompt(step["config"].get("text") or step["title"], flight)
    title = "%s · %s" % (flight["plan_name"], flight["session"] or "route")
    try:
        cfg = _ntfy.load()
        active = bool(cfg.active)
    except Exception as err:  # noqa: BLE001 — unreadable settings: the bell only
        _warn("flight %s: notify settings unreadable: %v", flight["id"], err)
        cfg, active = None, False
    out = {"delivered": "bell", "why": "phone notifications are off"}
    if active:
        try:
            ok, why = asyncio.run(
                _ntfy.publish(cfg, title=title, message=text, tags=["bird"])
            )
        except Exception as err:  # noqa: BLE001 — a notify stop never blocks
            ok, why = False, _error_line(err)
        if ok:
            out = {"delivered": "phone", "why": ""}
        else:
            out = {"delivered": "bell", "why": str(why or "the push failed")}
            _warn("flight %s: notify not sent: %v", flight["id"], out["why"])
    _emit(
        "flight.notified",
        flight,
        {
            "text": text,
            "title": title,
            "phone": out["delivered"] == "phone",
            "why": out["why"],
        },
    )
    return out


def _notify_outcome(res) -> dict:
    """A notify stop's verdict from :func:`_run_notify`'s answer: the note
    and receipt say where it went — never "sent" when it was not."""
    if not isinstance(res, dict):
        return {"action": "done", "note": "notified", "detail": {"receipt": "notified"}}
    why = str(res.get("why") or "")
    if res.get("delivered") == "phone":
        note = receipt = "sent to your phone"
    elif why == "phone notifications are off":
        note = receipt = "shown in the bell — phone notifications are off"
    else:
        note = "not sent to your phone: %s (shown in the bell)" % (why or "unknown")
        receipt = "not sent: %s" % (why or "unknown")
    return {"action": "done", "note": note, "detail": {"receipt": receipt}}


async def _send_reviews(flight: dict, step: dict, action: dict, now: float) -> dict:
    from backend.ticket_ingestion import pr_runner as _pr_runner

    cache = _REVIEWS.get(flight["id"]) or {}
    new = [str(i) for i in action.get("comments") or []]
    comments = [cache.get("comments", {}).get(i) for i in new]
    comments = [c for c in comments if c is not None]
    pr = cache.get("pr")
    if pr is None or not comments:
        raise _StepFailed("the new review comments could not be read")
    sess = await asyncio.to_thread(_session_obs, flight, now, False)
    cfg = step.get("config") or {}
    text = _pr_runner.build_consolidated_pr_prompt(
        pr,
        comments,
        sess.get("folder") or "",
        each_round=str(cfg.get("each_round") or ""),
        reply_declined=bool(cfg.get("reply_declined")),
    )
    await asyncio.to_thread(_deliver, flight["session"], text)
    d = flight["step_states"][step["id"]]["detail"]
    return dict(
        action,
        detail={
            "phase": "agent",
            "phase_since": now,
            "seen": list(d.get("seen") or []) + new,
            "review_rounds": int(d.get("review_rounds") or 0) + 1,
            "idle_since": None,
            "worked_at": 0.0,
        },
    )


def _split_base(
    flight: dict, step: dict, cfg: Optional[dict] = None
) -> Tuple[dict, str, str]:
    """What every worker of a split is created with (blocking) — the payload
    ``spawn_session`` builds for a worker of this session: a worktree of the
    session's own repository forked from its HEAD, on its CLI, under its
    fence — plus the workers' provider and title prefix.

    A provisioned session (a ticket or PR started from Intake) gets the same
    plain worktree of ITS checkout, never a provisioned twin: its ``Path`` is
    the server's own folder when Intake cloned a repository URL, so
    provisioning "from the same source" put a reviewer of a sitecheck PR in a
    clone of MindFlock; and a provisioned twin forks from the base branch,
    so it could not see the session's change at all. The clone (clone
    strategy) or the ``_base_`` clone (worktree strategy) is a full repo
    holding the session's branch and its base, so ``git diff <base>...HEAD``
    works in the worker and its branch merges from the session as is."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.mcp import tools as _mcp_tools

    srv = _server()
    parent = flight["session"]
    inst = srv.ENGINE.instances.get(parent)
    if inst is None:
        raise _StepFailed("the session is gone")
    row = _row(parent)
    cfg = step["config"] if cfg is None else cfg
    program = str(
        cfg.get("program") or getattr(inst, "Program", "") or row.get("program") or ""
    )
    payload: dict = {"spawned": True, "parent": parent}
    if program:
        payload["program"] = program
    try:
        folder = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        folder = str(row.get("folder") or "")
    path = str(getattr(inst, "Path", "") or row.get("path") or "")
    root = _gitlocal.canonical_repo_root(folder) if folder else None
    sha = _gitlocal.head_sha(folder) if folder else None
    if getattr(inst, "Provisioned", False) and not (root and sha):
        # Its Path may be an unrelated repository (see above): never guess.
        raise _StepFailed(
            "couldn't read %s's checkout to start a worker from it" % parent
        )
    payload["repo_path"] = root or path or folder
    if sha:
        payload["base_ref"] = sha
        branch = str(getattr(inst, "Branch", "") or row.get("branch") or "")
        if branch and branch != "HEAD":
            payload["base_branch"] = branch
    if cfg.get("model"):
        payload["profile_model"] = cfg["model"]
    only = [p.strip() for p in str(cfg.get("fence_only") or "").split(",") if p.strip()]
    try:
        fence = _mcp_tools._inherit_fence(
            _mcp_tools.row_fence(row), {"only": only, "keep_out": []}, parent
        )
    except Exception as err:  # noqa: BLE001 — ToolError: only= outside the parent's
        raise _StepFailed(str(err))
    if fence is not None:
        payload["fence"] = fence
    provider = _mcp_tools._provider_of_program(program)
    slug = _mcp_tools._slug(parent)[:40]
    return payload, provider, (slug + "-w") if slug else "worker-"


def _footer_ok(provider: str) -> bool:
    """``spawn_session``'s footer rule: the worker gets a report_result tool
    only when the MCP is attached to its CLI."""
    caps = _server()._agent_mcp_caps()
    return bool(caps.get("enabled")) and provider in (caps.get("providers") or [])


def _create_failure(title: str, since: float = 0.0) -> str:
    """Why the server's background create of ``title`` failed at or after
    ``since`` (``GET /api/create_failures``' record: kept ten minutes), else
    "" (blocking-free)."""
    from backend.web.core import session_removal as _removal

    srv = _server()
    with _removal._CREATE_FAILURES_LOCK:
        rec = dict((getattr(srv, "_CREATE_FAILURES", None) or {}).get(title) or {})
    if not rec.get("error") or float(rec.get("ts") or 0.0) < since:
        return ""
    if time.time() - float(rec.get("ts") or 0.0) > _removal._CREATE_FAILURE_TTL_S:
        return ""
    return str(rec["error"])


def _leftover_probe(parent: str) -> Optional[Callable[[str], bool]]:
    """``taken(title)`` for a provisioned session's would-be workers (blocking),
    or None: the MCP's ``_provisioned_branch_probe`` — a closed provisioned
    worker keeps its ``<workspace_dir>/mindflock-<title>`` clone or its
    ``mindflock/<title>`` branch in the ``_base_`` clone, and a create under
    that title fails in the background after its 202."""
    from backend.mcp import tools as _mcp_tools

    inst = _server().ENGINE.instances.get(parent)
    if inst is None or not getattr(inst, "Provisioned", False):
        return None
    try:
        folder = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        folder = str(_row(parent).get("folder") or "")
    strategy = str(getattr(inst, "WorkspaceStrategy", "") or "worktree")
    try:
        return _mcp_tools._provisioned_branch_probe(folder, strategy)
    except Exception:  # noqa: BLE001 — no probe: the create's own 409 is the backstop
        return None


def _taken_by(
    leftover: Optional[Callable[[str], bool]], refused: set
) -> Callable[[str], bool]:
    """``_free_worker_title``'s extra probe for one spawn: a provisioned
    leftover, or a title the create already refused as existing."""

    def taken(cand: str) -> bool:
        return cand in refused or bool(leftover is not None and leftover(cand))

    return taken


def _free_worker_title(
    prefix: str,
    repo: str,
    branch_prefix: Optional[str],
    taken_by: Optional[Callable[[str], bool]] = None,
) -> str:
    """The lowest free ``<prefix><N>`` — no live or starting title, no tmux
    name, (when it can tell) no leftover branch: ``spawn_session``'s rule —
    and none ``taken_by`` says is taken (a provisioned session's leftover
    clone or branch, :func:`_leftover_probe`; a title already refused this
    spawn), nor one whose create just failed in the background."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.mcp import tools as _mcp_tools

    srv = _server()
    taken = set(srv.ENGINE.instances.keys())
    try:
        taken |= {str(r.get("title") or "") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pass
    tmux = {_mcp_tools._tmux_name(t) for t in taken}
    # The branch a plain worktree session of that title gets in ``repo``: the
    # server's own prefix, which a provisioned session's row does not show.
    session_branch = getattr(srv, "_session_branch_name", None)

    def _leftover(cand: str) -> bool:
        if repo and branch_prefix is not None:
            if _gitlocal.branch_exists(repo, branch_prefix + cand):
                return True
        if repo and session_branch is not None:
            name = session_branch(cand)
            if name and _gitlocal.branch_exists(repo, name):
                return True
        if taken_by is not None and taken_by(cand):
            return True
        return bool(_create_failure(cand))

    n = 1
    while True:
        cand = "%s%d" % (prefix, n)
        if (
            cand not in taken
            and _mcp_tools._tmux_name(cand) not in tmux
            and not _leftover(cand)
        ):
            return cand
        n += 1


def _adopted_workers(flight: dict, step: dict, prefix: str) -> List[str]:
    """Workers an interrupted run of this split already made: live children
    titled ``<prefix><N>``, created since the flight took off, that no other
    split of the flight claims."""
    import re as _re

    from backend.web.core import lineage as _lineage

    srv = _server()
    claimed = set()
    for s in flight["steps"]:
        if s["kind"] == "split" and s["id"] != step["id"]:
            claimed |= set(
                flight["step_states"][s["id"]]["detail"].get("children") or []
            )
    pat = _re.compile("^%s\\d+$" % _re.escape(prefix))
    out = []
    for t in _lineage.children_of(srv.ENGINE.instances, flight["session"]):
        inst = srv.ENGINE.instances.get(t)
        if (
            pat.match(t)
            and t not in claimed
            and _epoch(inst) >= float(flight["started"] or 0.0)
        ):
            out.append(t)
    return out


async def _run_split(flight: dict, step: dict, action: dict) -> dict:
    """Spawn one worker per prompt as children of the flight's session
    (``spawn_session``'s payload: ``parent``, ``spawned``, the session's HEAD
    as ``base_ref``, its fence, the report-back footer). The spawn limits and
    the memory guard are the create path's own: a refusal fails the step with
    its sentence, keeping the workers already made."""
    from backend.mcp import tools as _mcp_tools
    from backend.web.core import lineage as _lineage
    from backend.web.core import session_create as _session_create

    srv = _server()
    parent = flight["session"]
    prompts = _flights.split_prompts(step["config"], flight)
    if not prompts:
        raise _StepFailed("the split has no prompts")
    base, provider, prefix = await asyncio.to_thread(_split_base, flight, step)
    made = await asyncio.to_thread(_adopted_workers, flight, step, prefix)
    todo = prompts[len(made) :]
    if todo:
        insts = srv.ENGINE.instances
        refused = _lineage.spawn_limit_error(insts, parent, True)
        cap = _lineage.limit(_lineage.MAX_CHILDREN_ENV, _lineage.DEFAULT_MAX_CHILDREN)
        have = len(_lineage.children_of(insts, parent))
        if refused is None and have + len(todo) > cap:
            refused = (
                "%d more workers would give %s %d live children (limit %d, set in %s)"
                % (
                    len(todo),
                    parent,
                    have + len(todo),
                    cap,
                    _lineage.SETTINGS_PLACE,
                )
            )
        if refused:
            raise _StepFailed(refused)
    footer = bool(step["config"].get("report_back", True)) and await asyncio.to_thread(
        _footer_ok, provider
    )
    branch_prefix = _mcp_tools.Toolbox._branch_prefix(_row(parent), parent)
    tried: set = set()
    taken_by = _taken_by(await asyncio.to_thread(_leftover_probe, parent), tried)
    for text in todo:
        payload = dict(base)
        body: dict = {}
        status = 0
        for _attempt in range(8):
            title = await asyncio.to_thread(
                _free_worker_title,
                prefix,
                str(payload.get("repo_path") or ""),
                branch_prefix,
                taken_by,
            )
            payload["title"] = title
            payload["prompt"] = text + (
                _mcp_tools.report_footer(title, parent, provider) if footer else ""
            )
            status, body = await _session_create.create_result(payload, auto="flight")
            err = str(body.get("error") or "")
            if status == 409 and "exists" in err:
                tried.add(title)
                continue  # lost a race for the title: the next number
            if status == 400 and "base_ref" in payload and "base" in err:
                # Can't fork from the session's HEAD: its default base, as
                # spawn_session falls back.
                payload.pop("base_ref", None)
                payload.pop("base_branch", None)
                continue
            break
        if status >= 400 or not body.get("title"):
            raise _StepFailed(
                "%s (after %d of %d workers)"
                % (
                    str(body.get("error") or "could not create a worker"),
                    len(made),
                    len(prompts),
                ),
                {"children": made},
            )
        made.append(str(body["title"]))
    return {
        "action": "done",
        "note": "spawned %d worker%s" % (len(made), "" if len(made) == 1 else "s"),
        "detail": {"children": made, "receipt": _flights.plural(len(made), "worker")},
    }


async def spawn_worker(
    flight: dict,
    step: dict,
    prompt: str,
    model: str = "",
    program: str = "",
    fence_only: str = "",
    report_back: bool = True,
    title_prefix: str = "",
) -> str:
    """One worker of the flight's session (a child: ``parent``, ``spawned``,
    forked from the session's HEAD, under its fence), as a split makes them,
    for a plug-in kind (a second-opinion review, a docs pass). ``program``
    picks another CLI, ``model`` its model; ``fence_only`` is the
    comma-separated paths it may change. Returns the worker's title; raises
    :class:`StepFailed` with the create path's own refusal."""
    from backend.mcp import tools as _mcp_tools
    from backend.web.core import lineage as _lineage
    from backend.web.core import session_create as _session_create

    srv = _server()
    parent = flight["session"]
    cfg = {
        "model": model,
        "program": program,
        "fence_only": fence_only,
        "report_back": report_back,
    }
    base, provider, prefix = await asyncio.to_thread(_split_base, flight, step, cfg)
    if title_prefix:
        prefix = _mcp_tools._slug(title_prefix)[:40] + "-"
    refused = _lineage.spawn_limit_error(srv.ENGINE.instances, parent, True)
    if refused:
        raise _StepFailed(refused)
    footer = report_back and await asyncio.to_thread(_footer_ok, provider)
    branch_prefix = _mcp_tools.Toolbox._branch_prefix(_row(parent), parent)
    tried: set = set()
    taken_by = _taken_by(await asyncio.to_thread(_leftover_probe, parent), tried)
    payload = dict(base)
    body: dict = {}
    status = 0
    for _attempt in range(8):
        title = await asyncio.to_thread(
            _free_worker_title,
            prefix,
            str(payload.get("repo_path") or ""),
            branch_prefix,
            taken_by,
        )
        payload["title"] = title
        payload["prompt"] = prompt + (
            _mcp_tools.report_footer(title, parent, provider) if footer else ""
        )
        status, body = await _session_create.create_result(payload, auto="flight")
        err = str(body.get("error") or "")
        if status == 409 and "exists" in err:
            tried.add(title)  # a leftover the probes can't see: the next one
            continue
        if status == 400 and "base_ref" in payload and "base" in err:
            payload.pop("base_ref", None)
            payload.pop("base_branch", None)
            continue
        break
    if status >= 400 or not body.get("title"):
        raise _StepFailed(str(body.get("error") or "could not create a worker"))
    return str(body["title"])


async def _perform(flight: dict, action: dict, now: float) -> dict:
    """Carry out ``action``'s side effect; return it amended (detail, or
    turned into done/fail/advance)."""
    kind = action["action"]
    step = _flights._current(flight)
    if step is None:
        return action
    try:
        if kind == "fail" and action.get("then") == "agent":
            await asyncio.to_thread(_deliver_fix, flight, step, action)
            return action
        if kind != "run":
            return action
        phase = action.get("phase")
        sk = step["kind"]
        if phase == "agent_run":
            got = dict(action.get("detail") or {})
            pid = str(got.get("plan") or "")
            cfg = step.get("config") or {}
            ss = flight["step_states"][step["id"]]["detail"]
            res = await _start_verify_run(
                pid,
                str(cfg.get("where") or _flights.VERIFY_WHERE_BRANCH),
                str(cfg.get("notes") or ""),
                _verify_walk(cfg),
                # "You" signs the check off even when every step passes.
                confirm=str(cfg.get("who") or "both") == "you",
            )
            if res["started"]:
                # Always written: the step's detail is merged across ticks,
                # so a checker run after an in-session one must clear it.
                walk = {"in_session": False, "walk_session": ""}
                if res.get("in_session"):
                    walk = {
                        "in_session": True,
                        "walk_session": res.get("session") or "",
                    }
                    action = dict(
                        action,
                        note="verifying in %s — talk to it there"
                        % (res.get("session") or "the session"),
                    )
                return dict(
                    action,
                    detail=dict(
                        got,
                        run_started=True,
                        run_started_at=now,
                        run_wait=None,
                        memory_since=None,
                        nudged_at=None,
                        **walk,
                    ),
                )
            # The memory guard refused the checker: NOT started. The stop
            # waits with the reason on screen at once, and how long it has
            # waited, retrying (flights._verify_guard); past MEMORY_PARK_S it
            # parks on the person with what works.
            since = float(ss.get("memory_since") or now)
            return _memory_wait(
                "Verify",
                res["detail"],
                since,
                now,
                dict(
                    got,
                    run_started=False,
                    run_wait="memory",
                    memory_since=since,
                    memory_detail=res["detail"],
                    last_try=now,
                ),
            )
        if phase == "nudge":
            v = action.get("detail") or {}
            pid = str(v.get("plan") or "")
            from backend.web.core import test_plans as _tp

            plan = await asyncio.to_thread(_tp.get, pid) if pid else None
            checker = str((plan or {}).get("run_session") or "")
            runs = (plan or {}).get("runs") or []
            own = bool(runs and runs[-1].get("in_session"))
            # Walked in the flight's own session: that agent is the one nudged.
            who = checker if own and checker else "the checker"
            said = "nudged %s" % who
            try:
                if not checker:
                    raise _StepFailed("no checker session")
                await asyncio.to_thread(_deliver, checker, _flights.VERIFY_NUDGE)
            except Exception as err:  # noqa: BLE001 — the close comes next anyway
                said = "couldn't nudge %s (%s)" % (who, str(err) or "error")
            return dict(action, detail=dict(v, nudged_at=now, nudge=said))
        if phase == "close":
            v = action.get("detail") or {}
            pid = str(v.get("plan") or "")
            from backend.web.core import test_plans_driver as _tpd

            await _tpd.close_run(
                pid, str(v.get("close_reason") or "not checked: the check was closed")
            )
            return dict(action, detail=dict(v, closed_at=now))
        if phase == "send":
            return await _send_reviews(flight, step, action, now)
        if phase == "push":
            info = await asyncio.to_thread(_arm, flight, "push")
            return dict(action, detail=dict(info, phase="push"))
        if sk == "agent":
            return await _run_agent(flight, step, action, now)
        if sk == "ship":
            cfg = step["config"]
            info = await asyncio.to_thread(
                _arm,
                flight,
                cfg["lane"],
                bool(cfg.get("ask_first")),
                cfg.get("message") or "",
            )
            return dict(action, detail=info)
        if sk == "verify":
            got = await asyncio.to_thread(_ensure_checklist, flight)
            return dict(action, detail=got)
        if sk == "wait":
            cfg = step["config"]
            until = (
                _flights.next_until(cfg["until"], now)
                if cfg.get("until")
                else now + float(_flights.wait_seconds(cfg))
            )
            return dict(action, detail={"until": until})
        if sk == "reviews":
            return dict(action, detail={"phase": "watch"})
        if sk == "checks":
            _CHECKS.pop(flight["id"], None)
            return dict(action, detail={})
        if sk == "command":
            sess = await asyncio.to_thread(_session_obs, flight, now, False)
            _start_command(flight, step, sess.get("folder") or "")
            return dict(action, detail={})
        if sk == "ticket":
            outcome = await _run_ticket(flight, step)
            if outcome == "skipped":
                return {
                    "action": "advance",
                    "note": "the ticket's source is not set up here",
                }
            return {
                "action": "done",
                "note": "the ticket was updated",
                "detail": {"receipt": _flights.ticket_receipt(step["config"])},
            }
        if sk == "notify":
            res = await asyncio.to_thread(_run_notify, flight, step)
            return _notify_outcome(res)
        if sk == "split":
            return await _run_split(flight, step, action)
        if sk == "join":
            return dict(action, detail={})
        plug = _kinds.get(sk)
        if plug is not None:
            return await plug.run(flight, step, action, now)
    except _StepFailed as err:
        out = _flights.fail_action(
            flight, str(err), {"session": {"present": bool(flight["session"])}}
        )
        if err.detail:
            out["detail"] = dict(err.detail)
        if out.get("then") == "agent":
            # Handed to the agent: tell it, or there is nobody to fix it.
            try:
                await asyncio.to_thread(_deliver_fix, flight, step, out)
            except Exception:  # noqa: BLE001
                out = dict(out, then="halt")
        return out
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 — never a silent "carrying out"
        # Anything else used to escape to the pass, which only logged it: the
        # stop kept its in-progress mark and read "carrying out <stop>" for
        # PERFORM_STALE_S, then ran the effect AGAIN, forever. Now it fails
        # here, saying what broke (the traceback goes to the log), and the
        # mark is cleared by the outcome record like any other verdict.
        _log(
            "flight %s: %s hit an error: %v\n%s",
            flight.get("id"),
            step.get("title") or step.get("kind"),
            err,
            traceback.format_exc(),
        )
        out = _flights.fail_action(
            flight,
            "%s hit an error: %s"
            % (step.get("title") or step.get("kind"), _error_line(err)),
            {"session": {"present": bool(flight["session"])}},
        )
        if out.get("then") == "agent":
            # A server error is not the agent's to fix, and the retry after
            # its fix would carry out an effect that may have happened.
            out = dict(out, then="halt")
        return out
    return action


def _error_line(err: BaseException) -> str:
    """An exception as one plain line: its message, else its type."""
    return _one_line(str(err)) or type(err).__name__


def _verify_evidence(flight: Optional[dict], step: dict) -> List[str]:
    """For a failed Verify stop: each failed step of its check with what it
    should do and what was seen (blocking; ``[]`` when unreadable)."""
    if not flight or step.get("kind") != "verify":
        return []
    from backend.web.core import test_plans as _tp

    ss = (flight.get("step_states") or {}).get(step.get("id")) or {}
    pid = str((ss.get("detail") or {}).get("plan") or "")
    try:
        plan = _tp.get(pid) if pid else None
        failures = _tp.failed_steps(plan) if plan else []
    except Exception:  # noqa: BLE001 — the reason still says which steps
        return []
    out: List[str] = []
    for f in failures[:12]:
        s = f.get("step") or {}
        line = "- %s" % (s.get("text") or "a step")
        if s.get("expect"):
            line += "\n  expected: %s" % _one_line(s["expect"], 400)
        if f.get("note"):
            line += "\n  seen: %s" % _one_line(f["note"], 1200)
        out.append(line)
    return out


def _deliver_fix(flight: dict, step: dict, action: dict) -> None:
    """Hand a stop's failure to the flight's agent (blocking)."""
    _deliver(flight["session"], _fix_note(step, action, flight))


def _fix_note(step: dict, action: dict, flight: Optional[dict] = None) -> str:
    """What the agent is told when a stop's failure is handed to it — for a
    Verify stop, with each failed step's evidence; for a reviews stop, with
    each finding to fix or rebut on the record."""
    reason = action.get("reason") or "failed"
    text = "[MindFlock route] %s failed: %s" % (
        step.get("title") or step.get("kind") or "a stop",
        reason,
    )
    evidence = _verify_evidence(flight, step)
    if evidence:
        text += "\nWhat Verify saw:\n" + "\n".join(evidence)
    then = ""
    if step.get("kind") == "review":
        # A reviewer's findings: each one fixed or rebutted, on the record.
        from backend.web.core.flight_kinds import review as _review

        then = _review.fix_instructions(step, reason)
    then = then or "Fix it; the route retries the stop once you are done."
    return text + "\n" + then


# --------------------------------------------------------------------------- #
# One flight, one pass
# --------------------------------------------------------------------------- #
class _Stale(Exception):
    pass


def _apply_if_unchanged(
    fid: str, seen: dict, action: dict, now: float, at: Optional[int] = None
) -> Optional[dict]:
    """Apply an effect-free ``action`` (to member ``at`` of a parallel group,
    default the cursor) unless a route moved the flight since it was read.
    None when stale; the stored record when applying it changed nothing but
    the clock (no write — see :func:`_same`)."""
    out: dict = {}

    def fn(f: dict) -> None:
        if _view_key(f)[:3] != _view_key(seen)[:3]:
            raise _Stale()
        before = copy.deepcopy(f)
        _flights.apply_at(f, f["cursor"] if at is None else at, action, now)
        if _same(before, f):
            out["unchanged"] = before
            raise _Stale()

    try:
        return _flights.mutate(fid, fn)
    except _Stale:
        return out.get("unchanged")


def _same(a: dict, b: dict) -> bool:
    """Equal but for ``updated`` — a pass that only re-observed the same wait."""
    return {k: v for k, v in a.items() if k != "updated"} == {
        k: v for k, v in b.items() if k != "updated"
    }


def _needs_effect(action: dict) -> bool:
    return action["action"] == "run" or (
        action["action"] == "fail" and action.get("then") == "agent"
    )


def _mark_intent(
    fid: str,
    seen: dict,
    action: dict,
    now: float,
    token: str,
    at: Optional[int] = None,
) -> Optional[dict]:
    """Record, under the lock and only if nobody moved the flight, that this
    step's side effect is about to be carried out (``detail.performing``). A
    fresh ``run`` also records the step running here, so a route that pauses,
    skips or cancels meanwhile sees the truth, and no pass starts it twice."""

    def fn(f: dict) -> None:
        if _view_key(f)[:3] != _view_key(seen)[:3]:
            raise _Stale()
        i = f["cursor"] if at is None else at
        cur = f["steps"][i] if 0 <= i < len(f["steps"]) else None
        if cur is None or f["step_states"][cur["id"]]["detail"].get("performing"):
            raise _Stale()
        if action["action"] == "run" and not action.get("phase"):
            _flights.apply_at(f, i, dict(action, detail={}), now)
        f["step_states"][cur["id"]]["detail"]["performing"] = {
            "token": token,
            "at": now,
            "action": action["action"],
            "phase": str(action.get("phase") or ""),
        }

    try:
        return _flights.mutate(fid, fn)
    except _Stale:
        return None


def _record_outcome(
    fid: str, sid: str, token: str, action: dict, now: float
) -> Optional[dict]:
    """Record a carried-out effect's outcome against its intent ``token``. The
    effect has happened whatever a route did meanwhile, so: a flight moved past
    the step (skipped, jumped, cancelled) only drops the mark; a paused one
    keeps the outcome AND stays paused."""

    def fn(f: dict) -> None:
        ss = f["step_states"].get(sid)
        mark = (ss or {}).get("detail", {}).get("performing")
        if not isinstance(mark, dict) or mark.get("token") != token:
            raise _Stale()
        ss["detail"].pop("performing", None)
        lo, hi = _flights.current_group(f)
        here = [
            k for k in range(lo, min(hi, len(f["steps"]))) if f["steps"][k]["id"] == sid
        ]
        if f["state"] in _flights.FINAL or not here:
            return
        if f["state"] == "halted":
            return
        paused = f["state"] == "paused"
        if paused:
            f["state"] = "running"
        if action["action"] == "run":
            for key in ("session", "repo_root", "branch"):
                if action.get(key):
                    f[key] = str(action[key])
            upd = action.get("detail") if isinstance(action.get("detail"), dict) else {}
            ss["detail"].update(upd)
            if action.get("note"):
                ss["note"] = str(action["note"])
            f["updated"] = now
        else:
            _flights.apply_at(f, here[0], action, now)
        if paused and f["state"] in _flights.ACTIVE:
            f["state"] = "paused"

    try:
        return _flights.mutate(fid, fn)
    except _Stale:
        return None


#: At most this many steps finish within one pass (a notify followed by a
#: wait, say) before the loop moves on to the next flight.
_CHAIN = 4


async def _step_member(
    fid: str, f: dict, i: int, now: float
) -> Optional[Tuple[dict, dict]]:
    """Observe, judge and apply step ``i`` of ``f`` (the cursor, or one member
    of the parallel group at it) → ``(after, action)``; None when stale or
    nothing changed."""
    view = _flights.member_view(f, i)
    obs = await observe(view, now)
    action = _launch_verdict(view, obs, _flights.next_action(view, obs, now), now)
    sess = obs.get("session") or {}
    if not f["repo_root"] and sess.get("present"):
        root = await asyncio.to_thread(_repo_root, f["session"])
        if root:
            action["repo_root"] = root
    if sess.get("branch") and sess["branch"] != f["branch"]:
        action["branch"] = sess["branch"]
    cur = _flights._current(view)
    if _needs_effect(action) and cur is not None:
        token = uuid.uuid4().hex
        marked = await asyncio.to_thread(_mark_intent, fid, f, action, now, token, i)
        if marked is None:
            return None
        action = await _perform(_flights.member_view(marked, i), action, now)
        after = await asyncio.to_thread(
            _record_outcome, fid, cur["id"], token, action, now
        )
        if after is None and cur["kind"] in ("ship", "reviews"):
            # The step was removed while its lane was being armed.
            await asyncio.to_thread(_disarm_if_removed, fid, cur["id"])
    else:
        after = await asyncio.to_thread(_apply_if_unchanged, fid, f, action, now, i)
    if after is None or _same(f, after):
        return None
    if action.get("by_agent") and cur is not None and cur["kind"] == "ship":
        # Done by the agent while this stop's lane waited "for new work": the
        # lane has nothing left to do for this flight.
        await asyncio.to_thread(_disarm_ours, fid, f["session"])
    if (
        cur is not None
        and cur["kind"] == "command"
        and action["action"] in ("done", "fail", "advance")
    ):
        with _JOBS_LOCK:
            _JOBS.pop((fid, cur["id"]), None)
    if (
        cur is not None
        and cur["kind"] == "verify"
        and action["action"] in ("done", "fail")
        and (cur.get("config") or {}).get("post_to_pr")
    ):
        posted = await _post_verify_results(fid, cur, obs, now)
        after = posted or after
    await asyncio.to_thread(_announce, f, after)
    return after, action


async def _post_verify_results(
    fid: str, step: dict, obs: dict, now: float
) -> Optional[dict]:
    """A finished Verify stop with ``post_to_pr``: comment its results
    (``test_plans.results_markdown``) on the session's PR — the driver posts,
    never the checker. Once per attempt (``detail.pr_comment`` records what
    happened); no PR or no checklist is skipped with a history note, and a
    posting error never fails the stop. Returns the flight after the record,
    or None."""
    from backend.web.core import github_pr as _gh
    from backend.web.core import test_plans as _tp

    flight = _flights.get(fid)
    ss = (flight or {}).get("step_states", {}).get(step["id"])
    if flight is None or ss is None or ss["detail"].get("pr_comment"):
        return None
    sess = obs.get("session") or {}
    pid = str((obs.get("verify") or {}).get("plan") or ss["detail"].get("plan") or "")
    try:
        plan = await asyncio.to_thread(_tp.get, pid) if pid else None
        if plan is None:
            outcome = "skipped: no checklist to post"
        else:
            pr, slug = await _pr_of(sess, flight)
            if pr is None or not slug:
                outcome = "skipped: the session has no PR"
            else:
                body = await asyncio.to_thread(_tp.results_markdown, plan)
                token = await _gh.api_token()
                if not token:
                    outcome = "skipped: no GitHub token"
                else:
                    status, data = await _gh._request(
                        "POST",
                        "/repos/%s/issues/%d/comments" % (slug, int(pr.number)),
                        token=token,
                        body={"body": body},
                    )
                    outcome = (
                        "posted"
                        if status == 201
                        else "failed: %s" % _gh._api_message(status, data)
                    )
    except Exception as err:  # noqa: BLE001 — never fails the stop
        outcome = "failed: %s" % err
    _log(
        "flight %s: verify results to the PR: %v",
        fid,
        outcome,
        level="warning" if outcome.startswith("failed") else "info",
    )

    def fn(x: dict) -> None:
        st = x["step_states"].get(step["id"])
        if st is None:
            return
        st["detail"]["pr_comment"] = outcome
        note = (
            "posted the results to the PR"
            if outcome == "posted"
            else "results not posted to the PR (%s)" % outcome.split(": ", 1)[-1]
        )
        _flights._hist(x, now, step["id"], "noted", note)

    try:
        return await asyncio.to_thread(_flights.mutate, fid, fn)
    except _flights.FlightError:
        return None


async def step_flight(fid: str) -> None:
    """One decision pass over one flight. Never raises.

    Effect-free verdicts are applied only if nothing moved the flight since it
    was read, and not written at all when they change nothing. A verdict with
    a side effect (a prompt, an armed lane, a command, a tracker call) is
    recorded as an INTENT first (:func:`_mark_intent`), carried out, then its
    outcome recorded against that intent (:func:`_record_outcome`) — so a
    pause / skip / cancel landing mid-effect never makes the effect repeat."""
    srv = _server()
    try:
        now = time.time()
        f, _took = await asyncio.to_thread(
            _flights.claim, fid, srv._SERVER_BOOT_ID, now
        )
        if f is None:
            return
        for _ in range(_CHAIN):
            if f["state"] not in _flights.ACTIVE:
                return
            lo, hi = _flights.current_group(f)
            if hi - lo < 2:
                out = await _step_member(fid, f, f["cursor"], now)
                if out is None:
                    return  # stale, or nothing changed: no write, nothing to say
                after, action = out
                if (
                    action["action"] not in ("done", "advance", "skip_when")
                    or after["state"] not in _flights.ACTIVE
                ):
                    return
                f = after
                continue
            # A parallel group (round 20): every open member, each its own
            # verdict against the flight as the one before left it.
            cursor = f["cursor"]
            members = [f["steps"][k]["id"] for k in _flights.open_members(f)]
            if not members:
                members = [f["steps"][cursor]["id"]]  # all through: advance
            for sid in members:
                if f["state"] not in _flights.ACTIVE:
                    return
                k = next((n for n, st in enumerate(f["steps"]) if st["id"] == sid), -1)
                glo, ghi = _flights.current_group(f)
                if not glo <= k < ghi:
                    continue  # the group moved on (or the member went)
                out = await _step_member(fid, f, k, now)
                if out is not None:
                    f = out[0]
            if f["state"] not in _flights.ACTIVE or f["cursor"] == cursor:
                return
    except Exception as err:  # noqa: BLE001 — one flight never stops a pass
        _log("flight %s pass failed: %v\n%s", fid, err, traceback.format_exc())


def _disarm_if_removed(fid: str, sid: str) -> None:
    """A lane armed for step ``sid`` after a route removed it is disarmed."""
    f = _flights.get(fid)
    if f is not None and sid not in f["step_states"]:
        _disarm_ours(fid, f["session"])


def _live_titles() -> set:
    srv = _server()
    live = set(srv.ENGINE.instances.keys())
    try:
        live |= {str(r.get("title") or "") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pass
    return live


async def run_pass() -> None:
    """One pass over every flight still in the air."""
    now = time.time()
    try:
        gone = await asyncio.to_thread(_flights.prune, _live_titles(), now)
        for f in gone:
            await asyncio.to_thread(_announce, None, f)
    except Exception as err:  # noqa: BLE001
        _log("flights prune failed: %v", err)
    try:
        everything = await asyncio.to_thread(_flights.list_flights, True)
    except Exception:  # noqa: BLE001 — an unreadable store: next pass
        return
    for f in everything:
        if _unannounced_finish(f, now):
            # Finished somewhere that does not announce (a flight replaced by
            # intake's arm_pending, a take-off already at its end).
            await asyncio.to_thread(_announce, None, f)
    flights = [f for f in everything if f["state"] not in _flights.FINAL]
    for f in flights:
        if f["state"] == _flights.READY:
            if await asyncio.to_thread(_catch_typed_turn, f):
                await step_flight(f["id"])
        elif f["state"] in _flights.ACTIVE:
            await step_flight(f["id"])
    _prune_taps(now)
    live = {f["id"] for f in flights}
    for cache in (_REVIEWS, _CHECKS):
        for k in [k for k in cache if k not in live]:
            cache.pop(k, None)


#: How far back the finish sweep looks (older finishes predate the sweep).
_FINISH_SWEEP_S = 3600.0


def _unannounced_finish(f: dict, now: float) -> bool:
    return (
        f["state"] in _flights.FINAL
        and now - float(f["finished"] or 0.0) < _FINISH_SWEEP_S
        and not any(str(k).startswith("finished:") for k in f["announced"])
    )


async def run_loop() -> None:
    """Drive flights forever (started by the lifespan): a pass every
    :data:`flights.RUN_INTERVAL_S` or on :func:`wake`."""
    global _LOOP, _WAKE
    _LOOP = asyncio.get_running_loop()
    _WAKE = asyncio.Event()
    subscribe()
    while True:
        try:
            await run_pass()
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=_flights.RUN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _WAKE.clear()


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def on_session_created(title: str) -> Optional[dict]:
    """``session.created``: attach the flight armed for ``title`` before its
    session existed (intake's :func:`flights.arm_pending`) — record its repo,
    reset the gone clock, say so, and wake the loop. None when there is none."""
    f = _flights.for_session(title)
    if f is None:
        return None
    root = ""
    try:
        root = _repo_root(title)
    except Exception:  # noqa: BLE001
        root = ""

    def fn(x: dict) -> None:
        x["gone_since"] = 0.0
        if root and not x["repo_root"]:
            x["repo_root"] = root

    after = _flights.mutate(f["id"], fn)
    if after is not None:
        _emit("flight.changed", after)
    wake()
    return after


def on_prompt(title: str) -> Optional[dict]:
    """A server send path typed a prompt into ``title`` (``/send``, voice,
    the prompt queue's drain, the mailbox lane behind MCP ``send_message``):
    its ready ``start_on: "prompt"`` flight takes off after that turn
    (:func:`flights.on_prompt`), announced, the loop woken → the flight, or
    None. Never raises — a send never fails on a flight."""
    try:
        out = _flights.on_prompt(title)
    except Exception as err:  # noqa: BLE001
        _log("flights on_prompt %s failed: %v", title, err)
        return None
    if out is None:
        return None
    before, after = out
    _announce(before, after)
    wake()
    return after


def _catch_typed_turn(f: dict) -> bool:
    """The tick's half of "take off on the next prompt": a turn typed straight
    into the terminal (the row's activity went working after the flight
    became ready — :func:`flights.ready_turn_started`) takes the ready
    flight off like a server send does. True when it took off."""
    if f.get("start_on") != "prompt" or not f.get("session"):
        return False
    try:
        row = _row(f["session"])
    except Exception:  # noqa: BLE001 — no row, no turn
        return False
    if not _flights.ready_turn_started(f, row):
        return False
    return on_prompt(f["session"]) is not None


def on_session_removed(title: str) -> None:
    """The session-removal hook: its flight is cancelled (and announced)."""
    f = _flights.for_session(title)
    if f is None:
        return
    try:
        after = _flights.mutate(
            f["id"], lambda x: _flights.op_cancel(x, _flights.SESSION_REMOVED)
        )
    except _flights.FlightError:
        return
    _announce(f, after)


def restore_on_reopen(snap: Optional[dict], title: str) -> Optional[dict]:
    """The reopen hook: a chain the session's close cancelled comes back on
    the reopened session ``title`` (:func:`flights.op_restore`). Only from the
    Recently-closed entry's own snapshot — a new session that merely reuses
    the title gets nothing. Not when another chain is already live on
    ``title``: that one is left alone, and the old one stays cancelled with a
    note saying why. Returns the restored flight, else None."""
    fid = str((snap or {}).get("id") or "")
    if not fid or not title:
        return None
    before = _flights.get(fid)
    if before is None:
        return None
    if _flights.for_session(title) is not None:
        try:
            _flights.mutate(
                fid,
                lambda x: _flights._hist(
                    x,
                    time.time(),
                    "",
                    "halted",
                    "not restored: another route is live on the reopened session",
                ),
            )
        except _flights.FlightError:
            pass
        return None
    try:
        after = _flights.mutate(fid, lambda x: _flights.op_restore(x, snap, title))
    except _flights.FlightError:
        return None
    _announce(before, after)
    return after


# --------------------------------------------------------------------------- #
# Operations behind the routes
# --------------------------------------------------------------------------- #
def _subject_of(raw) -> dict:
    return _flights._normalize_subject(raw if isinstance(raw, dict) else {})


class PreflightError(_flights.PlanError):
    """A start refused by :func:`preflight` before anything was created: the
    first problem's sentence and ``field`` (a route answers it like a bad
    step), every problem in ``problems``, and ``session`` — the session a
    client can offer instead (the one holding the branch, the one sharing the
    folder), "" when none."""

    def __init__(self, problems: List[dict]) -> None:
        first = problems[0]
        super().__init__(
            str(first.get("detail") or "this route can't start"),
            str(first.get("field") or ""),
            str(first.get("code") or "bad_step"),
        )
        self.problems = [dict(p) for p in problems]
        self.session = str(first.get("session") or "")


def _adhoc(raw_steps, base: Optional[dict], name: str) -> Tuple[dict, dict]:
    """:func:`flights.adhoc_plan` → ``(plan, {author's step id → new id})``:
    the ids the author gave are renumbered, so a ``start_at`` naming one
    ("build") goes through the map. An older ``adhoc_plan`` without
    ``id_map`` gives an empty map."""
    id_map: dict = {}
    try:
        plan = _flights.adhoc_plan(raw_steps, base, name, id_map=id_map)
    except TypeError as err:
        if "id_map" not in str(err):
            raise
        plan = _flights.adhoc_plan(raw_steps, base, name)
    return plan, {str(k): str(v) for k, v in id_map.items()}


def branch_holder(repo: str, branch: str, exclude: str = "") -> str:
    """The live session (not ``exclude``) whose checkout is on ``branch`` of
    ``repo`` (blocking), "" when none: a new session cut on that branch
    would fail with "branch … is already checked out at …"."""
    branch = str(branch or "").strip()
    if not branch:
        return ""
    want = os.path.realpath(repo) if repo else ""
    srv = _server()
    for title, inst in list(srv.ENGINE.instances.items()):
        if title == exclude or str(getattr(inst, "Branch", "") or "") != branch:
            continue
        if want:
            root = _repo_root(title)
            if root and os.path.realpath(root) != want:
                continue
        return str(title)
    return ""


def preflight(
    plan_or_steps,
    *,
    repo,
    session=None,
    attach=False,
    start_at=None,
    branch: str = "",
) -> List[dict]:
    """Everything that would stop a route AFTER it has created things, checked
    BEFORE (blocking) → ``[{"field", "detail", "session"?}]``, empty when it
    is clear to start. :func:`start` and :func:`attach` refuse with
    :class:`PreflightError` on the first one; a dry run lists them all.

    * ``start_at`` names no stop (``start_at``);
    * a Verify stop's ``where`` names an environment ``repo`` does not have
      (``steps[i].config.where``, with the ones it has) — it used to fly
      Agent → Commit → Push → PR and only then halt;
    * ``branch`` (the branch a NEW session would be cut on) is already a
      live session's: "<session> already has this branch — run the route on
      that session instead" (``session`` names it);
    * a Ship stop on ``session`` (an existing session) that shares its
      folder with another session, or is a copy window of a branch another
      window drives — the fast-track refusal, said now rather than at the
      first Ship stop (``session`` names the other one).

    ``plan_or_steps``: a plan (``{"steps": [...]}``) or an ad-hoc steps list
    (a bad list is a problem too, with its ``field``)."""
    problems: List[dict] = []
    id_map: dict = {}
    if isinstance(plan_or_steps, dict):
        plan = plan_or_steps
    else:
        try:
            plan, id_map = _adhoc(plan_or_steps, None, "")
        except _flights.PlanError as err:
            return [{"field": err.field or "steps", "detail": err.message}]
    steps = list(plan.get("steps") or [])
    title = str(session or "").strip()
    at = str(start_at or "").strip()
    at = id_map.get(at, at)
    cursor = 0
    try:
        cursor = _flights.start_cursor(plan, at, attach=bool(attach))
    except _flights.FlightError as err:
        problems.append({"field": "start_at", "detail": err.detail})
    repo = str(repo or "").strip()
    if not repo and title:
        repo = _repo_root(title)
    problems += _where_problems(steps, cursor, repo)
    if branch and not (title and attach):
        holder = branch_holder(repo, branch, exclude=title)
        if holder:
            problems.append(
                {
                    "field": "branch",
                    "code": "branch_taken",
                    "detail": "%s already has this branch — run the route on "
                    "that session instead" % holder,
                    "session": holder,
                }
            )
    if title:
        shared = _ship_problem(steps, cursor, title)
        if shared is not None:
            problems.append(shared)
    return problems


def _where_problems(steps: List[dict], cursor: int, repo: str) -> List[dict]:
    """A problem for each Verify stop from ``cursor`` on whose ``where`` the
    repo has no environment of (none when the repo is unknown)."""
    from backend.web.core import test_plans as _tp

    if not repo:
        return []
    out: List[dict] = []
    have: Optional[List[str]] = None
    for i, st in enumerate(steps):
        if i < cursor or st.get("kind") != "verify":
            continue
        where = str((st.get("config") or {}).get("where") or "").strip()
        # "live" always exists: the repo's own target (see _verify_env).
        if not where or where.lower() in (_flights.VERIFY_WHERE_BRANCH, "live"):
            continue
        try:
            if _tp.environment(repo, where) is not None:
                continue
            if have is None:
                have = [str(e.get("name") or "") for e in _tp.environments(repo)]
        except Exception as err:  # noqa: BLE001 — unknown is not "no such env"
            _warn("preflight: environments of %s unreadable: %v", repo, err)
            return out
        out.append(
            {
                "field": "steps[%d].config.where" % i,
                "detail": "%s: no environment named %s in this repo — it has %s"
                % (
                    st.get("title") or "Verify",
                    where,
                    ", ".join(n for n in have if n) or "none",
                ),
            }
        )
    return out


def _ship_problem(steps: List[dict], cursor: int, title: str) -> Optional[dict]:
    """The fast-track refusal the first Ship stop from ``cursor`` on would
    meet on ``title`` (a folder shared with another session, a copy window),
    as a problem — None when it would ship."""
    import json as _json

    srv = _server()
    refusal_of = getattr(srv, "_lane_refusal", None)
    if refusal_of is None:
        return None
    for i, st in enumerate(steps):
        if i < cursor or st.get("kind") != "ship":
            continue
        lane = str((st.get("config") or {}).get("lane") or "")
        try:
            refused = refusal_of(title, lane)
            if refused is None:
                continue
            body = _json.loads(refused.body)
        except Exception as err:  # noqa: BLE001 — the stop says it later
            _warn("preflight: lane check on %s: %v", title, err)
            return None
        return {
            "field": "steps[%d]" % i,
            "code": "shared_folder",
            "detail": str(body.get("error") or "this session can't ship by itself"),
            "session": str(body.get("shared_with") or body.get("driver") or ""),
        }
    return None


def _refuse(problems: List[dict]) -> None:
    if problems:
        raise PreflightError(problems)


async def start(body: dict, absorbed: Optional[list] = None) -> dict:
    """``POST /api/flights/start``. Raises :class:`flights.FlightError`, and
    :class:`flights.PlanError` for a bad ad-hoc ``steps`` list. The lanes of
    a fast-track run it took over are appended to ``absorbed``.

    ``start_on`` (docs/flights.md "Round 19"; absent = ``"now"``, today's
    behaviour): ``"prompt"`` / ``"run"`` store the flight ``ready`` — cursor
    0, nothing started, and a session created for it gets NO prompt."""
    srv = _server()
    body = body or {}
    start_on = _flights.normalize_start_on(body.get("start_on"))
    ready = start_on != "now"
    plan_id = str(body.get("plan_id") or "")
    subject = _subject_of(body.get("subject"))
    by = str(body.get("by") or "user")
    title = str(body.get("title") or "").strip()
    start_at = str(body.get("start_at") or "").strip()
    adhoc = body.get("steps") is not None
    plan = _flights.get_plan(plan_id) if plan_id or not adhoc else None
    if plan is None and (plan_id or not adhoc):
        raise _flights.FlightError("unknown_plan", 404, "no such saved route")
    if adhoc:
        if plan is None and not title and not str(body.get("repo_path") or "").strip():
            raise _flights.FlightError(
                "needs_title", 400, "an ad-hoc route needs a session (or a folder)"
            )
        plan, id_map = _adhoc(
            body.get("steps"),
            plan,
            subject.get("title") or ("Route on %s" % title if title else ""),
        )
        # The author's own step id ("build") → the id it was given.
        start_at = id_map.get(start_at, start_at)
    if title and title not in srv.ENGINE.instances:
        raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
    # "resume": where the earlier flight of this route stopped, else the
    # normal start (an explicit stop id is left as it is).
    start_at, resume = await asyncio.to_thread(
        resume_start_at, plan, start_at, session=title, subject=subject
    )
    carry_note = _flights.carry_on_note(resume) if resume else ""
    # Everything that can refuse is checked BEFORE a session is created: a
    # bad step id used to leave an orphan session with no flight, a
    # mistyped Verify environment flew to the PR before it halted.
    _flights.start_cursor(plan, start_at, attach=bool(title))
    repo_hint = (
        ""
        if title
        else (
            str(body.get("repo_path") or "").strip()
            or str((plan.get("session") or {}).get("repo_path") or "")
        )
    )
    _refuse(
        await asyncio.to_thread(
            lambda: _preflight_safe(
                plan,
                repo=repo_hint,
                session=title or None,
                attach=bool(title),
                start_at=start_at,
            )
        )
    )
    take = body.get("absorb_fasttrack")
    rec = None
    if title and (take is None or bool(take)):
        rec = await asyncio.to_thread(_fasttrack_to_absorb, title)
    lead: List[dict] = []
    if rec is not None:
        stage = await asyncio.to_thread(_stage_of, title)
        lead = _flights.absorb_steps(rec, stage)
        # A ready chain starts at 0: the absorbed rungs lead it.
        plan, start_at = _flights.with_lead(
            plan, lead, "" if ready else start_at, attach=not ready
        )
    replaced: list = []
    if title:
        busy = ""
        if not ready and not start_at and rec is None:
            busy = await asyncio.to_thread(_session_busy, title)
        flight = await asyncio.to_thread(
            lambda: _flights.take_off(
                plan,
                session=title,
                subject=subject,
                by=by,
                attach=True,
                start_at=start_at,
                repo_root=_repo_root(title),
                replaced=replaced,
                start_on=start_on,
                busy=busy,
                note=carry_note,
            )
        )
    else:
        steps = plan["steps"]
        first_agent = steps[0]["kind"] == "agent" and not start_at and not ready
        draft = {"subject": subject, "plan_name": plan["name"], "branch": ""}
        prompt = "" if ready else str(body.get("prompt") or "").strip()
        if first_agent and not prompt:
            prompt = _flights.fill_prompt(steps[0]["config"].get("prompt") or "", draft)
        repo = str(body.get("repo_path") or "").strip() or plan["session"]["repo_path"]
        if not repo:
            raise _flights.FlightError(
                "no_repo", 400, "pick a folder for the new session"
            )
        new_title = await _create_session(
            _session_payload(plan["session"], repo, prompt)
        )
        if adhoc and not plan_id and not subject.get("title"):
            plan = dict(plan, name="Route on %s" % new_title)
        flight = await asyncio.to_thread(
            lambda: _flights.take_off(
                plan,
                session=new_title,
                subject=subject,
                by=by,
                start_at=start_at,
                seeded=first_agent and bool(prompt),
                replaced=replaced,
                start_on=start_on,
                note=carry_note,
            )
        )
        if first_agent and not prompt:
            # Nothing was typed: the first agent step only waits for a turn.
            flight = await asyncio.to_thread(
                _flights.mutate,
                flight["id"],
                lambda x: x["step_states"][x["steps"][0]["id"]]["detail"].update(
                    no_prompt=True
                ),
            )
    if rec is not None:
        flight = await asyncio.to_thread(_take_over, flight, rec, lead)
        if absorbed is not None:
            absorbed.extend(s["config"]["lane"] for s in lead)
    for old in replaced:
        await asyncio.to_thread(_announce, None, old)
    _emit("flight.changed", flight)
    wake()
    return flight


def _session_busy(title: str) -> str:
    """Why the session's agent is on a turn right now (blocking) —
    ``"working"``, ``"loading"`` (a just-spawned worker still starting) or
    ``"queued"`` (its task waits in its prompt queue) — or "" when it is not:
    a route attached to a busy session must not count that unfinished work
    as its agent stop. A session with a LOOPING queue is never in progress:
    that queue always has items, so the stop would never settle."""
    try:
        q = _prompt_queue.get_state(title)
        obs = _session_obs({"session": title}, time.time(), agent=True)
    except Exception:  # noqa: BLE001 — unreadable: today's behaviour
        return ""
    if q.get("loop") and q.get("items"):
        # Its queue never empties, so an agent stop on it never settles:
        # never call a looping session's work "in progress".
        return ""
    if obs.get("pending") or obs.get("status") == "loading":
        return "loading"
    if obs.get("activity") == "working":
        return "working"
    if q.get("items") and (q.get("enabled") or q.get("held")):
        return "queued"
    return ""


def _fasttrack_to_absorb(title: str) -> Optional[dict]:
    """The session's own fast-track run a flight takes over: one still
    running, or one that stopped (``halted``) — never a finished one, nor a
    flight's own (blocking)."""
    rec = _autopilot.get(title)
    if not rec or rec.get("source") == "flight":
        return None
    return rec if rec.get("state") in ("running", "halted") else None


def _stage_of(title: str) -> str:
    """The session's git stage (``committed`` / ``pushed`` / ``pr`` …), read
    fresh the way the autopilot driver reads it, else off the published row."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is not None:
        try:
            return str((srv._session_stage(inst) or {}).get("stage") or "")
        except Exception:  # noqa: BLE001
            pass
    return str(_row(title).get("stage") or "")


def _take_over(flight: dict, rec: dict, lead: List[dict]) -> dict:
    """After a start absorbed ``rec``: say so in the flight's history and
    disarm the run, so the flight's ship steps carry the ladder on with no
    second driver (blocking). A record the flight's own step has armed in
    the meantime (``source == "flight"``) is left alone."""
    title = flight["session"]
    ids = [s["id"] for s in flight["steps"][flight["cursor"] :][: len(lead)]]
    halted = (
        str(rec.get("reason") or "shipping stopped")
        if rec.get("state") == "halted"
        else ""
    )
    after = _flights.mutate(
        flight["id"], lambda x: _flights.note_absorbed(x, ids, halted)
    )
    still = _autopilot.get(title)
    if still is not None and still.get("source") != "flight":
        _autopilot.disarm(title)
        try:
            _server()._publish_autopilot(title)
        except Exception:  # noqa: BLE001
            pass
    return after or flight


# --------------------------------------------------------------------------- #
# Carrying on: resume a route where it stopped, fly one again, and an Intake
# restart that finds its branch already checked out
# --------------------------------------------------------------------------- #
def _branch_of(title: str) -> str:
    inst = _server().ENGINE.instances.get(title) if title else None
    return str(getattr(inst, "Branch", "") or "") if inst is not None else ""


def resume_for(
    plan: dict,
    *,
    session: str = "",
    repo_root: str = "",
    branch: str = "",
    subject: Optional[dict] = None,
    exclude=(),
) -> Optional[dict]:
    """:func:`flights.resume_point` of ``plan`` against the earlier flights on
    ``session`` (its repo and branch read off the live session when not
    given), on ``branch`` of ``repo_root``, or of the same ticket / PR /
    issue ``subject`` (blocking). Never raises: unreadable = None."""
    try:
        if session:
            repo_root = repo_root or _repo_root(session)
            branch = branch or _branch_of(session)
        cands = _flights.resume_candidates(
            _flights.list_flights(include_finished=True),
            session=session,
            repo_root=repo_root,
            branch=branch,
            subject=subject,
            exclude=exclude,
        )
        return _flights.resume_point(plan, cands)
    except Exception as err:  # noqa: BLE001 — a lookup never blocks a start
        _warn("flights resume lookup failed: %v", err)
        return None


def resume_start_at(
    plan: dict, start_at: str, *, session: str = "", subject: Optional[dict] = None
) -> Tuple[str, Optional[dict]]:
    """A start's ``start_at`` with :data:`flights.RESUME` resolved
    (blocking): the resume point's stop id, or "" (the normal start) when
    there is none → ``(start_at, resume)``. Anything else is left alone."""
    if str(start_at or "").strip() != _flights.RESUME:
        return start_at, None
    if any(s.get("id") == _flights.RESUME for s in plan.get("steps") or []):
        return start_at, None
    point = resume_for(plan, session=session, subject=subject)
    return (point["stop"] if point else ""), point


#: How ``again`` found the session it flies on (the route's ``how``).
AGAIN_HOW = ("same", "chosen", "branch", "reopened")


def _closed_entry_for(f: dict) -> Optional[dict]:
    """The newest Recently-closed entry that holds ``f``'s work: its own
    chain's snapshot, its session's title, or (not main/master) its branch."""
    try:
        items = _server()._load_recently_closed()
    except Exception:  # noqa: BLE001
        return None
    branch = str(f.get("branch") or "")
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        snap = e.get("flight") if isinstance(e.get("flight"), dict) else {}
        if (
            snap.get("id") == f.get("id")
            or (f.get("session") and e.get("title") == f.get("session"))
            or (
                branch
                and branch not in _flights._SHARED_BRANCHES
                and e.get("branch") == branch
            )
        ):
            return e
    return None


async def _reopen_entry(entry_id: str) -> str:
    """Reopen a Recently-closed entry (the Recently closed dialog's own path)
    → the reopened session's title, "" when it could not be reopened."""
    import json as _json

    try:
        resp = await _server()._reopen_closed_entry(str(entry_id))
        body = _json.loads(resp.body)
    except Exception as err:  # noqa: BLE001
        _warn("reopen %s for a route failed: %v", entry_id, err)
        return ""
    if getattr(resp, "status_code", 500) >= 300 or not isinstance(body, dict):
        _warn("reopen %s for a route refused: %v", entry_id, body)
        return ""
    return str(body.get("title") or "")


def _branch_exists(repo: str, branch: str) -> bool:
    return bool(repo and branch) and _ref_ok(repo, "refs/heads/%s" % branch)


def _gone_refusal(f: dict) -> _flights.FlightError:
    title = f.get("session") or "its session"
    branch = str(f.get("branch") or "")
    repo = str(f.get("repo_root") or "")
    if branch and _branch_exists(repo, branch):
        detail = (
            "%s is gone, but its branch %s is still in %s — start a session on "
            "that branch, then fly it again on that session" % (title, branch, repo)
        )
    else:
        detail = (
            "%s is gone and nothing on this machine holds its work — fly it "
            "again on another session" % title
        )
    return _flights.FlightError("session_gone", 409, detail)


async def again(flight_id: str, body: Optional[dict] = None) -> dict:
    """``POST /api/flights/{id}/again``: fly an ended route (cancelled, done,
    halted) again — its own stops, a new flight, starting at ``at`` (a stop
    id; absent / "resume" = the first stop it had not finished, the first
    stop when it got past them all) — on ``title`` when given, else its own
    session; that gone, the live session that holds its branch, else the
    Recently-closed session that kept its work, reopened (which may bring
    the very route back as it was). → ``{"flight", "from", "session",
    "how"}`` (``how`` in :data:`AGAIN_HOW`; ``restored`` when the reopen
    brought this route itself back). Raises :class:`flights.FlightError`
    (404 ``not_found`` / ``unknown_session``, 409 ``in_flight`` /
    ``not_flown`` / ``session_gone``, 400 ``unknown_step``) and
    :class:`PreflightError`."""
    body = body or {}
    srv = _server()
    old = _flights.get(flight_id)
    if old is None:
        raise _flights.FlightError("not_found", 404, "no such route")
    plan, start_at = _flights.again_plan(old, str(body.get("at") or ""))
    title = str(body.get("title") or "").strip()
    how = "chosen"
    if title:
        if title not in srv.ENGINE.instances:
            raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
    else:
        title, how = str(old.get("session") or ""), "same"
        if not title or title not in srv.ENGINE.instances:
            title, how = "", ""
            holder = await asyncio.to_thread(
                branch_holder,
                str(old.get("repo_root") or ""),
                str(old.get("branch") or ""),
            )
            if holder and old.get("branch") not in _flights._SHARED_BRANCHES:
                title, how = holder, "branch"
            else:
                entry = await asyncio.to_thread(_closed_entry_for, old)
                if entry is not None:
                    title = await _reopen_entry(entry["id"])
                    how = "reopened" if title else ""
            if not title:
                raise await asyncio.to_thread(_gone_refusal, old)
            back = _flights.get(old["id"])
            if (
                how == "reopened"
                and back is not None
                and back["session"] == title
                and back["state"] in _flights.UNFINISHED
            ):
                # The reopen brought this very route back where it was.
                wake()
                return {
                    "flight": back,
                    "from": old["id"],
                    "session": title,
                    "how": "restored",
                }
    _refuse(
        await asyncio.to_thread(
            lambda: _preflight_safe(
                plan, repo="", session=title, attach=True, start_at=start_at
            )
        )
    )
    stop = next((s for s in plan["steps"] if s["id"] == start_at), plan["steps"][0])
    note = "flown again from route %s, at %s" % (old["id"], stop["title"])
    if how == "branch":
        note += " (on %s, which holds its branch)" % title
    elif how == "reopened":
        note += " (on %s, reopened)" % title
    replaced: list = []
    by = str(body.get("by") or "user")
    flight = await asyncio.to_thread(
        lambda: _flights.take_off(
            plan,
            session=title,
            subject=old.get("subject"),
            by=by,
            attach=True,
            start_at=start_at,
            repo_root=_repo_root(title) or str(old.get("repo_root") or ""),
            branch=_branch_of(title) or str(old.get("branch") or ""),
            replaced=replaced,
            note=note,
        )
    )
    for gone in replaced:
        await asyncio.to_thread(_announce, None, gone)
    _emit("flight.changed", flight)
    wake()
    return {"flight": flight, "from": old["id"], "session": title, "how": how}


def _holder_of(folder: str) -> str:
    """The session working in ``folder`` (blocking): a live one of this
    server, else one another server on this machine saved in state.json (it
    is adopted here within seconds), else ""."""
    srv = _server()
    real = os.path.realpath(folder)
    for t, inst in list(srv.ENGINE.instances.items()):
        try:
            wp = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            wp = ""
        if wp and os.path.realpath(wp) == real:
            return str(t)
    return _disk_holder(real)


def _disk_holder(real: str) -> str:
    """The session state.json says works in the folder ``real`` (another
    server on this machine saved it; this one adopts it within seconds), ""
    when none or unreadable (blocking)."""
    try:
        import json as _json

        from backend.config import config as _config

        raw = _config.LoadState().GetInstances()
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        for x in _json.loads(raw) if raw else []:
            wt = (x.get("worktree") or {}) if isinstance(x, dict) else {}
            wp = str(wt.get("worktree_path") or "")
            if wp and os.path.realpath(wp) == real and x.get("title"):
                return str(x["title"])
    except Exception:  # noqa: BLE001 — unreadable: only this server's view
        pass
    return ""


def _closed_holding(folder: str) -> Optional[dict]:
    """The newest Recently-closed entry whose kept folder is ``folder``."""
    try:
        items = _server()._load_recently_closed()
    except Exception:  # noqa: BLE001
        return None
    real = os.path.realpath(folder)
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        wp = str(
            (data.get("worktree") or {}).get("worktree_path") or e.get("folder") or ""
        )
        if wp and os.path.realpath(wp) == real:
            return e
    return None


async def _open_in_place(title: str, folder: str) -> str:
    """A session on a folder no session or closed entry accounts for (the
    run whose session a restart lost): in place, so ending it leaves the
    work exactly where it is — what Intake's Reopen does for such a folder.
    → its title, "" when it could not start."""
    srv = _server()
    name = title if title not in srv.ENGINE.instances else srv._unique_title(title)
    program = srv.ENGINE.default_program()
    try:
        inst = srv.session.NewInstance(
            srv.session.InstanceOptions(
                title=name, path=folder, program=program, in_place=True
            )
        )
        inst.ExtraEnv = srv._ports.env_for(name)
        inst.SetStatus(srv.Loading)
    except Exception as err:  # noqa: BLE001
        _warn("open %s in place failed: %v", folder, err)
        return ""
    with srv.ENGINE.lock:
        if name in srv.ENGINE.instances:
            return ""
        srv.ENGINE.instances[name] = inst
    srv._seed_event_snapshot(name)
    srv._events.BUS.emit(
        "session.created",
        session=name,
        new="loading",
        data={"program": program, "reopened": folder},
    )
    try:
        await asyncio.to_thread(inst.Start, True)
        srv.ENGINE.save()
    except Exception as err:  # noqa: BLE001
        _warn("open %s in place failed: %v", folder, err)
        srv._drop_failed_start(name, inst)
        return ""
    return name


class CarryOnRefused(RuntimeError):
    """An Intake restart whose branch is held where no session here can take
    it over: one plain sentence naming what to close."""


async def carry_on_intake(title: str, repo_url: str, branch: str) -> str:
    """An Intake start of ``title`` (a route armed on it) whose ``branch`` is
    already checked out — the leftover nothing could reclaim — carries on in
    the work that is there instead of dying at its first stop:

    * a live session (this server's, or one another server on this machine
      saved) works in that worktree → the route moves onto it;
    * a closed session kept it → that session is reopened (Recently closed)
      and the route moves onto it;
    * no session accounts for it → a session is opened in place on it.

    The route starts at its resume point (:func:`flights.resume_point` of the
    earlier flights on that session / branch / ticket), else past its leading
    Agent stops (the work already there is them). Nothing is ever deleted.
    → the session that carries it; "" when the branch is free (or no route
    is armed on ``title``): create the session as usual. Raises
    :class:`CarryOnRefused` when none of those could take it."""
    if not branch:
        return ""
    try:
        armed = await asyncio.to_thread(_flights.for_session, title)
    except Exception as err:  # noqa: BLE001 — no store, no route to carry
        _warn("carry on %s: routes unreadable: %v", title, err)
        return ""
    if armed is None:
        return ""
    from backend.web.core import worktree_reclaim as _reclaim

    held = await asyncio.to_thread(_reclaim.worktree_for_launch, repo_url, branch)
    if not held:
        return ""
    holder = await asyncio.to_thread(_holder_of, held)
    how = "already had this branch"
    entry = None
    if not holder:
        entry = await asyncio.to_thread(_closed_holding, held)
        if entry is not None:
            holder = await _reopen_entry(entry["id"])
            how = "reopened: it had kept this branch's work"
    if not holder:
        holder = await _open_in_place(title, held)
        how = "opened on the folder that holds this branch"
    if not holder:
        who = (entry or {}).get("title") or held
        raise CarryOnRefused(
            "branch %s is already checked out at %s and no session here could "
            "take it over — close %s, then Run it again" % (branch, held, who)
        )
    await asyncio.to_thread(_move_armed, armed["id"], holder, branch, how)
    return holder


def _move_armed(fid: str, holder: str, branch: str, how: str) -> Optional[dict]:
    """Move the armed intake flight ``fid`` onto ``holder`` at its resume
    point (blocking). A holder already on a route keeps it: the armed one is
    cancelled saying so (a halted one there is replaced, as a start does)."""
    armed = _flights.get(fid)
    if armed is None or armed["state"] in _flights.FINAL:
        return None
    other = _flights.for_session(holder) if holder != armed["session"] else None
    if other is not None and other["id"] != fid:
        if other["state"] in _flights.HOLDING:
            reason = (
                "%s already holds this branch and is on its route (%s) — that "
                "route carries on there"
                % (holder, other.get("plan_name") or other["id"])
            )
            after = _flights.mutate(fid, lambda x: _flights.op_cancel(x, reason))
            _announce(armed, after)
            return after
        if other["state"] == "halted":
            gone = _flights.mutate(
                other["id"], lambda x: _flights.op_cancel(x, "replaced by a new route")
            )
            _announce(other, gone)
    root = _repo_root(holder) or armed.get("repo_root") or ""
    plan = {"id": armed["plan_id"], "steps": armed["steps"]}
    point = resume_for(
        plan,
        session=holder,
        repo_root=root,
        branch=branch,
        subject=armed.get("subject"),
        exclude=(fid,),
    )
    if point is not None:
        cursor = int(point["index"])
        note = "%s — %s %s" % (_flights.carry_on_note(point), holder, how)
    else:
        steps = armed["steps"]
        cursor = next(
            (i for i, s in enumerate(steps) if s["kind"] != "agent"), len(steps)
        )
        note = "carried on in %s (%s)" % (holder, how)
        if cursor:
            note += ": the work already on its branch counts as its Agent stop"

    def fn(x: dict) -> None:
        _flights.op_carry_on(x, holder, cursor, note)
        if root:
            x["repo_root"] = root
        x["branch"] = branch or x.get("branch") or ""

    after = _flights.mutate(fid, fn)
    _announce(armed, after)
    wake()
    return after


_OPS = {
    "done": _flights.op_done,
    "skip": _flights.op_skip,
    "retry": _flights.op_retry,
    "jump": _flights.op_jump,
    "pause": _flights.op_pause,
    "resume": _flights.op_resume,
    "cancel": _flights.op_cancel,
    "rename": _flights.op_rename,
}


def run(flight_id: str) -> dict:
    """``POST /api/flights/{id}/run`` (▶ Run, MCP ``control_flight run``): a
    ready flight takes off at cursor 0 (:func:`flights.op_take_off` by
    ``run``) — its first stop starts on the next pass. Raises
    :class:`flights.FlightError` (404 ``unknown_flight``, 409 ``not_ready``)
    and :class:`flights.PlanError` (a stop not ready to fly)."""
    return _changed(flight_id, lambda f: _flights.op_take_off(f, "run"))


def set_start_on(flight_id: str, value) -> dict:
    """``PATCH /api/flights/{id} {start_on}``: how a ready flight starts
    (:func:`flights.op_start_on`; ``"now"`` runs it)."""
    return _changed(flight_id, lambda f: _flights.op_start_on(f, value))


#: Why a stop is parked on the person where Done means "check again" — a
#: retry of the stop: Verify's check stopped before it finished
#: (``blocked``), or memory kept it from starting (``memory``, any stop that
#: waits on memory to start something — Verify, a reviewer).
_RETRY_ASKS = ("blocked", "memory")


def _needs_you_perch(flight: dict, sid: str) -> bool:
    """Whether ``sid`` is the current stop, parked on the person for a reason
    where Done means "I sorted it — try again" (:data:`_RETRY_ASKS`): a
    Verify stop whose check could not finish, or a stop memory kept from
    starting its check or its reviewer."""
    cur = _flights._current(flight)
    ss = flight["step_states"].get(sid) or {}
    ask = (ss.get("detail") or {}).get("ask")
    return (
        cur is not None
        and cur["id"] == sid
        and ss.get("state") == "waiting_you"
        and not (ss.get("detail") or {}).get("failed_reason")
        and (
            (cur["kind"] == "verify" and ask in _RETRY_ASKS)
            or (ask == "memory" and cur["kind"] in _LAUNCHED_WORKER)
        )
    )


def _confirm_perch(flight: dict, sid: str) -> str:
    """The plan id when ``sid`` is a Verify stop waiting on a person to
    confirm its check (``ask`` "confirm"), else ``""``."""
    cur = _flights._current(flight)
    ss = flight["step_states"].get(sid) or {}
    d = ss.get("detail") or {}
    if (
        cur is not None
        and cur["id"] == sid
        and cur["kind"] == "verify"
        and ss.get("state") == "waiting_you"
        and d.get("ask") == "confirm"
    ):
        return str(d.get("plan") or "")
    return ""


def _no_blanket_done(flight: dict, sid: str) -> None:
    """There is no blanket Done on a Verify stop waiting on you: Done is
    "Confirm verified" on a check waiting for your confirm, "check again" on
    one that could not finish (:func:`_needs_you_perch`), and refused
    anywhere else — it would pass a check nobody saw. A Verify stop whose
    FAILURE was handed to you (``on_fail`` you) still takes Done: that
    answers the failure, not the check. (The machine word stays
    ``answer_behaviors``: clients read it.)"""
    cur = _flights._current(flight)
    ss = flight["step_states"].get(sid) or {}
    if (
        cur is not None
        and cur["id"] == sid
        and cur["kind"] == "verify"
        and ss.get("state") == "waiting_you"
        and not (ss.get("detail") or {}).get("failed_reason")
    ):
        raise _flights.FlightError(
            "answer_behaviors",
            409,
            "%s isn't verified yet — Retry to verify again, or Skip" % cur["title"],
        )


def fix(flight_id: str, step_id: str) -> dict:
    """The "Let the agent fix it" op on a halted stop (``POST
    /api/flights/{id}/steps/{sid}/op/fix``): the failure goes to the session's
    agent as a prompt, the stop waits for its turn to end, then runs again —
    what ``on_fail: agent`` does by itself, asked for once by hand. A failed
    Verify stop checks its behaviors again after the fix. 409 ``not_failed``
    on a stop that has not failed; 409 ``no_session`` with no agent to ask."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    if not before.get("session"):
        raise _flights.FlightError(
            "no_session", 409, "this route has no session whose agent could fix it"
        )
    after = _flights.mutate(flight_id, lambda f: _flights.op_fix(f, step_id))
    if after is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    step = next((x for x in after["steps"] if x["id"] == step_id), None)
    reason = str(
        (after["step_states"].get(step_id) or {}).get("detail", {}).get("reason") or ""
    )
    try:
        _deliver_fix(after, step or {"title": step_id}, {"reason": reason})
    except Exception as err:  # noqa: BLE001 — _StepFailed, or the queue's own
        # The agent can't be asked: the stop halts again, saying why.
        said = str(err) or "the agent could not be asked"
        after = (
            _flights.mutate(
                flight_id, lambda f: _flights.op_fix_failed(f, step_id, said)
            )
            or after
        )
    _announce(before, after)
    wake()
    return after


def operate(flight_id: str, op: str, step: str = "", note: str = "") -> dict:
    """A route's operation on a flight (blocking). Raises
    :class:`flights.FlightError` (404 unknown flight, 409 refusals)."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    pid = _confirm_perch(before, step) if op == "done" else ""
    if pid:
        # "Verified": the person confirms the check the agent left with them
        # (test_plans.confirm_verify); the judge moves the stop on next pass.
        from backend.web.core import test_plans as _tp

        try:
            _tp.confirm_verify(pid, True, note)
        except _tp.NotNeedsYou as err:
            raise _flights.FlightError("not_needs_you", 409, str(err))
        wake()
        return _flights.get(flight_id) or before
    perch = _needs_you_perch(before, step)
    if op == "done" and perch:
        # "I sorted it": the stop runs again — Verify checks again, memory
        # gets another try.
        op = "retry"
    fn = _OPS[op]
    if op == "done":
        _no_blanket_done(before, step)

    def _do(f: dict) -> None:
        if op == "retry" and perch and _needs_you_perch(f, step):
            # The planner retries a failed stop; a park whose sentence says
            # "press Retry" is one, so Retry is never refused there.
            f["step_states"][step]["state"] = "failed"
        if op == "done":
            fn(f, step, note)
        elif op in ("skip", "retry", "jump"):
            fn(f, step)
        elif op == "rename":
            fn(f, note)
        else:
            fn(f)

    after = _flights.mutate(flight_id, _do)
    if after is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    _close_abandoned_reviewers(before, after)
    _announce(before, after)
    wake()
    return after


def _is_its_reviewer(session: str, title: str, since: float) -> bool:
    """``title`` is still the reviewer that stop spawned: a child of the
    route's session, created once the stop started. Reviewer titles are
    reused (review-N): a namesake under another session — or one made after a
    hand-deleted reviewer — must never be closed by a stale route."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None or str(getattr(inst, "Parent", "") or "") != session:
        return False
    try:
        born = float(srv._created_epoch(inst) or 0.0)
    except Exception:  # noqa: BLE001
        return False
    return not since or born >= since - 5.0


def _cancel_queued(title: str, parent: str) -> bool:
    """Drop ``title`` from the launch gate's queue when it still waits there
    as ``parent``'s worker (never started: nothing to close). True if it was
    dropped."""
    try:
        from backend.web.core import launch_gate as _lg

        rec = _lg.waiting(title)
        if rec is None or str(rec.get("parent") or "") not in ("", parent):
            return False
        return bool(_lg.cancel(title))
    except Exception as err:  # noqa: BLE001 — never fails the op
        _warn("flights: dropping queued %s: %v", title, err)
        return False


def _close_abandoned_reviewers(before: dict, after: dict) -> List[str]:
    """Close the reviewer of every ``review`` stop that was waiting on one
    and no longer is — jumped back over, skipped, retried, cancelled or
    removed (blocking). Left open, it ran on with no stop to read its
    verdict, and the agent stop the route went back to counted it as one of
    its own workers. Closed the way the stop closes a finished one (its
    worktree goes only when clean and with no commits of its own); ``keep``
    leaves it. → the titles closed."""
    from backend.web.core.flight_kinds import review as _review

    title = str(before.get("session") or "")
    out: List[str] = []
    if not title:
        return out
    for s in before.get("steps") or []:
        if s.get("kind") != "review" or (s.get("config") or {}).get("keep"):
            continue
        was = before["step_states"].get(s["id"]) or {}
        kids = [str(k) for k in (was.get("detail") or {}).get("children") or [] if k]
        if was.get("state") not in ("running", "waiting_you") or not kids:
            continue  # waiting_you: parked while its reviewer waited on memory
        now = (after.get("step_states") or {}).get(s["id"]) or {}
        if (
            after.get("state") not in _flights.FINAL
            and now.get("state") == was.get("state")
            and [str(k) for k in (now.get("detail") or {}).get("children") or []]
            == kids
        ):
            # Still its reviewer. A cancelled route leaves the stop "running"
            # with its child, but nothing will read that verdict any more.
            continue
        for k in kids:
            if _cancel_queued(k, title):
                out.append(k)  # it never started: it leaves the launch queue
                continue
            if not _is_its_reviewer(title, k, float(was.get("since") or 0.0)):
                continue  # gone, or the title now names someone else's session
            try:
                if _review._close(title, k):
                    out.append(k)
            except Exception as err:  # noqa: BLE001 — never fails the op
                _warn("flight %s: closing reviewer %s: %v", after.get("id"), k, err)
    return out


def _changed(flight_id: str, fn) -> dict:
    """Run ``fn`` on the stored flight under the lock (a refusal raised in it
    writes nothing), then say so — ``flight.changed`` even when the cursor did
    not move — and wake the loop."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    after = _flights.mutate(flight_id, fn)
    if after is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    if _view_key(before) == _view_key(after):
        _emit("flight.changed", after)
    _announce(before, after)
    _stop_removed(before, after)
    _close_abandoned_reviewers(before, after)
    wake()
    return after


def change_steps(
    flight_id: str,
    op: str,
    step: str = "",
    raw=None,
    at: Optional[int] = None,
    group=None,
) -> dict:
    """Add / edit / remove / move steps of a flight in the air or on the
    ground (blocking): the change is checked against the flight as stored,
    under the lock, so a driver pass that moved it meanwhile wins and the
    route gets its 409. ``add_steps`` takes a list in ``raw`` (and ``group``:
    ``{name}``); ``add_plan`` a plan id; ``remove_group`` a step of the group;
    ``ungroup`` the group id in ``step``; ``add_parallel`` the step in
    ``raw`` placed in ``step``'s parallel group (round 20). Raises
    :class:`flights.FlightError` / :class:`flights.PlanError`."""
    plan = None
    if op == "add_plan":
        plan = _flights.get_plan(str(raw or ""))
        if plan is None:
            raise _flights.FlightError("unknown_plan", 404, "no such saved route")

    def _do(f: dict) -> None:
        if op == "add":
            _flights.op_add_step(f, raw, at)
        elif op == "add_parallel":
            _flights.op_add_parallel(f, raw, step, at)
        elif op == "add_steps":
            _flights.op_add_steps(f, raw, at, group)
        elif op == "add_plan":
            _flights.op_add_plan(f, plan, at)
        elif op == "edit":
            _flights.op_edit_step(f, step, raw)
        elif op == "move":
            _flights.op_move_step(f, step, at)
        elif op == "ungroup":
            _flights.op_ungroup(f, step)
        elif op == "remove_group":
            _flights.op_remove_group(f, step)
        else:
            _flights.op_remove_step(f, step)
        # A Loop back to the agent on a chain running in a folder: no agent.
        _flights.check_agent_loops(f)

    return _changed(flight_id, _do)


def layout(flight_id: str, positions) -> dict:
    """``POST /api/flights/{id}/layout``: free positions of many stops at once
    (:func:`flights.op_layout`, layout only — any step, any state)."""
    return _changed(flight_id, lambda f: _flights.op_layout(f, positions))


def split_off(flight_id: str, sid: str, pos=None) -> Tuple[dict, dict]:
    """``POST /api/flights/{id}/steps/{sid}/split_off``: ``sid`` and every
    stop after it leave into a new grounded flight → ``(flight, chain)``
    (:func:`flights.split_off`), both announced, the loop woken."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    after, chain = _flights.split_off(flight_id, sid, pos)
    if _view_key(before) == _view_key(after):
        _emit("flight.changed", after)
    _announce(before, after)
    _stop_removed(before, after)
    _close_abandoned_reviewers(before, after)
    _emit("flight.changed", chain)
    wake()
    return after, chain


def group_steps(flight_id: str, sids, name) -> Tuple[dict, dict]:
    """``POST /api/flights/{id}/group``: the steps ``sids`` become one new
    stop → ``(flight, group)`` (blocking)."""
    made: dict = {}

    def _do(f: dict) -> None:
        made.update(_flights.op_group(f, sids, name))

    return _changed(flight_id, _do), made


def _stop_removed(before: dict, after: dict) -> None:
    """Stop the side effects of steps a removal took out (docs/flights.md
    "Removing the stop that is flying"): the CURRENT one's armed lane (a
    ``ship`` step's, or a ``reviews`` step's push) is disarmed, any step's
    command job is forgotten. An ``agent`` step types nothing more; a
    ``split``'s workers are left alive."""
    gone = [s for s in before["steps"] if s["id"] not in after["step_states"]]
    if not gone:
        return
    with _JOBS_LOCK:
        for s in gone:
            _JOBS.pop((after["id"], s["id"]), None)
    cur = _flights._current(before)
    if (
        not _flights._on_ground(before)
        and cur is not None
        and cur in gone
        and cur["kind"] in ("ship", "reviews")
    ):
        _disarm_ours(after["id"], before["session"])


def _disarm_ours(flight_id: str, title: str) -> bool:
    """Disarm the session's autopilot record when THIS flight armed it and it
    has not finished (the matching half of ``lanes.arm_session(source=
    "flight", item=<flight id>)``); your own fast-track run is left alone."""
    rec = _autopilot.get(title) if title else None
    if (
        not rec
        or rec.get("source") != "flight"
        or rec.get("item") != flight_id
        or rec.get("state") == "done"
    ):
        return False
    _autopilot.disarm(title)
    try:
        _server()._publish_autopilot(title)
    except Exception:  # noqa: BLE001
        pass
    return True


async def ground(body: dict) -> dict:
    """``POST /api/flights/start {grounded: true, plan_id? | steps?, name?}``:
    a flight with no session (:func:`flights.ground`). Raises
    :class:`flights.FlightError` (404 ``unknown_plan``, 400 ``needs_steps``)
    and :class:`flights.PlanError`."""
    body = body or {}
    plan_id = str(body.get("plan_id") or "")
    plan = None
    if plan_id:
        plan = _flights.get_plan(plan_id)
        if plan is None:
            raise _flights.FlightError("unknown_plan", 404, "no such saved route")
    flight = await asyncio.to_thread(
        _flights.ground,
        plan,
        body.get("steps"),
        str(body.get("name") or ""),
        str(body.get("by") or "user"),
    )
    _emit("flight.changed", flight)
    return flight


async def attach(flight_id: str, body: dict) -> dict:
    """``POST /api/flights/{id}/attach {title}``: bind a grounded flight to a
    live session and take off (:func:`flights.attach`). ``{title: "",
    repo_path, program?, model?, in_place?, prompt?}`` first creates the
    session the way :func:`start` does for a saved plan with no title — a
    first ``agent`` step's prompt is its initial prompt — then attaches.
    Every refusal comes before a session is created. ``start_on``
    ``"prompt"`` / ``"run"`` (docs/flights.md "Round 19"; absent =
    ``"now"``): bound but left ``ready``, and a session made for it gets no
    prompt. Raises :class:`flights.FlightError`."""
    srv = _server()
    body = body or {}
    start_on = _flights.normalize_start_on(body.get("start_on"))
    ready = start_on != "now"
    f = _flights.get(flight_id)
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    if f["state"] != _flights.GROUNDED:
        raise _flights.FlightError(
            "not_grounded", 409, "this route already has a session"
        )
    _flights.validate_for_takeoff(f)
    title = str(body.get("title") or "").strip()
    folder = str(body.get("folder") or "").strip()
    if title == DEFAULT_SESSION or folder:
        # No session picked: the chain runs in a folder (the default one
        # unless named). Nothing is started for a chain that needs no
        # session; one that does gets a new session there.
        folder = (
            os.path.expanduser(folder)
            if folder
            else await asyncio.to_thread(_default_folder)
        )
        if not os.path.isdir(folder):
            raise _flights.FlightError("bad_folder", 400, "no folder %s" % folder)
        if not needs_session(f):
            flight = await asyncio.to_thread(
                _flights.attach,
                flight_id,
                "",
                folder,
                "",
                False,
                None,
                None,
                start_on,
            )
            _emit("flight.changed", flight)
            wake()
            return flight
        title = ""
        body = dict(body, repo_path=folder)
    seeded = False
    no_prompt = False
    if title:
        if title not in srv.ENGINE.instances:
            raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
        if _flights.for_session(title) is not None:
            raise _flights.FlightError(
                "in_flight", 409, "%s is already on a route" % title
            )
        _refuse(await asyncio.to_thread(_preflight_attach, f, title, ""))
    else:
        plan = _flights.get_plan(f["plan_id"]) or {}
        defaults = dict(_flights._blank_session(), **(plan.get("session") or {}))
        for key, field in (("program", "program"), ("model", "model")):
            if str(body.get(key) or "").strip():
                defaults[field] = str(body[key]).strip()
        if "in_place" in body:
            defaults["in_place"] = bool(body.get("in_place"))
        first = _flights._current(f)
        first_agent = (
            first is not None
            and first["kind"] == "agent"
            and not first.get("halt")
            and not ready
        )
        prompt = "" if ready else str(body.get("prompt") or "").strip()
        if first_agent and not prompt:
            prompt = _flights.fill_prompt(first["config"].get("prompt") or "", f)
        repo = str(body.get("repo_path") or "").strip() or defaults["repo_path"]
        if not repo:
            raise _flights.FlightError(
                "no_repo", 400, "pick a folder for the new session"
            )
        _refuse(await asyncio.to_thread(_preflight_attach, f, "", repo))
        title = await _create_session(_session_payload(defaults, repo, prompt))
        seeded = first_agent and bool(prompt)
        no_prompt = first_agent and not prompt
    inst = srv.ENGINE.instances.get(title)
    root = await asyncio.to_thread(_repo_root, title)
    flight = await asyncio.to_thread(
        _flights.attach,
        flight_id,
        title,
        root,
        str(getattr(inst, "Branch", "") or ""),
        seeded,
        None,
        None,
        start_on,
    )
    if no_prompt:
        # Nothing was typed: the first agent step only waits for a turn.
        flight = await asyncio.to_thread(
            _flights.mutate,
            flight["id"],
            lambda x: x["step_states"][x["steps"][0]["id"]]["detail"].update(
                no_prompt=True
            ),
        )
    _emit("flight.changed", flight)
    wake()
    return flight


def _preflight_attach(f: dict, title: str, repo: str) -> List[dict]:
    """:func:`preflight` for a grounded flight about to fly: from its cursor,
    on ``title`` (an existing session) or a new session in ``repo``."""
    steps = f.get("steps") or []
    cur = int(f.get("cursor") or 0)
    at = steps[cur]["id"] if 0 <= cur < len(steps) else ""
    return _preflight_safe(
        {"steps": steps},
        repo=repo,
        session=title or None,
        attach=False,
        start_at=at or None,
    )


def _preflight_safe(plan, **kw) -> List[dict]:
    """:func:`preflight` for a start that must go on when the CHECK itself
    breaks: the pre-check exists to refuse a start that would fail later, so a
    bug inside it is logged and reads as "no problems" — it never blocks a
    route. A refusal it means to make (a ``FlightError``) still raises."""
    fn = globals().get("preflight")
    if not callable(fn):
        return []
    try:
        return fn(plan, **kw) or []
    except _flights.FlightError:
        raise
    except Exception as err:  # noqa: BLE001 — a broken check never blocks
        _warn("flights preflight failed: %v", err)
        return []


def step_op(flight_id: str, step_id: str, name: str, body: Optional[dict]) -> dict:
    """``POST /api/flights/{id}/steps/{sid}/op/{name}``: a plug-in kind's own
    operation on one of its stops (``Kind.ops``), the store announced after
    it and the loop woken. 404 ``unknown_flight`` / ``unknown_step`` /
    ``unknown_op``."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    step = next((s for s in before["steps"] if s["id"] == step_id), None)
    if step is None:
        raise _flights.FlightError("unknown_step", 404, "no such stop")
    plug = _kinds.get(step["kind"])
    op = plug.ops.get(name) if plug is not None else None
    if op is None and name == "fix":
        # The built-in "Let the agent fix it" on any halted stop.
        return {"flight": fix(flight_id, step_id)}
    if op is None:
        raise _flights.FlightError(
            "unknown_op", 404, "%s has no operation %r" % (step["kind"], name)
        )
    out = op(copy.deepcopy(before), copy.deepcopy(step), dict(body or {}))
    after = _flights.get(flight_id)
    if after is not None and not _same(before, after):
        _announce(before, after)
    wake()
    return out if isinstance(out, dict) else {"flight": after}


def remove(flight_id: str) -> None:
    """``DELETE /api/flights/{id}``: finished, halted or grounded flights
    only — a running, waiting or paused one must be cancelled first."""
    f = _flights.get(flight_id)
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such route")
    if f["state"] in _flights.HOLDING and f["state"] != _flights.READY:
        # A ready chain has not started anything: it goes at once.
        raise _flights.FlightError("in_flight", 409, "stop the route first")
    try:
        # A checkpoint stop pins work under refs/mindflock/flights/<fid>/…
        from backend.web.core.flight_kinds import checkpoint as _checkpoint

        _checkpoint.forget(f)
    except Exception:  # noqa: BLE001 — the refs are a convenience, never a block
        pass
    try:
        # listen / someone stops minted inbound hook tokens: spend them.
        from backend.web.core.flight_kinds import _hooks

        _hooks.forget_flight(flight_id)
    except Exception:  # noqa: BLE001 — a spent token answers 404 anyway
        pass
    if f["state"] not in ("done", "halted", "cancelled"):
        _take_down_previews(f)  # a grounded one never put a preview up; cheap
    _flights.delete(flight_id)
    with _JOBS_LOCK:
        for k in [k for k in _JOBS if k[0] == flight_id]:
            _JOBS.pop(k, None)


def list_dto(all_: bool = False) -> List[dict]:
    """``GET /api/flights``: unfinished + the last 20 finished (all with
    ``all_``)."""
    flights = _flights.list_flights()
    if not all_:
        live = [f for f in flights if f["state"] not in _flights.FINAL]
        landed = sorted(
            (f for f in flights if f["state"] in _flights.FINAL),
            key=lambda f: -(f["finished"] or f["updated"]),
        )[:20]
        flights = live + landed
    notes = lane_notes()
    return [_flights.with_returns(f, notes) for f in flights]


def flight_dto(flight: dict) -> dict:
    """One flight as the API sends it: plus ``returns`` (which stops can go
    back to the agent, and why — ``flights.returns_of``)."""
    return _flights.with_returns(flight, lane_notes())


def lane_notes() -> bool:
    """Whether a lane hands its failures back to the agent at all
    (``repository.fasttrack_failure_notes``; unreadable settings: yes)."""
    try:
        from backend.config import settings as _settings

        return bool(_settings.load_settings().repository.fasttrack_failure_notes)
    except Exception:  # noqa: BLE001 — the default
        return True
