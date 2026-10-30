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
import os
import subprocess
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from backend.web.core import autopilot as _autopilot
from backend.web.core import events as _events
from backend.web.core import flights as _flights
from backend.web.core import prompt_queue as _prompt_queue

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


def _log(fmt: str, *args) -> None:
    try:
        from backend import log

        if log.ErrorLog is not None:
            log.ErrorLog.Printf(fmt, *args)
    except Exception:  # noqa: BLE001
        pass


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
    return (f["state"], f["cursor"], step.get("state"), f["reason"], f["session"])


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
        rounds = after["step_states"][step["id"]]["rounds"]
        key = "%s:%d:you" % (step["id"], rounds)
        cur = _flights._current(after)
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
    if keys:

        def _mark(f: dict) -> None:
            f["announced"] = (f["announced"] + keys)[-20:]

        _flights.mutate(after["id"], _mark)


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
        return {"present": False}
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


def _verify_obs(flight: dict, ss: dict, sess: dict) -> dict:
    """Checklist facts for a verify step (blocking)."""
    from backend.web.core import test_plans as _tp

    title = flight["session"]
    pid = str(ss["detail"].get("plan") or "")
    if not pid:
        repo = flight["repo_root"] or _repo_root(title)
        branch = sess.get("branch") or flight["branch"]
        if not branch:
            return {}
        pid = _tp.owner_for_branch(title, repo, branch)
    plan = _tp.get(pid) if pid else None
    if plan is None:
        return {}
    run = plan["runs"][-1] if plan.get("runs") else None
    results = (run or {}).get("results") or {}
    open_agent = [
        s["text"]
        for s in plan["steps"]
        if s.get("actor") == "agent" and not (results.get(s["id"]) or {}).get("result")
    ]
    return {
        "plan": pid,
        "branch": plan.get("branch") or "",
        "state": plan.get("state") or "",
        "error": plan.get("error") or "",
        "steps": [s["id"] for s in plan["steps"]],
        "settled": _tp.is_settled(plan),
        "open_human": [s["text"] for s in _tp.open_human_steps(plan)],
        "open_agent": open_agent,
        "failed": [f["step"]["text"] for f in _tp.failed_steps(plan)],
        "live": plan.get("state") in ("due", "done") or bool(plan.get("live_at")),
        "live_branch": plan.get("live_branch") or "",
    }


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


async def _reviews_obs(flight: dict, step: dict, sess: dict, now: float) -> dict:
    """``{"ids"}`` of the PR's actionable comments (None = could not ask),
    fetched at most once a minute per flight."""
    from backend.ticket_ingestion import pr_comments as _prc
    from backend.web.core import github_pr as _gh

    cache = _REVIEWS.get(flight["id"])
    if cache and now - cache["at"] < _REVIEWS_EVERY_S:
        return {"ids": cache["ids"]}
    ids = None
    comments: Dict[str, object] = {}
    pr = None
    try:
        pr, _slug = await _pr_of(sess, flight)
        if pr is not None:
            got = await _prc.fetch_actionable_comments(pr, _gh._github_config())
            for c in got:
                if not step["config"].get("include_bots", True) and _is_bot(c.author):
                    continue
                comments[str(c.id)] = c
            ids = list(comments)
    except Exception as err:  # noqa: BLE001 — "could not ask" is not "no comments"
        _log("flight %s: review comments unavailable: %v", flight["id"], err)
        ids = None
    _REVIEWS[flight["id"]] = {"at": now, "ids": ids, "comments": comments, "pr": pr}
    return {"ids": ids}


async def _checks_obs(flight: dict, sess: dict, now: float) -> dict:
    from backend.web.core import github_pr as _gh

    cache = _CHECKS.get(flight["id"])
    if cache and now - cache["at"] < _CHECKS_EVERY_S:
        return cache
    wt, branch = sess.get("folder") or "", sess.get("branch") or ""
    verdict, failing = "unknown", []
    if wt and branch:
        try:
            verdict = await _gh.pr_checks(wt, branch)
            if verdict == "failed":
                failing = await _failing_checks(wt, branch)
        except Exception:  # noqa: BLE001
            verdict = "unknown"
    out = {"at": now, "verdict": verdict, "failing": failing}
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


