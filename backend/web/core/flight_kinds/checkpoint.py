"""The ``checkpoint`` stop (docs/flights.md "Round 15"): remember where the
session's worktree is — HEAD's sha and, when there is uncommitted work, a
``git stash create`` object of it — so the ``rewind`` op can put it back.

What it records is kept alive (never garbage-collected) under
``refs/mindflock/flights/<fid>/<sid>``. Refs outside ``refs/worktree/`` are
shared by every worktree of a repo, so the ref lives in the session's repo
(its common git dir, ``detail.repo``) and :func:`forget` drops it from there
even once the worktree is gone; ``flight_driver.remove`` should call it.

Untracked files are not in a ``git stash create`` object; ``git reset
--hard`` leaves them where they are, so a rewind neither restores nor loses
them.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from typing import Optional

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights
from backend.web.core import git_ops as _git_ops

REF_PREFIX = "refs/mindflock/flights/"


def _git(folder: str, *args: str, git_dir: str = "") -> tuple:
    """``git -C folder args`` (or ``git --git-dir=…``) → ``(returncode,
    output)``; ``(-1, "")`` when git could not run."""
    head = ["--git-dir=" + git_dir] if git_dir else ["-C", folder]
    cp = _git_ops._run_git(
        [*head, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
    )
    if cp is None:
        return -1, ""
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def _part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(text or "")) or "_"


def ref_of(flight: dict, step: dict) -> str:
    return REF_PREFIX + "%s/%s" % (_part(flight["id"]), _part(step["id"]))


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    return {"note": _flights._text(c.get("note"), 500)}


def _title(config: dict) -> str:
    note = str(config.get("note") or "").strip().splitlines()
    return ("Checkpoint: " + note[0])[:80] if note else "Checkpoint"


def _take(flight: dict, step: dict) -> dict:
    """Blocking half of :func:`_run`: record HEAD (+ a stash of the
    uncommitted work) and pin it under the flight's ref."""
    from backend.web.core import flight_driver as _driver

    folder = _driver.worktree_of(flight)
    if not folder:
        raise _kinds.StepFailed("the session has no worktree to checkpoint")
    sha = _git_ops._git_head_sha(folder)
    if not sha:
        raise _kinds.StepFailed("the worktree has no commit to checkpoint")
    stash = ""
    if _git_ops._is_dirty(folder):
        code, out = _git(folder, "stash", "create", "mindflock checkpoint")
        if code != 0:
            raise _kinds.StepFailed("could not record the uncommitted work: %s" % out)
        stash = out.splitlines()[-1].strip() if out else ""
    code, common = _git(folder, "rev-parse", "--git-common-dir")
    repo = (
        (common if os.path.isabs(common) else os.path.join(folder, common))
        if code == 0
        else ""
    )
    ref = ref_of(flight, step)
    code, out = _git(folder, "update-ref", ref, stash or sha)
    if code != 0:
        raise _kinds.StepFailed("could not keep the checkpoint: %s" % out)
    return {
        "sha": sha,
        "stash": stash,
        "ref": ref,
        "repo": os.path.normpath(repo) if repo else "",
        "receipt": "at %s" % sha[:7],
    }


async def _run(flight: dict, step: dict, action: dict, now: float) -> dict:
    detail = await asyncio.to_thread(_take, flight, step)
    return {"action": "done", "note": detail["receipt"], "detail": detail}


def _judge(flight, step, ss, obs, now) -> dict:
    d = ss.get("detail") or {}
    if d.get("sha"):
        return {"action": "done", "note": d.get("receipt") or "", "detail": {}}
    return {"action": "run", "note": "taking the checkpoint again (it was interrupted)"}


def rewind(flight: dict, step: dict, body: dict) -> dict:
    """``POST …/steps/{sid}/op/rewind``: put the worktree back where the
    checkpoint found it and fly on from the step after it."""
    from backend.web.core import flight_driver as _driver

    d = (flight["step_states"].get(step["id"]) or {}).get("detail") or {}
    sha = str(d.get("sha") or "")
    if not sha:
        raise _flights.FlightError(
            "no_checkpoint", 409, "this checkpoint has not been taken yet"
        )
    # The jump below must be allowed before anything is reset.
    _flights._airborne(flight)
    sess = _driver._session_obs(flight, time.time(), False)
    if sess.get("activity") == "working":
        raise _flights.FlightError(
            "working",
            409,
            "the agent is working — wait for its turn to end (or stop it), then rewind",
        )
    folder = str(sess.get("folder") or "")
    if not folder or not os.path.isdir(folder):
        raise _flights.FlightError("no_worktree", 409, "the session's worktree is gone")
    code, out = _git(folder, "reset", "--hard", sha)
    if code != 0:
        raise _flights.FlightError(
            "rewind_failed", 409, "git reset --hard %s failed: %s" % (sha[:7], out)
        )
    stash = str(d.get("stash") or "")
    if stash:
        code, out = _git(folder, "stash", "apply", "--index", stash)
        if code != 0:
            code, out = _git(folder, "stash", "apply", stash)
        if code != 0:
            raise _flights.FlightError(
                "rewind_failed",
                409,
                "reset to %s, but the uncommitted work did not re-apply: %s"
                % (sha[:7], out),
            )
    ids = [s["id"] for s in flight["steps"]]
    i = ids.index(step["id"])
    if i + 1 < len(ids):
        # step_op announces the change and wakes the loop.
        fresh = _flights.mutate(flight["id"], lambda f: _flights.op_jump(f, ids[i + 1]))
    else:
        fresh = _flights.get(flight["id"])
    return {"flight": fresh, "sha": sha}


def forget(flight: Optional[dict]) -> int:
    """Drop every ref this flight's checkpoints keep alive (call it when the
    flight is removed); returns how many went. Never raises."""
    if not flight:
        return 0
    repos = set()
    for st in (flight.get("step_states") or {}).values():
        repo = str(((st or {}).get("detail") or {}).get("repo") or "")
        if repo and os.path.isdir(repo):
            repos.add(repo)
    prefix = REF_PREFIX + _part(flight.get("id")) + "/"
    gone = 0
    for repo in sorted(repos):
        code, out = _git(
            "", "for-each-ref", "--format=%(refname)", prefix, git_dir=repo
        )
        if code != 0:
            continue
        for ref in out.splitlines():
            if (
                ref.startswith(prefix)
                and _git("", "update-ref", "-d", ref, git_dir=repo)[0] == 0
            ):
                gone += 1
    return gone


_kinds.register(
    _kinds.Kind(
        kind="checkpoint",
        spec={
            "label": "Checkpoint",
            "actor": "robot",
            "family": "ship",
            "risk": [],
            "summary": "Remember where the worktree is (commits and uncommitted work) so you can rewind to it later.",
            "default_title": "Checkpoint",
            "fields": [
                _flights._f(
                    "note",
                    "text",
                    "Note",
                    default="",
                    placeholder="before the risky refactor",
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        default_title=_title,
        ops={"rewind": rewind},
    )
)
