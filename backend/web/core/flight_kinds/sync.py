"""The ``sync`` stop (docs/flights.md "Round 15"): bring the session's branch
up to date with another branch (the default one unless ``with`` names it)
by rebasing onto it or merging it in.

The git work runs as a shell job (``flight_driver.run_shell``); ``observe``
reads the worktree's state once the job has ended, so the judge (pure)
decides from facts: HEAD contains the target's tip and no rebase / merge is
in progress → done. A conflict is never aborted: the stop fails with the
conflicting files and the fix note tells the agent to resolve them; a retry
(the core runs ``run`` again) finds the rebase / merge still in progress and
re-checks it instead of starting another one. A server restart mid-job
re-checks the same way.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from typing import List

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights
from backend.web.core import git_ops as _git_ops

HOW = ("rebase", "merge")

#: A branch name worth handing to git: no leading dash, no spaces, no "..".
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/@+-]{1,200}$")

_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}


def _git(folder: str, *args: str) -> tuple:
    """``git -C folder args`` → ``(returncode, stdout)``; ``(-1, "")`` when
    git could not run."""
    cp = _git_ops._run_git(
        ["-C", folder, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ, **_ENV),
    )
    if cp is None:
        return -1, ""
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def default_branch(folder: str) -> str:
    """The repo's default branch: ``origin/HEAD``'s short name, else the
    first of main / master that exists (on origin or locally); "" when none."""
    code, out = _git(
        folder, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if code == 0 and out.startswith("origin/"):
        return out[len("origin/") :]
    for name in ("main", "master"):
        for ref in ("refs/remotes/origin/" + name, "refs/heads/" + name):
            if _git(folder, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
                return name
    return ""


def in_progress(folder: str) -> str:
    """``rebase`` / ``merge`` while one is under way in the worktree, else ""."""
    code, gd = _git(folder, "rev-parse", "--git-dir")
    if code != 0 or not gd:
        return ""
    gd = gd if os.path.isabs(gd) else os.path.join(folder, gd)
    if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(
        os.path.join(gd, "rebase-apply")
    ):
        return "rebase"
    if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
        return "merge"
    return ""


def conflicts(folder: str) -> List[str]:
    code, out = _git(folder, "diff", "--name-only", "--diff-filter=U")
    return [l for l in out.splitlines() if l.strip()][:40] if code == 0 else []


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    with_ = _flights._text(c.get("with"), 200)
    if with_ and not _BRANCH.match(with_):
        if strict:
            raise _flights.PlanError("%r is not a branch name" % with_, where + ".with")
        with_ = ""
    how = _flights._text(c.get("how"), 16).lower() or "rebase"
    if how not in HOW:
        if strict:
            raise _flights.PlanError("how is rebase or merge", where + ".how")
        how = "rebase"
    return {"with": with_, "how": how, "fetch": _flights._bool(c.get("fetch"), True)}


def _title(config: dict) -> str:
    verb = "Sync with" if config.get("how", "rebase") == "rebase" else "Merge"
    return "%s %s" % (verb, config.get("with") or "main")


def _script(cfg: dict, target: str, ref: str, origin: bool) -> str:
    lines = ["set -e"]
    if cfg["fetch"] and origin:
        lines.append("git fetch origin %s" % shlex.quote(target))
    if cfg["how"] == "merge":
        lines.append("git merge --no-edit %s" % shlex.quote(ref))
    else:
        lines.append("git rebase %s" % shlex.quote(ref))
    return "\n".join(lines)


def _start(flight: dict, step: dict) -> dict:
    """Blocking half of :func:`_run`: check the worktree, start the job (or
    not, when a rebase / merge is already under way) and say what it is."""
    from backend.web.core import flight_driver as _driver

    folder = _driver.worktree_of(flight)
    if not folder:
        raise _kinds.StepFailed("the session has no worktree to sync")
    cfg = step["config"]
    target = cfg["with"] or default_branch(folder)
    if not target:
        raise _kinds.StepFailed(
            "no branch to sync with: name one (the repo has no origin/HEAD, main or master)"
        )
    origin = _git_ops._has_origin(folder, force=True)
    ref = ("origin/" + target) if origin else target
    detail = {
        "folder": folder,
        "target": target,
        "ref": ref,
        "how": cfg["how"],
        "before": _git_ops._git_head_sha(folder),
    }
    busy = in_progress(folder)
    if busy:
        # A retry after a conflict: re-check what is there, never start over.
        return dict(detail, resumed=busy)
    if _git_ops._is_dirty(folder):
        raise _kinds.StepFailed(
            "the worktree has uncommitted changes: commit them first, then sync"
        )
    _driver.run_shell(
        flight,
        step,
        _script(cfg, target, ref, origin),
        300.0,
        folder,
        env_extra=_ENV,
    )
    return detail


async def _run(flight: dict, step: dict, action: dict, now: float) -> dict:
    detail = await asyncio.to_thread(_start, flight, step)
    return dict(
        action,
        note="syncing with %s" % detail["target"],
        detail=dict(detail, started=now),
    )


def _facts(flight: dict, step: dict, ss: dict) -> dict:
    """What the judge decides from: the job, and once it has ended the
    worktree's state against the target."""
    from backend.web.core import flight_driver as _driver

    job = _driver.job_of(flight, step)
    out = {"job": job}
    d = ss.get("detail") or {}
    if job is not None and job.get("running") and not d.get("resumed"):
        return out
    folder = str(d.get("folder") or "") or _driver.worktree_of(flight)
    ref = str(d.get("ref") or "")
    if not folder or not ref:
        return dict(out, lost=True)
    busy = in_progress(folder)
    tip = _git(folder, "rev-parse", "--verify", "--quiet", ref + "^{commit}")[1]
    contains = bool(tip) and (
        _git(folder, "merge-base", "--is-ancestor", tip, "HEAD")[0] == 0
    )
    out.update(
        busy=busy,
        conflicts=conflicts(folder) if busy else [],
        contains=contains,
        head=_git_ops._git_head_sha(folder),
        ahead=_git_ops._git_count(folder, ref + "..HEAD") or 0,
    )
    return out


