"""Branch session — the body of ``POST /api/instances/{title}/branch``.

Duplicate (``/copy``) opens a second agent in the SAME worktree, on the same
branch. Branch is its sibling for parallel work that must not collide: a new
session with its OWN worktree, on a NEW branch cut from the source's current
``HEAD`` (its branch tip, not the repo's default branch), carrying a copy of
the source's uncommitted changes so the branch starts from exactly what the
user sees. The source is never written: its index, files and branch stay as
they were (:mod:`backend.session.git.carry`).

Otherwise it mirrors Duplicate: same agent CLI, the source's own launch flags,
the same auth profile and model pin, no parent (a branch is the user's own
work, not a worker), and a fresh conversation. The new session goes through
:func:`core.session_create.create` like any other plain worktree session, so
it gets the usual branch name (``<branch_prefix><title>``), port block,
``.mindflock.toml`` worktree setup and events; its recorded base is the
source's branch, so diffs and ship compare against where it forked.

Refused (nothing created) for: a remote ``device::title`` row or a request
another device relayed here, a shared-folder (peer) session, a session still
starting, a folder that isn't a git repo or has no commit, a clone-strategy
workspace (its repository IS the source's own folder, removed with it), and
uncommitted changes git can't snapshot.

Like the other extracted modules this calls back through the server namespace
(``srv.<name>``) so ``monkeypatch.setattr(server, "_foo", ...)`` keeps working.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from fastapi.responses import JSONResponse

from backend import gitutil

__all__ = ["branch", "probe", "free_branch_title", "SUFFIX"]

#: What a branched session's title ends in (``<title>-branch``, ``-branch-2``…).
SUFFIX = "-branch"
_PROBE_TIMEOUT = 30.0
#: How many numbered titles to try before giving up.
_MAX_TRIES = 50


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def _refuse(message: str, status: int = 409) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def probe(folder: str) -> dict:
    """What branching ``folder`` needs, read from git: ``{"git", "head",
    "branch", "root"}`` — ``git`` False for a folder outside any repo, ``head``
    "" for a repo with no commit, ``branch`` "" when detached, ``root`` the
    canonical checkout the worktrees hang off ("" for a bare/separate-git-dir
    repo). Blocking; never raises."""
    inside = gitutil.git_out(
        ["rev-parse", "--is-inside-work-tree"], folder, timeout=_PROBE_TIMEOUT
    )
    if inside != "true":
        return {"git": False, "head": "", "branch": "", "root": ""}
    head = gitutil.git_out(
        ["rev-parse", "--verify", "-q", "HEAD^{commit}"], folder, timeout=_PROBE_TIMEOUT
    )
    branch = gitutil.git_out(
        ["symbolic-ref", "--short", "-q", "HEAD"], folder, timeout=_PROBE_TIMEOUT
    )
    common = gitutil.git_out(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        folder,
        timeout=_PROBE_TIMEOUT,
    )
    root = ""
    if common:
        common = os.path.normpath(common)
        if os.path.basename(common) == ".git":
            root = os.path.dirname(common)
    return {"git": True, "head": head, "branch": branch, "root": root}


def free_branch_title(source: str, repo: str) -> Optional[str]:
    """``<source>-branch``, else ``-branch-2``, ``-branch-3``…: the first name
    no session (live or renamed) goes by AND whose branch
    (``<branch_prefix><name>``) doesn't exist in ``repo`` yet — a closed or
    paused namesake keeps its branch, and a fork is never cut onto it. None
    when nothing in range is free. Blocking (git probes)."""
    srv = _server()
    base = source + SUFFIX
    taken = srv._session_branches()
    for n in range(1, _MAX_TRIES + 1):
        cand = base if n == 1 else "%s-%d" % (base, n)
        if cand in srv.ENGINE.instances or srv._aliases.owner_of(cand, taken):
            continue
        if srv._lineage.branch_taken_error(repo, srv._session_branch_name(cand)):
            continue
        return cand
    return None


def _launch_args(src) -> Optional[list]:
    """The source's own per-session flags, revalidated (None = inherit the
    global default, when they no longer pass validation)."""
    srv = _server()
    try:
        return list(
            srv.provider_config.validate_launch_args(
                list(getattr(src, "LaunchArgs", ()) or ())
            )
        )
    except ValueError:
        return None


async def branch(
    title: str, *, person: str = "", relayed: bool = False
) -> JSONResponse:
    """Branch ``title`` into a new session (202 + the new row, ``branched_from``
    and ``carried``), or refuse with ``{"error"}`` — see the module docstring.
    ``relayed`` is True for a request another MindFlock forwarded here."""
    srv = _server()
    from backend.session import git as _git
    from backend.web.core import peer_guard as _peer_guard
    from backend.web.core import remote as _remote

    if _remote.is_remote_title(title):
        device = title.split(_remote.NS, 1)[0]
        return _refuse(
            "Branch works on this device's own sessions — open MindFlock on "
            "%s to branch it there" % device
        )
    if relayed:
        return _refuse(
            "Branch works on this device's own sessions — another device can't "
            "branch %s from here" % title
        )
    src, err = srv._inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(src, "Branch")
    if peer_err is not None:
        return peer_err
    try:
        started = bool(src.Started())
    except Exception:  # noqa: BLE001
        started = False
    if src.Status == srv.Loading or not started:
        return _refuse(
            "%s is still starting — branch it once its workspace is ready" % title
        )
    try:
        folder = src.GetWorktreePath()
    except Exception:  # noqa: BLE001
        folder = ""
    if not folder or not os.path.isdir(folder):
        return _refuse("source workspace not ready")
    if not srv.git_available():
        return srv._no_git_response()

    facts = await asyncio.to_thread(probe, folder)
    if not facts["git"]:
        return _refuse("Branch needs a git repository — %s isn't one" % folder)
    if not facts["head"]:
        return _refuse(
            "Branch needs a commit to branch from — %s has none yet" % folder
        )
    root = facts["root"] or (getattr(src, "Path", "") or "")
    if (
        not getattr(src, "InPlace", False)
        and root
        and os.path.realpath(root) == os.path.realpath(folder)
    ):
        # A clone-strategy workspace is its own repository: a worktree cut
        # from it would vanish when the source is deleted.
        return _refuse(
            "Branch can't fork %s: its workspace is a separate clone, deleted "
            "with the session — commit and push, then start a session on the "
            "branch instead" % title
        )

    try:
        carry = await asyncio.to_thread(_git.snapshot_changes, folder)
    except RuntimeError as snap_err:
        return _refuse(
            "Branch couldn't copy the uncommitted changes in %s (%s) — commit or "
            "stash them and try again" % (folder, snap_err)
        )

    new_title = await asyncio.to_thread(free_branch_title, title, root)
    if new_title is None:
        return _refuse("no free name for a branch of %s — rename or close one" % title)

    payload = {
        "title": new_title,
        "program": getattr(src, "Program", "") or srv.ENGINE.default_program(),
        "repo_path": root,
        "base_ref": facts["head"],
        "profile_id": getattr(src, "ProfileId", "") or "",
        "profile_model": getattr(src, "ProfileModel", "") or "",
    }
    if facts["branch"]:
        payload["base_branch"] = facts["branch"]
    launch_args = _launch_args(src)
    if launch_args is not None:
        payload["launch_args"] = launch_args
    resp = await srv._session_create.create(
        payload, person=person, carry_ref=carry, branched_from=title
    )
    if resp.status_code != 202:
        return resp
    body = json.loads(resp.body)
    body["branched_from"] = title
    body["base_branch"] = facts["branch"]
    body["carried"] = bool(carry)
    return JSONResponse(body, status_code=202)
