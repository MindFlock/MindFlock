"""Move a session's workspace to a new folder, live (``POST
/api/instances/{title}/move``; docs/flights.md "Moving a session").

:func:`check` is the route's gate, answered before anything is touched:
404 ``unknown_session``, 409 ``remote`` (another device's row), 400
``bad_dest`` / ``bad_source``, 409 ``exists``, 409 ``moving`` (a move of it already runs),
409 ``working`` (the agent is mid-turn) and 409 ``in_flight`` (its flight or
fast-track runs) — those two yield to ``force``. :func:`start` then runs the
engine's ``MoveWorkspace`` on a worker thread and emits ``session.moved
{title, from, to}`` or ``session.move_failed {title, error}``.

The move itself (stop the agent, ``git worktree move`` / ``shutil.move`` +
``git worktree repair``, re-point the paths, the provider's
``carry_workdir``, relaunch) is the engine's, in
:mod:`backend.session.instance`; this module adds the web-side pieces: the
refusals, the fresh tmux env a relaunch needs, the flight ``repo_root``
refresh and the events.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Callable, Dict, Optional, Tuple

from backend import log
from backend.session import instance as _instance
from backend.web.core import events as _events

#: Titles with a move under way → ``{"from", "to"}``.
_MOVING: Dict[str, dict] = {}
_LOCK = threading.Lock()
#: Running move tasks (a bare create_task can be garbage-collected mid-run).
_TASKS: set = set()


def moving(title: str) -> Optional[dict]:
    """``{"from", "to"}`` while ``title``'s workspace is being moved, else None."""
    with _LOCK:
        rec = _MOVING.get(title)
        return dict(rec) if rec is not None else None


def _refuse(status: int, code: str, error: str) -> Tuple[int, dict]:
    return status, {"error": error, "code": code}


def _in_flight(title: str) -> str:
    """Why ``title`` is in flight ("" when it isn't): a flight the driver is
    moving, or a fast-track that is running."""
    try:
        from backend.web.core import flights as _flights

        f = _flights.for_session(title)
        if f is not None and f.get("state") in _flights.ACTIVE:
            return "its flight is running — pause it first"
    except Exception:  # noqa: BLE001 — an unreadable store holds nothing
        pass
    try:
        from backend.web.core import autopilot as _autopilot

        rec = _autopilot.get(title)
        if rec is not None and rec.get("state") == "running":
            return "its fast-track is running — stop it first"
    except Exception:  # noqa: BLE001
        pass
    return ""


def check(
    instances,
    title: str,
    payload: dict,
    activity: Callable[[object], str],
) -> Tuple[Optional[Tuple[int, dict]], dict]:
    """``(refusal, plan)``: ``refusal`` is ``(status, body)`` or None;
    ``plan`` is ``{"title", "from", "to"}`` when the move may start.
    ``activity(inst)`` is the session's activity (working / idle / …)."""
    from backend.web.core import remote as _remote

    if _remote.is_remote_title(title):
        return _refuse(409, "remote", "another device's session moves there"), {}
    inst = (instances or {}).get(title)
    if inst is None:
        return _refuse(404, "unknown_session", "instance not found: %s" % title), {}
    payload = payload if isinstance(payload, dict) else {}
    force = payload.get("force") is True
    try:
        old = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        old = ""
    if not old or not os.path.isdir(old):
        return _refuse(409, "not_ready", "workspace not ready"), {}
    if getattr(inst, "PeerShare", ""):
        return (
            _refuse(
                409, "peer_share", "a shared folder's session moves with its peer link"
            ),
            {},
        )
    try:
        new = _instance.CheckMoveDest(old, payload.get("dest"))
        _instance._git_layout(old)  # a repo's sub-folder is bad_source
    except _instance.MoveError as err:
        status = 409 if err.code == "exists" else 400
        return _refuse(status, err.code, str(err)), {}
    if moving(title) is not None:
        return _refuse(409, "moving", "this session is already being moved"), {}
    if not force:
        try:
            act = activity(inst)
        except Exception:  # noqa: BLE001 — no reading is no refusal
            act = ""
        if act == "working":
            return (
                _refuse(409, "working", "the agent is mid-turn — wait, or force"),
                {},
            )
        why = _in_flight(title)
        if why:
            return _refuse(409, "in_flight", why), {}
    return None, {"title": title, "from": old, "to": new}