async def _observe(flight: dict, step: dict, ss: dict, sess: dict, now: float):
    return await asyncio.to_thread(_facts, flight, step, ss)


def _receipt(d: dict, o: dict) -> str:
    target = d.get("target") or "main"
    if d.get("how") == "merge":
        if o.get("head") == d.get("before") and not d.get("resumed"):
            return "up to date with %s" % target
        return "merged %s" % target
    if o.get("head") == d.get("before") and not d.get("resumed"):
        return "up to date with %s" % target
    n = int(o.get("ahead") or 0)
    if n == 0:
        return "fast-forwarded to %s" % target
    return "rebased %d commit%s" % (n, "" if n == 1 else "s")


def _judge(flight, step, ss, obs, now) -> dict:
    o = obs.get("sync")
    d = ss.get("detail") or {}
    target = d.get("target") or step["config"].get("with") or "the default branch"
    if not o:
        return _flights._wait("checking the branch against %s" % target)
    if o.get("failed"):
        return {
            "action": "fail",
            "reason": o["failed"],
            "detail": o.get("detail") or {},
        }
    if o.get("lost"):
        return {"action": "run", "note": "syncing again (it was interrupted)"}
    # A resumed retry started no job: the one on record is the earlier try's.
    job = None if d.get("resumed") else o.get("job")
    if job is not None and job.get("running"):
        return _flights._wait("syncing with %s" % target)
    busy = o.get("busy")
    if busy:
        files = o.get("conflicts") or []
        finish = (
            "`git rebase --continue`"
            if busy == "rebase"
            else "`git commit` to finish the merge"
        )
        what = (
            "conflicts with %s in %s" % (target, ", ".join(files[:10]))
            if files
            else "a %s onto %s is in progress" % (busy, target)
        )
        return {
            "action": "fail",
            "reason": "%s — resolve them, `git add` the files and run %s"
            % (what, finish),
            "detail": {"conflicts": files},
        }
    if o.get("contains"):
        return {
            "action": "done",
            "note": "in step with %s" % target,
            "detail": {"receipt": _receipt(d, o)[:80], "after": o.get("head") or ""},
        }
    if job is None:
        # Interrupted (no job this server run) and not there yet: sync again.
        return {"action": "run", "note": "syncing again (it was interrupted)"}
    out = str(job.get("output") or "").strip()
    return {
        "action": "fail",
        "reason": "the %s with %s failed (exit %s): %s"
        % (d.get("how") or "sync", target, job.get("exit"), out[-1500:] or "no output"),
        "detail": {"output": out},
    }


_kinds.register(
    _kinds.Kind(
        kind="sync",
        spec={
            "label": "Sync",
            "actor": "robot",
            "family": "ship",
            "risk": [],
            "summary": "Rebase the branch onto another (the default one), or merge it in; a conflict goes to the agent.",
            "default_title": "Sync with main",
            "fields": [
                _flights._f(
                    "with",
                    "text",
                    "Branch (empty = the default branch)",
                    default="",
                    placeholder="main",
                ),
                _flights._f("how", "select", "How", default="rebase", options=HOW),
                _flights._f(
                    "fetch", "bool", "Fetch it from origin first", default=True
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        default_title=_title,
    )
)
