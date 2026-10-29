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
import datetime as _dt
import os
import subprocess
import threading
import time
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
    "operate",
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
    return obs


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
class _StepFailed(Exception):
    """An executor's refusal: becomes a ``fail`` action with this reason."""


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
    state = step["config"].get("state") or str(getattr(src, "done_state", "") or "")
    try:
        if state:
            await provider.set_state(str(subj["id"]), state)
        if step["config"].get("comment"):
            await provider.add_comment(
                str(subj["id"]), _flights.fill_prompt(step["config"]["comment"], flight)
            )
    except Exception as err:  # noqa: BLE001
        raise _StepFailed("the tracker refused: %s" % err)
    return "done"


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
                tags=["airplane"],
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


async def _perform(flight: dict, action: dict, now: float) -> dict:
    """Carry out ``action``'s side effect; return it amended (detail, or
    turned into done/fail/advance)."""
    kind = action["action"]
    step = _flights._current(flight)
    if step is None:
        return action
    try:
        if kind == "fail" and action.get("then") == "agent":
            note = (
                "[MindFlock flight] %s failed: %s\nFix it; the flight retries the step once you are done."
                % (
                    step["title"],
                    action.get("reason") or "failed",
                )
            )
            await asyncio.to_thread(_deliver, flight["session"], note)
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
    except _StepFailed as err:
        return _flights.fail_action(
            flight, str(err), {"session": {"present": bool(flight["session"])}}
        )
    return action


# --------------------------------------------------------------------------- #
# One flight, one pass
# --------------------------------------------------------------------------- #
class _Stale(Exception):
    pass


def _apply_if_unchanged(
    fid: str, seen: dict, action: dict, now: float
) -> Optional[dict]:
    """Apply ``action`` unless a route moved the flight since it was read."""

    def fn(f: dict) -> None:
        if _view_key(f)[:3] != _view_key(seen)[:3] or f.get("paused") != seen.get(
            "paused"
        ):
            raise _Stale()
        _flights.apply(f, action, now)

    try:
        return _flights.mutate(fid, fn)
    except _Stale:
        return None


#: At most this many steps finish within one pass (a notify followed by a
#: wait, say) before the loop moves on to the next flight.
_CHAIN = 4


async def step_flight(fid: str) -> None:
    """One decision pass over one flight. Never raises."""
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
            if (
                action["action"] == "wait"
                and not action.get("detail")
                and cur is not None
                and f["step_states"][cur["id"]]["note"] == action.get("note")
                and not action.get("repo_root")
                and not action.get("branch")
            ):
                return  # nothing changed; no write
            action = await _perform(f, action, now)
            after = await asyncio.to_thread(_apply_if_unchanged, fid, f, action, now)
            if after is None:
                return
            if (
                cur is not None
                and cur["kind"] == "command"
                and action["action"] in ("done", "fail", "advance")
            ):
                with _JOBS_LOCK:
                    _JOBS.pop((fid, cur["id"]), None)
            await asyncio.to_thread(_announce, f, after)
            if action["action"] not in ("done", "advance"):
                return
            f = after
    except Exception as err:  # noqa: BLE001 — one flight never stops a pass
        _log("flight %s pass failed: %v", fid, err)


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
        flights = await asyncio.to_thread(_flights.list_flights, False)
    except Exception:  # noqa: BLE001 — an unreadable store: next pass
        return
    for f in flights:
        if f["state"] in _flights.ACTIVE:
            await step_flight(f["id"])
    _prune_taps(now)
    live = {f["id"] for f in flights}
    for cache in (_REVIEWS, _CHECKS):
        for k in [k for k in cache if k not in live]:
            cache.pop(k, None)


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


async def start(body: dict) -> dict:
    """``POST /api/flights/start``. Raises :class:`flights.FlightError`."""
    srv = _server()
    body = body or {}
    plan = _flights.get_plan(str(body.get("plan_id") or ""))
    if plan is None:
        raise _flights.FlightError("unknown_plan", 404, "no such plan")
    subject = _subject_of(body.get("subject"))
    if isinstance(body.get("subject"), dict) and subject["kind"] in (
        "ticket",
        "pr",
        "issue",
    ):
        raise _flights.FlightError(
            "not_wired",
            501,
            "starting a ticket, PR or issue flight goes through Intake (not wired yet)",
        )
    by = str(body.get("by") or "user")
    title = str(body.get("title") or "").strip()
    start_at = str(body.get("start_at") or "").strip()
    if title:
        if title not in srv.ENGINE.instances:
            raise _flights.FlightError("unknown_session", 404, "no session %s" % title)
        flight = await asyncio.to_thread(
            lambda: _flights.take_off(
                plan,
                session=title,
                subject=subject,
                by=by,
                attach=True,
                start_at=start_at,
                repo_root=_repo_root(title),
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
        flight = await asyncio.to_thread(
            lambda: _flights.take_off(
                plan,
                session=new_title,
                subject=subject,
                by=by,
                start_at=start_at,
                seeded=first_agent and bool(prompt),
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
    _emit("flight.changed", flight)
    wake()
    return flight


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


def remove(flight_id: str) -> None:
    """``DELETE /api/flights/{id}``: finished (or halted) flights only."""
    f = _flights.get(flight_id)
    if f is None:
        raise _flights.FlightError("unknown_flight", 404, "no such flight")
    if f["state"] in _flights.ACTIVE:
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