def _repo_path(inst) -> str:
    try:
        return inst._git_worktree.GetRepoPath() or ""
    except Exception:  # noqa: BLE001
        return ""


def _refresh_flights(old_repo: str, new_repo: str) -> None:
    """Point every unfinished flight whose ``repo_root`` was ``old_repo`` at
    ``new_repo`` (the repo itself moved: an in-place session, a main clone)."""
    if not old_repo or not new_repo or old_repo == new_repo:
        return
    from backend.web.core import flights as _flights

    olds = {os.path.normpath(old_repo), os.path.realpath(old_repo)}

    def _set(f: dict) -> None:
        f["repo_root"] = new_repo

    for f in _flights.list_flights(include_finished=False):
        root = f.get("repo_root") or ""
        if root and os.path.normpath(root) in olds:
            _flights.mutate(f["id"], _set)


def run(engine, title: str, dest: str, env: Optional[dict] = None) -> Optional[str]:
    """Move ``title``'s workspace to ``dest`` now (blocking): the engine's
    ``MoveWorkspace`` with the session's fresh tmux ``env``, then the flight
    refresh. Returns the error text, or None. Emits nothing."""
    inst = engine.instances.get(title)
    if inst is None:
        return "instance not found: %s" % title
    old_repo = _repo_path(inst)
    if env is not None:
        inst.ExtraEnv = dict(env)
    err = _instance.MoveWorkspace(engine.instances, title, dest, save=engine.save)
    try:
        _refresh_flights(old_repo, _repo_path(inst))
    except Exception as ferr:  # noqa: BLE001 — the move itself stands
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("move %s: flight refresh failed: %v", title, ferr)
    if err is None:
        # A sleeping agent came back up in its new folder: it is awake.
        try:
            from backend.web.core import sleep as _sleep

            _sleep.drop(title)
        except Exception:  # noqa: BLE001
            pass
    return str(err) if err is not None else None


async def _run_and_emit(engine, plan: dict, env_fn: Callable[[object], dict]) -> None:
    title = plan["title"]
    try:
        inst = engine.instances.get(title)
        env = env_fn(inst) if inst is not None else None
        error = await asyncio.to_thread(run, engine, title, plan["to"], env)
    except Exception as err:  # noqa: BLE001
        error = str(err) or type(err).__name__
    finally:
        with _LOCK:
            _MOVING.pop(title, None)
    if error is None:
        _events.BUS.emit(
            "session.moved",
            session=title,
            old=plan["from"],
            new=plan["to"],
            data={"title": title, "from": plan["from"], "to": plan["to"]},
        )
    else:
        inst = engine.instances.get(title)
        where = inst.GetWorktreePath() if inst is not None else ""
        _events.BUS.emit(
            "session.move_failed",
            session=title,
            data={"title": title, "error": error, "path": where},
        )


def start(engine, plan: dict, env_fn: Callable[[object], dict]) -> bool:
    """Mark ``plan["title"]`` as moving and run the move in the background
    (on the running event loop). ``env_fn(inst)`` builds the tmux env the
    relaunch needs (the port block + auth-profile / local-model overlay).
    False (nothing started) when a move of it already runs."""
    with _LOCK:
        if plan["title"] in _MOVING:
            return False
        _MOVING[plan["title"]] = {"from": plan["from"], "to": plan["to"]}
    task = asyncio.get_running_loop().create_task(_run_and_emit(engine, plan, env_fn))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return True