async def observe(flight: dict, now: float) -> dict:
    """The observation :func:`flights.next_action` decides from."""
    step = _flights._current(flight)
    if step is None:
        return {"now": now}
    kind = step["kind"]
    ss = flight["step_states"][step["id"]]
    agent = kind in ("agent", "reviews") or ss["detail"].get("phase") == "fix"
    sess = await asyncio.to_thread(_session_obs, flight, now, agent)
    obs: dict = {"now": now, "session": sess}
    if ss["state"] not in ("running", "waiting_you"):
        return obs
    if kind == "ship" or (kind == "reviews" and ss["detail"].get("phase") == "push"):
        obs["autopilot"] = await asyncio.to_thread(_autopilot.get, flight["session"])
    if kind == "verify":
        try:
            obs["verify"] = await asyncio.to_thread(_verify_obs, flight, ss, sess)
        except Exception:  # noqa: BLE001
            obs["verify"] = {}
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
            _log("flight %s: workers unreadable: %v", flight["id"], err)
            obs["children"] = None
    return obs


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
class _StepFailed(Exception):
    """An executor's refusal: becomes a ``fail`` action with this reason
    (and ``detail``, what the step still records — a split's workers made)."""

    def __init__(self, reason: str, detail: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.detail = detail


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


async def _create_session(payload: dict) -> str:
    from backend.web.core import session_create as _session_create

    status, body = await _session_create.create_result(payload)
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
        try:
            title = await _create_session(
                _session_payload(plan.get("session") or {}, repo, text)
            )
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
            raise _StepFailed("a peer's session can't be fast-tracked from here")
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
    """Ask for the session's checklist (Verify's own manual path: create +
    generate on a daemon thread). The plan id is observed on later passes."""
    srv = _server()
    title = flight["session"]
    try:
        srv._ensure_test_plan(title, manual=True)
    except Exception as err:  # noqa: BLE001
        raise _StepFailed("could not ask for the checklist: %s" % err)
    return {}


async def _start_verify_run(plan_id: str) -> None:
    """``POST /api/test-plans/{id}/run`` in process (its body is the route)."""
    srv = _server()
    resp = await srv.run_test_plan(plan_id, None, {})
    if resp.status_code >= 400 and resp.status_code != 409:
        import json as _json

        try:
            msg = _json.loads(resp.body).get("error") or ""
        except Exception:  # noqa: BLE001
            msg = ""
        raise _StepFailed(msg or "the checklist run could not start")


def _start_command(flight: dict, step: dict, folder: str) -> None:
    key = (flight["id"], step["id"])
    cmd = step["config"]["command"]
    timeout = step["config"]["timeout_minutes"] * 60.0
    job = {"running": True, "exit": None, "output": ""}
    with _JOBS_LOCK:
        _JOBS[key] = job
    env = dict(os.environ)
    env["MINDFLOCK_FLIGHT"] = flight["id"]
    env["MINDFLOCK_SESSION_TITLE"] = flight["session"]

    def _work() -> None:
        try:
            cp = subprocess.run(
                ["/bin/sh", "-c", cmd],
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
            job.update(running=False, exit=code, output=out[-2000:])
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


def _run_notify(flight: dict, step: dict) -> None:
    from backend.web.core import ntfy as _ntfy

    text = _flights.fill_prompt(step["config"].get("text") or step["title"], flight)
    try:
        cfg = _ntfy.load()
        if cfg.active:
            _ntfy.publish_soon(
                cfg,
                title="%s · %s" % (flight["plan_name"], flight["session"] or "flight"),
                message=text,
                tags=["bird"],
            )
    except Exception as err:  # noqa: BLE001 — a notify step never blocks
        _log("flight %s: notify failed: %v", flight["id"], err)


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
    text = _pr_runner.build_consolidated_pr_prompt(
        pr, comments, sess.get("folder") or ""
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


def _split_base(flight: dict, step: dict) -> Tuple[dict, str, str]:
    """What every worker of a split is created with (blocking) — the payload
    ``spawn_session`` builds for a worker of this session: forked from its
    HEAD (a provisioned session's workers come from the same source), on its
    CLI, under its fence — plus the workers' provider and title prefix."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.mcp import tools as _mcp_tools

    srv = _server()
    parent = flight["session"]
    inst = srv.ENGINE.instances.get(parent)
    if inst is None:
        raise _StepFailed("the session is gone")
    row = _row(parent)
    cfg = step["config"]
    program = str(getattr(inst, "Program", "") or row.get("program") or "")
    payload: dict = {"spawned": True, "parent": parent}
    if program:
        payload["program"] = program
    try:
        folder = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        folder = str(row.get("folder") or "")
    path = str(getattr(inst, "Path", "") or row.get("path") or "")
    if getattr(inst, "Provisioned", False):
        if (
            path
            and path != "."
            and os.path.isabs(path)
            and not os.path.basename(os.path.normpath(path)).startswith("_base_")
            and _gitlocal.head_sha(path)
        ):
            payload["repo_path"] = path
        payload["provisioned"] = True
        payload["workspace_strategy"] = str(
            getattr(inst, "WorkspaceStrategy", "") or "worktree"
        )
    else:
        root = _gitlocal.canonical_repo_root(folder) if folder else None
        payload["repo_path"] = root or path or folder
        sha = _gitlocal.head_sha(folder) if folder else None
        if sha:
            payload["base_ref"] = sha
            branch = str(getattr(inst, "Branch", "") or row.get("branch") or "")
            if branch:
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


def _free_worker_title(prefix: str, repo: str, branch_prefix: Optional[str]) -> str:
    """The lowest free ``<prefix><N>`` — no live or starting title, no tmux
    name and (when it can tell) no leftover branch: ``spawn_session``'s rule."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.mcp import tools as _mcp_tools

    srv = _server()
    taken = set(srv.ENGINE.instances.keys())
    try:
        taken |= {str(r.get("title") or "") for r in srv._pending_rows()}
    except Exception:  # noqa: BLE001
        pass
    tmux = {_mcp_tools._tmux_name(t) for t in taken}
    n = 1
    while True:
        cand = "%s%d" % (prefix, n)
        if (
            cand not in taken
            and _mcp_tools._tmux_name(cand) not in tmux
            and not (
                repo
                and branch_prefix is not None
                and _gitlocal.branch_exists(repo, branch_prefix + cand)
            )
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
            )
            payload["title"] = title
            payload["prompt"] = text + (
                _mcp_tools.report_footer(title, parent, provider) if footer else ""
            )
            status, body = await _session_create.create_result(payload)
            err = str(body.get("error") or "")
            if status == 409 and "exists" in err:
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
        "detail": {"children": made},
    }


async def _perform(flight: dict, action: dict, now: float) -> dict:
    """Carry out ``action``'s side effect; return it amended (detail, or
    turned into done/fail/advance)."""
    kind = action["action"]
    step = _flights._current(flight)
    if step is None:
        return action
    try:
        if kind == "fail" and action.get("then") == "agent":
            await asyncio.to_thread(
                _deliver, flight["session"], _fix_note(step, action)
            )
            return action
        if kind != "run":
            return action
        phase = action.get("phase")
        sk = step["kind"]
        if phase == "agent_run":
            pid = str((action.get("detail") or {}).get("plan") or "")
            await _start_verify_run(pid)
            return dict(
                action, detail=dict(action.get("detail") or {}, run_started=True)
            )
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
            await asyncio.to_thread(_ensure_checklist, flight)
            return dict(action, detail={})
        if sk == "wait":
            cfg = step["config"]
            until = (
                _flights.next_until(cfg["until"], now)
                if cfg.get("until")
                else now + float(cfg.get("minutes") or 0) * 60.0
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
            return {"action": "done", "note": "the ticket was updated"}
        if sk == "notify":
            await asyncio.to_thread(_run_notify, flight, step)
            return {"action": "done", "note": "sent"}
        if sk == "split":
            return await _run_split(flight, step, action)
        if sk == "join":
            return dict(action, detail={})
    except _StepFailed as err:
        out = _flights.fail_action(
            flight, str(err), {"session": {"present": bool(flight["session"])}}
        )
        if err.detail:
            out["detail"] = dict(err.detail)
        if out.get("then") == "agent":
            # Handed to the agent: tell it, or there is nobody to fix it.
            try:
                await asyncio.to_thread(
                    _deliver, flight["session"], _fix_note(step, out)
                )
            except Exception:  # noqa: BLE001
                out = dict(out, then="halt")
        return out
    return action


def _fix_note(step: dict, action: dict) -> str:
    return (
        "[MindFlock flight] %s failed: %s\nFix it; the flight retries the step "
        "once you are done." % (step["title"], action.get("reason") or "failed")
    )


# --------------------------------------------------------------------------- #
# One flight, one pass
# --------------------------------------------------------------------------- #
class _Stale(Exception):
    pass


def _apply_if_unchanged(
    fid: str, seen: dict, action: dict, now: float
) -> Optional[dict]:
    """Apply an effect-free ``action`` unless a route moved the flight since it
    was read. None when stale; the stored record when applying it changed
    nothing but the clock (no write — see :func:`_same`)."""
    out: dict = {}

    def fn(f: dict) -> None:
        if _view_key(f)[:3] != _view_key(seen)[:3]:
            raise _Stale()
        before = copy.deepcopy(f)
        _flights.apply(f, action, now)
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
    fid: str, seen: dict, action: dict, now: float, token: str
) -> Optional[dict]:
    """Record, under the lock and only if nobody moved the flight, that this
    step's side effect is about to be carried out (``detail.performing``). A
    fresh ``run`` also records the step running here, so a route that pauses,
    skips or cancels meanwhile sees the truth, and no pass starts it twice."""

    def fn(f: dict) -> None:
        if _view_key(f)[:3] != _view_key(seen)[:3]:
            raise _Stale()
        cur = _flights._current(f)
        if cur is None or f["step_states"][cur["id"]]["detail"].get("performing"):
            raise _Stale()
        if action["action"] == "run" and not action.get("phase"):
            _flights.apply(f, dict(action, detail={}), now)
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
        cur = _flights._current(f)
        if f["state"] in _flights.FINAL or cur is None or cur["id"] != sid:
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
            _flights.apply(f, action, now)
        if paused and f["state"] in _flights.ACTIVE:
            f["state"] = "paused"

    try:
        return _flights.mutate(fid, fn)
    except _Stale:
        return None


#: At most this many steps finish within one pass (a notify followed by a
#: wait, say) before the loop moves on to the next flight.
_CHAIN = 4


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
            obs = await observe(f, now)
            action = _flights.next_action(f, obs, now)
            sess = obs.get("session") or {}
            if not f["repo_root"] and sess.get("present"):
                root = await asyncio.to_thread(_repo_root, f["session"])
                if root:
                    action["repo_root"] = root
            if sess.get("branch") and sess["branch"] != f["branch"]:
                action["branch"] = sess["branch"]
            cur = _flights._current(f)
            if _needs_effect(action) and cur is not None:
                token = uuid.uuid4().hex
                marked = await asyncio.to_thread(
                    _mark_intent, fid, f, action, now, token
                )
                if marked is None:
                    return
                action = await _perform(marked, action, now)
                after = await asyncio.to_thread(
                    _record_outcome, fid, cur["id"], token, action, now
                )
                if after is None and cur["kind"] in ("ship", "reviews"):
                    # The step was removed while its lane was being armed.
                    await asyncio.to_thread(_disarm_if_removed, fid, cur["id"])
                before = f
            else:
                after = await asyncio.to_thread(
                    _apply_if_unchanged, fid, f, action, now
                )
                before = f
            if after is None or _same(before, after):
                return  # stale, or nothing changed: no write, nothing to say
            if (
                cur is not None
                and cur["kind"] == "command"
                and action["action"] in ("done", "fail", "advance")
            ):
                with _JOBS_LOCK:
                    _JOBS.pop((fid, cur["id"]), None)
            await asyncio.to_thread(_announce, before, after)
            if (
                action["action"] not in ("done", "advance")
                or after["state"] not in _flights.ACTIVE
            ):
                return
            f = after
    except Exception as err:  # noqa: BLE001 — one flight never stops a pass
        _log("flight %s pass failed: %v", fid, err)


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
        if f["state"] in _flights.ACTIVE:
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


def on_session_removed(title: str) -> None:
    """The session-removal hook: its flight is cancelled (and announced)."""
    f = _flights.for_session(title)
    if f is None:
        return
    try:
        after = _flights.mutate(
            f["id"], lambda x: _flights.op_cancel(x, "its session was deleted")
        )
    except _flights.FlightError:
        return
    _announce(f, after)


# --------------------------------------------------------------------------- #
# Operations behind the routes
# --------------------------------------------------------------------------- #
def _subject_of(raw) -> dict:
    return _flights._normalize_subject(raw if isinstance(raw, dict) else {})


async def start(body: dict, absorbed: Optional[list] = None) -> dict:
    """``POST /api/flights/start``. Raises :class:`flights.FlightError`, and
    :class:`flights.PlanError` for a bad ad-hoc ``steps`` list. The lanes of
    a fast-track run it took over are appended to ``absorbed``."""
    srv = _server()
    body = body or {}
    plan_id = str(body.get("plan_id") or "")
    subject = _subject_of(body.get("subject"))
    by = str(body.get("by") or "user")
    title = str(body.get("title") or "").strip()
    start_at = str(body.get("start_at") or "").strip()
    adhoc = body.get("steps") is not None
    plan = _flights.get_plan(plan_id) if plan_id or not adhoc else None
    if plan is None and (plan_id or not adhoc):
        raise _flights.FlightError("unknown_plan", 404, "no such plan")
    if adhoc:
        if plan is None and not title and not str(body.get("repo_path") or "").strip():
            raise _flights.FlightError(
                "needs_title", 400, "an ad-hoc flight needs a session (or a folder)"
            )
        plan = _flights.adhoc_plan(
            body.get("steps"),
            plan,
            subject.get("title") or ("Flight on %s" % title if title else ""),
        )
    if title and title not in srv.ENGINE.instances:
        raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
    # Everything that can refuse is checked BEFORE a session is created: a
    # bad step id used to leave an orphan session with no flight.
    _flights.start_cursor(plan, start_at, attach=bool(title))
    take = body.get("absorb_fasttrack")
    rec = None
    if title and (take is None or bool(take)):
        rec = await asyncio.to_thread(_fasttrack_to_absorb, title)
    lead: List[dict] = []
    if rec is not None:
        stage = await asyncio.to_thread(_stage_of, title)
        lead = _flights.absorb_steps(rec, stage)
        plan, start_at = _flights.with_lead(plan, lead, start_at, attach=True)
    replaced: list = []
    if title:
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
            )
        )
    else:
        steps = plan["steps"]
        first_agent = steps[0]["kind"] == "agent" and not start_at
        draft = {"subject": subject, "plan_name": plan["name"], "branch": ""}
        prompt = str(body.get("prompt") or "").strip()
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
            plan = dict(plan, name="Flight on %s" % new_title)
        flight = await asyncio.to_thread(
            lambda: _flights.take_off(
                plan,
                session=new_title,
                subject=subject,
                by=by,
                start_at=start_at,
                seeded=first_agent and bool(prompt),
                replaced=replaced,
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
        str(rec.get("reason") or "Fast-track stopped")
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


_OPS = {
    "done": _flights.op_done,
    "skip": _flights.op_skip,
    "retry": _flights.op_retry,
    "jump": _flights.op_jump,
    "pause": _flights.op_pause,
    "resume": _flights.op_resume,
    "cancel": _flights.op_cancel,
}


def _verify_done(flight: dict, sid: str, note: str) -> None:
    """Done on a verify step waiting on you: the open human checklist items
    are recorded as passed (by a human), which is what settles the plan."""
    from backend.web.core import test_plans as _tp

    i = next((k for k, s in enumerate(flight["steps"]) if s["id"] == sid), -1)
    if i < 0 or flight["steps"][i]["kind"] != "verify":
        return
    pid = str(flight["step_states"][sid]["detail"].get("plan") or "")
    plan = _tp.get(pid) if pid else None
    if plan is None:
        return
    for step in _tp.open_human_steps(plan):
        try:
            _tp.record_result(
                pid, step["id"], "pass", note=note or "Done on the flight", by="human"
            )
        except Exception as err:  # noqa: BLE001
            _log("flight %s: could not record %s: %v", flight["id"], step["id"], err)


def operate(flight_id: str, op: str, step: str = "", note: str = "") -> dict:
    """A route's operation on a flight (blocking). Raises
    :class:`flights.FlightError` (404 unknown flight, 409 refusals)."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    fn = _OPS[op]
    if op == "done" and not before["step_states"].get(step, {}).get("detail", {}).get(
        "failed_reason"
    ):
        cur = _flights._current(before)
        if (
            cur is not None
            and cur["id"] == step
            and before["step_states"][step]["state"] == "waiting_you"
        ):
            _verify_done(before, step, note)

    def _do(f: dict) -> None:
        if op == "done":
            fn(f, step, note)
        elif op in ("skip", "retry", "jump"):
            fn(f, step)
        else:
            fn(f)

    after = _flights.mutate(flight_id, _do)
    if after is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    _announce(before, after)
    wake()
    return after


def _changed(flight_id: str, fn) -> dict:
    """Run ``fn`` on the stored flight under the lock (a refusal raised in it
    writes nothing), then say so — ``flight.changed`` even when the cursor did
    not move — and wake the loop."""
    before = _flights.get(flight_id)
    if before is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    after = _flights.mutate(flight_id, fn)
    if after is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    if _view_key(before) == _view_key(after):
        _emit("flight.changed", after)
    _announce(before, after)
    _stop_removed(before, after)
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
    ``ungroup`` the group id in ``step``. Raises
    :class:`flights.FlightError` / :class:`flights.PlanError`."""
    plan = None
    if op == "add_plan":
        plan = _flights.get_plan(str(raw or ""))
        if plan is None:
            raise _flights.FlightError("unknown_plan", 404, "no such plan")

    def _do(f: dict) -> None:
        if op == "add":
            _flights.op_add_step(f, raw, at)
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

    return _changed(flight_id, _do)


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
        before["state"] != _flights.GROUNDED
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
            raise _flights.FlightError("unknown_plan", 404, "no such plan")
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
    Every refusal comes before a session is created. Raises
    :class:`flights.FlightError`."""
    srv = _server()
    body = body or {}
    f = _flights.get(flight_id)
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    if f["state"] != _flights.GROUNDED:
        raise _flights.FlightError(
            "not_grounded", 409, "this flight already has a session"
        )
    title = str(body.get("title") or "").strip()
    seeded = False
    no_prompt = False
    if title:
        if title not in srv.ENGINE.instances:
            raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
        if _flights.for_session(title) is not None:
            raise _flights.FlightError(
                "in_flight", 409, "%s is already on a flight" % title
            )
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
            first is not None and first["kind"] == "agent" and not first.get("halt")
        )
        prompt = str(body.get("prompt") or "").strip()
        if first_agent and not prompt:
            prompt = _flights.fill_prompt(first["config"].get("prompt") or "", f)
        repo = str(body.get("repo_path") or "").strip() or defaults["repo_path"]
        if not repo:
            raise _flights.FlightError(
                "no_repo", 400, "pick a folder for the new session"
            )
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


def remove(flight_id: str) -> None:
    """``DELETE /api/flights/{id}``: finished, halted or grounded flights
    only — a running, waiting or paused one must be cancelled first."""
    f = _flights.get(flight_id)
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    if f["state"] in _flights.HOLDING:
        raise _flights.FlightError("in_flight", 409, "cancel the flight first")
    _flights.delete(flight_id)
    with _JOBS_LOCK:
        for k in [k for k in _JOBS if k[0] == flight_id]:
            _JOBS.pop(k, None)


def list_dto(all_: bool = False) -> List[dict]:
    """``GET /api/flights``: unfinished + the last 20 finished (all with
    ``all_``)."""
    flights = _flights.list_flights()
    if all_:
        return flights
    live = [f for f in flights if f["state"] not in _flights.FINAL]
    landed = sorted(
        (f for f in flights if f["state"] in _flights.FINAL),
        key=lambda f: -(f["finished"] or f["updated"]),
    )[:20]
    return live + landed
