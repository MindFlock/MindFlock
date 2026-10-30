"""The ``land`` stop (docs/flights.md "Round 15"): the flight's last stop,
once its work is on the default branch — check the branch is merged, finish
the flight ``done``, then tidy up behind it: delete the remote branch and
close (or delete) the session.

The check: ``git merge-base --is-ancestor <branch> origin/<default>`` after a
fetch, or — a squash merge rewrites the commits — the branch's PR merged into
that default branch (``github_pr.find_pr``). Neither → the stop fails "not
merged into main".

The tidy-up runs on a daemon thread (:func:`_close_later`) AFTER the step's
``done`` has been recorded, so the flight never loses its session mid-step: it
waits until the flight reads ``done`` (up to :data:`_FINISH_WAIT_S`; a flight
moved meanwhile is left alone), deletes the remote branch, then ends the
session the way the MCP ``kill_session`` tool does
(``session_end.end_session``). ``close: delete`` falls back to close — and the
note says why — when the tool would refuse a delete: the session runs in
place, its worktree has uncommitted or untracked files, or another session
shares its folder. What the thread did lands in the step's
``detail.closed`` ("deleted" / "closed" / "failed: …"); a server restart
between the ``done`` and the thread leaves the session open (the stop is
done; close it by hand).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from typing import Optional, Tuple

from backend.web.core import flights as F
from backend.web.core.flight_kinds import Kind, StepFailed, register

CLOSE = ("delete", "close")
#: The tidy-up waits this long before looking (the done must be recorded).
_DELAY_S = 2.0
#: ...and at most this long for the flight to read ``done``.
_FINISH_WAIT_S = 30.0


def _config(raw: dict, strict: bool, where: str) -> dict:
    close = F._text(raw.get("close"), 8).lower() or "delete"
    if close not in CLOSE:
        if strict:
            raise F.PlanError("close is delete or close", where + ".close")
        close = "delete"
    return {
        "delete_branch": F._bool(raw.get("delete_branch"), True),
        "remote_branch": F._bool(raw.get("remote_branch"), True),
        "close": close,
    }


def _judge(flight, step, ss, obs, now) -> dict:
    # Running only when a restart cut ``run`` short: check and land again.
    return {"action": "run", "note": ""}


def _git(path: str, *args: str, timeout: float = 60.0) -> Tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", "-C", path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return -1, str(err)
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def default_branch(folder: str) -> str:
    """origin's HEAD, else ``main`` / ``master`` when origin has one."""
    rc, out = _git(folder, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.startswith("origin/"):
        return out[len("origin/") :]
    for name in ("main", "master"):
        if (
            _git(folder, "rev-parse", "-q", "--verify", "refs/remotes/origin/" + name)[
                0
            ]
            == 0
        ):
            return name
    return ""


def _merged(folder: str, branch: str) -> Tuple[bool, str]:
    """(merged into the default branch, that branch's name). Blocking."""
    main = default_branch(folder)
    if not main:
        raise StepFailed(
            "cannot tell the default branch (no origin/HEAD, main or master)"
        )
    _git(folder, "fetch", "--quiet", "origin", main, timeout=120.0)
    rc, _ = _git(
        folder, "merge-base", "--is-ancestor", "refs/heads/" + branch, "origin/" + main
    )
    if rc == 0:
        return True, main
    try:
        from backend.web.core import github_pr

        pr = github_pr.find_pr_sync(folder, branch)
    except Exception:  # noqa: BLE001 — no token, no network: the git answer stands
        pr = None
    if pr and pr.get("state") == "MERGED" and (pr.get("base") or main) == main:
        return True, main
    return False, main


def _why_not_delete(sess: dict, folder: str) -> str:
    """Why ``kill_session(mode="delete")`` would refuse this session, "" when
    it would not (the spawned-by-an-agent rule aside: the owner's plan asked)."""
    from backend.web.core import flight_driver as D
    from backend.web.core import session_end

    srv = D._server()
    inst = srv.ENGINE.instances.get(sess.get("title") or "")
    if inst is not None and getattr(inst, "InPlace", False):
        return "it runs in place"
    rc, out = _git(folder, "status", "--porcelain")
    if rc != 0:
        return "its worktree could not be read"
    if out:
        return "it has uncommitted files"
    try:
        if session_end.folder_in_use(srv.ENGINE, sess.get("title") or "", folder):
            return "another session works in its folder"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _check(flight: dict, step: dict) -> dict:
    """The landing plan, or :class:`StepFailed`. Blocking."""
    from backend.web.core import flight_driver as D

    if flight["steps"][-1]["id"] != step["id"]:
        raise StepFailed("a Land stop is the last stop")
    sess = D._session_obs(flight, time.time(), False)
    folder, branch = sess.get("folder") or "", sess.get("branch") or ""
    if not sess.get("present") or not folder or not branch:
        raise StepFailed("no session branch to land")
    merged, main = _merged(folder, branch)
    if not merged:
        raise StepFailed("not merged into %s" % main)
    cfg = step["config"]
    mode, why = cfg["close"], ""
    if mode == "delete":
        why = _why_not_delete(dict(sess, title=flight["session"]), folder)
        if why:
            mode = "close"
    repo = flight.get("repo_root") or ""
    if not repo:
        rc, common = _git(
            folder, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        repo = (
            os.path.dirname(common) if rc == 0 and common.endswith("/.git") else folder
        )
    return {
        "title": flight["session"],
        "branch": branch,
        "folder": folder,
        "repo": repo,
        "main": main,
        "mode": mode,
        "why": why,
        "remote_branch": bool(cfg["remote_branch"]),
        "delete_branch": bool(cfg["delete_branch"]),
    }


async def _run(flight, step, action, now) -> dict:
    plan = await asyncio.to_thread(_check, flight, step)
    note = "landed on %s" % plan["main"]
    if plan["why"]:
        note += " — closing, not deleting: %s" % plan["why"]
    _start_close(flight["id"], step["id"], plan)
    return {
        "action": "done",
        "note": note,
        "detail": {"receipt": "landed", "pending_close": plan},
    }


def _start_close(fid: str, sid: str, plan: dict) -> None:
    threading.Thread(
        target=_close_later,
        args=(fid, sid, plan),
        name="mf-flight-land-%s" % fid,
        daemon=True,
    ).start()


def _record(fid: str, sid: str, closed: str) -> None:
    def fn(f: dict) -> None:
        ss = f["step_states"].get(sid)
        if ss is not None:
            ss["detail"]["closed"] = closed[:300]
            ss["detail"].pop("pending_close", None)

    try:
        F.mutate(fid, fn)
    except Exception:  # noqa: BLE001
        pass


def _finished(fid: str) -> Optional[bool]:
    """True once the flight reads ``done``, False when it ended otherwise
    (cancelled, halted, deleted), None while it may still land."""
    f = F.get(fid)
    if f is None or f["state"] in ("cancelled", "halted"):
        return False
    return True if f["state"] == "done" else None


def _close_later(fid: str, sid: str, plan: dict, sleep=time.sleep) -> str:
    """The tidy-up behind a landed flight (see the module docstring). Returns
    (and records on the step) what it did."""
    sleep(_DELAY_S)
    deadline = time.time() + _FINISH_WAIT_S
    done = _finished(fid)
    while done is None and time.time() < deadline:
        sleep(0.5)
        done = _finished(fid)
    if not done:
        _log(
            "flight %s: not landed after all; session %s left open", fid, plan["title"]
        )
        return ""
    problems = []
    branch, folder, repo = plan["branch"], plan["folder"], plan["repo"]
    if plan["remote_branch"] and plan["branch"] != plan["main"]:
        rc, _ = _git(folder, "ls-remote", "--exit-code", "--heads", "origin", branch)
        if rc == 0:
            rc, out = _git(
                folder, "push", "--quiet", "origin", "--delete", branch, timeout=120.0
            )
            if rc != 0:
                problems.append("remote branch kept: %s" % out[-200:])
    rc, sha = _git(repo, "rev-parse", "-q", "--verify", "refs/heads/" + branch)
    sha = sha if rc == 0 else ""
    closed = _end(plan["title"], plan["mode"] == "delete")
    if closed == "deleted" and branch != plan["main"]:
        has = _git(repo, "rev-parse", "-q", "--verify", "refs/heads/" + branch)[0] == 0
        if plan["delete_branch"] and has:
            rc, out = _git(repo, "branch", "-D", branch)
            if rc != 0:
                problems.append("branch kept: %s" % out[-200:])
        elif not plan["delete_branch"] and not has and sha:
            # The worktree's own branch goes with it; put it back.
            _git(repo, "branch", branch, sha)
    out = "; ".join([closed] + problems)
    _record(fid, sid, out)
    return out


def _log(fmt: str, *args) -> None:
    from backend.web.core import flight_driver as D

    D._log(fmt, *args)


def _end(title: str, wipe: bool) -> str:
    from backend.web.core import flight_driver as D
    from backend.web.core import session_end

    engine = D._server().ENGINE
    try:
        session_end.end_session(engine, title, wipe=wipe)
        return "deleted" if wipe else "closed"
    except session_end.SessionNotFound:
        return "already gone"
    except session_end.FolderInUse:
        if not wipe:
            return "failed: its folder is in use"
        return _end(title, False)
    except Exception as err:  # noqa: BLE001 — a thread: say it, never raise
        _log("flight land: closing %s failed: %v", title, err)
        return "failed: %s" % err


KIND = register(
    Kind(
        kind="land",
        spec={
            "label": "Land",
            "actor": "robot",
            "summary": "Once the branch is merged into main: finish the flight, "
            "delete the branch and close the session. Always the last stop.",
            "default_title": "Land",
            "family": "ship",
            "risk": ["irreversible"],
            "fields": [
                F._f("delete_branch", "bool", "Delete the branch", default=True),
                F._f("remote_branch", "bool", "…on origin too", default=True),
                F._f("close", "select", "Then", default="delete", options=CLOSE),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        needs_session=True,
    )
)
